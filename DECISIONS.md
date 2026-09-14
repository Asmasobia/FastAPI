# Decision log

Why this file exists: code shows *what* was built. Git history shows *when*. Neither
shows **why**, or what the alternatives cost — and that is the only part that can't
be reconstructed by reading the repo six months later.

It is also the difference between a project you can *show* and one you can
**defend**. "I containerised a FastAPI app" is a claim anyone can make. "I moved
migrations out of the container start command because at three replicas Alembic
races on `alembic_version`, and I can show you the CrashLoopBackOff" is a
demonstration.

Each entry follows the same shape, and the last two rows are the ones that matter
most — a decision without a stated cost is marketing, and one without a revisit
trigger is dogma.

| Field | Why it's there |
|---|---|
| **Context** | What forced a choice. Without this, later readers can't tell whether the reason still applies. |
| **Options considered** | Proves it was a decision, not a default. |
| **Decision** | What was actually done. |
| **Rationale** | Why that one won. |
| **What it cost** | Every decision has a downside. Naming it is what makes the entry credible. |
| **Revisit if** | The trigger that invalidates this. Turns a permanent-looking choice into a reversible one. |

---

## 001 — Separate `/health` and `/ready` endpoints

**Context.** The container needed a health endpoint. One existed — `/health`, which
checked nothing — and the Kubernetes readiness probe was pointed at it because it
was the only one available.

**Options considered.**

1. **One endpoint for both probes.** Simplest. What most tutorials show.
2. **One endpoint that checks dependencies, used for both probes.**
3. **Two endpoints with different contracts** — `/health` checks nothing, `/ready`
   checks dependencies.

**Decision.** Option 3. `/health` is dependency-free and drives liveness;
`/ready` checks the database and Redis and drives readiness.

**Rationale.** The two probes trigger *fundamentally different actions*, so they
are answering different questions:

- Liveness failure **kills and restarts the container** — expensive, destructive,
  loses in-flight requests.
- Readiness failure **removes the Pod from the Service** — cheap, reversible,
  self-healing the moment the check passes again.

Option 1 means readiness can't detect a broken database, so a Pod with a dead
connection pool stays in the Service and serves 500s. The probe is green while the
application is useless.

Option 2 is actively dangerous and is the classic outage: all replicas share the
database, so a 10-second database hiccup fails liveness on **every** replica
simultaneously. Kubernetes restarts them all. They all reconnect at once to a
database that was already struggling, which keeps it struggling, which fails the
probes again. A blip becomes a self-sustaining outage — and the cluster causes it
enthusiastically, because you asked it to.

The underlying principle generalises well beyond Kubernetes: **match the
aggressiveness of a health check to the cost of the action it triggers.**

**What it cost.** Two endpoints to maintain instead of one, and a subtle
cross-file coupling: `READINESS_TIMEOUT_SECONDS` (2s) must stay below the probe's
`timeoutSeconds` (3s), and nothing validates that relationship. Mitigated with
loud comments on **both** sides of the coupling — `src/config.py` and
`k8s/40-deployment.yaml` — rather than one.

**Revisit if.** A dependency is added that the app genuinely cannot function
without and whose failure is *per-instance* rather than shared. Then it belongs in
`/ready`'s fatal set, and the reason should be written down next to it.

---

## 002 — Redis is checked by `/ready` but is not fatal

**Context.** `/ready` had to decide what to do about Redis. The obvious reading —
"it's a dependency, so a dependency check should fail on it" — turns out to be
wrong here.

**Options considered.**

1. **Don't check Redis at all.** Simplest, but the probe body then tells an
   operator nothing about a real component.
2. **Check it and fail readiness.** Consistent-looking: a dependency is a
   dependency.
3. **Check it, report it, don't gate on it.**

**Decision.** Option 3. Redis status appears in the response body as `ok`,
`unavailable`, or `disabled`; only the database decides the status code.

**Rationale.** Two independent reasons, and either alone would be sufficient.

First, **the application's own code disagrees with option 2.** `src/cache.py` is
deliberately written so a cache failure becomes a cache *miss* and the request
still succeeds, just slower. Failing readiness on Redis would convert an optional
dependency into a required one by accident. **Your health check must agree with
your code about what is actually required** — otherwise one of the two is lying.

