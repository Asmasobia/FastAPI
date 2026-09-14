"""
rate_limit.py — RATE LIMITING via slowapi, with SHARED (cluster-wide) counters.

WHY RATE LIMIT:
  Protect the API from abuse and accidental hammering: brute-force login
  attempts, scrapers, a buggy client in a retry loop. A limit like "100 requests
  per minute per client" caps the damage any single caller can do and keeps the
  service available for everyone else.

HOW slowapi WORKS:
  You create a Limiter with a "key function" that identifies the caller (here,
  their IP). SlowAPIMiddleware (wired in main.py) then applies `default_limits` to
  every route. When a caller exceeds the limit, slowapi raises RateLimitExceeded,
  which we render as HTTP 429 Too Many Requests.

  Keying by IP is simple but blunt: many users can share one IP behind NAT, and
  one user can rotate IPs. Keying by authenticated user id is fairer but only
  works on authenticated routes, so a login brute-force — the case you most want
  to limit — would be unprotected. A production API usually does BOTH: by IP on
  public routes, by user id on authenticated ones.

------------------------------------------------------------------------------
THE PART THAT CHANGED, AND WHY — READ THIS ONE
------------------------------------------------------------------------------
slowapi's DEFAULT storage is IN-PROCESS memory. With a single process that is
completely correct, which is exactly what makes it dangerous: it works perfectly
right up to the moment you scale, and then it fails silently.

  ONE replica:    one counter.   "100/minute" means 100/minute.  ✅
  THREE replicas: three counters. "100/minute" means UP TO 300/minute. ❌

And it's worse than just being 3x too loose. Which counter a request increments
depends on which replica the load balancer happened to pick, so the effective
limit is also NON-DETERMINISTIC — the same client can be throttled on one request
and allowed on the very next one. A brute-force protection that a caller can beat
by retrying is not protection.

No exception is raised. Nothing is logged. Every test passes. The only symptom is
a security control that quietly does one third of its job — and it degrades
further every time someone (or an autoscaler) adds a replica.

  ⚠️ HORIZONTAL SCALING DOES NOT CREATE BUGS. IT REVEALS THE ONES YOU HAD.

     In-process state is the whole family: this limiter, an in-memory cache, an
     in-process scheduler firing N times, a local mutex providing no mutual
     exclusion, a websocket registry that only knows one replica's clients. The
     fixes are always one of three: move the state somewhere shared, elect a
     single owner, or take the work out of the per-process path entirely.

     Here we move the state: `storage_uri` points slowapi at Redis, so all
     replicas increment the SAME counter and the configured limit is the real one.
     Redis is the natural home for this — the operation is a single atomic
     INCR-with-expiry, which is precisely what Redis is fast at.
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

from src.config import get_settings

settings = get_settings()

# get_remote_address returns the client's IP — our "who is this" key.
#
# ⚠️ BEHIND A PROXY THIS IS THE PROXY'S IP, NOT THE CLIENT'S. Under Kubernetes
#    every request arrives via the ingress controller, so without care EVERY
#    caller shares one key and the first noisy client throttles everybody — a
#    self-inflicted denial of service. The fix is two-sided: the ingress must
#    forward X-Forwarded-For, and uvicorn must be told to trust it
#    (--proxy-headers, which uvicorn enables by default for `--forwarded-allow-ips`
#    matches). Verify it rather than assuming it: log the key for one request and
#    check it isn't the ingress Pod's IP.
#
#    Never trust X-Forwarded-For from an untrusted source — a client can send any
#    value it likes and rotate it per request, which defeats IP-based limiting
#    entirely. It is only trustworthy when a proxy you control appends it.
limiter = Limiter(
    key_func=get_remote_address,
    # Applied to every route unless a route overrides it with @limiter.limit(...)
    # or is exempted with @limiter.exempt.
    default_limits=[settings.rate_limit],

    # ---- SHARED COUNTERS: the fix described in the docstring ----------------
    # "memory://" (the default) = per-process. "redis://host:port/db" = shared.
    # Chosen by configuration, not by code, so the same image runs correctly both
    # locally with no Redis and in the cluster with it.
    storage_uri=settings.rate_limit_storage_uri,

    # ---- BOUND THE BLOCKING WAIT — measured, not guessed ---------------------
    # These are passed straight through to redis-py's from_url(). They exist
    # because of a problem found by ACTUALLY RUNNING the app with Redis
    # unreachable, and the numbers are worth understanding.
    #
    # slowapi's storage layer is SYNCHRONOUS (see the caveats at the bottom of
    # this file). A synchronous socket call inside an async event loop does not
    # yield — it freezes the ENTIRE worker, so no other request can be served for
    # its duration. redis-py's default connect timeout is the OS default, which on
    # this platform meant roughly two seconds per attempt.
    #
    # Measured with Redis down and no timeouts set:
    #     GET /users/me -> 401 in 4041 ms      (first request: connect + retry)
    #     GET /users/me -> 401 in 2051 ms      (a periodic backend re-check)
    #
    # Read that again, because the consequence is severe: A DEAD CACHE MADE EVERY
    # REQUEST TAKE TWO SECONDS. The graceful-degradation path below was working
    # exactly as designed and the service was still effectively down — because it
    # degraded correctly but not QUICKLY. "Falls back" and "falls back fast" are
    # different properties, and only the second one keeps you serving.
    #
    # With a 250 ms cap the worst case is a quarter-second stall on the first
    # request and on each exponential-backoff re-check, which real traffic
    # absorbs. On a cluster-local Redis a healthy connect is well under one
    # millisecond, so 250 ms is ~250x headroom — tolerant of a blip, nowhere near
    # long enough to matter.
    #
    # THE TRANSFERABLE LESSON: whenever a dependency is optional, the timeout is
    # what makes the optionality real. An unbounded call to something you claim you
    # can live without is a hard dependency wearing a disguise — and you only find
    # out during the incident. Set the timeout, then TEST WITH THE DEPENDENCY DOWN
    # and look at the latency, not just the status code.
    storage_options={
        "socket_connect_timeout": 0.25,  # cap on establishing the TCP connection
        "socket_timeout": 0.25,          # cap on waiting for a command's reply
    },

    # ---- GRACEFUL DEGRADATION when Redis is unreachable ---------------------
    # Without this, a Redis outage makes slowapi raise, and because the limiter
    # runs as MIDDLEWARE that exception fails EVERY REQUEST. You would have taken
    # the entire API down to protect it from traffic — the rate limiter becoming
    # the outage is a genuinely common incident.
    #
    # With it, slowapi marks the storage dead, falls back to an in-memory counter,
    # and periodically retries Redis (and logs "Rate limit storage recovered" when
    # it comes back). Limits become per-replica again — i.e. up to N times looser
    # — but the API keeps serving.
    #
    # THAT IS THE RIGHT TRADE, and being able to say why is the point: a rate
    # limiter is a SAFETY MECHANISM, not a source of truth. Approximate
    # enforcement is vastly better than a total outage. Compare a payment ledger,
    # where the opposite is true and you should refuse to serve rather than guess.
    # Same mechanism, opposite correct answer — it depends entirely on what the
    # number is for. (This is the same philosophy as src/cache.py, deliberately.)
    in_memory_fallback_enabled=True,

    # ---- Observability -------------------------------------------------------
    # Adds X-RateLimit-Limit / -Remaining / -Reset headers to responses.
    #
    # Two reasons this earns its keep. For CLIENTS, it turns "you got a 429" into
    # "you have 12 requests left, resetting in 20 seconds" — a well-behaved client
    # can back off instead of hammering. For US, it is how you PROVE the shared
    # counter works: curl the API repeatedly and watch X-RateLimit-Remaining count
    # down monotonically across replicas. With per-process counters it jumps
    # around as you land on different Pods. The header makes an invisible bug
    # visible from outside the cluster, with no exec and no logs.
    headers_enabled=True,

    # ---- Key namespacing -----------------------------------------------------
    # Every Redis key becomes "rl:...". Costs nothing and buys two things: you can
    # find them (`redis-cli --scan --pattern 'rl:*'`), and if this Redis is ever
    # shared with another purpose there is no chance of collision. Prefix your
    # keys in shared datastores as a habit, not as a reaction to a collision.
    key_prefix="rl",
)

# ------------------------------------------------------------------------------
# ⚠️ A SUBTLETY WORTH KNOWING ABOUT THIS SETUP — the honest caveats.
#
# 1. slowapi's storage calls are SYNCHRONOUS.
#    slowapi 0.1.9 builds its backend with limits' SYNC storage layer, so each
#    rate-limit check performs a BLOCKING Redis round trip inside the async event
#    loop. On a cluster-local Redis that is well under a millisecond, so in
#    practice it is fine — but it is a real cost, it scales with request rate, and
#    it is invisible unless you look for it. If request latency ever matters at
#    that level, the answer is to move rate limiting OUT of the application
#    entirely and into the ingress layer, which is where most large deployments
#    end up: the proxy rejects the request before it costs you a worker at all.
#
# 2. REDIS EVICTION CAN RESET COUNTERS.
#    k8s/21-redis.yaml runs Redis with `--maxmemory-policy allkeys-lru`, which is
#    right for a cache and means any key — including a rate-limit counter — can be
#    evicted under memory pressure. A client near its limit could have its counter
#    dropped and start again. In practice 200MB holds an enormous number of tiny
#    counters so this is unlikely, and per point above an approximate limiter is
#    acceptable. If it were not acceptable, the fix is a SEPARATE Redis for rate
#    limiting with `--maxmemory-policy noeviction`. Knowing that two workloads
#    with different durability needs are sharing one eviction policy — and having
#    decided that is fine — is different from not having noticed.
#
# 3. THE PROBE ENDPOINTS MUST BE EXEMPT.
#    default_limits applies to EVERY route, which would include /health and
#    /ready. A 429 on a readiness probe removes the Pod from the Service, so the
#    rate limiter could take your application out of rotation. See the
#    @limiter.exempt decorators in src/routers/system.py.
# ------------------------------------------------------------------------------
