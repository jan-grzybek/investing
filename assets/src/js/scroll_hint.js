/*
 * Edge fades for horizontally scrollable strips.
 *
 * Marks any `[data-scroll-hint]` element with `data-scroll` set to
 * `start`, `end` or `both`, naming the side(s) that still have content
 * beyond the edge. The attribute is removed entirely when everything
 * fits, so a strip that does not scroll carries no hint at all.
 *
 * WHY THIS IS JS AND NOT CSS
 *
 * The first version of this was pure CSS: four background layers, two
 * riding the content with `background-attachment: local` and two
 * pinned with `scroll`, the local pair covering the pinned pair at the
 * extremes. That is self-managing -- it needs no measurement and knows
 * on its own which side has more -- and it is why the strip that fits
 * showed nothing.
 *
 * It has one flaw, and it is the reason for this file: a background
 * paints *behind* content. The sort chips are opaque pills, so they
 * slid over the shadow rather than under it. The shadow darkened the
 * gaps between chips and left the chips themselves untouched, sliced
 * flat at the container edge. A shadow says "this passes beneath an
 * edge"; the chips passed on top of it, so the cue contradicted itself.
 *
 * A mask fixes that -- it acts on the rendered element, chips included
 * -- but masks have no equivalent of `background-attachment`, so a CSS
 * mask cannot know whether there is anything to scroll to. Applied
 * unconditionally it would fade the first chip of a strip that fits,
 * advertising content that does not exist, which is worse than the
 * problem it solves.
 *
 * Hence: the mask lives in CSS, the "is there more, and which way"
 * question is answered here. The strips are sort controls that do
 * nothing without JS anyway, so this costs nothing that was not
 * already conditional on scripting.
 */
(function () {
  function boot() {
    var strips = [].slice.call(document.querySelectorAll("[data-scroll-hint]"));
    if (!strips.length) return;

    strips.forEach(function (strip) {
      var queued = false;

      function measure() {
        queued = false;
        var max = strip.scrollWidth - strip.clientWidth;
        // A strip that fits carries no hint. This also covers the wide
        // frame, where the container query turns the strip back into a
        // table header row and there is nothing to scroll.
        if (max <= 1) {
          strip.removeAttribute("data-scroll");
          return;
        }
        // 1px of slack either end: sub-pixel scroll positions would
        // otherwise leave a hint showing at a hard extreme.
        var more_before = strip.scrollLeft > 1;
        var more_after = strip.scrollLeft < max - 1;
        strip.setAttribute(
          "data-scroll",
          more_before && more_after ? "both" : more_before ? "start" : "end",
        );
      }

      function schedule() {
        if (queued) return;
        queued = true;
        requestAnimationFrame(measure);
      }

      measure();
      strip.addEventListener("scroll", schedule, { passive: true });
      // Catches both the viewport changing and the container query
      // flipping the strip between a chip row and a table header.
      try {
        new ResizeObserver(schedule).observe(strip);
      } catch (e) {
        window.addEventListener("resize", schedule);
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
