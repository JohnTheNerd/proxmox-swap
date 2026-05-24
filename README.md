# Proxmox Swap Manager

Manages access between two Proxmox containers or virtual machines with a strict invariant: **both can never run simultaneously**. One runs by default (the host), while the other runs on demand (the guest) with a watchdog timer.

I wrote this because I share some of my compute resources with a friend, but privacy implications require only *one* of our workloads run at any given time.

Built with Python 3.12, FastAPI, and Tailwind CSS.

1. **Host container** runs by default (e.g., your personal workstation)
2. **Guest requests a session** via the web UI — the manager stops the host, starts the guest
3. **Watchdog timer** counts down from the initial timeout (default 2 hours)
4. **Guest can extend** the session by kicking the watchdog (default +1h, +2h, +4h, +8h buttons)
5. **Warning phase** triggers when the deadline approaches (default 30 minutes before)
6. **Grace period** starts after the warning (default 10 minutes), with desktop notifications
7. **Expiry** immediately swaps the containers back to host
8. **Owner bypasses all controls** and can kick the watchdog for longer than allowed

## Session State Machine

Sessions progress through four states with automatic transitions driven by the watchdog:

```
IDLE → ACTIVE → WARNING → GRACE → (swap back to IDLE)
```

| State | When | What Happens |
|---|---|---|
| **IDLE** | No active session | Host runs, guest is stopped. Guest can request a session. |
| **ACTIVE** | Session started | Guest runs, host is stopped. Timer counts down to `deadline`. Guest can kick the watchdog to extend. |
| **WARNING** | `now >= deadline - WARNING_SECONDS` (default 30 min before) | Same as ACTIVE, but the UI shows a warning. The guest can still kick. |
| **GRACE** | `now >= deadline - FORCE_GRACE_SECONDS` (default 10 min before) | Final countdown. Desktop notifications are sent. Once `now >= deadline`, the watchdog immediately swaps the containers back to host. |

The **kicking** mechanism adds time to the current deadline. Non-owners are capped by a rolling window (`MAX_SECONDS`, default 8 hours from `now`), so repeated kicks cannot extend a session indefinitely. Owners bypass this cap entirely.

## Safety Measures

- **Safe switching protocol**: 7-step verify → stop → verify → cooldown → start → verify → cross-check with undo paths at every step
- **Crash recovery**: PVE API is the source of truth on startup, while running sessions are respected
- **Watchdog patrol loop**: continuously enforces the container states, detecting and remediating violations (guest running without a session, both containers running, both containers stopped)
- **Atomic state writes**: writes to `.tmp` then `os.rename()` (POSIX atomic)
- **Async lock**: all switching operations are serialized via `asyncio.Lock`

## Prerequisites

### Proxmox Setup

1. Create an API token for a user with VM management permissions

2. Note the **CT IDs** for both containers (e.g., 110 for host, 128 for guest)

3. The containers should be on the **same Proxmox node** (or accessible via the same API endpoint)

### Proxmox Cluster

If you have a Proxmox cluster, you can configure multiple API hosts. The manager will try each one until a request succeeds:

```
PVE_HOST=https://pve1:8006,https://pve2:8006,https://pve3:8006
```

## Configuration

All configuration is via environment variables. Sensitive values can be provided via files using the `*_FILE` pattern (e.g., `PVE_API_TOKEN_SECRET_FILE=/run/secrets/token`).

