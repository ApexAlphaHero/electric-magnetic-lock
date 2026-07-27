// Optional phone-based tag reading, plus confirmation prompts.
//
// Web NFC (NDEFReader) exists only in Chrome on Android, and only in a secure
// context — with a self-signed certificate that means the Pi's CA must be
// installed on the phone, otherwise the API is simply absent. Everything here
// is progressive enhancement: the page is fully usable with no NFC at all, via
// the "Recent unknown scans" list or by typing the UID.

(function () {
  "use strict";

  // The reader reports UIDs as unseparated uppercase hex (AABB1122); Web NFC
  // gives colon-separated lowercase (aa:bb:11:22). The server normalizes too —
  // this is just so the field shows the operator the canonical form.
  function normalizeUid(raw) {
    return String(raw).replace(/[\s:-]/g, "").toUpperCase();
  }

  function initNfc() {
    var panel = document.getElementById("nfc-panel");
    var unsupported = document.getElementById("nfc-unsupported");
    var button = document.getElementById("nfc-scan");
    var status = document.getElementById("nfc-status");
    var uidField = document.getElementById("uid");
    if (!panel || !unsupported || !button || !status || !uidField) return;

    if (!("NDEFReader" in window)) {
      unsupported.classList.remove("hidden");
      return;
    }
    panel.classList.remove("hidden");

    button.addEventListener("click", function () {
      status.textContent = "Hold the tag against the back of your phone…";
      button.disabled = true;

      var reader = new NDEFReader();
      reader.scan().then(function () {
        reader.onreading = function (event) {
          if (!event.serialNumber) {
            // Some tags enumerate without exposing a serial number; nothing
            // useful to fill in, so send the operator to the manual path.
            status.textContent = "Tag read, but it reported no UID. Enter it by hand.";
            button.disabled = false;
            return;
          }
          uidField.value = normalizeUid(event.serialNumber);
          uidField.focus();
          status.textContent = "Got it — check the name, then Add tag.";
          button.disabled = false;
        };
        reader.onreadingerror = function () {
          status.textContent = "Could not read that tag. Try again, or enter the UID by hand.";
          button.disabled = false;
        };
      }).catch(function (err) {
        status.textContent =
          err && err.name === "NotAllowedError"
            ? "NFC permission was denied. Allow NFC for this site, or enter the UID by hand."
            : "NFC is unavailable on this device: " + (err && err.message ? err.message : err);
        button.disabled = false;
      });
    });
  }

  // Inline onclick handlers are blocked by the page's Content-Security-Policy,
  // so destructive buttons declare their prompt with data-confirm instead.
  function initConfirms() {
    document.querySelectorAll("[data-confirm]").forEach(function (el) {
      el.addEventListener("click", function (event) {
        if (!window.confirm(el.getAttribute("data-confirm"))) {
          event.preventDefault();
        }
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    initNfc();
    initConfirms();
  });
})();
