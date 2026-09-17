"""Integration: refresh-token rotation, reuse detection, and logout revocation
(Privacy Phase 0 — see docs/PRIVACY_IMPLEMENTATION_PLAN.md §3.2).

Exercises app/services/auth_sessions.py end to end against a real Postgres:
each refresh token is single-use, replaying an already-rotated one revokes
every session for the account, and logout revokes the token it was given.
"""

import pytest

pytestmark = pytest.mark.asyncio

API = "/api/v1"


def _cookie_name() -> str:
    from app.core.config import get_settings

    return get_settings().refresh_cookie_name


async def test_refresh_rotates_the_token(ctx):
    client, _company, _headers = ctx
    cookie_name = _cookie_name()
    old_value = client.cookies.get(cookie_name)
    assert old_value

    resp = await client.post(f"{API}/auth/refresh")
    assert resp.status_code == 200, resp.text
    new_value = client.cookies.get(cookie_name)
    assert new_value and new_value != old_value


async def test_replaying_a_rotated_refresh_token_revokes_the_session(ctx):
    client, _company, _headers = ctx
    cookie_name = _cookie_name()
    spent_token = client.cookies.get(cookie_name)

    first = await client.post(f"{API}/auth/refresh")
    assert first.status_code == 200, first.text
    live_token = client.cookies.get(cookie_name)
    assert live_token != spent_token

    # Replay the token that refresh() just rotated away.
    replay = await client.post(f"{API}/auth/refresh", cookies={cookie_name: spent_token})
    assert replay.status_code == 401
    assert replay.json()["code"] == "ERR_TOKEN_REUSE"

    # Reuse detection revoked every session for this user — the token minted
    # by the legitimate refresh above must be dead too, not just the replayed one.
    second = await client.post(f"{API}/auth/refresh", cookies={cookie_name: live_token})
    assert second.status_code == 401


async def test_logout_revokes_the_refresh_token(ctx):
    client, _company, _headers = ctx
    cookie_name = _cookie_name()
    live_token = client.cookies.get(cookie_name)
    assert live_token

    logout_resp = await client.post(f"{API}/auth/logout")
    assert logout_resp.status_code == 200, logout_resp.text
    # logout's Set-Cookie deletes it from the jar — replay the value captured before.
    assert client.cookies.get(cookie_name) is None

    replay = await client.post(f"{API}/auth/refresh", cookies={cookie_name: live_token})
    assert replay.status_code == 401
