"""Watchdog loop, timer enforcement, crash recovery."""

import asyncio
import logging
import time

from app.config import config
from app.models import AuditEntry, Session, SessionState, StateFile, session_status_dict
from app.pve import get_container_status, start_container, stop_container
from app.state import enforce_timer, force_stop_session, kick_watchdog, load_state, save_state, start_session, stop_session

logger = logging.getLogger("proxmox-swap")


class Watchdog:
    """Manages the watchdog patrol loop and crash recovery."""

    def __init__(self):
        self._task: asyncio.Task | None = None
        self._state: StateFile | None = None
        self._lock = asyncio.Lock()

    @property
    def state(self) -> StateFile | None:
        return self._state

    async def start(self) -> None:
        """Unleash the watchdog. Runs crash recovery first."""
        self._state = await load_state()
        await self._crash_recovery()
        self._task = asyncio.create_task(self._patrol_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _crash_recovery(self) -> None:
        """Recover state on startup. PVE API is source of truth."""
        logger.info("Running crash recovery...")
        host_status = await get_container_status(config.HOST_CT_ID)
        guest_status = await get_container_status(config.GUEST_CT_ID)
        logger.info("PVE ground truth: host=%s, guest=%s", host_status, guest_status)

        persisted = self._state
        has_session = persisted is not None and persisted.session is not None and persisted.session.state != SessionState.IDLE

        if host_status == "running" and guest_status == "stopped":
            # Normal state — clear any stale session
            if has_session:
                logger.info("Crash recovery: host running, clearing stale session")
                self._state = StateFile()
                self._state.add_audit(AuditEntry(
                    action="crash_recovery",
                    details="Host running, guest stopped — cleared stale session",
                    actor="Watchdog",
                ))
                save_state(self._state)
            return

        if host_status == "stopped" and guest_status == "running":
            if has_session:
                # Valid session — restore it, enforce timer will catch up
                logger.info("Crash recovery: guest running with valid session, restoring")
                await enforce_timer(self._state)
            else:
                # No session — expired or unauthorized. Stop guest, start host.
                logger.info("Crash recovery: guest running without session, stopping")
                await stop_container(config.GUEST_CT_ID)
                await start_container(config.HOST_CT_ID)
                self._state = StateFile()
                self._state.add_audit(AuditEntry(
                    action="crash_recovery",
                    details="Guest running without session — stopped guest, started host",
                    actor="Watchdog",
                ))
                save_state(self._state)
            return

        if host_status == "running" and guest_status == "running":
            if has_session:
                # Active session — stop host to respect it
                logger.warning("Crash recovery: both running with active session, stopping host")
                await stop_container(config.HOST_CT_ID)
                if self._state.session:
                    self._state.add_audit(AuditEntry(
                        action="watchdog_host_stop",
                        session_id=self._state.session.session_id,
                        details="Both containers running — watchdog stopped host to respect session",
                        actor="Watchdog",
                    ))
                    save_state(self._state)
            else:
                # No session — stop guest
                logger.warning("Crash recovery: both running without session, stopping guest")
                await stop_container(config.GUEST_CT_ID)
                self._state.add_audit(AuditEntry(
                    action="crash_recovery",
                    details="Both containers running without session — stopped guest",
                    actor="Watchdog",
                ))
                save_state(self._state)
            return

        # Both stopped
        if has_session and self._state.session:
            # Guest had a session — restore it
            # Preserve the original deadline if it still exceeds the courtesy minimum
            logger.info("Crash recovery: both stopped, guest had session, starting guest")
            now = time.time()
            courtesy_deadline = now + config.COURTESY_SESSION_SECONDS
            original_deadline = self._state.session.deadline
            restored_deadline = max(original_deadline, courtesy_deadline)
            session = Session(
                session_id=self._state.session.session_id,
                state=SessionState.ACTIVE,
                deadline=restored_deadline,
                warning_at=restored_deadline - config.WARNING_SECONDS,
                grace_at=restored_deadline - config.FORCE_GRACE_SECONDS,
                started_at=self._state.session.started_at,
            )
            self._state.session = session
            details = f"Both stopped, guest had session — restored deadline={restored_deadline:.0f}s (original={original_deadline:.0f}s, courtesy-min={courtesy_deadline:.0f}s)"
            self._state.add_audit(AuditEntry(
                action="crash_recovery",
                session_id=session.session_id,
                details=details,
                actor="Watchdog",
            ))
            save_state(self._state)
            await start_container(config.GUEST_CT_ID)
        elif has_session:
            # Host had a session (weird) — start host
            logger.info("Crash recovery: both stopped, host had session, starting host")
            await start_container(config.HOST_CT_ID)
            self._state = StateFile()
            self._state.add_audit(AuditEntry(
                action="crash_recovery",
                details="Both stopped, host had session — started host, cleared session",
                actor="Watchdog",
            ))
            save_state(self._state)
        else:
            # No session info — start host (default state)
            logger.info("Crash recovery: both stopped, no session, starting host")
            await start_container(config.HOST_CT_ID)
            self._state = StateFile()
            save_state(self._state)

    async def _patrol_loop(self) -> None:
        """Main watchdog patrol loop."""
        logger.info("Watchdog unleashed (patrol interval=%ds)", config.WATCHDOG_INTERVAL_SECONDS)
        while True:
            try:
                await self._patrol_cycle()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Patrol cycle error")
            await asyncio.sleep(config.WATCHDOG_INTERVAL_SECONDS)

    async def _patrol_cycle(self) -> None:
        """Single watchdog patrol cycle."""
        async with self._lock:
            if self._state is None:
                return

            host_status = await get_container_status(config.HOST_CT_ID)
            guest_status = await get_container_status(config.GUEST_CT_ID)

            # Critical: both running?
            if host_status == "running" and guest_status == "running":
                if self._state.session and self._state.session.state != SessionState.IDLE:
                    # Active session — stop host to respect it
                    logger.warning("Both running with active session, stopping host")
                    await stop_container(config.HOST_CT_ID)
                    self._state.add_audit(AuditEntry(
                        action="watchdog_host_stop",
                        session_id=self._state.session.session_id,
                        details="Both containers running — watchdog stopped host to respect session",
                        actor="Watchdog",
                    ))
                    save_state(self._state)
                else:
                    # No session — stop guest
                    logger.warning("Both containers running! Stopping guest immediately.")
                    await stop_container(config.GUEST_CT_ID)
                    self._state.add_audit(AuditEntry(
                        action="watchdog_guest_stop",
                        details="Both containers running without session — stopped guest",
                        actor="Watchdog",
                    ))
                    save_state(self._state)
                return

            # Both stopped?
            if host_status == "stopped" and guest_status == "stopped":
                logger.warning("Both containers stopped! Running recovery.")
                if self._state.session and self._state.session.state != SessionState.IDLE:
                    # Guest had a session — start guest
                    await start_container(config.GUEST_CT_ID)
                    logger.info("Recovery: started guest (had active session)")
                    self._state.add_audit(AuditEntry(
                        action="watchdog_recovery",
                        session_id=self._state.session.session_id,
                        details="Both containers stopped — restarted guest",
                        actor="Watchdog",
                    ))
                    save_state(self._state)
                else:
                    # Start host
                    await start_container(config.HOST_CT_ID)
                    logger.info("Recovery: started host (no active session)")
                    self._state.add_audit(AuditEntry(
                        action="watchdog_recovery",
                        details="Both containers stopped — restarted host",
                        actor="Watchdog",
                    ))
                    save_state(self._state)
                return

            # Enforce timer for active sessions (handles expiry swap immediately)
            if self._state.session and self._state.session.state != SessionState.IDLE:
                await enforce_timer(self._state)

    async def get_session_status(self) -> dict:
        """Get current session status for API. Thread-safe."""
        async with self._lock:
            if self._state is None or self._state.session is None or self._state.session.state == SessionState.IDLE:
                return session_status_dict(None)
            return session_status_dict(self._state.session)

    async def start_session(self, user_name: str = "") -> tuple[bool, str]:
        """Start a new guest session. Acquires patrol lock via state.py lock."""
        if self._state is None:
            self._state = StateFile()
        return await start_session(self._state, user_name=user_name)

    async def stop_session(self, user_name: str = "") -> tuple[bool, str]:
        """Stop the current guest session."""
        if self._state is None:
            return False, "No state initialized"
        return await stop_session(self._state, user_name=user_name)

    async def kick_watchdog(self, seconds: int, is_owner: bool = False, user_name: str = "") -> tuple[bool, str]:
        """Kick the watchdog timer."""
        async with self._lock:
            if self._state is None:
                return False, "No state initialized"
            return await kick_watchdog(self._state, seconds, is_owner, user_name=user_name)

    async def force_stop_session(self, user_name: str = "") -> tuple[bool, str]:
        """Owner override: force stop guest, start host immediately."""
        if self._state is None:
            return False, "No state initialized"
        return await force_stop_session(self._state, user_name=user_name)
