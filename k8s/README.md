# Kubernetes manifests

Everything needed to run this application on a local Kubernetes cluster
(k3s via Rancher Desktop, minikube, kind, or Docker Desktop's Kubernetes).

Every file here is **heavily commented on purpose**. The comments explain *why*
each field has the value it does, what the alternatives were, and what the choice
costs — not just what the field means. Read the manifests; they are the
documentation.

---

## Files, and the order they apply in

The numeric prefixes control `kubectl apply -f k8s/`, which processes files
**alphabetically**. Tens leave room to insert later without renaming everything.

| File | What it is | Why it's at this position |
|---|---|---|
| `00-namespace.yaml` | `Namespace: tasks-api` | Must exist before anything can be created inside it |
| `10-configmap.yaml` | Non-secret configuration | Pods reference it at start; must exist before the Deployment's Pods run |
| `11-secret.example.yaml` | **Template** for the Secret | Not applied directly — see below |
| `20-postgres.yaml` | Headless `Service` + `StatefulSet` | The database must be reachable before migrations |
| `21-redis.yaml` | `Service` + `Deployment` (cache) | Independent of everything else |
| `30-migration-job.yaml` | `Job` running `alembic upgrade head` | Schema must exist before the app queries it |
| `40-deployment.yaml` | The application (**no `replicas` field** — see below) | Needs config, secrets, and a migrated schema |
| `41-service.yaml` | `ClusterIP` fronting the app Pods | Stable address for the Ingress to target |
| `42-ingress.yaml` | `Ingress` + Traefik `Middleware` | External HTTPS entry point |
| `50-hpa.yaml` | `HorizontalPodAutoscaler` (3–6 replicas, CPU 70%) | Owns the replica count; apply right after the Deployment |
| `51-pdb.yaml` | `PodDisruptionBudget` (`minAvailable: 2`) | Needs the Pods to exist to be meaningful; coupled to `minReplicas: 3` |

> **The most important caveat on this page:** filename order controls
> **creation**, not **readiness**. Kubernetes will happily create the Deployment
> while Postgres is still starting. Ordering by readiness needs an explicit
> `kubectl wait`, an initContainer, Helm hooks, or ArgoCD sync waves — see the
> sequence below and the comments in `30-migration-job.yaml`.

---

## First-time deploy

### 1. Build the image *into the cluster's image store*

```bash
# From the repository root (one level up from k8s/)
docker build -t tasks-api:0.1.0 .
```

> **This is the single most common failure in the whole project.** The manifests
> reference `tasks-api:0.1.0`, which exists in no registry. Two things must both
> be true:
>
> 1. `imagePullPolicy: IfNotPresent` (already set in the manifests), so
>    Kubernetes uses the local copy instead of trying Docker Hub.
> 2. The image must be in the **cluster's** image store, not only your local
>    Docker daemon. `docker images` listing it proves nothing about what the
>    cluster can see.
>
> With **Rancher Desktop on the `dockerd` (moby) runtime**, `docker build` puts
> the image where k3s can find it, so the command above is enough.
>
> With Rancher Desktop on the **`containerd`** runtime, use `nerdctl` and the
> k8s namespace instead:
>
> ```bash
> nerdctl --namespace k8s.io build -t tasks-api:0.1.0 .
> ```
>
> On **minikube**: `eval $(minikube docker-env)` before building, or
> `minikube image load tasks-api:0.1.0`.
> On **kind**: `kind load docker-image tasks-api:0.1.0`.
>
> Symptom if you get this wrong: `ImagePullBackOff` or `ErrImageNeverPull`.
> `kubectl describe pod <name> -n tasks-api` says so explicitly — read the
> Events section rather than guessing.

### 2. Create the Secret

The Secret is **not** in this repository (`.gitignore` blocks
`*secret*.yaml`), because a Kubernetes Secret is base64-**encoded**, not
encrypted — committing one is exactly as bad as committing a plaintext password.

Derive both values from one shell variable so they cannot drift:

```bash
DB_PASS="$(python -c 'import secrets; print(secrets.token_urlsafe(24))')"
JWT_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"

kubectl create namespace tasks-api --dry-run=client -o yaml | kubectl apply -f -

kubectl create secret generic tasks-api-secrets \
  --namespace tasks-api \
  --from-literal=JWT_SECRET_KEY="$JWT_KEY" \
  --from-literal=POSTGRES_PASSWORD="$DB_PASS" \
  --from-literal=DATABASE_URL="postgresql+asyncpg://tasks:${DB_PASS}@postgres:5432/tasks"
```

The alternative — copy `11-secret.example.yaml` to `11-secret.yaml`, fill it in,
apply it — is fine too. If you do that, verify the ignore rule actually works
before you trust it:

```bash
git check-ignore -v k8s/11-secret.yaml   # must print a matching rule
```

Silence means the file is **not** ignored. Stop and fix that before committing.

Read the comments in `11-secret.example.yaml` for what a Secret does and does
not protect, and for how this is done properly in production (External Secrets
Operator or Sealed Secrets).

### 3. Create the TLS certificate for the Ingress

```bash
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout tls.key -out tls.crt \
  -subj "/CN=tasks.localdev/O=tasks-api" \
  -addext "subjectAltName=DNS:tasks.localdev"

kubectl create secret tls tasks-api-tls \
  --namespace tasks-api --cert=tls.crt --key=tls.key

rm tls.key tls.crt
```

The `-addext subjectAltName` part is not optional — browsers have required a SAN
since 2017 and reject certificates that only set a Common Name.

Then add the hostname to your hosts file so a browser can resolve it:

- Windows: `C:\Windows\System32\drivers\etc\hosts` (needs admin)
- macOS / Linux: `/etc/hosts` (needs sudo)

```
127.0.0.1 tasks.localdev
```

(You can skip this and still test with `curl --resolve` — see
`42-ingress.yaml`.)

### 4. Apply, in readiness order

`kubectl apply -f k8s/` in one shot creates everything but guarantees nothing
about ordering. This sequence guarantees it:

```bash
# Namespace and config first
kubectl apply -f k8s/00-namespace.yaml
kubectl apply -f k8s/10-configmap.yaml

# Data stores
kubectl apply -f k8s/20-postgres.yaml
kubectl apply -f k8s/21-redis.yaml

# Wait for Postgres to actually accept connections (not just exist)
kubectl wait --for=condition=ready pod -l app=postgres \
  -n tasks-api --timeout=180s

# Migrations, then block until they complete
kubectl delete job tasks-api-migrate -n tasks-api --ignore-not-found
kubectl apply -f k8s/30-migration-job.yaml
kubectl wait --for=condition=complete job/tasks-api-migrate \
  -n tasks-api --timeout=120s

# The application. 40-deployment.yaml has NO `replicas` field — the HPA owns it —
# so applying it alone gives you 1 replica. Apply the HPA immediately after.
kubectl apply -f k8s/40-deployment.yaml
kubectl apply -f k8s/50-hpa.yaml
kubectl apply -f k8s/41-service.yaml
kubectl apply -f k8s/42-ingress.yaml
kubectl apply -f k8s/51-pdb.yaml

# Confirm the rollout succeeded rather than assuming it did
kubectl rollout status deployment/tasks-api -n tasks-api --timeout=130s

# The HPA should take you from 1 to minReplicas: 3 within ~15-30 seconds
kubectl get hpa tasks-api -n tasks-api

# And the budget must allow exactly 1 disruption. 0 means drains will hang.
kubectl get pdb tasks-api -n tasks-api
```

Three things worth noticing about that sequence:

- The `kubectl wait` lines are the ones doing the real work. Without them this is
  just "create everything and hope".
- `kubectl wait --for=condition=complete` does **not** return early if the Job
  fails — it waits out the full timeout. That awkwardness is a large part of why
  real pipelines use Helm hooks or ArgoCD sync waves instead of hand-rolled
  `kubectl` sequencing.
- The Deployment starts at **1** replica, briefly, before the HPA raises it to 3.
  That is the cost of moving replica ownership to the HPA, and it is the right
  trade — a `replicas` field in both places makes the two controllers fight
  forever (see the comment block at the top of `40-deployment.yaml`).

> **If `kubectl get hpa` shows `TARGETS: <unknown>/70%`, metrics-server is
> missing.** Confirm with `kubectl top pods -n tasks-api`. Without it the HPA is
> inert — created, healthy-looking, and doing nothing. Rancher Desktop's k3s
> bundles metrics-server; kind and minikube generally do not.

### 5. Verify

```bash
kubectl get all -n tasks-api

# The command that actually matters when something is wrong:
kubectl get endpoints tasks-api -n tasks-api    # empty = your bug

# From inside the cluster
kubectl run tmp --rm -it --restart=Never --image=curlimages/curl \
  -n tasks-api -- curl -sS http://tasks-api:8000/health

# Through the Ingress, without needing a hosts entry
curl -k --resolve tasks.localdev:443:127.0.0.1 https://tasks.localdev/health

# Or tunnel from your machine
kubectl port-forward -n tasks-api svc/tasks-api 8000:8000
# then: curl http://localhost:8000/health
```

### Check what readiness actually reports

`/health` only proves the process is alive. `/ready` is the interesting one — it
reports *per-dependency* status, so it tells you which layer is broken:

```bash
kubectl port-forward -n tasks-api svc/tasks-api 8000:8000
curl -s localhost:8000/ready | python -m json.tool
# {"status": "ready", "checks": {"database": "ok", "cache": "ok"}}
```

The database is **fatal**; Redis is **reported but not fatal**. Prove both, because
the asymmetry is the design and it's worth seeing rather than trusting:

```bash
# Scale Redis to zero. The cache is optional, so the Pod must STAY ready.
kubectl scale deployment/redis -n tasks-api --replicas=0
curl -s localhost:8000/ready              # status still "ready", cache "unavailable"
kubectl get endpoints tasks-api -n tasks-api   # all 3 Pod IPs still listed
kubectl scale deployment/redis -n tasks-api --replicas=1

# Scale Postgres to zero. The database is required, so readiness must FAIL.
kubectl scale statefulset/postgres -n tasks-api --replicas=0
kubectl get pods -n tasks-api -w          # tasks-api Pods go 1/1 -> 0/1 READY,
                                          # but they are NOT restarted
kubectl get endpoints tasks-api -n tasks-api   # empty — removed from the Service
kubectl scale statefulset/postgres -n tasks-api --replicas=1
```

⚠️ **Watch the RESTARTS column during that second test.** It must stay at 0. The
Pods leave the Service but are never killed — because the database check is in
*readiness*, not liveness. Had it been in liveness, every replica would restart
simultaneously and hammer the recovering database. Seeing `0/1 READY` with
`0 RESTARTS` is what the whole probe split buys you, in one line of output.

### Verify the rate limit is cluster-wide

The `X-RateLimit-*` headers exist so you can prove the shared counter works from
outside the cluster, with no exec and no logs:

```bash
for i in $(seq 1 5); do
  curl -s -o /dev/null -D - localhost:8000/users/me \
    | grep -i 'x-ratelimit-remaining'
done
# remaining must count DOWN monotonically: 99, 98, 97, 96, 95
```

Monotonic means all three replicas share one counter. If the numbers **jump around**
(99, 99, 98, 99, 97) you are hitting per-process counters — check that
`RATE_LIMIT_STORAGE_URI` reached the Pods:

```bash
kubectl exec -n tasks-api deploy/tasks-api -- printenv RATE_LIMIT_STORAGE_URI
# redis://redis:6379/1

# And see the counters themselves:
kubectl exec -n tasks-api deploy/redis -- redis-cli -n 1 --scan --pattern 'rl*'
```

⚠️ If you edited the ConfigMap and the value above is stale, that's not a bug —
env vars are read once at container start. `kubectl rollout restart
deployment/tasks-api -n tasks-api`.

---

## Updating the application

```bash
docker build -t tasks-api:0.2.0 .
kubectl set image deployment/tasks-api api=tasks-api:0.2.0 -n tasks-api
kubectl rollout status deployment/tasks-api -n tasks-api --timeout=130s \
  || kubectl rollout undo deployment/tasks-api -n tasks-api
```

That last `||` is the whole automatic-rollback story. `progressDeadlineSeconds`
makes Kubernetes *report* a stalled rollout; it does not undo it. Rolling back is
your pipeline's job.

Prefer `kubectl set image` (or editing the tag in the manifest and re-applying)
over `kubectl rollout restart` when the code changed — `restart` re-creates Pods
with the *same* image, which is what you want after a ConfigMap or Secret change,
not after a build.

**Always bump the tag.** Rebuilding `tasks-api:0.1.0` with new content and
re-applying does nothing at all: the Pod template is unchanged, so there is no
new ReplicaSet and no rollout. This looks like Kubernetes ignoring you.

---

## After changing a ConfigMap or Secret

Environment variables are read **once**, at container start. Editing a ConfigMap
or Secret does not restart anything — the running Pods keep the old values
indefinitely, with no error and nothing in the logs.

```bash
kubectl apply -f k8s/10-configmap.yaml
kubectl rollout restart deployment/tasks-api -n tasks-api
```

Production setups automate this with a checksum annotation on the Pod template
(what Helm charts do) or the [Reloader](https://github.com/stakater/Reloader)
controller.

---

## Debugging: test each layer inward

When something doesn't work, don't change YAML hopefully. Test from the inside
out — **the first step that fails is the layer with the bug**, and everything
outside it is innocent.

```bash
# 1. Is the process serving inside its own container?
kubectl exec -n tasks-api deploy/tasks-api -- \
  python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:8000/health').status)"

# 2. Does the Service route to it?
kubectl run tmp --rm -it --restart=Never --image=curlimages/curl \
  -n tasks-api -- curl -sS http://tasks-api:8000/health

# 3. Can you reach it from your machine?
kubectl port-forward -n tasks-api svc/tasks-api 8000:8000

# 4. Does the Ingress route it?
curl -k --resolve tasks.localdev:443:127.0.0.1 https://tasks.localdev/health
```

Useful commands by symptom:

| Symptom | Command | What you're looking for |
|---|---|---|
| Pod not starting | `kubectl describe pod <name> -n tasks-api` | The **Events** section at the bottom |
| `ImagePullBackOff` | `kubectl describe pod <name> -n tasks-api` | Image name/tag typo, or image not in the cluster's store |
| `CreateContainerConfigError` | `kubectl describe pod <name> -n tasks-api` | A missing ConfigMap or Secret key |
| `CrashLoopBackOff` | `kubectl logs <pod> -n tasks-api --previous` | `--previous` shows the **crashed** container's logs, not the new one's |
| `0/1 READY` but Running | `kubectl describe pod <name> -n tasks-api` | Readiness probe failures in Events |
| Service returns nothing | `kubectl get endpoints <svc> -n tasks-api` | Empty = selector mismatch **or** no ready Pods |
| Ingress not working | `kubectl get ingress -n tasks-api` | Empty `ADDRESS` = no controller claimed it |
| Restarts climbing | `kubectl describe pod <name>` | `Last State: Terminated, Reason: OOMKilled` (exit 137) |
| HPA shows `<unknown>` | `kubectl top pods -n tasks-api` | metrics-server missing, or a container with no CPU request |
| HPA won't scale down | `kubectl describe hpa tasks-api -n tasks-api` | Almost always `scaleDown.stabilizationWindowSeconds: 300` working as designed |
| Replica count oscillates | `kubectl get hpa,deploy -n tasks-api` | A `replicas` field fighting the HPA — it must not exist in the Deployment |
| `kubectl drain` hangs | `kubectl get pdb -A` | Any row with `ALLOWED DISRUPTIONS: 0` — the drain is retrying, not stuck |
| All Pods on one node | `kubectl get pods -n tasks-api -o wide` | Anti-affinity is `preferred`, so one node is expected here — see `40-deployment.yaml` |
| Everything looks fine | `kubectl get events -n tasks-api --sort-by=.lastTimestamp` | The cluster's own account of what happened |

---

## Tearing it down

```bash
kubectl delete namespace tasks-api
```

One command removes every object in the namespace.

> **Except the PersistentVolumeClaim.** Deleting a StatefulSet does not delete
> its PVCs — that's deliberate data protection. Deleting the *namespace* does
> remove them, since PVCs are namespaced. But if you delete only the StatefulSet
> and recreate it, it **reuses the old volume**, which means:
>
> - the old data is still there when you expected a fresh start, and
> - the old database password still applies, because `POSTGRES_PASSWORD` only
>   takes effect when initialising an **empty** data directory.
>
> That second one is the most confusing behaviour a newcomer meets. To genuinely
> start over:
>
> ```bash
> kubectl delete statefulset postgres -n tasks-api
> kubectl delete pvc postgres-data-postgres-0 -n tasks-api
> ```

---

## What these manifests deliberately do not do

Being explicit about the gaps is part of the point — an unstated limitation reads
as an oversight; a stated one reads as a judgement call.

| Not here | Why not | What production uses |
|---|---|---|
| Secrets in git | Base64 is not encryption | External Secrets Operator, Sealed Secrets |
| Real TLS certificates | Let's Encrypt can't validate a hosts-file domain | cert-manager with a `ClusterIssuer` |
| A templating layer | Duplication is still small enough to read | Helm or Kustomize |
| Postgres HA / backups | A StatefulSet gives restart, not failover or PITR | A managed database, or CloudNativePG |
| `NetworkPolicy` | k3s's default CNI (flannel) does not enforce them | Calico or Cilium, with default-deny |
| Metrics and tracing | Not yet instrumented | Prometheus + Grafana, OpenTelemetry |
| `ResourceQuota` / `LimitRange` | Single-tenant learning cluster | Namespace-level quotas per team |
| Multi-node HA | One node, so anti-affinity is a soft preference | ≥3 nodes across failure domains |
| Database connection pooling | It's why `maxReplicas` is **6**, not 60 — see `50-hpa.yaml` | PgBouncer in transaction mode |
| Autoscaling on anything but CPU | No metrics pipeline yet, and CPU fits an HTTP API | prometheus-adapter, or KEDA for queue-driven work |

Running a database in Kubernetes by hand is a **learning exercise here, not a
recommendation**. Kubernetes gives you restart, rescheduling, and volume
attachment. It does not give you point-in-time recovery, automated failover with
correct fencing, connection pooling, or verified backups — and those are the hard
parts of running a database.
