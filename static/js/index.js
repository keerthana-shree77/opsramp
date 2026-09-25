/* Carries the human-readable tenant name along with the tenant id, so the
   report header and the export filenames can show it. */
/* Multi-select helpers, and returning the form to its defaults after the
   results open in their own tab. */
(function () {
  "use strict";

  var form = document.getElementById("compare-form");
  if (!form) { return; }

  function each(selector, fn) {
    Array.prototype.slice.call(document.querySelectorAll(selector)).forEach(fn);
  }

  each("[data-select-all]", function (button) {
    button.addEventListener("click", function () {
      var select = document.getElementById(button.getAttribute("data-select-all"));
      if (!select) { return; }
      Array.prototype.slice.call(select.options).forEach(function (option) {
        option.selected = true;
      });
    });
  });

  each("[data-clear]", function (button) {
    button.addEventListener("click", function () {
      var select = document.getElementById(button.getAttribute("data-clear"));
      if (!select) { return; }
      Array.prototype.slice.call(select.options).forEach(function (option) {
        option.selected = false;
      });
    });
  });

  /* "All" is exclusive: picking it clears the specific categories, and picking
     a specific one clears "All". */
  var categories = document.getElementById("category");
  if (categories) {
    categories.addEventListener("change", function () {
      var options = Array.prototype.slice.call(categories.options);
      var all = options.filter(function (o) { return o.value === "all"; })[0];
      if (!all) { return; }
      if (all.selected && options.some(function (o) {
        return o.selected && o.value !== "all";
      })) {
        if (categories.dataset.lastAll === "1") {
          all.selected = false;
        } else {
          options.forEach(function (o) { o.selected = o.value === "all"; });
        }
      }
      categories.dataset.lastAll = all.selected ? "1" : "0";
    });
  }

  form.addEventListener("submit", function () {
    var button = form.querySelector('button[type="submit"]');
    if (button) {
      var original = button.textContent;
      button.disabled = true;
      button.textContent = "Opening results in a new tab…";
      /* The results open in their own tab, so this page stays usable: put it
         back to its defaults for the next run. */
      window.setTimeout(function () {
        form.reset();
        if (categories) {
          Array.prototype.slice.call(categories.options).forEach(function (o) {
            o.selected = o.value === "all";
          });
        }
        button.disabled = false;
        button.textContent = original;
      }, 1200);
    }
  });
})();
