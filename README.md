# Tasks API — a FastAPI deep-dive

A production-shaped **task-tracker REST API** built to learn (and be able to
talk about) every core FastAPI pattern: async SQLAlchemy, JWT auth, Pydantic
validation, dependency injection, cursor pagination, caching, structured
logging, testing, migrations, and Docker.

> **Why this repo exists.** It's a focused 10-day deep-dive to go from "we use
> FastAPI at work" to being able to design it, live-code it, and explain every
> decision. The source is heavily commented on purpose — reading the code *is*
> the tutorial.

---

## Problem statement

Users own **Projects**. Projects contain **Tasks**. Tasks have a status,
priority, optional assignee and due date, and threaded **Comments**. Everything
is scoped to the authenticated user — you only ever see and modify your own
data.

---

## Tech choices (and why)

| Choice | Why |
| --- | --- |
| **FastAPI** | Async-first, automatic validation + OpenAPI docs, first-class dependency injection. |
| **Async SQLAlchemy 2.x** | Non-blocking DB access so one worker serves many concurrent requests. Modern typed `Mapped[...]` models. |
| **Pydantic v2** | Validates/coerces every request at the boundary; separates API shape from DB shape. |
| **JWT (HS256)** | Stateless auth — the token carries the proof, so no server-side session store. |
| **SQLite (dev) / Postgres (prod)** | Runs with **zero setup** locally; swaps to Postgres via **one env var**, no code change. |
| **Alembic** | Versioned, reversible schema migrations instead of destructive recreation. |
| **Redis** | Optional read-through cache; the app degrades gracefully if it's absent. |
| **slowapi** | Simple per-client rate limiting. |
| **pytest + httpx** | Async, in-process endpoint tests with swappable dependencies. |

---

## Architecture

A clean **layered** design. Each layer has one job and only talks to the layer
below it. This is the single most important thing to be able to draw on a
whiteboard.

```
                 HTTP request
                      │
                      ▼
        ┌─────────────────────────────┐
        │   Middleware                 │  request-id, timing, structured logs
        │   (src/middleware.py)        │  + rate limiting (slowapi)
        └─────────────┬───────────────┘
                      ▼
        ┌─────────────────────────────┐
        │   Routers  (src/routers/*)   │  THIN: parse input, call a service,
        │                              │  shape the response. No business logic.
        └─────────────┬───────────────┘
                      │ depends on
                      ▼
        ┌─────────────────────────────┐
        │   Dependencies               │  get_db, get_current_user, get_cache,
        │   (src/dependencies.py)      │  get_settings — injected via Depends()
        └─────────────┬───────────────┘
                      ▼
        ┌─────────────────────────────┐
        │   Services  (src/services/*) │  ALL business logic + authorization +
        │                              │  SQL. Raises domain errors (not HTTP).
        └─────────────┬───────────────┘
                      ▼
        ┌──────────────┬──────────────┐
        │  ORM models  │  Async engine │  SQLAlchemy models + session/engine
        │ (src/models) │   (src/db.py) │
        └──────────────┴───────┬───────┘
                               ▼
                       SQLite / Postgres

   Cross-cutting:
     • Pydantic schemas (src/schemas/*) validate IN and shape OUT at the edge.
     • Exceptions (src/exceptions.py) + handlers (src/error_handlers.py) turn
       domain errors into ONE consistent JSON envelope.
     • Cache (src/cache.py), background tasks (src/notifications.py),
       security (src/security.py), config (src/config.py).
```

**Request lifecycle (create a task):**
`POST /projects/{id}/tasks` → middleware stamps a request-id → router validates
the body against `TaskCreate` → `get_current_user` decodes the JWT and loads the
user → `task_service.create_task` checks project ownership, inserts, commits →
schedules a background "email" if assigned → returns `TaskOut` (201).

---

## Project layout

```
src/
  main.py             App instance, lifespan, middleware, handlers, router includes.
  config.py           Typed settings from env/.env (pydantic-settings).
  db.py               Async engine + session factory + get_db dependency.
  dependencies.py     Depends(): DB session, current user, cache, settings.
  security.py         Password hashing (bcrypt) + JWT encode/decode.
  exceptions.py       Domain error types (NotFound, PermissionDenied, ...).
  error_handlers.py   Turn errors into the standard {"error": {...}} envelope.
  middleware.py       Request-id + timing + logging middleware.
  logging_config.py   Structured logging with a per-request trace id (ContextVar).
  cache.py            Redis wrapper that degrades gracefully.
  rate_limit.py       slowapi limiter, Redis-backed so limits are cluster-wide.
  notifications.py    Fake email, used to demo BackgroundTasks.
  models/             SQLAlchemy ORM models (the DATABASE shape).
  schemas/            Pydantic models (the API shape).
  routers/            HTTP endpoints, split by resource.
    system.py         /health (liveness) and /ready (readiness) — and why they differ.
  services/           Business logic + authorization + queries.
migrations/           Alembic (env.py wired to our models + settings).
tests/                pytest suite (conftest has the test DB + auth fixtures).
k8s/                  Kubernetes manifests (apply in numeric order; see k8s/README.md).
Dockerfile            Multi-stage, non-root, healthcheck.
docker-compose.yml    API + Postgres + Redis.
DECISIONS.md          The non-obvious choices, and what each one costs.
```

