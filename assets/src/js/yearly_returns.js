(function () {
  function boot() {
    var table = document.querySelector("table.returns-yearly__table");
    if (!table) return;
    var toggle = document.querySelector(".returns-yearly__toggle");
    if (!toggle) return;
    var total = toggle.getAttribute("data-total") || "";
    var showLabel = "Show all " + total + " years";
    var hideLabel = "Show fewer years";
    toggle.addEventListener("click", function () {
      var open = table.getAttribute("data-expanded") === "true";
      if (open) {
        table.removeAttribute("data-expanded");
        toggle.setAttribute("aria-expanded", "false");
        toggle.textContent = showLabel;
      } else {
        table.setAttribute("data-expanded", "true");
        toggle.setAttribute("aria-expanded", "true");
        toggle.textContent = hideLabel;
      }
    });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
