"""
system.py — OPERATIONAL ENDPOINTS: /health (liveness) and /ready (readiness).

These two endpoints look almost identical and mean completely different things.
Conflating them is one of the most common — and most expensive — mistakes in
containerised deployments, so this file exists to keep the distinction explicit
and in one place.

------------------------------------------------------------------------------
THE DISTINCTION, IN ONE TABLE
------------------------------------------------------------------------------
                    /health (liveness)          /ready (readiness)
  Question          "Is this process alive?"    "Should I get traffic?"
  Checks            NOTHING external            Dependencies (the database)
  On failure        THE CONTAINER IS KILLED     Removed from the Service
                    and restarted               (keeps running, no traffic)
  Recovery          Restart — expensive,        Automatic the moment the check
                    loses in-flight requests    passes again — cheap, reversible
  Right answer to   A deadlocked event loop,    A dead connection pool, a
                    a wedged process            database that is down, still
                                                warming up caches

------------------------------------------------------------------------------
⚠️ THE OUTAGE THIS FILE IS DESIGNED TO PREVENT — worth memorising
------------------------------------------------------------------------------
Someone reasonably thinks "an app that can't reach its database is broken, so the
liveness probe should check the database."

Then the database has a 10-second hiccup.

Every replica's liveness probe fails at the same moment, because they all share
the database. Kubernetes kills EVERY replica simultaneously. All of them restart,
and all of them open a flood of fresh connections to a database that was already
struggling — which keeps it struggling, which fails the probes again, which
restarts them again.

A 10-second blip has become a total, self-sustaining outage, and the cluster is
causing it enthusiastically because you asked it to.

  THE RULE: liveness checks ONLY the process itself, never a dependency.
            Dependency health belongs in READINESS, where the consequence is
            "stop sending me traffic" — correct, reversible, and self-healing.

Note this is not really a Kubernetes rule. It is: match the aggressiveness of a
health check to the cost of the action it triggers. Restarting is expensive and
irreversible; removing from a load balancer is cheap and undoes itself.
"""

import asyncio
import logging

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from src.config import get_settings
from src.dependencies import CacheDep, DbSession
from src.rate_limit import limiter

logger = logging.getLogger("app.system")
settings = get_settings()

# tags=["system"] groups these together in /docs and separates plumbing from
# business endpoints. No prefix: probe paths should be short, stable, and
# guessable, and an operator should never have to read code to find them.
router = APIRouter(tags=["system"])


# ==============================================================================
# LIVENESS — /health
# ==============================================================================
@router.get("/health", summary="Liveness probe — is the process alive?")
@limiter.exempt
async def health() -> dict:
    """
    Return 200 with a small status body. Checks nothing.

    WHY IT DELIBERATELY CHECKS NOTHING:
      Returning 200 from here proves quite a lot on its own: the process exists,
      the event loop is running and scheduling work, the ASGI stack is intact, and
      the port is accepting connections. That is exactly the set of things whose
      only remedy IS a restart.

      Every dependency you add to this endpoint is a new way for a healthy process
      to be killed — see the outage described in this module's docstring.

    WHY IT IS UNAUTHENTICATED:
      kubelet has no credentials and cannot obtain any. An authenticated probe
      simply fails. Nothing here reveals anything an attacker benefits from: the
      app name and version are already in the OpenAPI schema.
    """
    return {"status": "ok", "app": settings.app_name, "version": settings.app_version}


