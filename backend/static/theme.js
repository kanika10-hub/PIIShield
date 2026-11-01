/*
 * How the page is shown: light or dark theme, and the simple view or the
 * technical details. Each is the choice saved in this browser; the theme
 * otherwise follows the system setting, and the view starts simple.
 * Loaded in <head> so the page is drawn the right way from the start;
 * app.css holds the colours of each theme and what each view hides.
 */
(function () {
  "use strict";

  const KEY = "theme";
  const DETAILS_KEY = "details";
  const root = document.documentElement;
  const system = window.matchMedia("(prefers-color-scheme: dark)");

  // Storage can be blocked: the theme then follows the system setting,
  // and the view starts simple
  function saved(key) {
    try {
      return localStorage.getItem(key || KEY);
    } catch (error) {
      return null;
    }
  }

  function save(value, key) {
    try {
      localStorage.setItem(key || KEY, value);
    } catch (error) {
      // Not remembered after a reload
    }
  }

  // The button names the theme it switches to
  function apply(theme) {
    root.dataset.theme = theme;

    const button = document.getElementById("theme-button");
    if (button) button.textContent = theme === "dark" ? "Light mode" : "Dark mode";
  }

  // The technical details: block and finding IDs, every check, the JSON
  // files. The simple view hides them (.tech-only in app.css); app.js
  // hears "detailschange" to leave a tab that is no longer shown.
  function applyDetails(on) {
    root.classList.toggle("tech", on);

    const button = document.getElementById("details-button");

    if (button) {
      button.textContent = on ? "Hide technical details" : "Show technical details";
      button.setAttribute("aria-pressed", String(on));
    }

    document.dispatchEvent(new CustomEvent("detailschange", { detail: { on } }));
  }

  apply(saved() || (system.matches ? "dark" : "light"));
  applyDetails(saved(DETAILS_KEY) === "on");

  // Follow the system setting until a theme is chosen here
  system.addEventListener("change", (event) => {
    if (!saved()) apply(event.matches ? "dark" : "light");
  });

  document.addEventListener("DOMContentLoaded", () => {
    apply(root.dataset.theme);
    applyDetails(root.classList.contains("tech"));

    document.getElementById("theme-button").addEventListener("click", () => {
      const next = root.dataset.theme === "dark" ? "light" : "dark";
      save(next);
      apply(next);
    });

    document.getElementById("details-button").addEventListener("click", () => {
      const on = !root.classList.contains("tech");
      save(on ? "on" : "off", DETAILS_KEY);
      applyDetails(on);
    });
  });
})();
