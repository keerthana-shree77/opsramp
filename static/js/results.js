/* Client-side filtering and search over the comparison table. */
(function () {
  "use strict";

  var table = document.getElementById("results-table");
  var counter = document.getElementById("row-count");
  if (!table || !counter) { return; }

  var rows = Array.prototype.slice.call(table.tBodies[0].rows);
  var total = Number(counter.getAttribute("data-total")) || rows.length;
  var selects = Array.prototype.slice.call(document.querySelectorAll("#filters select[data-field]"));
  var search = document.getElementById("f-search");
  var reset = document.getElementById("f-reset");

  var timer = null;

  /* The compliance matrix links here with the filters it wants already
     chosen, so a cell opens exactly the rows it stands for. A value that is
     not among the options is ignored rather than silently hiding everything. */
  function applyQueryFilters() {
    var params = new URLSearchParams(window.location.search);
    var applied = [];
    selects.forEach(function (select) {
      var wanted = params.get(select.getAttribute("data-field"));
      if (!wanted) { return; }
      var match = Array.prototype.slice.call(select.options).filter(function (o) {
        return o.value === wanted;
      })[0];
      if (match) {
        select.value = wanted;
        applied.push(wanted);
      }
    });
    var term = params.get("q");
    if (term && search) {
      search.value = term;
      applied.push(term);
    }
    return applied;
  }

  function apply() {
    var term = (search && search.value ? search.value : "").trim().toLowerCase();
    var criteria = selects
      .map(function (select) {
        return { field: select.getAttribute("data-field"), value: select.value };
      })
      .filter(function (item) { return item.value !== ""; });

    var visible = 0;
    rows.forEach(function (row) {
      if (!row.hasAttribute("data-result")) { return; }
      var ok = criteria.every(function (item) {
        return row.getAttribute("data-" + item.field) === item.value;
      });
      if (ok && term) {
        ok = (row.getAttribute("data-search") || "").indexOf(term) !== -1;
      }
      row.hidden = !ok;
      if (ok) { visible += 1; }
    });

    counter.textContent = visible + " of " + total + " rows";
  }

  selects.forEach(function (select) { select.addEventListener("change", apply); });

  if (search) {
    search.addEventListener("input", function () {
      window.clearTimeout(timer);
      timer = window.setTimeout(apply, 120);
    });
  }

  if (reset) {
    reset.addEventListener("click", function () {
      selects.forEach(function (select) { select.value = ""; });
      if (search) { search.value = ""; }
      apply();
    });
  }

  /* Clicking a summary card filters by that result. */
  Array.prototype.slice.call(document.querySelectorAll(".summary-card")).forEach(function (card) {
    var status = card.getAttribute("data-status");
    if (!status) { return; }
    card.addEventListener("click", function () {
      var select = document.getElementById("f-result");
      if (select) { select.value = status; apply(); }
    });
  });

  var preset = applyQueryFilters();
  apply();

  if (preset.length) {
    var note = document.createElement("p");
    note.className = "hint filter-note";
    note.textContent =
      "Filtered to " + preset.join(" / ") + " from the compliance matrix. ";
    var clear = document.createElement("button");
    clear.type = "button";
    clear.className = "linkish";
    clear.textContent = "Show everything";
    clear.addEventListener("click", function () {
      selects.forEach(function (select) { select.value = ""; });
      if (search) { search.value = ""; }
      note.remove();
      apply();
    });
    note.appendChild(clear);
    counter.parentNode.insertBefore(note, counter);
  }
})();
