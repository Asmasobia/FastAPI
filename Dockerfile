# ==============================================================================
# Dockerfile — how to package this app into a portable container IMAGE.
#
# WHY DOCKER: "works on my machine" dies here. The image bundles the exact
# Python version, dependencies, and code, so it runs identically on your laptop,
# CI, and production. One artifact, everywhere.
#
# WHY MULTI-STAGE (two FROM lines): we install dependencies in a "builder" stage
# with all the build tools, then COPY only the finished packages into a clean,
# small "runtime" stage. The final image doesn't carry compilers or caches, so
# it's smaller (faster to push/pull) and has a smaller attack surface.
#
# NOTE: Docker isn't installed on your current machine, so this file is here to
# read/learn and to run later once you install Docker Desktop. The commands to
# build/run it are in the README.
# ==============================================================================

# ---- STAGE 1: builder — install dependencies into an isolated prefix ----
FROM python:3.12-slim AS builder

WORKDIR /app

# Copy ONLY requirements first (not the whole app). This leverages Docker's
# LAYER CACHE: as long as requirements.txt is unchanged, Docker reuses the
# cached "pip install" layer on rebuilds, even when your source code changed.
# Copying code first would bust that cache on every code edit. Big speedup.
COPY requirements.txt .

# Install into a separate directory (--target=/deps) so we can copy just that
# folder into the runtime stage. --no-cache-dir keeps the layer lean.
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir --target=/deps -r requirements.txt


# ---- STAGE 2: runtime — the final, slim image that actually ships ----
FROM python:3.12-slim

WORKDIR /app

# Environment hygiene:
#   PYTHONUNBUFFERED=1  -> logs stream out immediately (no buffering), so you
#                          see them in `docker logs` in real time.
#   PYTHONDONTWRITEBYTECODE=1 -> don't litter .pyc files in the container.
#   PYTHONPATH=/deps    -> make the copied dependencies importable.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/deps

# Bring over the installed dependencies from the builder stage.
COPY --from=builder /deps /deps

# Copy the application code and migration setup.
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini

# SECURITY: run as a NON-ROOT user. If the app is ever compromised, the attacker
# is a low-privilege user inside the container, not root. Running as root in
# containers is a common, avoidable risk. We create a user and switch to it.
RUN useradd --create-home --uid 1000 appuser
USER appuser

# Document the port the app listens on (informational; publish it with -p).
EXPOSE 8000

# HEALTHCHECK: Docker periodically runs this to know if the container is healthy.
# We hit /health with a tiny inline Python script (no curl needed in the image).
# If it exits non-zero repeatedly, orchestrators can restart the container.
#
# ⚠️ /health AND NOT /ready, ON PURPOSE. Docker has only ONE health concept, and
#    an unhealthy container is a container it restarts. That makes it a LIVENESS
#    check, so it must not depend on anything external — otherwise a brief
#    database hiccup restarts the app, the restart hammers the recovering
#    database, and a 10-second blip becomes a self-sustaining outage.
#
#    Kubernetes is the environment that can tell the two apart, and it ignores
#    this HEALTHCHECK entirely — it uses the probes in k8s/40-deployment.yaml,
#    where /ready gates SERVICE MEMBERSHIP (cheap, reversible) and /health gates
#    RESTARTS (expensive, destructive). Two probes because there are two
#    questions; one probe here because Docker only asks one.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# The command that starts the server. --host 0.0.0.0 makes it reachable from
# OUTSIDE the container (127.0.0.1 would only be reachable inside it). We do NOT
# use --reload here; reload is a dev-only convenience.
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
