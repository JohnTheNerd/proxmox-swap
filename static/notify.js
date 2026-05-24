/* Desktop notification helper — fires on WARNING/GRACE transitions. */

var STORAGE_KEY = "proxmox-swap-notifications";
var notifiedThisSession = new Set();

function isEnabled() {
    return localStorage.getItem(STORAGE_KEY) === "true";
}

function setEnabled(val) {
    localStorage.setItem(STORAGE_KEY, val);
}

function notify(title, body) {
    if (!("Notification" in window)) return;
    if (!isEnabled()) return;
    if (Notification.permission === "granted") {
        new Notification(title, { body: body, tag: "proxmox-swap-" + title });
    }
}

function sendTestNotification() {
    notify("Test Notification", "Desktop notifications are working!");
}

function requestPermission() {
    if (!("Notification" in window)) return Promise.resolve();
    return Notification.requestPermission().then(function (p) {
        if (p === "granted") setEnabled(true);
    });
}

function shouldShowPrompt() {
    return getStatus() === "default";
}

function getStatus() {
    if (!("Notification" in window)) return "unsupported";
    if (Notification.permission === "denied") return "denied";
    if (Notification.permission === "granted") {
        if (isEnabled()) return "enabled";
        if (localStorage.getItem(STORAGE_KEY) === "false") return "disabled";
    }
    return "default";
}

function initPrompt() {
    var el = document.getElementById("notify-prompt");
    if (!el || !shouldShowPrompt()) return;
    el.classList.remove("hidden");

    var accept = document.getElementById("notify-accept");
    var decline = document.getElementById("notify-decline");
    if (accept) {
        accept.addEventListener("click", function () {
            requestPermission().then(function () { location.reload(); });
        });
    }
    if (decline) {
        decline.addEventListener("click", function () {
            setEnabled(false);
            el.classList.add("hidden");
        });
    }
}

function formatRemaining(seconds) {
    var m = Math.floor(seconds / 60);
    var s = Math.floor(seconds % 60);
    if (m >= 60) {
        var h = Math.floor(m / 60);
        m = m % 60;
        return h + "h " + m + "m";
    }
    return m + "m " + s + "s";
}

function onStateTransition(prevState, newState, sessionData) {
    if (newState === "WARNING" && !notifiedThisSession.has("WARNING")) {
        var remaining = sessionData ? Math.max(0, Math.ceil((sessionData.grace_at - Date.now() / 1000))) : 0;
        notify("Session Warning", "Save your work — " + formatRemaining(remaining) + " left.");
        notifiedThisSession.add("WARNING");
    } else if (newState === "GRACE" && !notifiedThisSession.has("GRACE")) {
        var remaining2 = sessionData ? Math.max(0, Math.ceil((sessionData.deadline - Date.now() / 1000))) : 0;
        notify("Session Grace Period", "Shutting down in " + formatRemaining(remaining2) + ".");
        notifiedThisSession.add("GRACE");
    } else if (newState === "IDLE") {
        notifiedThisSession.clear();
    }
}

window.ProxmoxSwapNotify = {
    requestPermission: requestPermission,
    shouldShowPrompt: shouldShowPrompt,
    getStatus: getStatus,
    initPrompt: initPrompt,
    sendTestNotification: sendTestNotification,
    setEnabled: setEnabled,
    onStateTransition: onStateTransition
};

initPrompt();
