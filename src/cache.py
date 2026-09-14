"""
cache.py — a tiny async Redis CACHE wrapper that DEGRADES GRACEFULLY.

WHY CACHE:
  Some reads are hot and expensive (a dashboard endpoint, a rarely-changing
  record). Caching stores the computed result in fast in-memory storage (Redis)
  with a TTL (time-to-live), so repeat reads skip the database. Classic
  read-through cache: check cache -> miss -> hit DB -> store in cache -> return.

WHY "DEGRADE GRACEFULLY":
  A cache is an OPTIMIZATION, not a source of truth. If Redis is down or not
  configured, the app must still work — just slower. So every method here
  swallows connection errors and behaves as a cache MISS. Losing the cache must
  never take down the API. (This is also why the project runs fine with
  CACHE_ENABLED=false and no Redis at all.)

DESIGN:
  One CacheClient object is created at startup (in the lifespan) and closed at
  shutdown. Endpoints get it via a dependency. When disabled/unavailable, get()
  returns None (miss) and set() is a no-op.
"""

import logging

# redis.asyncio is the async client shipped inside the modern redis package.
import redis.asyncio as aioredis

logger = logging.getLogger("app.cache")


class CacheClient:
    def __init__(self, redis_url: str, enabled: bool) -> None:
        self._enabled = enabled
        self._redis: aioredis.Redis | None = None
        if enabled:
            # from_url builds a client with a CONNECTION POOL. It does NOT
            # connect yet — the first command connects lazily. decode_responses
            # makes returned values str instead of bytes, so callers get plain
            # strings back.
            self._redis = aioredis.from_url(
                redis_url, decode_responses=True
            )

    @property
    def enabled(self) -> bool:
        """
        Whether caching was switched on by configuration.

        WHY EXPOSE THIS: the readiness endpoint (src/routers/system.py) needs to
        tell three states apart — "Redis is fine", "Redis is broken", and "we were
        never asked to use Redis". Without this property it could only distinguish
        the first two, and a deliberately cache-free deployment would be reported
        as a failure. A health report that cries wolf gets ignored, which makes it
        worse than no health report at all.
        """
        return self._enabled

    async def ping(self) -> bool:
        """
        Return True if Redis answers a PING, False otherwise.

        WHY A DEDICATED PING RATHER THAN REUSING get():
          get() deliberately swallows every error and returns None, because for a
          cache a failure and a miss are the same thing to the caller. That's
          exactly the behaviour we want in the request path and exactly the wrong
          behaviour for a health check, which needs to distinguish them. So the
          health check gets its own method whose contract is "did the connection
          work", not "was there a value".

        WHY PING AND NOT A REAL COMMAND:
          PING is the protocol-level liveness command: it proves a TCP connection
          was established AND that the Redis server completed a request/response
          round trip. A bare TCP connect would succeed against a Redis that is
          still loading its dataset from disk and rejecting commands with
          -LOADING. Always probe at the layer your clients actually use.

        NOTE this still catches broadly, like the rest of this class. The caller
        only needs a yes/no; the specific exception is logged, not returned.
        """
        if not self._enabled or self._redis is None:
            return False
        try:
            return bool(await self._redis.ping())
        except Exception as exc:
            logger.warning("Cache PING failed: %s", exc)
            return False

    async def get(self, key: str) -> str | None:
        """Return the cached string for key, or None on miss/disabled/error."""
        if not self._enabled or self._redis is None:
            return None
        try:
            return await self._redis.get(key)
        except Exception as exc:
            # Cache down? Treat as a miss and log at WARNING (not ERROR) — the
            # request will still succeed by falling back to the DB.
            logger.warning("Cache GET failed for %s: %s", key, exc)
            return None

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        """
        Store value under key with an expiry (ttl). `ex=ttl_seconds` sets the
        TTL atomically with the write, so a stale value can't outlive its
        window. No-op if caching is off/unavailable.
        """
        if not self._enabled or self._redis is None:
            return
        try:
            await self._redis.set(key, value, ex=ttl_seconds)
        except Exception as exc:
            logger.warning("Cache SET failed for %s: %s", key, exc)

    async def delete(self, key: str) -> None:
        """
        Remove a key. Called on WRITES to a cached resource so the next read
        doesn't serve stale data — this is CACHE INVALIDATION, the genuinely
        hard part of caching ("there are only two hard problems in CS...").
        """
        if not self._enabled or self._redis is None:
            return
        try:
            await self._redis.delete(key)
        except Exception as exc:
            logger.warning("Cache DELETE failed for %s: %s", key, exc)

    async def close(self) -> None:
        """Close the connection pool cleanly on shutdown (called by lifespan)."""
        if self._redis is not None:
            await self._redis.aclose()