Second, **the cascade.** All replicas share one Redis. If Redis were fatal, a Redis
blip would fail readiness on every replica at the same instant, emptying the
Service's endpoint list. An empty endpoint list means *every* request fails, not
just the cacheable ones. You would have turned "the cache is down and things are
slower" into "the site is down".

The general rule extracted from this: **readiness is a good tool for independent,
per-instance faults and a dangerous one for faults every instance shares.**
Deciding which category a dependency falls into is the actual judgement call, and
it's why the answer differs for the database and for Redis even though both are
"dependencies".

Reporting without gating also keeps the third state meaningful. `disabled` is a
valid, deliberate configuration; treating it as a failure would make the probe cry
wolf, people would learn to ignore the body, and its diagnostic value would be
gone. A health check that cries wolf is worse than none.

**What it cost.** A Redis outage is invisible to Kubernetes. Nothing will alert on
it from the probe alone — you need separate monitoring, and the degradation shows
up as latency rather than errors, which is harder to notice. Accepted knowingly:
that is what the response body and the cache's WARNING logs are for.

**Revisit if.** Redis stops being just a cache — becoming a session store, a job
queue, or the sole home of the rate-limit counters *with strict enforcement
required*. Then losing it is a functional failure, not a slowdown.

---

## 003 — Rate-limit counters live in Redis, not process memory

**Context.** `slowapi`'s default backend counts requests in the process's own
memory. Correct with one process. `k8s/40-deployment.yaml` runs three.

**Options considered.**

1. **Leave it in memory.** Zero dependencies, and every test passes.
2. **Move the counters to Redis** via `storage_uri`.
3. **Move rate limiting out of the app entirely, into the ingress layer.**

**Decision.** Option 2, configured by `RATE_LIMIT_STORAGE_URI` so the same image
runs correctly with `memory://` locally and Redis in the cluster.

**Rationale.** Option 1 is not merely "3× too loose" — it is *incoherent*. Three
replicas means three independent counters, so `100/minute` becomes up to
300/minute, **and** which counter a request increments depends on which replica
the Service picked. So the effective limit is non-deterministic: the same client is
throttled on one request and allowed on the next. A brute-force protection a caller
can beat by retrying is not a protection.

Nothing errors. Nothing logs. Every test passes. The config says `100/minute` and
looks correct. It also degrades further every time someone — or the HPA — adds a
replica.

This is one instance of a general shape worth naming:

> **Horizontal scaling doesn't create bugs. It reveals the ones you had.**

In-process state is the whole family: this limiter, an in-memory cache, an
in-process scheduler firing N times, a local mutex providing no mutual exclusion, a
websocket registry that only knows one replica's clients. The fixes are always one
of three — **move the state somewhere shared** (this decision), **elect a single
owner** (a leader election via a Lease), or **take the work out of the per-process
path entirely** (decision 004).

Option 3 is where large deployments genuinely end up, because the proxy rejects
the request before it costs you a worker at all. It's not right *yet*: it would put
a security control in ingress annotations that aren't portable between
controllers, and it would mean the limit no longer works under `docker compose` or
in tests. Application-level first, edge-level when request volume makes the
in-process cost matter.

**What it cost.** Three things, all real.

1. **A blocking call in the event loop.** slowapi 0.1.9 uses `limits`' *synchronous*
   storage layer, so each check performs a blocking Redis round trip inside the
   async loop. Sub-millisecond on a cluster-local Redis, but non-zero and scaling
   with request rate.
2. **A latency cliff when Redis is down — which measurement caught and
   configuration fixed.** With the graceful-degradation fallback enabled but no
   socket timeouts set, a dead Redis produced:

   ```
   GET /users/me -> 401 in 4041 ms      (first request: connect + retry)
   GET /users/me -> 401 in 2051 ms      (a periodic backend re-check)
   ```

   The fallback was working exactly as designed and the service was still
   effectively down, because it degraded *correctly* but not *quickly*. Setting
   `socket_connect_timeout` / `socket_timeout` to 0.25s brought the worst case to
   273ms with subsequent requests at 5–7ms. **The lesson: whenever a dependency is
   optional, the timeout is what makes the optionality real. An unbounded call to
   something you claim you can live without is a hard dependency in disguise — and
   you find out during the incident. Test with the dependency down and look at the
   latency, not just the status code.**
