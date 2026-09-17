"""Refresh-token lifecycle — issue, rotate, revoke (Privacy Phase 0).

Standard refresh-token-rotation with reuse detection: each refresh token is
single-use. Presenting it via ``/auth/refresh`` revokes it and mints a
replacement; presenting an already-revoked one again — the signature is
still valid, only the DB row says otherwise — means the token was copied
(stolen) and used a second time, so every refresh token for that user is
revoked and the account has to log in again everywhere.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import AuthenticationError
from app.core.security import create_refresh_token, decode_token
from app.models.core import RefreshToken


async def issue_refresh_token(db: AsyncSession, user_id: uuid.UUID) -> str:
    """Mint a refresh token, record it, and commit."""
    token, jti = create_refresh_token(user_id)
    settings = get_settings()
    db.add(
        RefreshToken(
            jti=jti,
            user_id=user_id,
            expires_at=datetime.now(UTC) + timedelta(days=settings.refresh_token_expire_days),
        )
    )
    await db.commit()
    return token


async def rotate_refresh_token(db: AsyncSession, presented_token: str) -> tuple[str, uuid.UUID]:
    """Validate, revoke and replace a refresh token. Returns (new_token, user_id).

    Raises ``AuthenticationError`` if the token is expired, unknown, or already
    revoked — in the last case, every other live token for that user is
    revoked too before raising, since a revoked token being presented again is
    exactly the reuse signal described in the module docstring.
    """
    payload = decode_token(presented_token, "refresh")
    jti = uuid.UUID(payload["jti"])
    user_id = uuid.UUID(payload["sub"])

    row = await db.scalar(select(RefreshToken).where(RefreshToken.jti == jti))
    if row is None:
        raise AuthenticationError("Refresh token not recognised")

    if row.revoked_at is not None:
        await revoke_all_for_user(db, row.user_id, reason="reuse_detected")
        await db.commit()
        raise AuthenticationError(
            "Refresh token already used — all sessions revoked", code="ERR_TOKEN_REUSE"
        )

    new_token, new_jti = create_refresh_token(user_id)
    now = datetime.now(UTC)
    row.revoked_at = now
    row.revoked_reason = "rotated"
    row.replaced_by_jti = new_jti
    settings = get_settings()
    db.add(
        RefreshToken(
            jti=new_jti,
            user_id=user_id,
            expires_at=now + timedelta(days=settings.refresh_token_expire_days),
        )
    )
    await db.commit()
    return new_token, user_id


async def revoke_refresh_token(db: AsyncSession, presented_token: str) -> None:
    """Logout: revoke the single presented token. A token that fails to decode
    or isn't on file is not an error here — logout must succeed regardless."""
    try:
        payload = decode_token(presented_token, "refresh")
    except AuthenticationError:
        return
    jti = uuid.UUID(payload["jti"])
    row = await db.scalar(select(RefreshToken).where(RefreshToken.jti == jti))
    if row is not None and row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        row.revoked_reason = "logout"
        await db.commit()


async def revoke_all_for_user(db: AsyncSession, user_id: uuid.UUID, *, reason: str) -> None:
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC), revoked_reason=reason)
    )
