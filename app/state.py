"""State machine, safe switching protocol, JSON persistence, audit log."""

import asyncio
import json
import logging
import os
import time
import uuid

from app.config import config
from app.models import AuditEntry, Session, SessionState, StateFile
from app.pve import get_container_status, poll_status, start_container, stop_container

logger = logging.getLogger("proxmox-swap")

_switch_lock = asyncio.Lock()


def _ensure_data_dir() -> None:
    d = os.path.dirname(config.STATE_FILE_PATH)
    if d:
        os.makedirs(d, exist_ok=True)


async def load_state() -> StateFile:
    """Load persisted state from disk."""
    if os.path.exists(config.STATE_FILE_PATH):
        try:
            with open(config.STATE_FILE_PATH) as f:
                data = json.load(f)
            return StateFile.from_dict(data)
        except Exception:
            logger.warning("Failed to load state file, starting fresh")
    return StateFile()


def save_state(state: StateFile) -> None:
    """Atomically write state to disk (write to .tmp then rename)."""
    _ensure_data_dir()
    tmp_path = config.STATE_FILE_PATH + ".tmp"
    data = state.to_dict()
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
    os.rename(tmp_path, config.STATE_FILE_PATH)


def _make_audit(action: str, session_id: str = "", details: str = "", actor: str = "") -> AuditEntry:
    return AuditEntry(action=action, session_id=session_id, details=details, actor=actor)


async def start_session(state: StateFile, user_name: str = "") -> tuple[bool, str]:
    """Start a guest session by stopping host and starting guest.

    Returns (success, error_message). On success, state.session is updated.
    """
    async with _switch_lock:
        host_status = await get_container_status(config.HOST_CT_ID)
        guest_status = await get_container_status(config.GUEST_CT_ID)
        logger.info("Pre-switch check: host=%s, guest=%s", host_status, guest_status)

        if host_status != "running" or guest_status != "stopped":
            msg = f"Precondition failed: host={host_status}, guest={guest_status}"
            return False, msg

        # Step 2: Stop host
        ok = await stop_container(config.HOST_CT_ID)
        if not ok:
            # Undo: restart host
            await start_container(config.HOST_CT_ID)
            return False, "Failed to stop host container"

        # Step 3: Verify host stopped
        if not await poll_status(config.HOST_CT_ID, "stopped", timeout=float(config.CONTAINER_STOP_TIMEOUT_SECONDS)):
            # Undo: restart host
            await start_container(config.HOST_CT_ID)
            return False, "Host did not stop in time"

        # Step 4: Cooldown
        await asyncio.sleep(float(config.SWITCH_COOLDOWN_SECONDS))

        # Step 5: Start guest
        ok = await start_container(config.GUEST_CT_ID)
        if not ok:
            # Undo: restart host
            await start_container(config.HOST_CT_ID)
            return False, "Failed to start guest container"

        # Step 6: Verify guest running
        if not await poll_status(config.GUEST_CT_ID, "running", timeout=float(config.CONTAINER_START_TIMEOUT_SECONDS)):
            # Undo: stop guest, restart host
            await stop_container(config.GUEST_CT_ID)
            await start_container(config.HOST_CT_ID)
            return False, "Guest did not start in time"

        # Step 7: Final cross-check
        host_final = await get_container_status(config.HOST_CT_ID)
        guest_final = await get_container_status(config.GUEST_CT_ID)
        if host_final != "stopped" or guest_final != "running":
            if guest_final != "running":
                await start_container(config.HOST_CT_ID)
            if host_final != "stopped":
                await stop_container(config.HOST_CT_ID)
            return False, f"Final cross-check failed: host={host_final}, guest={guest_final}"

        # Create session
        now = time.time()
        session = Session(
            session_id=uuid.uuid4().hex[:12],
            state=SessionState.ACTIVE,
            deadline=now + config.DEFAULT_TIMEOUT_SECONDS,
            warning_at=now + config.DEFAULT_TIMEOUT_SECONDS - config.WARNING_SECONDS,
            grace_at=now + config.DEFAULT_TIMEOUT_SECONDS - config.FORCE_GRACE_SECONDS,
        )
        state.session = session
        state.add_audit(_make_audit("session_start", session.session_id, "Host stopped, guest started", user_name))
        save_state(state)
        logger.info("Session started: %s, deadline=%.0fs", session.session_id, config.DEFAULT_TIMEOUT_SECONDS)
        return True, ""


async def stop_session(state: StateFile, user_name: str = "") -> tuple[bool, str]:
    """End guest session by stopping guest and starting host.

    Returns (success, error_message).
    """
    async with _switch_lock:
        host_status = await get_container_status(config.HOST_CT_ID)
        guest_status = await get_container_status(config.GUEST_CT_ID)
        logger.info("Pre-switch check (stop): host=%s, guest=%s", host_status, guest_status)

        if state.session is None:
            return False, "No active session"

        session_id = state.session.session_id

        # Step 1: Stop guest
        ok = await stop_container(config.GUEST_CT_ID)
        if not ok:
            return False, "Failed to stop guest container"

        # Step 2: Verify guest stopped
        if not await poll_status(config.GUEST_CT_ID, "stopped"):
            # Guest still running — keep it running with courtesy session
            return False, "Guest did not stop in time"

        # Step 3: Cooldown
        await asyncio.sleep(float(config.SWITCH_COOLDOWN_SECONDS))

        # Step 4: Start host
        ok = await start_container(config.HOST_CT_ID)
        if not ok:
            # Undo: start guest with courtesy session
            await start_container(config.GUEST_CT_ID)
            return False, "Failed to restart host"

        # Step 5: Verify host running
        if not await poll_status(config.HOST_CT_ID, "running", timeout=float(config.CONTAINER_START_TIMEOUT_SECONDS)):
            # Host not running — start guest with courtesy
            await start_container(config.GUEST_CT_ID)
            return False, "Host did not start in time"

        # Step 6: Final cross-check
        host_final = await get_container_status(config.HOST_CT_ID)
        guest_final = await get_container_status(config.GUEST_CT_ID)
        if host_final != "running" or guest_final != "stopped":
            if host_final != "running":
                await start_container(config.GUEST_CT_ID)
            return False, f"Final cross-check failed: host={host_final}, guest={guest_final}"

        state.session = None
        state.add_audit(_make_audit("session_stop", session_id, "Guest stopped, host started", user_name))
        save_state(state)
        logger.info("Session stopped: %s", session_id)
        return True, ""


