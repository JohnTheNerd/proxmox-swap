/* Settings dropdown — notification preferences. */

var btn = document.getElementById("settings-btn");
var dropdown = document.getElementById("settings-dropdown");
var statusEl = document.getElementById("notify-status");
var toggle = document.getElementById("notify-toggle");
var testBtn = document.getElementById("notify-test");

var labels = {
    unsupported: "Notifications not supported",
    enabled: "Enabled — you'll get alerts for warnings and grace periods while this tab is open!",
    disabled: "Disabled",
    default: "Click to enable desktop notifications",
    denied: "Blocked by browser — enable in your browser settings",
};

function renderSettings() {
    var status = window.ProxmoxSwapNotify.getStatus();

    if (status === "unsupported") {
        if (btn) btn.style.display = "none";
        return;
    }

    if (statusEl) statusEl.textContent = labels[status] || "Unknown";

    if (toggle) {
        toggle.checked = status === "enabled";
        var toggleLabel = toggle.parentElement;
        if (status === "denied") {
            toggleLabel.style.display = "none";
        } else {
            toggleLabel.style.display = "";
            toggleLabel.classList.remove("disabled");
        }
    }

    if (testBtn) {
        if (status === "enabled") {
            testBtn.classList.remove("hidden");
        } else {
            testBtn.classList.add("hidden");
        }
    }
}

if (btn && dropdown) {
    btn.addEventListener("click", function (e) {
        e.stopPropagation();
        dropdown.classList.toggle("hidden");
    });

    document.addEventListener("click", function () {
        dropdown.classList.add("hidden");
    });

    dropdown.addEventListener("click", function (e) {
        e.stopPropagation();
    });
}

if (toggle) {
    var toggleLabel = toggle.parentElement;
    if (toggleLabel) {
        toggleLabel.addEventListener("click", function (e) {
            e.preventDefault();
            e.stopPropagation();
            if (window.ProxmoxSwapNotify.getStatus() === "denied") return;

            var newChecked = !toggle.checked;
            toggle.checked = newChecked;

            if (newChecked) {
                if (Notification.permission === "granted") {
                    window.ProxmoxSwapNotify.setEnabled(true);
                    renderSettings();
                } else {
                    window.ProxmoxSwapNotify.requestPermission().finally(function () { renderSettings(); });
                }
            } else {
                window.ProxmoxSwapNotify.setEnabled(false);
                renderSettings();
            }
        });
    }
}

if (testBtn) {
    testBtn.addEventListener("click", function () {
        window.ProxmoxSwapNotify.sendTestNotification();
    });
}

renderSettings();
