"""JWT auth + password hashing.

Token model (Section 4.1):
  * access token  — 15 min, returned in the JSON body, sent as ``Authorization: Bearer``
  * refresh token — 7 days, stored in an httpOnly cookie

JWT payload: {"sub": user_uuid, "email": ..., "company_id": active company,
"roles": [...], "type": "access"|"refresh", "exp": ...}. Refresh tokens
additionally carry a "jti", checked against the ``refresh_tokens`` table
(``app/services/auth_sessions.py``) on every use — see Privacy Phase 0.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import bcrypt
import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db, set_company_context
from app.core.exceptions import AuthenticationError

_bearer = HTTPBearer(auto_error=False)


# --- Passwords ---------------------------------------------------------------


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


# --- Tokens -------------------------------------------------------------------


def _create_token(claims: dict[str, Any], expires_delta: timedelta, token_type: str) -> str:
    settings = get_settings()
    payload = {
        **claims,
        "type": token_type,
        "iat": datetime.now(UTC),
        "exp": datetime.now(UTC) + expires_delta,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def create_access_token(
    user_id: uuid.UUID, email: str, company_id: uuid.UUID | None, roles: list[str]
) -> str:
    settings = get_settings()
    return _create_token(
        {
            "sub": str(user_id),
            "email": email,
            "company_id": str(company_id) if company_id else None,
            "roles": roles,
        },
        timedelta(minutes=settings.access_token_expire_minutes),
        "access",
    )


def create_refresh_token(user_id: uuid.UUID, jti: uuid.UUID | None = None) -> tuple[str, uuid.UUID]:
    """Returns (token, jti) — the caller persists the jti in ``refresh_tokens`` so it
    can later be revoked (logout) or checked for reuse (rotation) without trusting
    the JWT payload alone."""
    settings = get_settings()
    jti = jti or uuid.uuid4()
    token = _create_token(
        {"sub": str(user_id), "jti": str(jti)},
        timedelta(days=settings.refresh_token_expire_days),
        "refresh",
    )
    return token, jti


def decode_token(token: str, expected_type: str) -> dict[str, Any]:
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("Token has expired", code="ERR_TOKEN_EXPIRED") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthenticationError("Invalid token") from exc
    if payload.get("type") != expected_type:
        raise AuthenticationError("Invalid token type")
    return payload


# --- Request identity ----------------------------------------------------------


class CurrentUser:
    """Lightweight identity resolved from the access token (no DB hit)."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.id: uuid.UUID = uuid.UUID(payload["sub"])
        self.email: str = payload.get("email", "")
        self.company_id: uuid.UUID | None = (
            uuid.UUID(payload["company_id"]) if payload.get("company_id") else None
        )
        self.roles: list[str] = payload.get("roles", [])


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> CurrentUser:
    if credentials is None:
        raise AuthenticationError("Not authenticated")
    return CurrentUser(decode_token(credentials.credentials, "access"))


async def get_tenant_db(
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AsyncSession:
    """Session with the RLS tenant context applied for the active company."""
    await set_company_context(db, current_user.company_id)
    return db


def get_refresh_token_from_cookie(request: Request) -> str:
    token = request.cookies.get(get_settings().refresh_cookie_name)
    if not token:
        raise AuthenticationError("Missing refresh token")
    return token