3. **Counters can be evicted.** `k8s/21-redis.yaml` runs `--maxmemory-policy
   allkeys-lru`, right for a cache and meaning any key — including a rate-limit
   counter — can be dropped under memory pressure. Accepted: a rate limiter is a
   safety mechanism, not a source of truth, so approximate enforcement beats an
   outage. (Contrast a payment ledger, where the opposite is true. Same mechanism,
   opposite correct answer, decided entirely by what the number is *for*.)

**Revisit if.** Request rate makes the blocking round trip measurable in p99
latency, or strict enforcement becomes a compliance requirement. The first points
at ingress-level limiting; the second at a dedicated Redis with
`--maxmemory-policy noeviction`.

---

## 004 — Migrations run in a Job, not in the container start command

**Context.** `docker-compose.yml` starts the API with
`sh -c "alembic upgrade head && uvicorn ..."`. This has worked correctly every
single time — which is precisely why the bug is invisible.

**Options considered.**

1. **Keep it in the start command.**
2. **An initContainer in the Deployment.**
3. **A Kubernetes Job.**
4. **A Helm `pre-upgrade` hook or ArgoCD sync wave.**
5. **Run it manually before each deploy.**

**Decision.** Option 3 (`k8s/30-migration-job.yaml`), with `k8s/40-deployment.yaml`
deliberately setting **no** `command` so the Dockerfile's `CMD` — uvicorn only —
runs. `docker-compose.yml` keeps the chained command **unchanged, on purpose**.

**Rationale.** At three replicas, option 1 means three processes running
`alembic upgrade head` against the same database concurrently. Alembic takes no
cross-process lock by default, so all three read the same starting revision and
attempt the same DDL. Some fail — "relation already exists", or a deadlock on the
`alembic_version` row — exit non-zero, get restarted, and try again.

The result is a `CrashLoopBackOff` that **eventually resolves itself** once one
replica wins. That self-healing is what makes this survive in real codebases: it
looks like a transient blip rather than a structural fault. A multi-step migration
can leave a partially applied schema with a version row claiming otherwise.

A Job maps exactly onto the requirement — run a Pod to completion, once.

**Option 2 is the most commonly proposed wrong answer and worth being able to
reject out loud:** an initContainer runs in *every* Pod, so three replicas still
means three concurrent migrations. Identical bug, relocated.

Option 4 is the real production answer, because Helm and ArgoCD *wait* for the Job
before touching the Deployment — ordering guaranteed rather than hoped for. Not
adopted yet only because there's no templating layer in this project. With plain
kubectl the approximation is `kubectl wait --for=condition=complete`.

**Compose keeps the chained command deliberately.** One container means no
concurrency, and it keeps local dev a single command. **Two environments can have
two different correct answers**; recognising that is knowing which property each
environment needs — compose needs one-command boot, Kubernetes needs "exactly once"
under concurrency. The reasoning is written into `docker-compose.yml` so nobody
"fixes" it.

**What it cost.** Deploying now has an ordered, two-step sequence that must be
respected, and `kubectl apply -f k8s/` alone is no longer sufficient — it orders
*creation*, not *readiness*. Re-applying a Job also fails with `field is
immutable`, so the flow needs an explicit delete first. Both are documented in
`k8s/README.md`.

**Revisit if.** You adopt Helm or ArgoCD — then the Job becomes a hook or a sync
wave and the ordering stops being manual. Or you reach a schema change that cannot
run before the old code stops (a destructive migration), which needs the
expand/contract pattern instead: deploy a migration that is compatible with both
versions, roll the code, then remove the old column in a second migration.

---

## 005 — Probe endpoints are exempt from rate limiting

**Context.** `slowapi`'s `default_limits` applies to **every** route, which
includes `/health` and `/ready`.

**Options considered.**

1. **Leave them limited.** They're routes like any other.
2. **Raise the global limit until probes comfortably fit.**
3. **Exempt them explicitly** with `@limiter.exempt`.

**Decision.** Option 3, on both endpoints, with a test
(`tests/test_health.py::test_probe_endpoints_are_exempt_from_rate_limiting`) that
fails if either decorator is removed.

**Rationale.** Probe traffic is relentless: one request every few seconds, forever,
from a kubelet whose source IP may be shared with other traffic on that node. If
probes ever exhaust the limit, `/ready` returns **429** — which is not a 2xx — so
Kubernetes reads it as "not ready" and **removes the Pod from the Service.**

The rate limiter would have taken the application out of rotation. The logs would
be full of successful requests. Nothing would point at the limiter. That failure
mode is worth a decorator and a test.

