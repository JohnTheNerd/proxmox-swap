"""Test script to verify PVE API endpoint correctness."""

import asyncio
import json
import os
import sys
import time


def load_env(env_path: str) -> None:
    """Minimal .env parser — no dependencies."""
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip("\"'")
                os.environ.setdefault(key, value)

load_env(os.path.join(os.path.dirname(__file__), ".env"))

import httpx
from app.config import config


def _get_auth_header() -> str:
    return f"PVEAPIToken={config.PVE_API_USER}!{config.PVE_API_TOKEN_ID}={config.PVE_API_TOKEN_SECRET}"


async def test_endpoint(client: httpx.AsyncClient, label: str, method: str, host: str, path: str, **kwargs) -> dict:
    url = f"{host}{path}"
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"  {method} {url}")
    print(f"{'='*60}")

    headers = kwargs.pop("headers", {})
    headers.setdefault("Authorization", _get_auth_header())
    kwargs.setdefault("headers", headers)
    kwargs.setdefault("timeout", float(config.PVE_API_TIMEOUT_SECONDS))

    resp = await client.request(method, url, **kwargs)
    print(f"  Status: {resp.status_code}")

    try:
        data = resp.json()
        print(f"  Response (truncated): {json.dumps(data, indent=2)[:500]}")
    except Exception:
        print(f"  Response (text): {resp.text[:500]}")

    return {
        "label": label,
        "status_code": resp.status_code,
        "data": data if resp.headers.get("content-type", "").startswith("application/json") else resp.text,
    }


async def main():
    print(f"PVE Host: {config.PVE_HOSTS[0]}")
    print(f"API User: {config.PVE_API_USER}")
    print(f"Token ID: {config.PVE_API_TOKEN_ID}")
    print(f"Host CT: {config.HOST_CT_ID}, Guest CT: {config.GUEST_CT_ID}")
    print(f"TLS Verify: {config.PVE_VERIFY_TLS}")

    host = config.PVE_HOSTS[0]
    results = []
    errors = []

    async with httpx.AsyncClient(verify=config.PVE_VERIFY_TLS, timeout=float(config.PVE_API_TIMEOUT_SECONDS)) as client:

        # -------------------------------------------------------
        # 1. Verify the API token works (access ticket / version)
        # -------------------------------------------------------
        res = await test_endpoint(client, "PVE Version (auth check)", "GET", host, "/api2/json/version")
        results.append(res)
        if res["status_code"] != 200:
            errors.append(f"Version endpoint returned {res['status_code']} — token may be invalid or host unreachable")

        # -------------------------------------------------------
        # 2. List all LXC containers
        # -------------------------------------------------------
        res = await test_endpoint(client, "List LXC containers", "GET", host, "/api2/json/lxc")
        results.append(res)
        if res["status_code"] == 200:
            ct_ids = [d["vmid"] for d in (res["data"] or [])]
            print(f"  Found container IDs: {ct_ids}")
            if config.HOST_CT_ID not in ct_ids:
                errors.append(f"Host CT {config.HOST_CT_ID} NOT found in container list")
            if config.GUEST_CT_ID not in ct_ids:
                errors.append(f"Guest CT {config.GUEST_CT_ID} NOT found in container list")

        # -------------------------------------------------------
        # 3. Get status of both containers
        # -------------------------------------------------------
        for ct_name, ct_id in [("Host CT", config.HOST_CT_ID), ("Guest CT", config.GUEST_CT_ID)]:
            res = await test_endpoint(client, f"Status of {ct_name} ({ct_id})", "GET", host, f"/api2/json/lxc/{ct_id}/status")
            results.append(res)
            if res["status_code"] == 200:
                print(f"  Container {ct_id} status: {res['data'].get('status', 'unknown')}")
            elif res["status_code"] == 404:
                errors.append(f"CT {ct_id} returned 404 — container may not exist")

        # -------------------------------------------------------
        # 4. Get config of both containers (verifies GET /lxc/{id}/config endpoint shape)
        # -------------------------------------------------------
        for ct_name, ct_id in [("Host CT", config.HOST_CT_ID), ("Guest CT", config.GUEST_CT_ID)]:
            res = await test_endpoint(client, f"Config of {ct_name} ({ct_id})", "GET", host, f"/api2/json/lxc/{ct_id}/config")
            results.append(res)
            if res["status_code"] == 200:
                rootfs = res["data"].get("rootfs", "")
                print(f"  Rootfs: {rootfs[:100]}")

        # -------------------------------------------------------
        # 5. Test idempotent start on the already-running container
        # -------------------------------------------------------
        running_ct = None
        for ct_id in [config.HOST_CT_ID, config.GUEST_CT_ID]:
            res = await test_endpoint(client, f"Status check for start test (CT {ct_id})", "GET", host, f"/api2/json/lxc/{ct_id}/status")
            if res["status_code"] == 200 and res["data"].get("status") == "running":
                running_ct = ct_id
                break

        if running_ct:
            res = await test_endpoint(client, f"Idempotent start (CT {running_ct} already running)", "POST", host, f"/api2/json/lxc/{running_ct}/status/start")
            results.append(res)
            # PVE returns 409 Conflict for an already-running container on /start
            # The code in pve.py avoids this by checking status first
            if res["status_code"] == 409:
                print(f"  NOTE: PVE returned 409 (container already running) — pve.py handles this with a pre-check")
            elif res["status_code"] == 200:
                print(f"  Start task submitted (task-UUID: {res['data']})")
        else:
            print("\n[SKIP] No running container found to test idempotent start")

        # -------------------------------------------------------
        # 6. Test the /access/ticket endpoint shape (alternative auth path)
        # -------------------------------------------------------
        print(f"\n{'='*60}")
        print(f"  Access token info")
        print(f"{'='*60}")
        res = await test_endpoint(client, "Token info", "GET", host, "/api2/json/access/ticket")
        results.append(res)
        # This endpoint expects a session ticket, not API token — expect 401 or different shape
        print(f"  Status: {res['status_code']} (401 expected — this endpoint uses session tickets, not API tokens)")

    # -------------------------------------------------------
    # Summary
    # -------------------------------------------------------
    print(f"\n{'='*60}")
    print(f"  SUMMARY")
    print(f"{'='*60}")
    for r in results:
        icon = "OK" if r["status_code"] == 200 else "!!"
        print(f"  [{icon}] {r['label']}: HTTP {r['status_code']}")

    if errors:
        print(f"\n  Issues found:")
        for e in errors:
            print(f"    - {e}")
        return 1
    else:
        print(f"\n  All endpoints responded correctly!")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
