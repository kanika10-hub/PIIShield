/*
 * Light or dark theme: the choice saved in this browser, else the
 * system setting. Loaded in <head> so the page is drawn in the right
 * theme from the start; app.css holds the colours of each theme.
 */
(function () {
  "use strict";

  const KEY = "theme";
  const root = document.documentElement;
  const system = window.matchMedia("(prefers-color-scheme: dark)");

  // Storage can be blocked: the theme then follows the system setting
  function saved() {
    try {
      return localStorage.getItem(KEY);
    } catch (error) {
      return null;
    }
  }

  function save(theme) {
    try {
      localStorage.setItem(KEY, theme);
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

  apply(saved() || (system.matches ? "dark" : "light"));

  // Follow the system setting until a theme is chosen here
  system.addEventListener("change", (event) => {
    if (!saved()) apply(event.matches ? "dark" : "light");
  });

  document.addEventListener("DOMContentLoaded", () => {
    apply(root.dataset.theme);

    document.getElementById("theme-button").addEventListener("click", () => {
      const next = root.dataset.theme === "dark" ? "light" : "dark";
      save(next);
      apply(next);
    });
  });
})();
