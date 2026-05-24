/* Main SPA logic — polling, rendering, actions. */

var state = null;
var containers = null;
var prevSessionState = null;

/* ── Fetch with redirect handling ── */

function apiFetch(url, opts) {
    return fetch(url, Object.assign({ credentials: "same-origin", redirect: "manual" }, opts || {}))
        .then(function (res) {
            if (res.type === "opaqueredirect" || (res.status >= 300 && res.status < 400)) {
                window.location.href = "/";
            }
            return res;
        });
}

/* ── Polling ── */

function pollSession() {
    apiFetch("/session")
        .then(function (res) {
            if (res.status === 401 || res.status === 403) {
                location.reload();
                return;
            }
            return res.json();
        })
        .then(function (data) {
            if (!data) return;
            // Update timer
            if (window.ProxmoxSwapTimer && window.ProxmoxSwapTimer.updateFromServer) {
                window.ProxmoxSwapTimer.updateFromServer(data);
            }
            // Detect state transitions for notifications
            if (prevSessionState && data.state && data.state !== prevSessionState) {
                if (window.ProxmoxSwapNotify && window.ProxmoxSwapNotify.onStateTransition) {
                    window.ProxmoxSwapNotify.onStateTransition(prevSessionState, data.state, data);
                }
            }
            prevSessionState = data.state;
            state = data;
            renderStatus();
        })
        .catch(function () { });
}

function pollContainers() {
    apiFetch("/containers/status")
        .then(function (res) { return res.json(); })
        .then(function (data) {
            containers = data;
            renderContainers();
        })
        .catch(function () { });
}

function pollHistory() {
    apiFetch("/history")
        .then(function (res) { return res.json(); })
        .then(function (entries) {
            renderHistory(entries);
        })
        .catch(function () { });
}

/* ── Actions ── */

function doAction(button, method, path, body) {
    var orig = button.innerHTML;
    button.classList.add("loading");
    button.disabled = true;

    var opts = { method: method };
    if (body) {
        opts.headers = { "Content-Type": "application/json" };
        opts.body = JSON.stringify(body);
    }

    apiFetch(path, opts)
        .then(function (res) {
            if (res.status === 401 || res.status === 403) {
                location.reload();
                return;
            }
            // Re-poll immediately
            pollSession();
            pollContainers();
            // Async history refresh — don't block
            pollHistory();
        })
        .catch(function () { })
        .finally(function () {
            button.classList.remove("loading");
            button.disabled = false;
            button.innerHTML = orig;
        });
}

/* ── Rendering ── */

function show(el) { el.classList.remove("hidden"); }
function hide(el) { el.classList.add("hidden"); }

function renderStatus() {
    if (!state) return;
    var s = state.state;

    // Status dot color
    var dot = document.getElementById("status-dot");
    var label = document.getElementById("status-label");
    var dotColors = { IDLE: "bg-green-500", ACTIVE: "bg-blue-500", WARNING: "bg-amber-500", GRACE: "bg-red-500" };
    dot.className = "w-2.5 h-2.5 rounded-full " + (dotColors[s] || "bg-zinc-500") + " animate-pulse-slow";
    label.textContent = s;

    // Session info
    var sessionInfo = document.getElementById("session-info");
    if (s !== "IDLE") {
        show(sessionInfo);
        document.getElementById("session-id").textContent = state.session_id;
        if (state.kick_count > 0) {
            show(document.getElementById("kick-count"));
            document.querySelector("#kick-count span").textContent = state.kick_count;
        } else {
            hide(document.getElementById("kick-count"));
        }
    } else {
        hide(sessionInfo);
    }

    // Warning banners
    var bannerWarning = document.getElementById("banner-warning");
    var bannerGrace = document.getElementById("banner-grace");
    hide(bannerWarning);
    hide(bannerGrace);
    if (s === "WARNING") {
        var mins = Math.max(0, Math.ceil((state.grace_at - Date.now() / 1000) / 60));
        document.getElementById("warning-text").textContent = "Save your work — shutdown in " + mins + " minutes.";
        show(bannerWarning);
    } else if (s === "GRACE") {
        show(bannerGrace);
    }

    // Controls
    var idleControls = document.getElementById("controls-idle");
    var activeControls = document.getElementById("controls-active");
    if (s === "IDLE") {
        show(idleControls);
        hide(activeControls);
    } else {
        hide(idleControls);
        show(activeControls);
        renderKickButtons();
    }

    // User badge
    renderUserBadge();
}

