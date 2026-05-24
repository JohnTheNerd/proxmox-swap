/* Local countdown timer — ticks client-side, synced by app.js. */

var deadline = null;
var timerState = "IDLE";

function format(secs) {
    secs = Math.max(0, Math.ceil(secs));
    var h = Math.floor(secs / 3600);
    var m = Math.floor((secs % 3600) / 60);
    var s = secs % 60;
    if (h > 0) {
        return h + ":" + String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0");
    }
    return String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0");
}

function renderTimer() {
    var timerEl = document.getElementById("timer-display");
    if (!timerEl) return;

    if (timerState === "IDLE") {
        timerEl.textContent = "No active session";
        timerEl.className = "text-zinc-500 text-base";
        return;
    }

    var remaining = Math.max(0, (deadline - Date.now()) / 1000);
    timerEl.textContent = format(remaining);
    timerEl.className = "font-mono font-bold text-6xl tracking-tight text-zinc-100";
}

function tick() {
    renderTimer();
    if (deadline && Date.now() >= deadline) {
        timerState = "IDLE";
        deadline = null;
        renderTimer();
    }
}

function updateFromServer(data) {
    if (data.state === "IDLE" || !data.state) {
        timerState = "IDLE";
        deadline = null;
    } else if (data.remaining_seconds > 0) {
        timerState = data.state;
        deadline = Date.now() + (data.remaining_seconds * 1000);
    }
    renderTimer();
}

/* Local tick every second. */
setInterval(tick, 1000);

/* Expose for app.js. */
window.ProxmoxSwapTimer = { updateFromServer: updateFromServer };
