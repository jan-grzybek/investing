/*
 * Pointer-driven scrubber for the return chart.
 *
 * A finger or cursor dragged across the plot reveals the date and
 * per-series cumulative return at that x-coordinate via a vertical
 * guide, a marker riding each curve, and a tooltip card carrying the
 * values plus the alpha between them.
 *
 * The script is data-agnostic: every figure that wants the
 * interaction declares `data-chart='{...}'` on its `.return-chart`
 * element, with `start` (ISO date), `totalDays`, `view` (the SVG
 * viewBox size), `plot` (the inset box the curves are drawn in,
 * in viewBox units), `yMin`/`yMax` (the multiplier domain mapped
 * onto the plot's vertical extent), and one entry per series in
 * `series` with `kind` (`jg` or `bench`), `label`, `x` (day offsets
 * from `start`) and `y` (return multiples).
 *
 * Keeping the data in the DOM rather than baking it into the script
 * means the payload is identical for every render, so its SHA-256 is
 * stable and can be pinned in CSP without re-hashing on each build.
 *
 * The plot inset matters: the axes reserve 64 units on the left and
 * 98 on the right of a 1000-unit viewBox, so a naive
 * pointer-x-over-container-width mapping would report a date roughly
 * two months off at either edge. Everything here converts through
 * the same box the SVG drew into.
 *
 * Linear interpolation between adjacent samples gives the tooltip
 * its values: the visual curve uses a Pchip spline, but linear is
 * faithful enough between 200 dense samples and keeps the script
 * small and dependency-free.
 *
 * `touch-action: pan-y` on the plot (set in CSS) lets vertical page
 * scrolling start from a touch on the chart while horizontal motion
 * is captured for scrubbing. `pointer*` events unify mouse and touch.
 */
