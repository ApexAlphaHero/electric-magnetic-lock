// Updates page: trigger a check or an update, then poll the status file the
// privileged updater writes.
//
// Applying an update restarts door_admin, so this page must survive its own
// server going away mid-poll. Connection failures during a run are expected and
// reported as "restarting", not as errors — the updater keeps writing status to
// disk throughout, and the next successful poll picks up where it left off.

(function () {
  "use strict";

  var POLL_MS = 2000;

  function el(id) { return document.getElementById(id); }

  function post(url, csrf) {
    var body = new URLSearchParams();
    body.set("csrf", csrf);
    return fetch(url, {
      method: "POST",
      body: body,
      credentials: "same-origin",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
    }).then(function (r) { return r.json(); });
  }

  document.addEventListener("DOMContentLoaded", function () {
    var check = el("check"), apply = el("apply"), status = el("update-status");
    if (!check || !apply || !status) return;

    var timer = null;
    var sawDisconnect = false;

    function stop() {
      if (timer) { clearInterval(timer); timer = null; }
      check.disabled = false;
      apply.disabled = false;
    }

    function renderPending(list) {
      var body = el("pending-body"), card = el("pending-card"), behind = el("behind");
      if (behind) behind.textContent = list.length;
      if (card) card.hidden = list.length === 0;
      apply.classList.toggle("hidden", list.length === 0);
      if (!body) return;
      body.textContent = "";
      list.forEach(function (c) {
        var tr = document.createElement("tr");
        [[c.sha, "mono nowrap"], [c.subject, ""], [(c.date || "").replace("T", " "), "mono nowrap"]]
          .forEach(function (pair) {
            var td = document.createElement("td");
            td.className = pair[1];
            td.textContent = pair[0];   // textContent, never innerHTML: commit
            tr.appendChild(td);         // subjects are attacker-influenced text
          });
        body.appendChild(tr);
      });
    }

    function poll() {
      fetch("/updates/status", { credentials: "same-origin" })
        .then(function (r) {
          if (r.status === 401 || r.status === 403) throw new Error("signed out");
          return r.json();
        })
        .then(function (d) {
          if (sawDisconnect) {
            // door_admin came back after its restart — reload so the page and
            // its CSRF token match the new process.
            window.location.reload();
            return;
          }
          status.textContent = d.message || "";
          renderPending(d.pending || []);
          var current = el("current-sha");
          if (current && d.current) current.textContent = d.current.sha;

          if (d.state !== "running") {
            stop();
            if (d.state === "ok" || d.state === "rolled_back") {
              // Reload to pick up the fresh log tail.
              setTimeout(function () { window.location.reload(); }, 1200);
            }
          }
        })
        .catch(function () {
          // Expected while door_admin restarts as part of the update.
          sawDisconnect = true;
          status.textContent = "Web admin is restarting — reconnecting…";
        });
    }

    function begin(message) {
      check.disabled = true;
      apply.disabled = true;
      status.textContent = message;
      if (!timer) timer = setInterval(poll, POLL_MS);
    }

    check.addEventListener("click", function () {
      begin("Checking…");
      post("/updates/check", check.getAttribute("data-csrf"))
        .then(function (d) { if (!d.ok) { stop(); status.textContent = "Could not check: " + (d.error || ""); } })
        .catch(function (e) { stop(); status.textContent = "Could not check: " + e; });
    });

    apply.addEventListener("click", function () {
      // The shared data-confirm handler in nfc.js is not loaded on this page,
      // so the prompt is done here.
      var prompt = apply.getAttribute("data-confirm");
      if (prompt && !window.confirm(prompt)) return;
      begin("Starting update…");
      post("/updates/apply", apply.getAttribute("data-csrf"))
        .then(function (d) { if (!d.ok) { stop(); status.textContent = "Could not start: " + (d.error || ""); } })
        .catch(function (e) { stop(); status.textContent = "Could not start: " + e; });
    });
  });
})();
