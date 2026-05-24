"""Data models for sessions, audit log, and API requests/responses."""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


class SessionState(str, enum.Enum):
    IDLE = "IDLE"
    ACTIVE = "ACTIVE"
    WARNING = "WARNING"
    GRACE = "GRACE"


class UserRole(str, enum.Enum):
    GUEST = "guest"
    OWNER = "owner"


@dataclass
class Session:
    """Active guest session."""

    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    state: SessionState = SessionState.IDLE
    deadline: float = 0.0  # unix timestamp
    warning_at: float = 0.0  # unix timestamp when WARNING was triggered
    grace_at: float = 0.0  # unix timestamp when GRACE was triggered
    started_at: float = field(default_factory=lambda: datetime.now(timezone.utc).timestamp())
    last_kick_at: float = 0.0
    kick_count: int = 0

    @property
    def remaining_seconds(self) -> float:
        return max(0, self.deadline - datetime.now(timezone.utc).timestamp())

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "state": self.state.value,
            "deadline": self.deadline,
            "warning_at": self.warning_at,
            "grace_at": self.grace_at,
            "started_at": self.started_at,
            "last_kick_at": self.last_kick_at,
            "kick_count": self.kick_count,
            "remaining_seconds": self.remaining_seconds,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Session:
        return cls(
            session_id=data["session_id"],
            state=SessionState(data["state"]),
            deadline=data["deadline"],
            warning_at=data.get("warning_at", 0),
            grace_at=data.get("grace_at", 0),
            started_at=data["started_at"],
            last_kick_at=data.get("last_kick_at", 0),
            kick_count=data.get("kick_count", 0),
        )


@dataclass
class AuditEntry:
    timestamp: float = field(default_factory=lambda: datetime.now(timezone.utc).timestamp())
    action: str = ""
    session_id: str = ""
    details: str = ""
    actor: str = ""


@dataclass
class StateFile:
    """Persisted state."""

    session: Session | None = None
    audit_log: list[AuditEntry] = field(default_factory=list)

    def add_audit(self, entry: AuditEntry) -> None:
        now = datetime.now(timezone.utc).timestamp()
        year_seconds = 365 * 86400
        self.audit_log = [
            e for e in self.audit_log
            if now - e.timestamp < year_seconds
        ]
        self.audit_log.append(entry)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session": self.session.to_dict() if self.session else None,
            "audit_log": [
                {
                    "timestamp": e.timestamp,
                    "action": e.action,
                    "session_id": e.session_id,
                    "details": e.details,
                    "actor": e.actor,
                }
                for e in self.audit_log
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StateFile:
        session = None
        if data.get("session"):
            session = Session.from_dict(data["session"])
        audit_log = []
        for entry in data.get("audit_log", []):
            audit_log.append(
                AuditEntry(
                    timestamp=entry["timestamp"],
                    action=entry["action"],
                    session_id=entry.get("session_id", ""),
                    details=entry.get("details", ""),
                    actor=entry.get("actor", ""),
                )
            )
        return cls(session=session, audit_log=audit_log)


# --- Request/Response helpers (plain dicts, no Pydantic) ---

def session_status_dict(session: Session | None) -> dict[str, Any]:
    if session is None:
        return {"state": SessionState.IDLE.value, "session_id": None, "remaining_seconds": 0, "deadline": 0, "kick_count": 0, "grace_at": 0}
    return {
        "state": session.state.value,
        "session_id": session.session_id,
        "remaining_seconds": session.remaining_seconds,
        "deadline": session.deadline,
        "kick_count": session.kick_count,
        "grace_at": session.grace_at,
    }


def timer_dict(session: Session | None) -> dict[str, Any]:
    if session is None:
        return {"state": SessionState.IDLE.value, "remaining_seconds": 0, "deadline": 0}
    return {"state": session.state.value, "remaining_seconds": session.remaining_seconds, "deadline": session.deadline}


def error_dict(msg: str) -> dict[str, str]:
    return {"error": msg}