---

## Run it locally (no Docker, no external services)

The app defaults to SQLite and runs immediately.

```bash
# 1. Create + activate a Python 3.12 virtual environment
py -3.12 -m venv .venv
source .venv/Scripts/activate      # Git Bash on Windows
# .venv\Scripts\activate           # PowerShell/CMD
# source .venv/bin/activate        # macOS/Linux

# 2. Install dependencies (dev set includes test tools)
pip install -r requirements-dev.txt

# 3. Create your .env from the template
cp .env.example .env

# 4. Apply migrations (creates the SQLite schema)
alembic upgrade head

# 5. Run the API
uvicorn src.main:app --reload
```

Now open:

- **http://localhost:8000/docs** — interactive Swagger UI (click **Authorize**
  after logging in to call protected endpoints).
- **http://localhost:8000/redoc** — alternative docs.
- **http://localhost:8000/health** — liveness probe. Checks nothing external, on
  purpose: failing it means "restart the process", and a health check whose
  failure restarts everything must never depend on a shared service.
- **http://localhost:8000/ready** — readiness probe. Checks the database (fatal)
  and Redis (reported, not fatal), and returns 503 when the instance should not
  receive traffic. See [`src/routers/system.py`](src/routers/system.py) for why
  those two dependencies are treated differently.

### Try the full flow with curl

```bash
# Register
curl -X POST localhost:8000/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"me@example.com","full_name":"Me","password":"password123"}'

# Login -> grab the token
TOKEN=$(curl -s -X POST localhost:8000/auth/login \
  -d "username=me@example.com&password=password123" | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

# Create a project
PID=$(curl -s -X POST localhost:8000/projects \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"My Project"}' | python -c "import sys,json;print(json.load(sys.stdin)['id'])")

# Create a task in it
curl -X POST localhost:8000/projects/$PID/tasks \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"title":"First task","priority":2}'

# List tasks (paginated + filterable)
curl -H "Authorization: Bearer $TOKEN" "localhost:8000/tasks?status=todo&limit=10"
```

---

## Run it with Docker (full Postgres + Redis stack)

Requires Docker Desktop.

```bash
docker compose up --build
# API on http://localhost:8000, Postgres on 5432, Redis on 6379
```

Compose points the app at Postgres via `DATABASE_URL` — the **only** change
needed to move off SQLite. It runs `alembic upgrade head` before starting the
server.

---

## Switching to PostgreSQL (without Docker)

1. Start a Postgres and create a database.
2. In `.env`, set:
   ```
   DATABASE_URL="postgresql+asyncpg://USER:PASS@localhost:5432/tasks"
   ```
3. `alembic upgrade head && uvicorn src.main:app --reload`

No Python code changes — that's the payoff of the async engine layer in
`src/db.py`.

---

## Testing

```bash
pytest                          # run all tests
pytest --cov=src --cov-report=term-missing   # with coverage
```

**29 tests, ~80% coverage.** The suite covers happy paths and error paths for
auth, projects, tasks (incl. filtering and full cursor-pagination walk), and
comments, plus a cross-user **authorization** test proving one user can't touch
another's data.

- Tests use an **in-memory SQLite** DB created fresh per test (perfect
  isolation), driven **in-process** via `httpx.AsyncClient(ASGITransport(app))`
  — no network, no server.
- `app.dependency_overrides` swaps the real DB/cache for test doubles. This is
  *why* those are behind `Depends()`.
- **Coverage note:** async SQLAlchemy runs query work inside greenlets, which
  `coverage.py` doesn't trace by default — so some service functions *look*
  under-covered even though the passing tests clearly exercise them (you can't
  fetch a task back that was never created). Real coverage is higher than the
  number suggests.

---

## Database migrations (Alembic)

```bash
alembic upgrade head                          # apply all migrations
alembic revision --autogenerate -m "message"  # generate a new one from model changes
alembic downgrade -1                           # roll back one step
alembic current                                # show applied revision
```

`migrations/env.py` is wired to our `Base.metadata` and reads the DB URL from
settings, so autogenerate diffs your models against the live DB. **Always review
autogenerated migrations before committing** — Alembic can miss enum changes,
server defaults, and never writes data migrations.

---

## Key design decisions

- **Layered (routers → services → models).** Routers never contain business
  logic or SQL; services never know about HTTP. Keeps each layer testable and
  reusable (a CLI or worker could call the services directly).
- **Two model families.** ORM models = DB shape; Pydantic schemas = API shape.
  This is why `hashed_password` can never leak — no output schema includes it.
- **Domain errors, not HTTP, in services.** Services raise `NotFoundError` etc.;
  a single set of handlers maps them to status codes + one JSON envelope
  `{"error": {code, message, trace_id}}`. Every error looks the same.