Exempting them also keeps the numbers honest: machine traffic shouldn't consume a
human caller's budget.

Option 2 only moves the threshold, and it weakens the limit for everyone to
accommodate traffic that should never have been counted.

**What it cost.** The exemption is registered by function *name*
(`src.routers.system.ready`), so renaming or moving either function silently
un-exempts it. That is exactly why the test asserts the registration rather than
trusting it — and why the test deliberately inspects a private attribute
(`limiter._exempt_routes`) instead of sending 101 requests, which would be slow,
order-dependent, and flaky.

**Revisit if.** Rate limiting moves to the ingress layer (see decision 003). The
exemption then has to be expressed there instead, and this one becomes dead code —
which is worth remembering, because a stale exemption that no longer does anything
reads like protection you don't have.

---

## 006 — `preStop: sleep 5`, and `exec` in the Compose command

**Context.** A rolling update is advertised as zero-downtime and is not, by default.
When a Pod is deleted, endpoint removal and SIGTERM are dispatched **concurrently**,
and endpoint removal then has to propagate — endpoints controller → EndpointSlice →
every node's kube-proxy → iptables rules. Nothing waits for that. During the window,
some node still routes new connections to a Pod that has stopped accepting them.

Measured with an in-cluster load generator: **~3 failed requests per 500 during a
rollout, about 0.6%** — intermittent, unlogged, and invisible in any dashboard.

**Options considered.**

1. **Do nothing.** 0.6% is within noise and clients retry.
2. **Fix it in the application** — delay inside the SIGTERM handler before closing
   the listener.
3. **`lifecycle.preStop` with a sleep**, so the delay happens *before* SIGTERM.
4. **A service mesh** that handles connection draining.

**Decision.** Option 3: `preStop: exec: ["sleep", "5"]` with
`terminationGracePeriodSeconds: 45` in `k8s/40-deployment.yaml`. Separately, the
application's own drain is left to uvicorn plus the `lifespan` teardown in
`src/main.py`.

**Rationale.** Option 1 was rejected once the number existed rather than being
argued about — the load generator is the reason this is a decision and not an
opinion.

Option 2 **cannot work.** The dropped requests are refused by the kernel because
nothing is listening; the application is not involved in its own failure and has no
hook to intervene. This is the fact that makes the whole thing counter-intuitive: a
flawless SIGTERM handler drops these requests anyway.

Option 3 works because of an ordering property, not a timing one: `preStop` runs
**before** SIGTERM and kubelet **waits for it**. During the sleep the app is
completely unsignalled and still serving, so the routing rules converge while
traffic is still being served correctly, and new connections stop arriving
*naturally*. It is not buying time; it is reordering two events Kubernetes runs
concurrently.

Option 4 solves this and much more, and is not justified by one service.

**The grace period is arithmetic, not a default:**
`45 >= preStop (5) + longest in-flight request (~30) + margin`. Getting it backwards
is **strictly worse than no hook** — SIGKILL then arrives during the sleep, so you
get neither the routing delay nor a graceful drain.

**A real instance of the same class of bug was found and fixed in
`docker-compose.yml`.** It started the app with
`sh -c "alembic upgrade head && uvicorn ..."`. Because `A && B` is a *compound*
command, the shell cannot exec-optimise itself away, so `sh` remains PID 1 — and
signals go to PID 1. `docker compose stop` sent SIGTERM to the shell, which does not
forward it while waiting, so uvicorn never learned it was shutting down and was
SIGKILLed after Docker's grace period. The visible symptom is only "stopping the
stack is slow"; the invisible one is that the `lifespan` teardown never runs, so
Redis is not closed and the DB pool is not disposed. The fix was one word: `exec`
before `uvicorn`. `k8s/40-deployment.yaml` was already immune because it sets no
`command` at all, so the Dockerfile's exec-form `CMD` makes uvicorn PID 1.

**What it cost.** Every Pod deletion is now 5 seconds slower, so rollouts and
scale-downs take longer. `sleep` must exist in the image — true for
`python:3.12-slim`, false for `distroless`/`scratch`, where the hook would fail with
only a `FailedPreStopHook` event as evidence and the dropped requests would quietly
return. And a failing `preStop` does **not** stop termination, so this is
best-effort by design.

It also does not cover existing **keep-alive** connections, which bypass endpoint
removal entirely — so this reduces dropped requests rather than eliminating them,
and client retries plus idempotency are still required. A hard node failure gives no
signal at all.

