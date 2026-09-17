"""Integration: /auth/login throttles repeated attempts from one source
(Privacy Phase 0 — see docs/PRIVACY_IMPLEMENTATION_PLAN.md §3.2).

Rate limiting is off by default in the test environment (see
``tests/conftest.py`` — every other integration test logs in through the
shared ``ctx`` fixture, and a real Redis in CI would otherwise throttle those
unrelated logins once enough of them land inside one window). This file turns
it on for itself only, via ``_rate_limiting_enabled``, and talks to the app
through its own client IP so it can't share a rate-limit bucket with anything
else in the suite.

Skips without a reachable Redis (in addition to the existing
TEST_DATABASE_URL skip from ``ctx``) — fails open with a warning otherwise,
so a login-throttling assertion would fail for the wrong reason.
"""

import os

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.integration.conftest import PW

pytestmark = pytest.mark.asyncio

API = "/api/v1"


async def _redis_reachable() -> bool:
    try:
        import redis.asyncio as aioredis

        from app.core.config import get_settings

        client = aioredis.from_url(get_settings().redis_url, socket_connect_timeout=1)
        await client.ping()
        await client.aclose()
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest_asyncio.fixture()
async def _rate_limiting_enabled():
    from app.core.config import get_settings

    if not await _redis_reachable():
        pytest.skip("Redis not reachable — rate limiting fails open, nothing to assert")

    original = os.environ.get("AUTH_RATE_LIMIT_ENABLED")
    os.environ["AUTH_RATE_LIMIT_ENABLED"] = "true"
    get_settings.cache_clear()
    try:
        yield
    finally:
        if original is None:
            os.environ.pop("AUTH_RATE_LIMIT_ENABLED", None)
        else:
            os.environ["AUTH_RATE_LIMIT_ENABLED"] = original
        get_settings.cache_clear()


async def test_login_is_throttled_after_repeated_attempts(ctx, _rate_limiting_enabled):
    from app.core.config import get_settings
    from app.main import app

    _client, _company, _headers = ctx  # ensures schema + admin user exist
    settings = get_settings()

    # A client IP no other test in this suite uses (ASGITransport otherwise
    # defaults every test client to 127.0.0.1) — this bucket is ours alone.
    transport = ASGITransport(app=app, client=("203.0.113.77", 1))
    async with AsyncClient(transport=transport, base_url="http://test") as isolated:
        last_status = None
        for _ in range(settings.auth_rate_limit_attempts + 1):
            resp = await isolated.post(
                f"{API}/auth/login", json={"email": "admin@test.io", "password": PW}
            )
            last_status = resp.status_code

        assert last_status == 429, f"expected the final attempt to be throttled, got {last_status}"
        assert resp.json()["code"] == "ERR_RATE_LIMITED"
