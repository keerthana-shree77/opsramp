/* Keeps the compliance matrix current.

   The grid itself is rendered by the server, so there is one rendering path
   rather than two that can drift apart. This polls for progress, updates the
   status line live, and reloads once another account has been measured so its
   cells appear.

   Polling continues while the page is idle, slowly: a refresh runs on a timer
   in the background, and someone who left the page open should see it arrive
   rather than find stale figures the next time they look. */
(function () {
  "use strict";

  var card = document.getElementById("overview-card");
  if (!card) { return; }

  var pollUrl = card.getAttribute("data-poll-url");
  var lastDone = parseInt(card.getAttribute("data-done"), 10) || 0;
  var wasRunning = card.getAttribute("data-status") === "running";

  var statusLine = document.getElementById("sweep-status");
  var bar = document.getElementById("sweep-bar");
  var fill = document.getElementById("sweep-fill");

  /* Timestamps are rendered server-side as epochs and shown in the reader's
     own locale, which the server has no way to know. */
  function localise(id) {
    var stamp = document.getElementById(id);
    if (!stamp) { return; }
    var epoch = parseFloat(stamp.getAttribute("data-epoch"));
    if (!isNaN(epoch) && epoch > 0) {
      stamp.textContent = new Date(epoch * 1000).toLocaleString();
    }
  }
  localise("last-refreshed");
  localise("last-read");
  localise("next-refresh");

  Array.prototype.slice.call(document.querySelectorAll(".meter-fill")).forEach(
    function (meter) {
      var rate = parseFloat(meter.getAttribute("data-rate"));
      if (!isNaN(rate)) {
        meter.style.width = Math.max(0, Math.min(100, rate)) + "%";
      }
    }
  );

  function show(element, visible) {
    if (element) { element.classList.toggle("hidden", !visible); }
  }

  /* The recipes an account is judged by. The menu is a plain form and works
     without any of this; what the script adds is the summary keeping up with
     the boxes as they are ticked, so the count is right before Apply is
     pressed, and closing a menu when another is opened. */
  var menus = Array.prototype.slice.call(
    document.querySelectorAll("details.recipe-menu")
  );

  function describe(menu) {
    var label = menu.querySelector(".recipe-menu-label");
    if (!label) { return; }
    var ticked = Array.prototype.slice.call(
      menu.querySelectorAll('input[type="checkbox"]:checked')
    ).map(function (box) {
      var text = box.parentNode.querySelector("span");
      return text ? text.textContent.trim() : box.value;
    });
    if (!ticked.length) {
      label.textContent = "Automatic";
    } else if (ticked.length === 1) {
      label.textContent = ticked[0];
    } else {
      label.textContent = ticked[0] + " ";
      var more = document.createElement("span");
      more.className = "recipe-menu-more";
      more.textContent = "+" + (ticked.length - 1);
      label.appendChild(more);
    }
  }

  menus.forEach(function (menu) {
    menu.addEventListener("change", function () { describe(menu); });
    menu.addEventListener("toggle", function () {
      if (!menu.open) { return; }
      menus.forEach(function (other) {
        if (other !== menu) { other.open = false; }
      });
    });
  });

  /* "Add a new recipe" opens the upload form rather than navigating away
     from a half-made choice. */
  Array.prototype.slice.call(
    document.querySelectorAll('.recipe-menu-actions a[href="#add-recipe"]')
  ).forEach(function (link) {
    link.addEventListener("click", function (event) {
      var target = document.getElementById("add-recipe");
      if (!target) { return; }
      event.preventDefault();
      if (target.tagName === "DETAILS") { target.open = true; }
      target.scrollIntoView({ behavior: "smooth", block: "center" });
      var file = target.querySelector('input[type="file"]');
      if (file) { file.focus(); }
    });
  });

  var BUSY_DELAY = 4000;
  var IDLE_DELAY = 30000;
  var failures = 0;

  function poll() {
    fetch(pollUrl, { headers: { Accept: "application/json" }, credentials: "same-origin" })
      .then(function (response) {
        if (!response.ok) { throw new Error("progress unavailable"); }
        return response.json();
      })
      .then(function (state) {
        failures = 0;
        var running = state.status === "running";

        show(statusLine, running);
        show(bar, running);
        if (running && statusLine) {
          statusLine.textContent = "Measuring " + (state.message || "") + "…";
        }
        if (running && fill && state.total_count) {
          var percent = Math.round((state.done_count / state.total_count) * 100);
          fill.style.width = percent + "%";
          bar.setAttribute("aria-valuenow", String(percent));
        }

        /* Another account has finished, or a sweep has just ended: pull the
           freshly rendered grid rather than rebuilding it here. */
        if (state.done_count !== lastDone || (wasRunning && !running)) {
          window.location.reload();
          return;
        }
        wasRunning = running;
        window.setTimeout(poll, running ? BUSY_DELAY : IDLE_DELAY);
      })
      .catch(function () {
        failures += 1;
        /* Back off rather than hammer: a portal that is down stays down for
           longer than one poll, and the page is still readable meanwhile. */
        if (failures < 10) {
          window.setTimeout(poll, IDLE_DELAY * Math.min(failures, 4));
        }
      });
  }

  window.setTimeout(poll, wasRunning ? BUSY_DELAY : IDLE_DELAY);
})();