async def force_stop_session(state: StateFile, user_name: str = "") -> tuple[bool, str]:
    """Owner override: immediately stop guest and start host, no grace period.

    Uses force shutdown (not graceful). Works even without an active session.
    Returns (success, error_message).
    """
    async with _switch_lock:
        host_status = await get_container_status(config.HOST_CT_ID)
        guest_status = await get_container_status(config.GUEST_CT_ID)
        logger.info("Force stop (owner): host=%s, guest=%s", host_status, guest_status)

        session_id = ""
        if state.session:
            session_id = state.session.session_id

        # Step 1: Force stop guest
        ok = await stop_container(config.GUEST_CT_ID, shutdown=False)
        if not ok:
            return False, "Failed to force stop guest container"

        # Step 2: Verify guest stopped
        if not await poll_status(config.GUEST_CT_ID, "stopped", timeout=float(config.CONTAINER_STOP_TIMEOUT_SECONDS)):
            return False, "Guest did not stop in time"

        # Step 3: Cooldown
        await asyncio.sleep(float(config.SWITCH_COOLDOWN_SECONDS))

        # Step 4: Start host
        ok = await start_container(config.HOST_CT_ID)
        if not ok:
            await start_container(config.GUEST_CT_ID)
            return False, "Failed to restart host"

        # Step 5: Verify host running
        if not await poll_status(config.HOST_CT_ID, "running", timeout=float(config.CONTAINER_START_TIMEOUT_SECONDS)):
            await start_container(config.GUEST_CT_ID)
            return False, "Host did not start in time"

        # Step 6: Final cross-check
        host_final = await get_container_status(config.HOST_CT_ID)
        guest_final = await get_container_status(config.GUEST_CT_ID)
        if host_final != "running" or guest_final != "stopped":
            if host_final != "running":
                await start_container(config.GUEST_CT_ID)
            return False, f"Final cross-check failed: host={host_final}, guest={guest_final}"

        state.session = None
        state.add_audit(_make_audit("force_stop", session_id, "Owner forced swap to host", user_name))
        save_state(state)
        logger.info("Force stop completed: %s", session_id or "no-session")
        return True, ""


async def kick_watchdog(state: StateFile, seconds: int, is_owner: bool = False, user_name: str = "") -> tuple[bool, str]:
    """Extend the session deadline. The requested seconds are added to the current
    deadline, but the deadline can never exceed now + MAX_SECONDS (rolling window).
    Owners bypass the rolling cap entirely.
    Returns (success, error_message)."""
    if state.session is None or state.session.state == SessionState.IDLE:
        return False, "No active session"
    if seconds <= 0:
        return False, "Kick seconds must be positive"

    now = time.time()
    if is_owner:
        new_deadline = state.session.deadline + seconds
    else:
        rolling_max = now + config.MAX_SECONDS
        new_deadline = min(state.session.deadline + seconds, rolling_max)
        if new_deadline <= state.session.deadline:
            return False, f"Session already at rolling maximum of {config.MAX_SECONDS}s from now"

    state.session.deadline = new_deadline
    state.session.warning_at = state.session.deadline - config.WARNING_SECONDS
    state.session.grace_at = state.session.deadline - config.FORCE_GRACE_SECONDS
    state.session.last_kick_at = now
    state.session.kick_count += 1
    state.add_audit(
        _make_audit("session_kick", state.session.session_id, f"Extended by {seconds}s (kick #{state.session.kick_count})", user_name)
    )
    save_state(state)
    logger.info("Session %s kicked by %ds, new deadline=%.0f", state.session.session_id, seconds, state.session.deadline)
    return True, ""


async def enforce_timer(state: StateFile, user_name: str = "Watchdog") -> tuple[bool, str]:
    """Check timer and transition state (WARNING/GRACE). Call from watchdog.

    When the deadline passes (GRACE → expiry), immediately stops the guest and
    starts the host via stop_session(). Returns (success, error_message).
    If the swap fails, the session stays in GRACE and the watchdog will retry.
    """
    if state.session is None or state.session.state == SessionState.IDLE:
        return True, ""

    now = time.time()
    session = state.session

    if session.state == SessionState.ACTIVE and now >= session.warning_at:
        session.state = SessionState.WARNING
        state.add_audit(_make_audit("state_warning", session.session_id, "Warning triggered", user_name))
        save_state(state)
        logger.info("Session %s entered WARNING", session.session_id)
        return True, ""

    elif session.state == SessionState.WARNING and now >= session.grace_at:
        session.state = SessionState.GRACE
        state.add_audit(_make_audit("state_grace", session.session_id, "Grace period started", user_name))
        save_state(state)
        logger.info("Session %s entered GRACE", session.session_id)
        return True, ""

    elif session.state == SessionState.GRACE and now >= session.deadline:
        # Timer expired — immediately swap containers
        logger.info("Session %s expired, swapping containers", session.session_id)
        return await stop_session(state, user_name=user_name)

    return True, ""