| Variable | Required | Default | Description |
|---|---|---|---|
| `PVE_HOST` | Yes | — | Comma-separated list of Proxmox API host URLs (e.g., `https://pve:8006`) |
| `PVE_API_USER` | Yes | — | Proxmox API user (e.g., `root@pam` or `user@pve`) |
| `PVE_API_TOKEN_ID` | Yes | — | API token ID (e.g., `proxmox-swap`) |
| `PVE_API_TOKEN_SECRET` | Yes | — | API token secret UUID |
| `PVE_VERIFY_TLS` | No | `true` | Verify Proxmox TLS certificates. Set to `false` for self-signed certs |
| `HOST_CT_ID` | Yes | — | Container ID for the host |
| `GUEST_CT_ID` | Yes | — | Container ID for the guest |
| `OWNER_NAMES` | header | — | Comma-separated list of owner names (used when `AUTH_MODE=header`) |
| `OWNER_GROUPS` | oidc | — | Comma-separated list of group names that grant owner role (used when `AUTH_MODE=oidc`) |
| `AUTH_MODE` | No | `header` | Auth mode: `none`, `header`, or `oidc` |
| `AUTH_HEADER` | No | `X-Auth-Request-Name` | Header name for user name in header auth mode |
| `OIDC_ISSUER` | OIDC | — | OIDC issuer URL (must include the `.well-known/openid-configuration` path, e.g., `https://accounts.google.com`) |
| `OIDC_CLIENT_ID` | OIDC | — | OIDC client ID |
| `OIDC_CLIENT_SECRET` | OIDC | — | OIDC client secret |
| `SESSION_SECRET` | OIDC | — | Signing secret for Starlette sessions (required for OIDC) |
| `OIDC_PKCE` | No | `true` | Enable PKCE for OIDC flow. Set to `false` for IdPs that don't support PKCE |
| `OIDC_BASE_URI` | No | — | Override the base URL used for the OIDC redirect URI (e.g., `https://app.example.com`). The callback URL will be `{OIDC_BASE_URI}/auth/callback`. Use this when behind a reverse proxy so the OIDC provider sees the correct redirect URI |
| `OIDC_BASE_URI` | No | (inferred from request) | Base URL for the OIDC callback redirect URI (e.g., `https://swap.example.com`). Appends `/auth/callback` automatically. Use this when behind a reverse proxy so the redirect URI matches your IdP's registered callback URL |
| `OIDC_SCOPES` | No | `openid profile groups` | Space-separated OIDC scopes to request |
| `OIDC_USERNAME_CLAIM` | No | `preferred_username` | OIDC userinfo claim to use as the user identifier |
| `OIDC_GROUPS_CLAIM` | No | `groups` | OIDC userinfo claim containing user groups |
| `DEFAULT_TIMEOUT_SECONDS` | No | `7200` | Initial session timeout (2 hours) |
| `MAX_SECONDS` | No | `28800` | Maximum length the watchdog can be kicked (8 hours) |
| `WARNING_SECONDS` | No | `1800` | Seconds before deadline to enter warning (30 minutes) |
| `FORCE_GRACE_SECONDS` | No | `600` | Seconds before deadline to enter grace period (10 minutes) |
| `PVE_API_TIMEOUT_SECONDS` | No | `30` | HTTP request timeout for PVE API calls |
| `PVE_RETRY_COUNT` | No | `3` | Retries per host before trying the next |
| `PVE_RETRY_DELAY_SECONDS` | No | `1` | Base delay between retries (exponential backoff) |
| `CONTAINER_START_TIMEOUT_SECONDS` | No | `120` | Max time to wait for container start |
| `CONTAINER_STOP_TIMEOUT_SECONDS` | No | `120` | Max time to wait for container stop |
| `SWITCH_COOLDOWN_SECONDS` | No | `1` | Cooldown between stop and start during switching |
| `POLL_INTERVAL_SECONDS` | No | `2` | Poll interval for container status verification |
| `COURTESY_SESSION_SECONDS` | No | `1800` | Courtesy session duration after crash recovery |
| `STATE_FILE_PATH` | No | `/data/state.json` | Path to the persisted state file |
| `WATCHDOG_INTERVAL_SECONDS` | No | `30` | Interval between watchdog patrol cycles |

\* `OWNER_NAMES` required for header mode, `OWNER_GROUPS` required for OIDC mode

## Deployment

### Docker Compose (Development)

```bash
# Copy and edit the example env file
cp .env.example .env

# Start
docker compose up --build
```

