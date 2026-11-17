// Runs before the page is drawn: the saved theme, else the OS setting (no flash of the wrong theme).
(function () {
  var t = null;
  try { t = localStorage.getItem("sc_theme"); } catch (e) { t = null; }
  if (t !== "light" && t !== "dark") {
    t = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  document.documentElement.setAttribute("data-theme", t);
})();