# ==============================================================================
# READINESS — /ready
# ==============================================================================
@router.get(
    "/ready",
    summary="Readiness probe — should this instance receive traffic?",
    responses={
        200: {"description": "Ready to serve traffic"},
        503: {"description": "Not ready — a required dependency is unavailable"},
    },
)
@limiter.exempt
async def ready(db: DbSession, cache: CacheDep, response: Response) -> dict:
    """
    Check every dependency this instance NEEDS in order to serve a real request,
    and report 200 or 503.

    ⚠️ WHY @limiter.exempt IS NOT OPTIONAL HERE — the subtlest bug in this file.

      SlowAPIMiddleware applies `default_limits` to EVERY route, and probe traffic
      is relentless: one request every 5 seconds forever, from a kubelet whose
      source IP is shared with anything else on that node. If probe requests ever
      exhaust the limit, this endpoint starts returning 429 — which is not 200 —
      so Kubernetes removes the Pod from the Service.

      In other words: WITHOUT THIS DECORATOR, THE RATE LIMITER CAN TAKE YOUR
      APPLICATION OUT OF ROTATION. It would look like an application failure, in
      logs full of successful requests, and nothing would point at the limiter.

      Exempting probes also keeps the numbers honest — you do not want machine
      traffic consuming a human caller's budget.

    THE CHECKS, AND WHY EACH IS FATAL OR NOT
    ----------------------------------------
    This is the actual design work, and the reasoning matters more than the code.

    DATABASE — FATAL.
      Every endpoint that does anything meaningful needs the database. An instance
      that cannot reach it can only return 500s, so it should not be in the
      Service. Removing it is also the *useful* thing to do: if only ONE replica
      has a broken connection pool, readiness routes traffic to the healthy ones
      and users never notice.

    REDIS — NOT FATAL. Reported, but the instance stays ready.
      Two independent reasons:

      1. src/cache.py is written to degrade gracefully — a cache failure becomes a
         miss and the request still succeeds, just slower. Making Redis fatal here
         would contradict the application's own design, converting an OPTIONAL
         dependency into a required one by accident. Your health check must agree
         with your code about what is actually required.

      2. THE CASCADE. All replicas share one Redis. If Redis were fatal, a Redis
         blip would fail readiness on EVERY replica at the same instant, emptying
         the Service's endpoint list — and an empty endpoint list means every
         request fails, not just the cacheable ones. You would have turned "the
         cache is down and things are slower" into "the site is down".

      So Redis appears in the response body as diagnostic information — visible to
      a human reading `curl /ready`, invisible to the probe's pass/fail decision.
      That separation is the whole trick: report everything, gate on the essential.

    ⚠️ THE TENSION WORTH ACKNOWLEDGING (rather than pretending it away):
       a readiness probe that fails on shared-dependency trouble can shrink your
       capacity exactly when you need it most, and shifting traffic off one
       struggling replica can push the load onto its neighbours until they fail
       too. Readiness is a good tool for INDEPENDENT, per-instance faults and a
       dangerous one for faults every instance shares. Deciding which category a
       dependency falls into is the judgement call — and it's why the answers above
       are different for the database and for Redis even though both are
       "dependencies".
    """
    checks: dict[str, str] = {}
    ready_to_serve = True

    # ---- DATABASE: the required dependency -----------------------------------
    #
    # WHY `SELECT 1` AND NOT A REAL QUERY:
    #   It is the cheapest statement that proves the entire chain end to end — the
    #   pool handed out a connection, the socket is alive, the server parsed and
    #   answered a statement, and the driver decoded the result. Querying a real
    #   table would additionally couple the probe to your schema, so a migration
    #   could fail readiness on a perfectly healthy database.
    #
    # WHY IT USES THE APP'S OWN SESSION DEPENDENCY (DbSession) rather than opening
    # a fresh connection:
    #   Because it must exercise THE SAME CONNECTION POOL the real endpoints use.
    #   A probe that opens its own connection reports "database reachable" while
    #   the pool is exhausted and every actual request is timing out — a health
    #   check that is technically truthful and operationally useless. Probe the
    #   path your users take.
    #
    # WHY THE EXPLICIT TIMEOUT:
    #   kubelet abandons its HTTP request after `timeoutSeconds`, but abandoning
    #   the request does NOT stop this coroutine — it would keep waiting on the
    #   database while holding a pool connection. At one probe every 5 seconds, a
    #   stalled database leaks a connection per probe until the pool is empty, and
    #   then the health check has CAUSED the outage it was meant to report.
    #   Bounding the wait here guarantees the connection goes back.
    #
    #   readiness_timeout_seconds (2.0) is deliberately BELOW the probe's
    #   timeoutSeconds (3) in k8s/40-deployment.yaml, so we always produce a real
    #   503 with a diagnostic body rather than letting kubelet time out and record
    #   only "probe failed". Own your failures; a timeout you generated tells you
    #   more than one imposed on you.
    try:
        await asyncio.wait_for(
            db.execute(text("SELECT 1")),
            timeout=settings.readiness_timeout_seconds,
        )
        checks["database"] = "ok"
    except TimeoutError:
        # asyncio.TimeoutError is an alias of the builtin TimeoutError on 3.11+.
        # NOTE: cancelling a query mid-flight is not free — the driver sends a
        # cancel request and may discard the connection. The pool replaces it, and
        # trading one connection for a bounded probe is clearly the right way round.
        checks["database"] = f"timeout after {settings.readiness_timeout_seconds}s"
        ready_to_serve = False
        logger.warning("Readiness: database check timed out")
    except Exception as exc:
        # Broad on purpose. A readiness probe must NEVER raise: an unhandled
        # exception becomes a 500, and while 500 does correctly fail the probe, it
        # arrives with no diagnostic body and a scary stack trace in the logs. We
        # would rather emit a deliberate 503 that says exactly what failed.
        checks["database"] = f"error: {type(exc).__name__}"
        ready_to_serve = False
        logger.warning("Readiness: database check failed: %s", exc)

    # ---- REDIS: reported, never fatal ----------------------------------------
    #
    # Three distinct states, and telling them apart is the point (see
    # CacheClient.enabled). "disabled" is a valid, healthy configuration; treating
    # it as a failure would make the probe cry wolf, and a health check that cries
    # wolf gets ignored — which is worse than not having one.
    if not cache.enabled:
        checks["cache"] = "disabled"
    else:
        try:
            reachable = await asyncio.wait_for(
                cache.ping(), timeout=settings.readiness_timeout_seconds
            )
            checks["cache"] = "ok" if reachable else "unavailable"
        except TimeoutError:
            checks["cache"] = "timeout"
        # NOTE: `ready_to_serve` is NOT touched in this block. That absence is the
        # decision — see the docstring. If you ever add a dependency the app truly
        # cannot work without (an auth provider, a message broker it must publish
        # to), set the flag there and write down why.

    if not ready_to_serve:
        # 503 SERVICE UNAVAILABLE is the correct code: "I understood you, I am
        # temporarily unable to serve, try again." Kubernetes only reads the status
        # code — anything outside 200–399 fails the probe — but returning the
        # semantically right code matters for every OTHER consumer: load balancers,
        # API gateways, and monitoring all treat 503 as retryable and 500 as a bug.
        #
        # WHY SET response.status_code INSTEAD OF RAISING HTTPException:
        #   HTTPException would be caught by http_exception_handler (main.py) and
        #   rewrapped in the standard error envelope with a trace id. That is right
        #   for API errors that clients consume, and wrong here: a probe response
        #   should be a flat, stable diagnostic document, not an error object. This
        #   way the SHAPE of the body is identical whether we return 200 or 503, so
        #   whatever you use to read it never has to branch.
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        logger.warning("Readiness check FAILED: %s", checks)

    return {
        "status": "ready" if ready_to_serve else "not ready",
        "checks": checks,
        # The Pod name, injected by the Downward API in k8s/40-deployment.yaml, is
        # NOT included here on purpose — it is already in every log line via the
        # POD_NAME environment variable, and a probe body is a poor place to leak
        # topology. Curl the Service repeatedly and you can tell replicas apart
        # from their logs; you do not need it in the payload.
    }