**Revisit if.** The cluster reaches Kubernetes 1.30+, where the native
`preStop: { sleep: { seconds: 5 } }` needs no binary and works on distroless. Or if
long-lived connections (WebSocket, gRPC streaming) are added, at which point
`Connection: close` on drain, GOAWAY, or a mesh becomes necessary rather than
optional.

---

## 007 — The HPA owns `replicas`, and `maxReplicas` comes from `max_connections`

**Context.** Adding a HorizontalPodAutoscaler (`k8s/50-hpa.yaml`) moves the replica
count from "a number a human typed" to "a number a controller derives every 15
seconds". Two questions had to be answered that the `kubectl autoscale` one-liner
hides: who owns `spec.replicas`, and what should `maxReplicas` be.

**Options considered — ownership of `replicas`.**

1. **Keep `replicas: 3` in the Deployment** alongside the HPA.
2. **Remove `replicas`** from the Deployment; the floor moves to `minReplicas`.
3. **Keep it, and use server-side apply** with a distinct field manager (or Argo CD
   `ignoreDifferences`) to stop it being reconciled.

**Options considered — `maxReplicas`.**

1. **A round number** — 10, or 20.
2. **Derived from the node's allocatable capacity.**
3. **Derived from the tightest downstream dependency.**

**Decision.** Option 2 for ownership: `replicas` is **removed** from
`k8s/40-deployment.yaml`, with the old line left commented out and the reasoning
written above it. Option 3 for the ceiling: `maxReplicas: 6`, `minReplicas: 3`,
`averageUtilization: 70` on CPU.

**Rationale — ownership.** Option 1 puts two controllers on one field. Every
`kubectl apply` sets 3, the HPA raises it to 5, the next apply (or a GitOps sync,
which may run every few minutes forever) knocks it back. Pods churn continuously
with **no load change to explain it**, so it presents as a flapping application
rather than as a configuration conflict — which is what makes it expensive to
diagnose. Omitting the field means "unmanaged": Kubernetes defaults it to 1 at
creation and never touches it again. Option 3 is legitimate and correct, and needs
tooling knowledge to read; removing the field states the intention in the file
itself.

**Rationale — the ceiling.** This is the part worth defending. SQLAlchemy's default
async pool is `pool_size=5 + max_overflow=10` = **15 connections per process**
(`src/db.py` does not override it), uvicorn runs one worker per Pod, and Postgres
defaults to `max_connections=100`:

```
 6 × 15 =  90   ✓
10 × 15 = 150   ✗  →  "FATAL: sorry, too many clients already"
```

At ten replicas under load the autoscaler **takes down the database it is scaling to
protect**, and the failure surfaces in the application, so the app looks like the
victim rather than the cause. The general principle: *the thing you scale into has
limits too, and Kubernetes knows nothing about them.* Option 1 is how that outage
gets shipped. Option 2 is a real second constraint on a single-node cluster
(6 × 100m + 6 × 128Mi must fit in allocatable) but it is not the *tightest* one.

CPU was chosen as the metric because this is a request/response HTTP API where CPU
is what saturates first. Memory was explicitly rejected: Python returns very little
memory to the OS after a spike, so a memory-based HPA scales out and never scales
back in — a ratchet. And because the HPA takes the **maximum** desired count across
all metrics, a sticky metric does not merely add a signal, it becomes a floor that
suppresses every scale-down.

**What it cost.** Three things, all real.

1. **Applying `40-deployment.yaml` alone now yields one replica** until the HPA
   raises it. The README's apply sequence places the two adjacent, and the trade is
   worth it — a `replicas` field in both places is a permanent conflict.
2. **The HPA is only as honest as the CPU request.** `averageUtilization: 70` means
   70% *of the request*, so with `requests.cpu: 100m` against `limits.cpu: 1000m`
   the scale-out threshold is **70m — 7% of what the container may actually use**.
   That is very eager scale-out (safe, flat latency) at the cost of more, smaller
   Pods, each carrying its own memory baseline and its own database connections. The
   honest fix is a request sized from measurement; there is no load history to size
   from yet, and both files say so rather than implying the number was measured.
3. **Silent failure modes inherited.** No metrics-server means the HPA reports
   `<unknown>` and does nothing, with no alarming event. And a single container
   without a CPU request — a sidecar added later — disables the calculation for the
   whole Pod.