The Compose file includes two Caddy reverse proxies (guest on port 5001, owner on port 5002) that inject the respective user name headers.

### Docker Swarm (Production)

```bash
# Create Docker secrets
echo -n "token-id" | docker secret create pve_api_token_id -
echo -n "token-secret" | docker secret create pve_api_token_secret -
echo -n "oidc-client-id" | docker secret create oidc_client_id -
echo -n "oidc-client-secret" | docker secret create oidc_client_secret -
echo -n "$(openssl rand -hex 32)" | docker secret create session_secret -

# Deploy
docker stack deploy -c docker-swarm.yml proxmox-swap
```

### Behind a Reverse Proxy

The app works well behind an OIDC reverse proxy (e.g., [oauth2-proxy](https://github.com/oauth2-proxy/oauth2-proxy) or [Traefik Forward Auth](https://doc.traefik.io/traefik/middlewares/http/forwardauth/)). Set `AUTH_MODE=header` and configure the proxy to inject the user's name into `X-Auth-Request-Name`.

## Authentication Modes

### None Mode

Set `AUTH_MODE=none` to disable authentication entirely. Everyone is treated as an owner. Useful for local development or trusted environments.

### Header Mode

Auth via trusted headers from a reverse proxy. The proxy handles authentication and injects the user's name into a configurable header. Owner/guest is determined by matching against `OWNER_NAMES`. Simple, no OIDC setup needed.

### OIDC Mode

Full OIDC flow with session cookies. Users log in through your IdP (Google, Keycloak, etc.). Owner/guest is determined by group membership from the IdP — configure `OWNER_GROUPS` and `OIDC_GROUPS_CLAIM`. The user identifier comes from `OIDC_USERNAME_CLAIM` (default `name`), not email. Supports PKCE when `SESSION_SECRET` is set — disabled for IdPs that don't support it.

Register the callback URL `https://your-domain/auth/callback` (or `http://localhost:8000/auth/callback` for local dev) in your IdP's OAuth client configuration.

## Web UI

- **Dashboard**: current session status, timer, controls, and history
- **Timer**: updates via HTMX polling (5s interval)
- **Controls**: start session, extend (+1h/+2h/+4h/+8h), end session
- **Owner override**: force-swap button (owner only, immediate, no grace period)
- **Desktop notifications**: browser notifications for warning and grace transitions
- **History**: full audit log visible to all users

## API Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/` | Required | Dashboard page |
| `GET` | `/session` | Required | Session status (HTML fragment for HTMX, JSON otherwise) |
| `POST` | `/session` | Required | Start a guest session |
| `PATCH` | `/session` | Required | Extend session (`seconds` form field) |
| `DELETE` | `/session` | Required | End the current session |
| `POST` | `/session/force-stop` | Owner | Force-swap to host immediately |
| `GET` | `/history` | Required | Audit log entries |
| `GET` | `/containers/status` | Required | Container status (HTML fragment for HTMX) |
| `POST` | `/containers/{id}/start` | Owner | Start a container directly |
| `POST` | `/containers/{id}/stop` | Owner | Graceful shutdown of a container |
| `POST` | `/containers/{id}/force-stop` | Owner | Force stop a container |
| `GET` | `/health` | None | Health check (`{"status": "ok"}`) |
| `GET` | `/auth/login` | None | OIDC login redirect |
| `GET` | `/auth/callback` | None | OIDC callback handler |
| `POST` | `/auth/logout` | None | Clear session cookie |

## Session States

```
IDLE → ACTIVE → WARNING → GRACE → (expiry → IDLE)
              ↑           ↓
              └── kick ───┘
```

- **IDLE**: No active session, host container running
- **ACTIVE**: Guest session running, timer counting down
- **WARNING**: Deadline approaching (configurable lead time), visual warning in UI
- **GRACE**: Final countdown, desktop notification sent, last chance to extend
- **Expiry**: Timer expires, containers immediately swapped back to host
