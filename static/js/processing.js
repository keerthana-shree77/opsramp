/* Polls the job endpoint and paints the progress indicator. */
(function () {
  "use strict";

  var card = document.getElementById("progress-card");
  if (!card) { return; }
  var url = card.getAttribute("data-poll-url");

  var fill = document.getElementById("progress-fill");
  var wrap = document.getElementById("progress-bar-wrap");
  var percentText = document.getElementById("progress-percent");
  var message = document.getElementById("progress-message");
  var errorBox = document.getElementById("progress-error");
  var errorText = document.getElementById("progress-error-text");

  var fields = {
    "stat-discovered": "assets_discovered",
    "stat-selected": "assets_selected",
    "stat-processed": "assets_processed",
    "stat-components": "components"
  };

  var delay = 1500;
  var failures = 0;

  function setNumber(id, value) {
    var node = document.getElementById(id);
    if (!node) { return; }
    node.textContent = (value === undefined || value === null) ? "–" : String(value);
  }

  function paint(state) {
    var percent = Math.max(0, Math.min(100, Number(state.percent) || 0));
    fill.style.width = percent + "%";
    percentText.textContent = percent + "%";
    wrap.setAttribute("aria-valuenow", String(percent));
    if (state.message) { message.textContent = state.message; }
    Object.keys(fields).forEach(function (id) {
      setNumber(id, state[fields[id]]);
    });
  }

  function showError(text) {
    errorText.textContent = text;
    errorBox.classList.remove("hidden");
    message.textContent = "Stopped.";
  }

  function poll() {
    fetch(url, { headers: { "Accept": "application/json" }, credentials: "same-origin" })
      .then(function (response) {
        if (response.status === 404) { throw new Error("This job is no longer available."); }
        if (!response.ok) { throw new Error("Progress could not be read."); }
        return response.json();
      })
      .then(function (state) {
        failures = 0;
        paint(state);
        if (state.status === "error") {
          showError(state.error || "The comparison failed.");
          return;
        }
        if (state.redirect) {
          window.location.href = state.redirect;
          return;
        }
        window.setTimeout(poll, delay);
      })
      .catch(function (err) {
        failures += 1;
        if (failures >= 5) {
          showError(err.message || "Lost contact with the server.");
          return;
        }
        window.setTimeout(poll, delay * 2);
      });
  }

  poll();
})();
