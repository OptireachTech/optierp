"""Redis-backed fixed-window rate limiting for the credential-testing surface.

Scope (Privacy Phase 0): ``/auth/login`` and ``/auth/refresh`` — the two
endpoints an attacker can hit without already holding a valid session, so
they carry the abuse risk everything else behind ``get_current_user`` doesn't.
Keyed by client IP: stops one source from hammering the endpoint (credential
stuffing, refresh-token guessing). It does not stop a slow, distributed
attempt spread across many IPs at one account — that needs account-level
lockout/anomaly detection, a deliberately separate, larger piece of work.

Fails open: if Redis is unreachable, the request is allowed through and a
warning is logged, rather than locking out every user because caching
infrastructure hiccuped. Same tradeoff app/core/websocket.py already makes
for realtime.
"""

from functools import lru_cache

import redis.asyncio as aioredis
from fastapi import Request

from app.core.config import get_settings
from app.core.exceptions import RateLimitedError
from app.core.logging import get_logger

logger = get_logger(__name__)


@lru_cache
def _redis() -> aioredis.Redis:
    return aioredis.from_url(get_settings().redis_url, decode_responses=True)


async def _check(scope: str, identifier: str, limit: int, window_seconds: int) -> None:
    if not get_settings().auth_rate_limit_enabled:
        return
    key = f"rate_limit:{scope}:{identifier}"
    try:
        client = _redis()
        count = await client.incr(key)
        if count == 1:
            await client.expire(key, window_seconds)
    except Exception:  # noqa: BLE001 — Redis unavailable must not block auth
        logger.warning("rate_limit_backend_unavailable", scope=scope)
        return
    if count > limit:
        raise RateLimitedError(
            f"Too many attempts — try again in under {window_seconds} seconds",
        )


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def rate_limit_login(request: Request) -> None:
    settings = get_settings()
    await _check(
        "login", _client_ip(request), settings.auth_rate_limit_attempts, settings.auth_rate_limit_window_seconds
    )


async def rate_limit_refresh(request: Request) -> None:
    settings = get_settings()
    await _check(
        "refresh", _client_ip(request), settings.auth_rate_limit_attempts, settings.auth_rate_limit_window_seconds
    )
