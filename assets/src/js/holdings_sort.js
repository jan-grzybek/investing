/*
 * Click-to-sort for the Holdings and Closed positions tables.
 *
 * Each table carries `data-holdings-table="<scope>"`, one `<th
 * data-sort-key="..." data-sort-kind="text|number">` per sortable
 * column, and one `<tbody class="holdings__section">` per group
 * (equities / fixed income). Rows expose their keys as
 * `data-sort-<key>`.
 *
 * Sorting is per-`<tbody>`: rows are reordered inside their own
 * group so a sort can never shuffle a bond into the equity sleeve.
 * The group's band row stays pinned at the top of its section.
 *
 * `aria-sort` on the active `<th>` drives both the screen-reader
 * announcement and the visible indicator triangle (CSS), so there is
 * exactly one source of truth for "which column is sorted, which
 * way". The first click on a column picks the direction its datatype
 * reads naturally in -- A-Z for text, high-to-low for numbers --
 * and subsequent clicks toggle.
 */
(function () {
  function key(row, name) {
    return row.getAttribute("data-sort-" + name) || "";
  }

  function compare(a, b, name, kind, dir) {
    var av = key(a, name);
    var bv = key(b, name);
    var c;
    if (kind === "number") {
      var an = parseFloat(av);
      var bn = parseFloat(bv);
      var aNaN = isNaN(an);
      var bNaN = isNaN(bn);
      // Rows with no value for the active column sort last in both
      // directions: "unknown" is not a small number.
      if (aNaN && bNaN) return 0;
      if (aNaN) return 1;
      if (bNaN) return -1;
      c = an < bn ? -1 : an > bn ? 1 : 0;
    } else {
      c = av < bv ? -1 : av > bv ? 1 : 0;
    }
    return dir === "desc" ? -c : c;
  }

  function setup(table) {
    var sections = table.querySelectorAll("tbody.holdings__section");
    var heads = table.querySelectorAll("th[data-sort-key]");
    if (!sections.length || !heads.length) return;

    // Captured once so re-sorting is always a permutation of the
    // upstream order rather than of the last sort's output -- which
    // is what makes the tie-break stable.
    var original = [];
    for (var s = 0; s < sections.length; s++) {
      original.push(Array.prototype.slice.call(sections[s].querySelectorAll(".holdings__row")));
    }

    var state = { key: null, dir: null };

    function apply(name, kind, dir) {
      for (var i = 0; i < sections.length; i++) {
        var rows = original[i].slice();
        rows.sort(function (a, b) {
          var c = compare(a, b, name, kind, dir);
          if (c !== 0) return c;
          return original[i].indexOf(a) - original[i].indexOf(b);
        });
        for (var r = 0; r < rows.length; r++) sections[i].appendChild(rows[r]);
      }
    }

    function activate(th) {
      var name = th.getAttribute("data-sort-key");
      var kind = th.getAttribute("data-sort-kind");
      var dir;
      if (state.key === name) {
        dir = state.dir === "ascending" ? "descending" : "ascending";
      } else {
        dir = kind === "number" ? "descending" : "ascending";
      }
      apply(name, kind, dir === "descending" ? "desc" : "asc");
      state.key = name;
      state.dir = dir;
      for (var i = 0; i < heads.length; i++) {
        heads[i].setAttribute("aria-sort", heads[i] === th ? dir : "none");
      }
    }

    for (var h = 0; h < heads.length; h++) {
      (function (th) {
        var btn = th.querySelector(".holdings__sort");
        if (!btn) return;
        btn.addEventListener("click", function () {
          activate(th);
        });
      })(heads[h]);
    }
  }

  function boot() {
    var tables = document.querySelectorAll("table[data-holdings-table]");
    for (var i = 0; i < tables.length; i++) setup(tables[i]);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
