"""API endpoints — JSON-only responses."""

import json
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.routing import APIRoute
from fastapi.exceptions import HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response

from app.auth import AuthResult, authenticate, oidc_callback, oidc_login, oidc_logout
from app.config import config
from app.models import AuditEntry, UserRole, error_dict, session_status_dict
from app.pve import get_container_status, start_container, stop_container
from app.state import save_state

logger = logging.getLogger("proxmox-swap")


async def require_auth(request: Request) -> AuthResult:
    """FastAPI dependency: authenticates and returns AuthResult, or redirects/raises."""
    auth = await authenticate(request)
    if not auth.authenticated:
        if config.AUTH_MODE == "oidc":
            raise HTTPException(status_code=303, headers={"Location": "/auth/login"})
        raise HTTPException(status_code=401, detail="Not authenticated")
    return auth


async def require_owner(auth: AuthResult = Depends(require_auth)) -> AuthResult:
    """FastAPI dependency: requires owner role."""
    if auth.role != UserRole.OWNER:
        raise HTTPException(status_code=403, detail="Owner access required")
    return auth


def _user_dict(auth: AuthResult) -> dict:
    return {"name": auth.name, "role": auth.role.value}


def create_router(watchdog):
    """Create the API router with watchdog injected."""
    router = APIRouter(dependencies=[Depends(require_auth)])

    @router.get("/")
    async def index(request: Request, auth: AuthResult = Depends(require_auth)):
        return FileResponse("static/index.html")

    @router.get("/session")
    async def get_session(request: Request, auth: AuthResult = Depends(require_auth)):
        """Session status + user info as JSON."""
        st = await watchdog.get_session_status()
        st["user"] = _user_dict(auth)
        st["max_seconds"] = config.MAX_SECONDS
        return JSONResponse(content=st)

    @router.post("/session")
    async def create_session(request: Request, auth: AuthResult = Depends(require_auth)):
        """Start a guest session."""
        success, msg = await watchdog.start_session(auth.name)
        if not success:
            return JSONResponse(status_code=400, content=error_dict(msg))
        return Response(content=json.dumps({"ok": True}), status_code=200)

    @router.delete("/session")
    async def delete_session(request: Request, auth: AuthResult = Depends(require_auth)):
        """End the current session."""
        success, msg = await watchdog.stop_session(auth.name)
        if not success:
            return JSONResponse(status_code=400, content=error_dict(msg))
        return Response(content=json.dumps({"ok": True}), status_code=200)

    @router.patch("/session")
    async def patch_session(request: Request, auth: AuthResult = Depends(require_auth)):
        """Kick the watchdog timer. Expects JSON body: {"seconds": int}."""
        body = json.loads(await request.body())
        seconds = body.get("seconds", 0)
        success, msg = await watchdog.kick_watchdog(seconds, auth.role == UserRole.OWNER, auth.name)
        if not success:
            return JSONResponse(status_code=400, content=error_dict(msg))
        return Response(content=json.dumps({"ok": True}), status_code=200)

    @router.post("/session/force-stop")
    async def force_stop_session(request: Request, auth: AuthResult = Depends(require_owner)):
        """Owner override, immediate swap."""
        success, msg = await watchdog.force_stop_session(auth.name)
        if not success:
            return JSONResponse(status_code=400, content=error_dict(msg))
        return Response(content=json.dumps({"ok": True}), status_code=200)

    @router.get("/history")
    async def get_history(request: Request):
        """Audit log as JSON array."""
        state = watchdog.state
        entries = []
        if state:
            entries = [
                {"timestamp": e.timestamp, "action": e.action,
                 "session_id": e.session_id, "details": e.details,
                 "actor": e.actor}
                for e in reversed(state.audit_log)
            ]
        return JSONResponse(content=entries)

    @router.get("/containers/status")
    async def get_containers_status(request: Request, auth: AuthResult = Depends(require_auth)):
        """Container status as JSON."""
        status_data = await watchdog.get_session_status()
        host_status = await get_container_status(config.HOST_CT_ID)
        guest_status = await get_container_status(config.GUEST_CT_ID)
        session_active = status_data["state"] in ("ACTIVE", "WARNING", "GRACE")
        return JSONResponse(content={
            "host": {
                "ct_id": config.HOST_CT_ID,
                "status": host_status,
                "active": not session_active,
            },
            "guest": {
                "ct_id": config.GUEST_CT_ID,
                "status": guest_status,
                "active": session_active,
            },
        })

    @router.post("/containers/{ct_id}/start")
    async def container_start(request: Request, ct_id: int, auth: AuthResult = Depends(require_owner)):
        """Owner-only: start a container directly."""
        await start_container(ct_id)
        if watchdog.state:
            watchdog.state.add_audit(AuditEntry(
                action="container_start",
                details=f"Started CT {ct_id}",
                actor=auth.name,
            ))
            save_state(watchdog.state)
        return Response(content=json.dumps({"ok": True}), status_code=200)

    @router.post("/containers/{ct_id}/stop")
    async def container_stop(request: Request, ct_id: int, auth: AuthResult = Depends(require_owner)):
        """Owner-only: graceful shutdown of a container."""
        await stop_container(ct_id, shutdown=True)
        if watchdog.state:
            watchdog.state.add_audit(AuditEntry(
                action="container_stop",
                details=f"Stopped CT {ct_id}",
                actor=auth.name,
            ))
            save_state(watchdog.state)
        return Response(content=json.dumps({"ok": True}), status_code=200)

    @router.post("/containers/{ct_id}/force-stop")
    async def container_force_stop(request: Request, ct_id: int, auth: AuthResult = Depends(require_owner)):
        """Owner-only: force stop a container."""
        await stop_container(ct_id, shutdown=False)
        if watchdog.state:
            watchdog.state.add_audit(AuditEntry(
                action="container_force_stop",
                details=f"Force stopped CT {ct_id}",
                actor=auth.name,
            ))
            save_state(watchdog.state)
        return Response(content=json.dumps({"ok": True}), status_code=200)

    return router


def create_public_router():
    """Router for endpoints that don't require auth."""
    return APIRouter(routes=[
        APIRoute("/health", endpoint=health_check, methods=["GET"]),
        APIRoute("/auth/login", endpoint=oidc_login, methods=["GET"]),
        APIRoute("/auth/callback", endpoint=oidc_callback, methods=["GET"]),
        APIRoute("/auth/logout", endpoint=oidc_logout, methods=["POST"]),
    ])


async def health_check():
    return {"status": "ok"}