- **`expire_on_commit=False`** on the async session — otherwise attribute access
  after commit triggers a lazy (blocking) reload and crashes in async code.
- **`selectinload` for comments** to avoid the N+1 query problem on task detail.
- **Cursor pagination** (opaque, keyset-based) instead of offset — fast at any
  depth and stable under inserts.
- **UUID string primary keys** — app-generated, non-enumerable, portable across
  SQLite/Postgres.
- **404 vs 403:** we return **403** when a resource exists but isn't yours (vs.
  hiding it behind 404). Clearer for a learning project; the trade-off (404
  avoids revealing that an id exists) is a legitimate alternative.

---

## What I deliberately did NOT build (and what I'd add next)

Knowing what you skipped — and why — is a senior signal.

- **Durable background jobs (Celery/RQ).** `BackgroundTasks` is in-process and
  fire-and-forget: work is lost if the process dies, no retries. For
  must-happen work (payments, critical emails) I'd add **Celery + Redis/RabbitMQ**
  for retries, backoff, and a dead-letter queue.
- **Distributed tracing (OpenTelemetry).** I propagate a request-id manually; I'd
  auto-instrument with `opentelemetry-instrumentation-fastapi` and export to
  Jaeger/Tempo.
- **Metrics (Prometheus).** I'd add `prometheus-fastapi-instrumentator` to
  expose `/metrics` (latency histograms, error rates).
- **Load testing.** I'd validate p99 latency under target RPS with **k6** or
  Locust before claiming performance numbers.
- **Kubernetes deploy.** The Dockerfile is prod-shaped (multi-stage, non-root,
  healthcheck); I'd add a Deployment/Service/Ingress + liveness/readiness probes
  + an HPA for autoscaling.
- **Refresh tokens / token revocation.** Access tokens are short-lived; a real
  system adds refresh tokens and a revocation list (or short TTLs + rotation).
- **Rate limiting backend.** slowapi is in-memory (per process); across multiple
  instances I'd back it with Redis so limits are global.

---

## Interview cheat-sheet

Short answers to the questions this project is designed to let you ace.

**Walk me through your FastAPI service.**
Layered API: middleware (request-id/logging/rate-limit) → thin routers →
dependencies (DB session, current user) → services (business logic +
authorization + SQL) → async SQLAlchemy models over SQLite/Postgres. Pydantic
validates input and shapes output at the edge; domain errors become one JSON
envelope via exception handlers.

**How does `Depends()` work?**
You declare a parameter `x = Depends(fn)`. FastAPI calls `fn` (resolving *its*
dependencies first), injects the result, and caches it for the request. Yield
dependencies (like `get_db`) run teardown after the response. Dependencies are
reusable, testable (overridable via `dependency_overrides`), and keep
cross-cutting concerns out of endpoints.

**Sync vs async endpoints — when which?**
Use `async def` when the work is I/O-bound and awaitable (DB, HTTP, Redis) — the
event loop serves other requests while you await. Use `def` for CPU-bound or
blocking-only libraries; FastAPI runs those in a threadpool so they don't block
the loop. The cardinal sin is calling blocking I/O inside `async def` — it
freezes the whole event loop.

**How do you prevent N+1 in async SQLAlchemy?**
Eager-load relationships you'll access: `select(Task).options(selectinload(
Task.comments))`. That's 2 queries total (tasks, then all comments via `IN`)
instead of 1 + N. In async you *must* eager-load, because a lazy load on
attribute access isn't awaitable and raises. Also set `expire_on_commit=False`
so objects stay usable after commit.

**How do you propagate a trace id?**
Middleware reads/creates an `X-Request-ID`, stores it in a `contextvars.
ContextVar`, and a logging filter stamps it on every log line — so all logs for
one request share an id even across async tasks. It's returned in the response
header and included in every error envelope. (A ContextVar, not a global,
because it's isolated per async task.)

**How would you add rate limiting?**
slowapi with a key function (IP here; user-id for authed routes) and a limit
like `100/minute`; exceeding it returns 429. Backed by in-memory storage per
process — I'd point it at Redis for a shared limit across instances.

**JWT vs session cookies — you chose JWT, why?**
JWT is **stateless**: the signed token carries the identity, so any instance can
verify it with the secret — no shared session store, easy horizontal scaling and
good for APIs/mobile. Trade-off: you can't easily revoke a token before it
expires, so I keep them short-lived (and would add refresh tokens + a revocation
list). Session cookies are **stateful** (server stores the session), trivially
revocable, and browser-friendly with CSRF protection — better for classic
server-rendered web apps.

**What happens on startup/shutdown?**
The `lifespan` async context manager builds the cache client and (on SQLite)
ensures tables; on shutdown it closes Redis and disposes the DB pool. On SIGTERM
uvicorn stops taking new requests, drains in-flight ones, then runs teardown —
graceful shutdown.

---

## License

MIT — learn freely.
