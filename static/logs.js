// Auto-refresh for the System log page.
//
// Polls the same filters the page was rendered with and replaces the table body.
// Every value goes in through textContent, never innerHTML: journal messages are
// arbitrary bytes written by other processes on the box, and this app's CSP is
// strict precisely so that a log line can never become markup.
(function () {
  "use strict";

  var body = document.getElementById("log-body");
  if (!body) return;

  var endpoint = body.dataset.endpoint;
  if (!endpoint) return;

  var updated = document.getElementById("log-updated");
  var REFRESH_MS = 10000;

  function cell(row, text, className) {
    var td = document.createElement("td");
    if (className) td.className = className;
    td.textContent = text;
    row.appendChild(td);
  }

  function render(entries) {
    body.replaceChildren();
    entries.forEach(function (entry) {
      var tr = document.createElement("tr");
      cell(tr, entry.time, "mono nowrap");
      cell(tr, entry.unit.replace(/\.service$/, ""), "nowrap");

      var level = document.createElement("td");
      var badge = document.createElement("span");
      badge.className = "tag lvl-" + entry.level;
      badge.textContent = entry.level;
      level.appendChild(badge);
      tr.appendChild(level);

      cell(tr, entry.message, "logmsg");
      body.appendChild(tr);
    });
  }

  function poll() {
    fetch(endpoint + window.location.search, { credentials: "same-origin" })
      .then(function (response) {
        // A 403 means the session expired or the page was disabled; stop
        // rather than hammering it once a second for the rest of the day.
        if (response.status === 403 || response.status === 401) {
          window.clearInterval(timer);
          return null;
        }
        return response.ok ? response.json() : null;
      })
      .then(function (data) {
        if (!data || data.error) return;
        render(data.entries);
        if (updated) {
          updated.textContent = "Updated " + new Date().toLocaleTimeString() + ".";
        }
      })
      .catch(function () {
        // Transient — a restart of door_admin, say. The next tick retries.
      });
  }

  var timer = window.setInterval(poll, REFRESH_MS);
  window.addEventListener("beforeunload", function () {
    window.clearInterval(timer);
  });
})();