function renderUserBadge() {
    if (!state || !state.user) return;
    var u = state.user;
    document.getElementById("user-name").textContent = u.name;
    var roleEl = document.getElementById("user-role");
    roleEl.textContent = u.role;
    roleEl.className = u.role === "owner"
        ? "inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium bg-blue-500/10 text-blue-400"
        : "inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium bg-zinc-800 text-zinc-500";
}

function renderContainers() {
    if (!containers) return;
    var isOwner = state && state.user && state.user.role === "owner";

    ["host", "guest"].forEach(function (role) {
        var ct = containers[role];
        var dot = document.getElementById(role + "-dot");
        var badge = document.getElementById(role + "-badge");
        var ctId = document.getElementById(role + "-ct-id");
        var status = document.getElementById(role + "-status");
        var controls = document.getElementById(role + "-controls");

        // Dot + badge
        if (ct.active) {
            dot.className = "w-2 h-2 rounded-full bg-green-500 animate-pulse-slow";
            badge.className = "inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium bg-green-500/10 text-green-400";
            badge.textContent = "Active";
        } else {
            dot.className = "w-2 h-2 rounded-full bg-zinc-600";
            badge.className = "inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium bg-zinc-800 text-zinc-500";
            badge.textContent = "Standby";
        }

        // CT ID + status
        ctId.textContent = "CT " + ct.ct_id;
        ctId.className = "font-mono text-sm " + (ct.status === "running" ? "text-green-400" : "text-zinc-600");
        status.textContent = ct.status;
        status.className = "text-xs font-medium uppercase " + (ct.status === "running" ? "text-green-500" : "text-zinc-600");

        // Owner controls
        if (isOwner) {
            show(controls);
            if (ct.status === "running") {
                hide(document.getElementById(role + "-start"));
                show(document.getElementById(role + "-stop"));
                show(document.getElementById(role + "-force-stop"));
            } else {
                show(document.getElementById(role + "-start"));
                hide(document.getElementById(role + "-stop"));
                hide(document.getElementById(role + "-force-stop"));
            }
        } else {
            hide(controls);
        }
    });
}

function renderHistory(entries) {
    var emptyEl = document.getElementById("history-empty");
    var tableEl = document.getElementById("history-table");
    var bodyEl = document.getElementById("history-body");
    var tpl = document.getElementById("history-row-tpl");

    if (!entries || entries.length === 0) {
        show(emptyEl);
        hide(tableEl);
        return;
    }

    hide(emptyEl);
    show(tableEl);

    // Clear previous rows
    bodyEl.innerHTML = "";

    var labels = {
        "session_start": { label: "Started", cls: "bg-blue-500/10 text-blue-400" },
        "session_stop": { label: "Stopped", cls: "bg-green-500/10 text-green-400" },
        "force_stop": { label: "Force Stop", cls: "bg-amber-500/10 text-amber-400" },
        "session_kick": { label: "Watchdog Kick", cls: "bg-zinc-800 text-zinc-400" },
        "state_warning": { label: "Warning", cls: "bg-yellow-500/10 text-yellow-400" },
        "state_grace": { label: "Grace", cls: "bg-red-500/10 text-red-400" },
        "session_expired": { label: "Expired", cls: "bg-red-500/10 text-red-300" },
        "watchdog_host_stop": { label: "Watchdog", cls: "bg-purple-500/10 text-purple-400" },
        "watchdog_guest_stop": { label: "Watchdog", cls: "bg-purple-500/10 text-purple-400" },
        "watchdog_recovery": { label: "Watchdog", cls: "bg-purple-500/10 text-purple-400" },
        "crash_recovery": { label: "Crash Recovery", cls: "bg-orange-500/10 text-orange-400" },
        "container_start": { label: "Container Start", cls: "bg-green-500/10 text-green-300" },
        "container_stop": { label: "Container Stop", cls: "bg-amber-500/10 text-amber-300" },
        "container_force_stop": { label: "Container Force Stop", cls: "bg-red-500/10 text-red-300" }
    };

    entries.forEach(function (e) {
        var row = tpl.content.cloneNode(true);
        var cells = row.querySelectorAll("td");
        var ts = new Date(e.timestamp * 1000);
        cells[0].textContent = ts.toLocaleDateString() + " " + ts.toLocaleTimeString();
        var info = labels[e.action] || { label: e.action, cls: "bg-zinc-800 text-zinc-500" };
        var badge = cells[1].querySelector("span");
        badge.textContent = info.label;
        badge.classList.add.apply(badge.classList, info.cls.split(" "));
        cells[2].textContent = e.session_id || "—";
        cells[3].textContent = e.actor || "—";
        cells[4].textContent = e.details;
        bodyEl.appendChild(row);
    });
}

