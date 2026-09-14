"""
test_health.py — tests for the two OPERATIONAL endpoints, /health and /ready.

ANATOMY OF A TEST HERE:
  - It's `async def` because our client is async (pytest-asyncio runs it).
  - It names `client` as a parameter -> pytest injects the `client` fixture
    from conftest.py (an app-wired AsyncClient).
  - It follows Arrange / Act / Assert:
        Act:    hit the endpoint.
        Assert: check the status code and body.

WHY PROBE ENDPOINTS DESERVE REAL TESTS — this is not box-ticking:
  A broken readiness endpoint does not produce a broken feature; it produces
  Pods that never enter the Service, or Pods that serve traffic while their
  database is unreachable. Both failures show up as an OUTAGE rather than as a
  bug report, and neither is visible in any other test. The blast radius of these
  twenty lines is larger than that of most endpoints in the app.

  There is also a specific regression these tests catch. If someone removes
  @limiter.exempt from /ready, a hammered probe endpoint starts returning 429 —
  and Kubernetes reads any non-2xx/3xx as "not ready" and pulls the Pod out of
  rotation. test_readiness_is_exempt_from_rate_limiting below fails the moment
  that decorator disappears.
"""

from src.rate_limit import limiter


async def test_health_ok(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    # Also confirms the app metadata is wired from settings.
    assert "version" in body


async def test_health_checks_no_dependencies(client):
    """
    /health must stay dependency-free.

    WHY ASSERT AN ABSENCE: liveness failure KILLS the container. The day someone
    adds a database check here, a brief database hiccup restarts every replica
    simultaneously and turns a 10-second blip into a self-sustaining outage (see
    src/routers/system.py). Nothing else in the suite would notice — the endpoint
    would still return 200 in tests, because the test database is always up.

    Asserting that the body contains NO `checks` key is a cheap, durable tripwire
    on that specific mistake: the moment someone starts reporting dependency
    status from the liveness endpoint, this test asks them to justify it.
    """
    body = (await client.get("/health")).json()
    assert "checks" not in body


async def test_readiness_ok_when_database_reachable(client):
    """
    The happy path. The `client` fixture overrides get_db with the in-memory test
    database, so SELECT 1 succeeds and the instance reports ready.
    """
    resp = await client.get("/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"] == "ok"


async def test_readiness_reports_cache_as_disabled_not_failed(client):
    """
    Redis is not configured in tests (the fixture injects a no-op cache with
    enabled = False), so the cache must be reported as "disabled".

    WHY THIS DISTINCTION IS WORTH A TEST: "disabled" is a healthy, deliberate
    configuration. If it were reported as a failure, every cache-free deployment
    would show a scary readiness body, people would learn to ignore the body, and
    the probe's diagnostic value would be gone. A health check that cries wolf is
    worse than no health check.
    """
    body = (await client.get("/ready")).json()
    assert body["checks"]["cache"] == "disabled"
    # ...and crucially, a disabled cache does NOT make the instance unready.
    assert body["status"] == "ready"


async def test_readiness_503_when_database_unreachable(client, monkeypatch):
    """
    THE TEST THAT ACTUALLY MATTERS: a broken database must produce 503, not 200
    and not 500.

    HOW IT'S ARRANGED: monkeypatch replaces AsyncSession.execute with something
    that raises, so the readiness handler's `except` branch runs. monkeypatch
    undoes the patch automatically at the end of the test — which is why it's
    preferred over assigning the attribute by hand and hoping you remember to
    restore it. A leaked patch breaks unrelated tests later in the run, with a
    failure that points at the wrong file.

    WHAT EACH ASSERTION DEFENDS:
      503, not 500 — Kubernetes only reads the status code, but every OTHER
        consumer (load balancers, gateways, monitoring) treats 503 as retryable
        and 500 as a bug. Getting this right costs nothing and means alerts fire
        with the right severity.
      A diagnostic body — the response must still say WHICH check failed. A bare
        503 tells an on-call engineer nothing and sends them reading source code
        at 3am.
      The same body shape as a 200 — `status` and `checks` are present either way,
        so anything parsing this never has to branch on the status code.
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    async def boom(*args, **kwargs):
        raise ConnectionRefusedError("simulated: database is down")

    monkeypatch.setattr(AsyncSession, "execute", boom)

    resp = await client.get("/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not ready"
    # The message names the exception type so an operator can tell a refused
    # connection apart from a timeout or an auth failure without opening logs.
    assert "ConnectionRefusedError" in body["checks"]["database"]


async def test_probe_endpoints_are_exempt_from_rate_limiting():
    """
    Guard the non-obvious coupling between the rate limiter and the probes.

    THE BUG THIS PREVENTS: slowapi's `default_limits` applies to EVERY route.
    Probe traffic is relentless — one request every few seconds, forever, from a
    kubelet whose source IP may be shared. If probes ever exhaust the limit,
    /ready returns 429, Kubernetes reads that as "not ready", and the Pod is
    removed from the Service. The rate limiter would have taken the application
    out of rotation, and the logs would be full of successful requests with
    nothing pointing at the cause.

    WHY IT INSPECTS limiter._exempt_routes DIRECTLY RATHER THAN SENDING 101
    REQUESTS:
      Sending enough requests to trip the limit would make this test slow, and —
      worse — order-dependent and flaky, because the in-memory counter is shared
      across the whole test session and keyed by client IP. Asserting the
      registration is fast, deterministic, and tests the actual thing we care
      about: that the exemption is wired up.

      The tradeoff, stated honestly: this touches a private attribute, so a
      slowapi upgrade could rename it and break the test. That's an acceptable
      cost for catching a production outage, and a broken test here is loud and
      obvious rather than silent.
    """
    assert "src.routers.system.ready" in limiter._exempt_routes
    assert "src.routers.system.health" in limiter._exempt_routes
