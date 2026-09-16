"""Auth endpoints: JWT login / refresh / logout / me / switch-company."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import PermissionDeniedError
from app.core.permissions import get_user_roles
from app.core.rate_limit import rate_limit_login, rate_limit_refresh
from app.core.security import CurrentUser, create_access_token, get_current_user, get_refresh_token_from_cookie
from app.core.database import get_db
from app.models.core import User
from app.schemas.auth import LoginRequest, SwitchCompanyRequest, TokenResponse
from app.schemas.common import MessageResponse
from app.schemas.core import UserResponse
from app.services import auth_sessions, user as user_service

router = APIRouter(prefix="/auth", tags=["auth"])


def _set_refresh_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=settings.refresh_cookie_name,
        value=token,
        httponly=True,
        secure=settings.refresh_cookie_secure,
        samesite="lax",
        max_age=settings.refresh_token_expire_days * 24 * 3600,
        path="/api/v1/auth",
    )


async def _token_response(db: AsyncSession, user: User, company_id) -> TokenResponse:
    roles = await get_user_roles(db, user.id, company_id)
    settings = get_settings()
    return TokenResponse(
        access_token=create_access_token(user.id, user.email, company_id, roles),
        expires_in=settings.access_token_expire_minutes * 60,
        user_id=user.id,
        email=user.email,
        full_name=user.full_name,
        company_id=company_id,
        roles=roles,
    )


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Log in with email and password",
    description="Returns a short-lived access token in the body and sets the "
    "long-lived refresh token as an httpOnly cookie.",
    dependencies=[Depends(rate_limit_login)],
)
async def login(
    payload: LoginRequest, response: Response, db: Annotated[AsyncSession, Depends(get_db)]
) -> TokenResponse:
    user = await user_service.authenticate(db, payload.email, payload.password)
    company_id = payload.company_id or user.default_company_id
    token = await auth_sessions.issue_refresh_token(db, user.id)
    _set_refresh_cookie(response, token)
    return await _token_response(db, user, company_id)


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Refresh the access token",
    description="Reads the refresh token from the httpOnly cookie, rotates it, "
    "and returns a fresh access token. Reusing an already-rotated refresh "
    "token revokes every session for the account.",
    dependencies=[Depends(rate_limit_refresh)],
)
async def refresh(
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    refresh_token: Annotated[str, Depends(get_refresh_token_from_cookie)],
) -> TokenResponse:
    new_token, user_id = await auth_sessions.rotate_refresh_token(db, refresh_token)
    user = await user_service.get_user(db, user_id)
    _set_refresh_cookie(response, new_token)
    return await _token_response(db, user, user.default_company_id)


@router.post(
    "/logout",
    response_model=MessageResponse,
    summary="Log out",
    description="Revokes the refresh token and clears its cookie. Access tokens "
    "simply expire (15 min).",
)
async def logout(
    request: Request, response: Response, db: Annotated[AsyncSession, Depends(get_db)]
) -> MessageResponse:
    settings = get_settings()
    token = request.cookies.get(settings.refresh_cookie_name)
    if token:
        await auth_sessions.revoke_refresh_token(db, token)
    response.delete_cookie(settings.refresh_cookie_name, path="/api/v1/auth")
    return MessageResponse(message="Logged out")


@router.get(
    "/me",
    response_model=UserResponse,
    summary="Current user profile",
    description="Returns the authenticated user's profile and role assignments.",
)
async def me(
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserResponse:
    user = await user_service.get_user(db, current_user.id)
    resp = UserResponse.model_validate(user)
    resp.role_names = sorted({r.role for r in user.roles})
    return resp


@router.post(
    "/switch-company",
    response_model=TokenResponse,
    summary="Switch the active company",
    description="Issues a new access token scoped to another company the user can access.",
)
async def switch_company(
    payload: SwitchCompanyRequest,
    request: Request,
    response: Response,
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TokenResponse:
    user = await user_service.get_user(db, current_user.id)
    roles = await get_user_roles(db, user.id, payload.company_id)
    if not roles:
        raise PermissionDeniedError("You do not have access to this company")
    # Rotate rather than mint alongside: the old refresh token's session is
    # over from the caller's perspective, and leaving it live would mean two
    # concurrently valid refresh tokens for what feels like one session.
    old_token = request.cookies.get(get_settings().refresh_cookie_name)
    if old_token:
        await auth_sessions.revoke_refresh_token(db, old_token)
    new_token = await auth_sessions.issue_refresh_token(db, user.id)
    _set_refresh_cookie(response, new_token)
    return await _token_response(db, user, payload.company_id)
