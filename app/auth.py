"""Auth middleware: header mode (trusted headers) or OIDC mode (full flow, session cookies)."""

import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any

from authlib.integrations.starlette_client import OAuth
from fastapi import Request
from fastapi.responses import RedirectResponse, Response

from app.config import config
from app.models import UserRole

logger = logging.getLogger("proxmox-swap")

# Simple in-memory session store (single replica, no need for Redis)
_sessions: dict[str, dict[str, Any]] = {}
SESSION_TTL = 86400  # 24 hours


def _init_oauth() -> OAuth:
    oauth = OAuth()
    client_kwargs = {"scope": config.OIDC_SCOPES}
    if config.OIDC_PKCE:
        client_kwargs["code_challenge_method"] = "S256"
    oauth.register(
        name="oidc",
        client_id=config.OIDC_CLIENT_ID,
        client_secret=config.OIDC_CLIENT_SECRET,
        server_metadata_url=config.OIDC_ISSUER,
        client_kwargs=client_kwargs,
    )
    return oauth


oauth = _init_oauth() if config.AUTH_MODE == "oidc" else None


@dataclass
class AuthResult:
    name: str = ""
    groups: list[str] = None
    role: UserRole = UserRole.GUEST
    authenticated: bool = False

    def __post_init__(self):
        if self.groups is None:
            self.groups = []


def _resolve_role_oidc(groups: list[str]) -> UserRole:
    """OIDC: determine role by group membership."""
    for user_group in groups:
        if user_group in config.OWNER_GROUPS:
            return UserRole.OWNER
    return UserRole.GUEST


def _resolve_role_header(name: str) -> UserRole:
    """Header: determine role by name match."""
    name_lower = name.lower().strip()
    for owner_name in config.OWNER_NAMES:
        if name_lower == owner_name.lower().strip():
            return UserRole.OWNER
    return UserRole.GUEST


async def authenticate(request: Request) -> AuthResult:
    """Authenticate the request based on AUTH_MODE.

    Returns AuthResult with name, groups, role, and authenticated flag.
    """
    if config.AUTH_MODE == "none":
        return AuthResult(name="dev@localhost", role=UserRole.OWNER, authenticated=True)

    if config.AUTH_MODE == "header":
        return _auth_from_header(request)

    if config.AUTH_MODE == "oidc":
        return await _auth_from_oidc(request)

    return AuthResult()


def _auth_from_header(request: Request) -> AuthResult:
    """Auth via trusted header from reverse proxy."""
    name = request.headers.get(config.AUTH_HEADER, "").strip()
    if not name:
        return AuthResult()
    role = _resolve_role_header(name)
    return AuthResult(name=name, role=role, authenticated=True)


async def _auth_from_oidc(request: Request) -> AuthResult:
    """Auth via OIDC session cookie."""
    session_id = request.cookies.get("proxmox_swap_session")
    if not session_id:
        return AuthResult()

    session_data = _sessions.get(session_id)
    if not session_data:
        return AuthResult()

    # Check TTL
    if time.time() - session_data.get("created_at", 0) > SESSION_TTL:
        _sessions.pop(session_id, None)
        return AuthResult()

    name = session_data.get("name", "")
    if not name:
        return AuthResult()

    groups = session_data.get("groups", [])
    role = _resolve_role_oidc(groups)
    return AuthResult(name=name, groups=groups, role=role, authenticated=True)


async def oidc_login(request: Request):
    """Redirect to OIDC provider."""
    if oauth is None:
        return Response(
            content="Authentication is handled by your reverse proxy. It seems misconfigured - contact your administrator.",
            status_code=503,
            media_type="text/html",
        )
    if config.OIDC_BASE_URI:
        redirect_uri = config.OIDC_BASE_URI.rstrip("/") + "/auth/callback"
    else:
        redirect_uri = str(request.url_for("oidc_callback"))
    return await oauth.oidc.authorize_redirect(request, redirect_uri)


async def oidc_callback(request: Request):
    """Handle OIDC callback, set session cookie."""
    if oauth is None:
        return Response(
            content="Authentication is handled by your reverse proxy. It seems misconfigured - contact your administrator.",
            status_code=503,
            media_type="text/html",
        )
    token = await oauth.oidc.authorize_access_token(request)
    userinfo = token.get("userinfo", {})

    # Extract name from configurable claim, fall back to sub
    name = userinfo.get(config.OIDC_USERNAME_CLAIM, "") or userinfo.get("sub", "")
    if not name:
        logger.warning("OIDC callback: no user name found")
        response = RedirectResponse(url="/?error=no_name")
        return response

    # Extract groups from configurable claim
    groups = userinfo.get(config.OIDC_GROUPS_CLAIM, [])
    if groups is None:
        groups = []

    # Create session
    session_id = secrets.token_hex(64)
    _sessions[session_id] = {
        "name": name,
        "groups": groups,
        "userinfo": userinfo,
        "created_at": time.time(),
    }

    response = RedirectResponse(url="/")
    response.set_cookie(
        key="proxmox_swap_session",
        value=session_id,
        httponly=True,
        samesite="lax",
        max_age=SESSION_TTL,
    )
    return response


async def oidc_logout(request: Request) -> RedirectResponse:
    """Clear session cookie."""
    session_id = request.cookies.get("proxmox_swap_session")
    if session_id:
        _sessions.pop(session_id, None)
    response = RedirectResponse(url="/")
    response.delete_cookie("proxmox_swap_session")
    return response
