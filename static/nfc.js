// Tag capture on the Tags page, plus confirmation prompts.
//
// Two capture paths share one status line:
//
//   1. The Pi's own reader — the default. Arms the ACR1552 over the control
//      socket, then polls for the result. Works in every browser on every
//      device, needs no certificate trust, and is the only option on iOS or
//      Firefox.
//   2. The phone's NFC — progressive enhancement, shown only where the Web NFC
//      API actually exists (Chromium on Android, secure context). Absent
//      everywhere else, which is why it is never the primary button.

(function () {
  "use strict";

  var POLL_MS = 1500;

  function el(id) { return document.getElementById(id); }

  // The reader reports UIDs as unseparated uppercase hex (AABB1122); Web NFC
  // gives colon-separated lowercase (aa:bb:11:22). The server normalizes too —
  // this is just so the field shows the operator the canonical form.
  function normalizeUid(raw) {
    return String(raw).replace(/[\s:-]/g, "").toUpperCase();
  }

  function form(pairs) {
    var body = new URLSearchParams();
    Object.keys(pairs).forEach(function (k) { body.set(k, pairs[k]); });
    return {
      method: "POST",
      body: body,
      credentials: "same-origin",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
    };
  }

  // ── 1. Pi door reader ──────────────────────────────────────────────────────

  function initReaderScan(status) {
    var scan = el("reader-scan"), cancel = el("reader-cancel"), name = el("name");
    if (!scan || !cancel) return;
    var timer = null;

    function idle(message) {
      if (timer) { clearInterval(timer); timer = null; }
      scan.disabled = false;
      cancel.classList.add("hidden");
      if (message) status.textContent = message;
    }

    function report(capture) {
      if (capture.status === "added") {
        status.textContent = "Added " + capture.name + " (" + capture.uid + "). Reloading…";
        if (timer) { clearInterval(timer); timer = null; }
        setTimeout(function () { window.location.reload(); }, 900);
        return;
      }
      if (capture.status === "already") {
        idle("That tag is already enrolled as " + capture.name + ".");
      } else if (capture.status === "timeout") {
        idle("Timed out — no tag was scanned.");
      } else {
        idle("Could not add the tag: " + (capture.error || "unknown error"));
      }
    }

    function poll() {
      fetch("/tags/enroll_status", { credentials: "same-origin" })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          var s = d.result || {};
          if (s.capture) return report(s.capture);
          if (!s.armed) return idle("Reader disarmed.");
          status.textContent = "Hold the tag to the door reader… " + s.remaining + "s";
        })
        .catch(function () {
          // A dropped poll is not fatal — the door service keeps the result
          // until it is collected, so just try again on the next tick.
        });
    }

    scan.addEventListener("click", function () {
      scan.disabled = true;
      status.textContent = "Arming the reader…";
      fetch("/tags/arm", form({ csrf: scan.getAttribute("data-csrf"),
                               name: name ? name.value : "" }))
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (!d.ok) return idle("Could not arm the reader: " + (d.error || ""));
          cancel.classList.remove("hidden");
          status.textContent = "Hold the tag to the door reader…";
          timer = setInterval(poll, POLL_MS);
        })
        .catch(function (e) { idle("Could not reach the door service: " + e); });
    });

    cancel.addEventListener("click", function () {
      fetch("/tags/cancel_arm", form({ csrf: cancel.getAttribute("data-csrf") }))
        .then(function () { idle("Cancelled."); })
        .catch(function () { idle("Cancelled."); });
    });
  }

  // ── 2. Phone NFC (optional) ────────────────────────────────────────────────

  function initPhoneScan(status) {
    var button = el("nfc-scan"), unsupported = el("nfc-unsupported"), uid = el("uid");
    if (!button || !unsupported || !uid) return;

    if (!("NDEFReader" in window)) {
      // Say *which* requirement is missing. Both branches name the browser
      // requirement: sending a Firefox user off to install a CA would waste
      // their time, since Firefox has no Web NFC however trusted the page gets.
      var reason = el("nfc-reason"), caHelp = el("nfc-ca");
      if (reason) {
        if (!window.isSecureContext) {
          reason.textContent =
            "This page's certificate is not trusted yet, and the API is hidden " +
            "outside a secure context. It also requires Chrome, Edge or Samsung " +
            "Internet on Android — Firefox and iOS never expose it, trusted " +
            "certificate or not.";
          if (caHelp) caHelp.classList.remove("hidden");
        } else {
          reason.textContent =
            "It requires Chrome, Edge or Samsung Internet on Android. Firefox " +
            "does not implement it, no iOS browser has it, and it does not " +
            "exist on desktop.";
        }
      }
      unsupported.classList.remove("hidden");
      return;
    }

    button.classList.remove("hidden");
    button.addEventListener("click", function () {
      status.textContent = "Hold the tag against the back of your phone…";
      button.disabled = true;

      var reader = new NDEFReader();
      reader.scan().then(function () {
        reader.onreading = function (event) {
          if (!event.serialNumber) {
            // Some tags enumerate without exposing a serial number; nothing
            // useful to fill in, so send the operator to another path.
            status.textContent = "Tag read, but it reported no UID. Use the door reader.";
            button.disabled = false;
            return;
          }
          uid.value = normalizeUid(event.serialNumber);
          uid.focus();
          status.textContent = "Got it — check the name, then Add tag.";
          button.disabled = false;
        };
        reader.onreadingerror = function () {
          status.textContent = "Could not read that tag. Try the door reader instead.";
          button.disabled = false;
        };
      }).catch(function (err) {
        status.textContent =
          err && err.name === "NotAllowedError"
            ? "NFC permission denied. Allow NFC for this site, or use the door reader."
            : "Phone NFC unavailable: " + (err && err.message ? err.message : err);
        button.disabled = false;
      });
    });
  }

  // Inline onclick handlers are blocked by the page's Content-Security-Policy,
  // so destructive buttons declare their prompt with data-confirm instead.
  function initConfirms() {
    document.querySelectorAll("[data-confirm]").forEach(function (node) {
      node.addEventListener("click", function (event) {
        if (!window.confirm(node.getAttribute("data-confirm"))) {
          event.preventDefault();
        }
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    var status = el("scan-status");
    if (status) {
      initReaderScan(status);
      initPhoneScan(status);
    }
    initConfirms();
  });
})();