/* ── Kick button rendering ── */

function formatSeconds(s) {
    if (s >= 3600) {
        var h = Math.floor(s / 3600);
        var m = (s % 3600) / 60;
        return "+" + h + (m > 0 ? "h" + m + "m" : "h");
    }
    return "+" + Math.floor(s / 60) + "m";
}

function renderKickButtons() {
    if (!state || !state.max_seconds) return;
    var container = document.getElementById("kick-buttons");
    container.innerHTML = "";
    var max = state.max_seconds;
    var fractions = [
        { seconds: Math.ceil(max / 8) },
        { seconds: Math.ceil(max / 4) },
        { seconds: Math.ceil(max / 2) },
        { seconds: max }
    ];
    fractions.forEach(function (f) {
        var btn = document.createElement("button");
        btn.className = "inline-flex items-center rounded-md px-3 py-2 text-sm font-medium bg-zinc-800 text-zinc-200 border border-zinc-700 hover:bg-zinc-700 hover:border-zinc-600 transition-colors";
        btn.textContent = formatSeconds(f.seconds);
        btn.addEventListener("click", function () {
            doAction(this, "PATCH", "/session", { seconds: f.seconds });
        });
        container.appendChild(btn);
    });
}

/* ── Button handlers ── */

document.getElementById("btn-start").addEventListener("click", function () {
    doAction(this, "POST", "/session", null);
});
document.getElementById("btn-end").addEventListener("click", function () {
    doAction(this, "DELETE", "/session", null);
});
// Container controls — handlers set up, buttons shown/hidden by renderContainers
document.getElementById("host-start").addEventListener("click", function () {
    if (containers && containers.host) doAction(this, "POST", "/containers/" + containers.host.ct_id + "/start", null);
});
document.getElementById("host-stop").addEventListener("click", function () {
    if (containers && containers.host) doAction(this, "POST", "/containers/" + containers.host.ct_id + "/stop", null);
});
document.getElementById("host-force-stop").addEventListener("click", function () {
    if (containers && containers.host) doAction(this, "POST", "/containers/" + containers.host.ct_id + "/force-stop", null);
});
document.getElementById("guest-start").addEventListener("click", function () {
    if (containers && containers.guest) doAction(this, "POST", "/containers/" + containers.guest.ct_id + "/start", null);
});
document.getElementById("guest-stop").addEventListener("click", function () {
    if (containers && containers.guest) doAction(this, "POST", "/containers/" + containers.guest.ct_id + "/stop", null);
});
document.getElementById("guest-force-stop").addEventListener("click", function () {
    if (containers && containers.guest) doAction(this, "POST", "/containers/" + containers.guest.ct_id + "/force-stop", null);
});

/* ── Init ── */
pollSession();
pollContainers();
pollHistory();
setInterval(pollSession, 5000);
setInterval(pollContainers, 5000);
setInterval(pollHistory, 5000);