This also retroactively strengthens decision 003: with a fixed 3 replicas, in-memory
rate-limit counters were a *known* 3× inaccuracy. Under an HPA the multiplier changes
with load, so a documented "100/minute" becomes 300 at three replicas and 600 at six
— and it moves during a spike, exactly when the limit is supposed to be protecting
you. Autoscaling converts a known inaccuracy into an unpredictable one.

**Revisit if.** PgBouncer (transaction pooling) is introduced or `pool_size` is set
explicitly per Pod — then `maxReplicas` is no longer bounded by the database and the
arithmetic in `50-hpa.yaml` must be recomputed rather than the number simply raised.
Also revisit if a queue-backed worker is added (Celery, per the README): CPU is the
wrong metric for an I/O-bound consumer, and that workload wants KEDA scaling on
backlog, not a CPU-based HPA.

---

## 008 — A PodDisruptionBudget with `minAvailable: 2`, applied even though the cluster has one node

**Date:** during the Kubernetes hardening pass
**Status:** accepted
**Files:** `k8s/51-pdb.yaml` (new), `k8s/40-deployment.yaml` (spreading), `k8s/50-hpa.yaml` (coupling), `k8s/README.md`

**Context.** Every protection added so far guards the application against *itself*:
probes catch a broken process, resource requests stop it starving its neighbours,
`preStop` stops a rollout dropping requests, the HPA sizes the fleet to load. None of
them address disruption arriving from **outside** the application — a cluster
administrator running `kubectl drain` to patch a kernel, a managed node pool
upgrading itself on a schedule nobody here controls, the cluster autoscaler
consolidating an idle node away, the descheduler rebalancing.

Each of those is a well-behaved controller asking *politely* to remove Pods, through
the eviction API. And none of them knows how many of this application's Pods may
safely go at once. With no answer available, a drain evicts **every** Pod of this app
that happens to be on that node, simultaneously, and reports success. On a
single-node cluster with three replicas, that is all of them.

The second half of the same problem: nothing so far prevented all three replicas
being scheduled onto one node in the first place, so "three replicas" was a number in
a manifest rather than a property of the system.

**Options considered.**

*For the disruption budget:*

1. **No PDB.** Correct-looking on a single-node cluster, since a PDB cannot deliver
   its guarantee there anyway.
2. **`minAvailable: 3`** (equal to `minReplicas`) — maximum protection.
3. **`minAvailable: 2`** — one eviction at a time, two Pods always serving.
4. **`maxUnavailable: 1`** — the same behaviour at 3 replicas, expressed as a ceiling.

*For placement:*

5. **`requiredDuringSchedulingIgnoredDuringExecution` anti-affinity** on
   `kubernetes.io/hostname` — a hard guarantee of one Pod per node.
6. **`preferredDuringSchedulingIgnoredDuringExecution`** — spread when possible.
7. **`topologySpreadConstraints` with `DoNotSchedule`** — the modern, quantitative
   form, active.
8. **`preferred` anti-affinity active, `topologySpreadConstraints` written but
   commented.**

**Decision.** Option **3** and option **8**.

`k8s/51-pdb.yaml`: `policy/v1`, selector `{app: tasks-api, component: api}`,
`minAvailable: 2`, `unhealthyPodEvictionPolicy` deliberately unset.
`k8s/40-deployment.yaml`: soft `podAntiAffinity` on `kubernetes.io/hostname`, with the
production-shaped `topologySpreadConstraints` (`maxSkew: 1` on
`topology.kubernetes.io/zone`, `DoNotSchedule`) present as commented YAML.

**Rationale.**

Option 2 is the trap, and it is worth naming precisely because it reads as care.
`minAvailable` equal to the replica count makes `ALLOWED DISRUPTIONS: 0`
**permanently**. Every voluntary eviction is refused forever: node drains retry until
they time out, managed node-pool upgrades stall part-way, autoscaler scale-down never
runs so you pay for idle nodes, and kernel patches do not land. Nothing logs an error,
because from the cluster's point of view nothing is wrong. *A PDB set to its most
protective value is not protection, it is a scheduled outage* — the maintenance it
blocks is maintenance you need, so it eventually happens in a hurry, badly.

