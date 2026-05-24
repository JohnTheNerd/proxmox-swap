"""Configuration loader using plain os.environ with *_FILE secret support."""

import os
from types import SimpleNamespace
from typing import Any


def _get_env(name: str, default: str | None = None, file_fallback: bool = True) -> str:
    if file_fallback:
        file_env = f"{name}_FILE"
        file_path = os.environ.get(file_env)
        if file_path and os.path.isfile(file_path):
            with open(file_path) as f:
                return f.read().strip()
    val = os.environ.get(name, default)
    if val is None:
        raise ValueError(f"Required env var {name} not set (tried {name} and {name}_FILE)")
    return val


def _get_int(name: str, default: int) -> int:
    val = os.environ.get(name)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got: {val}")


def _get_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.lower() in ("true", "1", "yes", "on")


def load_config() -> SimpleNamespace:
    data: dict[str, Any] = {
        "PVE_HOSTS": [
            h.strip()
            for h in _get_env("PVE_HOST").split(",")
            if h.strip()
        ],
        "PVE_API_USER": _get_env("PVE_API_USER"),
        "PVE_API_TOKEN_ID": _get_env("PVE_API_TOKEN_ID"),
        "PVE_API_TOKEN_SECRET": _get_env("PVE_API_TOKEN_SECRET"),
        "HOST_CT_ID": _get_int("HOST_CT_ID", default=-1),
        "GUEST_CT_ID": _get_int("GUEST_CT_ID", default=-1),
        "OWNER_NAMES": [
            e.strip()
            for e in os.environ.get("OWNER_NAMES", "").split(",")
            if e.strip()
        ],
        "OWNER_GROUPS": [
            e.strip()
            for e in os.environ.get("OWNER_GROUPS", "").split(",")
            if e.strip()
        ],
        "AUTH_MODE": os.environ.get("AUTH_MODE", "header"),
        "AUTH_HEADER": os.environ.get("AUTH_HEADER", "X-Auth-Request-Name"),
        "OIDC_ISSUER": os.environ.get("OIDC_ISSUER", ""),
        "OIDC_CLIENT_ID": _get_env("OIDC_CLIENT_ID", default="", file_fallback=True),
        "OIDC_CLIENT_SECRET": _get_env("OIDC_CLIENT_SECRET", default="", file_fallback=True),
        "SESSION_SECRET": _get_env("SESSION_SECRET", default="", file_fallback=True),
        "OIDC_PKCE": _get_bool("OIDC_PKCE", True),
        "OIDC_BASE_URI": os.environ.get("OIDC_BASE_URI", ""),
        "OIDC_SCOPES": os.environ.get("OIDC_SCOPES", "openid profile groups"),
        "OIDC_USERNAME_CLAIM": os.environ.get("OIDC_USERNAME_CLAIM", "preferred_username"),
        "OIDC_GROUPS_CLAIM": os.environ.get("OIDC_GROUPS_CLAIM", "groups"),
        "DEFAULT_TIMEOUT_SECONDS": _get_int("DEFAULT_TIMEOUT_SECONDS", 7200),
        "MAX_SECONDS": _get_int("MAX_SECONDS", 28800),
        "WARNING_SECONDS": _get_int("WARNING_SECONDS", 1800),
        "FORCE_GRACE_SECONDS": _get_int("FORCE_GRACE_SECONDS", 600),
        # PVE API timeouts
        "PVE_API_TIMEOUT_SECONDS": _get_int("PVE_API_TIMEOUT_SECONDS", 30),
        "PVE_VERIFY_TLS": _get_bool("PVE_VERIFY_TLS", True),
        "PVE_RETRY_COUNT": _get_int("PVE_RETRY_COUNT", 3),
        "PVE_RETRY_DELAY_SECONDS": _get_int("PVE_RETRY_DELAY_SECONDS", 1),
        # Container switching timeouts
        "CONTAINER_START_TIMEOUT_SECONDS": _get_int("CONTAINER_START_TIMEOUT_SECONDS", 120),
        "CONTAINER_STOP_TIMEOUT_SECONDS": _get_int("CONTAINER_STOP_TIMEOUT_SECONDS", 120),
        "SWITCH_COOLDOWN_SECONDS": _get_int("SWITCH_COOLDOWN_SECONDS", 1),
        "POLL_INTERVAL_SECONDS": _get_int("POLL_INTERVAL_SECONDS", 2),
        # Crash recovery
        "COURTESY_SESSION_SECONDS": _get_int("COURTESY_SESSION_SECONDS", 1800),
        # State & watchdog
        "STATE_FILE_PATH": os.environ.get("STATE_FILE_PATH", "/data/state.json"),
        "WATCHDOG_INTERVAL_SECONDS": _get_int("WATCHDOG_INTERVAL_SECONDS", 30),
    }
    return SimpleNamespace(**data)


def validate(cfg: SimpleNamespace) -> list[str]:
    errors = []
    if not cfg.PVE_HOSTS:
        errors.append("PVE_HOSTS is required")
    if not cfg.PVE_API_USER:
        errors.append("PVE_API_USER is required")
    if not cfg.PVE_API_TOKEN_ID:
        errors.append("PVE_API_TOKEN_ID is required")
    if not cfg.PVE_API_TOKEN_SECRET:
        errors.append("PVE_API_TOKEN_SECRET is required")
    if cfg.HOST_CT_ID < 0:
        errors.append("HOST_CT_ID is required and must be positive")
    if cfg.GUEST_CT_ID < 0:
        errors.append("GUEST_CT_ID is required and must be positive")
    if cfg.HOST_CT_ID == cfg.GUEST_CT_ID:
        errors.append("HOST_CT_ID and GUEST_CT_ID must be different")
    if not cfg.OWNER_GROUPS and cfg.AUTH_MODE == "oidc":
        errors.append("OWNER_GROUPS is required when AUTH_MODE=oidc")
    if not cfg.OWNER_NAMES and cfg.AUTH_MODE == "header":
        errors.append("OWNER_NAMES is required when AUTH_MODE=header")
    if cfg.AUTH_MODE == "oidc":
        if not cfg.OIDC_ISSUER:
            errors.append("OIDC_ISSUER is required when AUTH_MODE=oidc")
        if not cfg.OIDC_CLIENT_ID:
            errors.append("OIDC_CLIENT_ID is required when AUTH_MODE=oidc")
        if not cfg.OIDC_CLIENT_SECRET:
            errors.append("OIDC_CLIENT_SECRET is required when AUTH_MODE=oidc")
        if not cfg.SESSION_SECRET:
            errors.append("SESSION_SECRET is required when AUTH_MODE=oidc")
    if cfg.WARNING_SECONDS <= cfg.FORCE_GRACE_SECONDS:
        errors.append("WARNING_SECONDS must be greater than FORCE_GRACE_SECONDS")
    return errors


config = load_config()
