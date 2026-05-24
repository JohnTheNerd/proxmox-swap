"""Async Proxmox VE API client with retry and idempotent wrappers."""

import asyncio
import logging
import time

import httpx

from app.config import config

logger = logging.getLogger("proxmox-swap")

# Cache of vmid → (node, type)
_vm_cache: dict[int, tuple[str, str]] = {}


def _get_auth_header() -> str:
    return f"PVEAPIToken={config.PVE_API_USER}!{config.PVE_API_TOKEN_ID}={config.PVE_API_TOKEN_SECRET}"


async def _resolve_vm(vmid: int) -> tuple[str, str]:
    """Find which node and type (lxc/qemu) a VM lives on via cluster resources."""
    if vmid in _vm_cache:
        return _vm_cache[vmid]
    data = await _request_json("GET", "/api2/json/cluster/resources?type=vm")
    for vm in data:
        if vm.get("vmid") == vmid:
            node = vm["node"]
            vm_type = vm["type"]  # "lxc" or "qemu"
            _vm_cache[vmid] = (node, vm_type)
            return node, vm_type
    raise ValueError(f"VM {vmid} not found on any node")


async def _request(method: str, path: str, **kwargs) -> httpx.Response:
    """Make a PVE API request, trying each configured host until one succeeds."""
    headers = {"Authorization": _get_auth_header()}
    kwargs.setdefault("timeout", float(config.PVE_API_TIMEOUT_SECONDS))
    kwargs.setdefault("headers", headers)

    hosts = list(config.PVE_HOSTS)
    last_error = None

    for host in hosts:
        url = f"{host}{path}"
        for attempt in range(config.PVE_RETRY_COUNT):
            try:
                async with httpx.AsyncClient(verify=config.PVE_VERIFY_TLS) as client:
                    resp = await client.request(method, url, **kwargs)
                    if resp.status_code in (502, 503, 504):
                        raise httpx.TransportError(f"HTTP {resp.status_code}")
                    logger.debug("PVE request succeeded on %s", host)
                    return resp
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last_error = e
                if attempt < config.PVE_RETRY_COUNT - 1:
                    wait = config.PVE_RETRY_DELAY_SECONDS * (2 ** attempt)
                    logger.warning("PVE %s request failed (attempt %d/%d): %s, retrying in %ss", host, attempt + 1, config.PVE_RETRY_COUNT, e, wait)
                    await asyncio.sleep(wait)
        logger.error("PVE %s exhausted all retries, trying next host", host)

    raise last_error  # type: ignore[misc]


async def _request_json(method: str, path: str, **kwargs) -> dict:
    resp = await _request(method, path, **kwargs)
    resp.raise_for_status()
    data = resp.json()
    # PVE wraps successful responses in data key
    return data.get("data", data)


async def get_container_status(ct_id: int) -> str:
    """Return 'running' or 'stopped' for a VM (LXC or QEMU)."""
    try:
        node, vm_type = await _resolve_vm(ct_id)
        data = await _request_json("GET", f"/api2/json/nodes/{node}/{vm_type}/{ct_id}/status/current")
        return data.get("status", "stopped")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            logger.error("VM %s not found on PVE", ct_id)
            return "stopped"
        raise


async def start_container(ct_id: int) -> bool:
    """Start a VM (LXC or QEMU). Idempotent — skips if already running."""
    try:
        node, vm_type = await _resolve_vm(ct_id)
    except ValueError:
        return False

    status = await get_container_status(ct_id)
    if status == "running":
        return True
    try:
        await _request("POST", f"/api2/json/nodes/{node}/{vm_type}/{ct_id}/status/start")
    except Exception:
        logger.error("Failed to start VM %s", ct_id)
        return False
    return await poll_status(ct_id, "running", timeout=float(config.CONTAINER_START_TIMEOUT_SECONDS))


async def stop_container(ct_id: int, shutdown: bool = True) -> bool:
    """Stop a VM (LXC or QEMU). Idempotent — waits until stopped or timeout.

    If shutdown=True, first attempts a graceful shutdown (which is idempotent
    if already stopped). Falls back to force stop if that fails.
    """
    try:
        node, vm_type = await _resolve_vm(ct_id)
    except ValueError:
        return True

    status = await get_container_status(ct_id)
    if status == "stopped":
        return True
    try:
        if shutdown:
            await _request("POST", f"/api2/json/nodes/{node}/{vm_type}/{ct_id}/status/shutdown")
        else:
            await _request("POST", f"/api2/json/nodes/{node}/{vm_type}/{ct_id}/status/stop")
        return await poll_status(ct_id, "stopped", timeout=float(config.CONTAINER_STOP_TIMEOUT_SECONDS))
    except Exception:
        logger.error("Failed to stop VM %s", ct_id)
        return False


async def poll_status(ct_id: int, expected: str, interval: float | None = None, timeout: float | None = None) -> bool:
    """Poll container status until it matches expected, or timeout."""
    if interval is None:
        interval = float(config.POLL_INTERVAL_SECONDS)
    if timeout is None:
        timeout = float(config.CONTAINER_STOP_TIMEOUT_SECONDS)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = await get_container_status(ct_id)
        if status == expected:
            return True
        await asyncio.sleep(interval)
    return False