`minAvailable: 2` is `minReplicas - 1`, which is exactly what a rolling node upgrade
needs: one Pod evicted at a time, two left serving, and enough margin that the
replacement being slow to become Ready is survivable. The **absolute** form was chosen
over option 4's ceiling specifically because there is now an HPA: at the ceiling of 6
replicas, `minAvailable: 2` allows 4 concurrent evictions, so drains get faster as the
fleet grows, where `maxUnavailable: 1` would keep them slow forever. Percentages would
be the right call for a fleet of hundreds; at 3–6 Pods they only add rounding
questions.

⚠️ **This is the same decision as `minReplicas: 3` in `50-hpa.yaml`, seen from the
other side.** A PDB with `minAvailable: 2` is only satisfiable above 2 replicas. Patch
`minReplicas` to 2 and this budget permanently allows zero disruptions; patch it to 1
and drains block forever. The availability floor and the disruption budget are
coupled, they live in two files, and **nothing validates that they agree** — an edit
to the autoscaler can silently break node drains. Both files state the coupling
explicitly, which is the only mechanism available.

Option 1 was rejected even though its premise is correct. A PDB on one node genuinely
cannot keep 2 Pods available while the only node is drained — the drain simply blocks.
But blocking is the *right* failure: it makes the drain fail **loudly** instead of
quietly destroying the service, and it is what the manifest must say for the cluster
this application would actually be deployed to. Shipping the object and documenting
its single-node limitation is stronger than omitting it and discovering the gap during
a cluster upgrade.

On placement, option 5 is unsatisfiable here — `required` anti-affinity on hostname
with one node leaves **2 of 3 Pods `Pending` forever**, degrading the app permanently
in the name of availability. Option 7 has the same defect via `DoNotSchedule`. Option 6
gives a manifest that is correct in both environments: it spreads across three nodes
and schedules anyway on one. Option 8 adds the constraint that `maxSkew` expresses
something anti-affinity structurally cannot — "roughly evenly", rather than binary
"never together" — as commented YAML, with two honest reasons for it not being active:
one topology domain makes any spread constraint vacuous, and running both mechanisms
simultaneously means two placement systems with overlapping intent and no clear owner.

`policy/v1` rather than `policy/v1beta1` is not a preference: the beta API was
**removed** in Kubernetes 1.25 and a v1beta1 manifest fails outright.

**What it cost.** Four things.

1. **On this cluster the guarantee is unenforceable and the drain just blocks.** Named
   in the file rather than implied.
2. **The PDB is silently useless without readiness probes.** It counts *Ready* Pods
   (`status.currentHealthy`), so with no readiness probe every Pod is "healthy" the
   instant it starts and the budget is computed from a meaningless number. This makes
   the probe work a hard prerequisite, not a related improvement.
3. **`unhealthyPodEvictionPolicy` is left unset, which preserves a real deadlock.**
   The default `IfHealthyBudget` means an unhealthy Pod may only be evicted if the
   budget currently allows a disruption — so if all 3 Pods are broken,
   `currentHealthy: 0`, nothing may be evicted, and the PDB ends up protecting an
   outage from being repaired. `AlwaysAllow` fixes it, but it is beta in 1.27 and GA in
   1.31, and older API servers **silently drop unknown fields**: setting it on such a
   cluster applies cleanly, sits in the manifest, and does nothing — worse than
   omitting it, because you would believe you were protected. The file records the
   round-trip check (`kubectl get pdb ... -o yaml | grep unhealthyPod`) that decides
   when to enable it.
4. **It does not protect against the things people assume it does.** Not
   `kubectl delete pod` (a PDB constrains the *eviction* subresource; delete is a
   different call and is deliberately unchecked, so `kubectl delete pod -l
   app=tasks-api` takes the service down and reports success). Not involuntary
   disruption — a kernel panic, failed disk, or network partition consults no budget,
   and a Pod on an unreachable node waits out the default 300-second
   `node.kubernetes.io/unreachable` toleration before rescheduling. Not grace periods:
   this object controls *how many* Pods go down at once, while decision 006's `preStop`
   and `terminationGracePeriodSeconds` control whether *each one* goes down without
   dropping requests.

**Revisit if.** The cluster gains nodes — then `topologySpreadConstraints` should be
uncommented (on `zone` if zones exist, `hostname` otherwise) and the anti-affinity
block removed rather than kept alongside it, and the PDB's guarantee becomes real
rather than aspirational. Revisit `unhealthyPodEvictionPolicy: AlwaysAllow` on 1.31+
once the round-trip check passes. Revisit the value if `minReplicas` in `50-hpa.yaml`
ever changes — the two must move together, and nothing will tell you otherwise.