(function () {
  var M = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  function fmtDate(ts) {
    var d = new Date(ts);
    return M[d.getUTCMonth()] + " " + d.getUTCDate() + ", " + d.getUTCFullYear();
  }

  function signed(value, unit) {
    var a = Math.abs(value);
    var n = Math.round(a * 10) / 10 >= 100 ? value.toFixed(0) : value.toFixed(1);
    return (value >= 0 ? "+" : "") + n + unit;
  }

  function fmtPct(mult) {
    return signed((mult - 1) * 100, "%");
  }

  function valueAt(s, d) {
    var x = s.x,
      y = s.y,
      n = x.length;
    if (n === 0) return null;
    if (d <= x[0]) return y[0];
    if (d >= x[n - 1]) return y[n - 1];
    var lo = 0,
      hi = n - 1;
    while (lo + 1 < hi) {
      var m = (lo + hi) >> 1;
      if (x[m] <= d) lo = m;
      else hi = m;
    }
    if (x[hi] === x[lo]) return y[lo];
    return y[lo] + (y[hi] - y[lo]) * ((d - x[lo]) / (x[hi] - x[lo]));
  }

  function init(fig) {
    var raw = fig.getAttribute("data-chart");
    if (!raw) return;
    var data;
    try {
      data = JSON.parse(raw);
    } catch (e) {
      return;
    }
    var plot = fig.querySelector(".return-chart__plot");
    if (!plot) return;
    var hover = plot.querySelector(".return-chart__hover");
    if (!hover) return;
    var guide = hover.querySelector(".return-chart__guide");
    var tip = hover.querySelector(".return-chart__tooltip");
    var dateEl = hover.querySelector(".return-chart__tooltip-date");
    var rowsEl = hover.querySelector(".return-chart__tooltip-rows");
    var deltaBar = hover.querySelector(".return-chart__hover-delta-bar");
    var deltaRow = hover.querySelector(".return-chart__tooltip-delta");

    var startMs = Date.parse(data.start + "T00:00:00Z");
    var totalDays = data.totalDays || 0;
    var W = (data.view && data.view.w) || 1000;
    var H = (data.view && data.view.h) || 372;
    var box = data.plot || { x0: 0, x1: W, y0: 0, y1: H };
    var yMin = data.yMin;
    var ySpan = data.yMax - data.yMin || 1;

    rowsEl.innerHTML = "";
    data.series.forEach(function (s) {
      var row = document.createElement("div");
      row.className = "return-chart__tooltip-row";
      var sw = document.createElement("span");
      sw.className = "return-chart__tooltip-swatch return-chart__tooltip-swatch--" + s.kind;
      var lbl = document.createElement("span");
      lbl.className = "return-chart__tooltip-label";
      // The swatch goes *inside* the label, not beside it. The row is
      // `display: contents` over a two-column grid, so every element
      // it contains is itself a grid item -- a third child per series
      // pushes that series' value into the next row's label slot and
      // scrambles the whole card from the second line down.
      lbl.appendChild(sw);
      lbl.appendChild(document.createTextNode(s.label));
      var val = document.createElement("span");
      val.className = "return-chart__tooltip-value return-chart__tooltip-value--" + s.kind;
      row.appendChild(lbl);
      row.appendChild(val);
      rowsEl.appendChild(row);
      s._val = val;
      var mk = document.createElement("div");
      mk.className = "return-chart__marker return-chart__marker--" + s.kind;
      hover.appendChild(mk);
      s._mk = mk;
    });

    function svgY(v) {
      return box.y1 - ((v - yMin) / ySpan) * (box.y1 - box.y0);
    }

    function update(clientX) {
      var r = plot.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) return;
      // One viewBox unit in container pixels.
      //
      // Height, not width. The SVG carries `preserveAspectRatio=
      // "xMinYMid slice"` so the wide frame can show the end-label
      // gutter and the phone frame can clip it away, which means the
      // rendered content is *wider* than the box whenever it is
      // clipped -- `r.width / W` would then under-report the scale and
      // every marker would drift left of its curve. The vertical axis
      // is never clipped in either frame (the element's aspect ratio
      // always divides VIEW_H exactly), so height is the honest
      // reference, and `xMin` means there is no offset to correct for.
      var scale = r.height / H;
      var left = box.x0 * scale;
      var right = box.x1 * scale;
      if (right <= left) return;
      var px = Math.max(left, Math.min(right, clientX - r.left));
      var frac = (px - left) / (right - left);
      var days = frac * totalDays;
      dateEl.textContent = fmtDate(startMs + days * 86400000);

      var x = box.x0 + frac * (box.x1 - box.x0);
      var xpx = x * scale;
      var ys = [];
      data.series.forEach(function (s) {
        var v = valueAt(s, days);
        ys.push(v);
        s._val.textContent = fmtPct(v);
        s._mk.style.left = xpx + "px";
        s._mk.style.top = svgY(v) * scale + "px";
      });

      if (deltaBar && deltaRow && ys.length >= 2) {
        var a = svgY(ys[0]);
        var b = svgY(ys[1]);
        var pp = (ys[0] - ys[1]) * 100;
        var color = pp >= 0 ? "var(--positive)" : "var(--negative)";
        deltaBar.style.left = xpx + "px";
        deltaBar.style.top = Math.min(a, b) * scale + "px";
        deltaBar.style.height = Math.abs(a - b) * scale + "px";
        deltaBar.style.setProperty("--delta-color", color);
        deltaRow.textContent = signed(pp, " pp");
        deltaRow.style.color = color;
      }

      guide.style.left = xpx + "px";
      if (xpx > r.width * 0.55) {
        tip.style.left = "auto";
        tip.style.right = r.width - xpx + "px";
        tip.style.transform = "translateX(-12px)";
      } else {
        tip.style.right = "auto";
        tip.style.left = xpx + "px";
        tip.style.transform = "translateX(12px)";
      }
    }

    function show() {
      hover.classList.add("is-active");
    }
    function hide() {
      hover.classList.remove("is-active");
    }

    plot.addEventListener("pointerenter", function (e) {
      if (e.pointerType === "mouse") {
        update(e.clientX);
        show();
      }
    });
    plot.addEventListener("pointermove", function (e) {
      update(e.clientX);
      show();
    });
    plot.addEventListener("pointerdown", function (e) {
      update(e.clientX);
      show();
      try {
        plot.setPointerCapture(e.pointerId);
      } catch (err) {}
    });
    plot.addEventListener("pointerup", function (e) {
      try {
        plot.releasePointerCapture(e.pointerId);
      } catch (err) {}
      if (e.pointerType !== "mouse") hide();
    });
    plot.addEventListener("pointerleave", hide);
    plot.addEventListener("pointercancel", hide);
  }

  function boot() {
    var figs = document.querySelectorAll(".return-chart[data-chart]");
    for (var i = 0; i < figs.length; i++) init(figs[i]);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
