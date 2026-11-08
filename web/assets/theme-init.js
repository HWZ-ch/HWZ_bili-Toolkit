(() => {
  try {
    const mode = localStorage.getItem("hwz-theme") || "light";
    const dark = mode === "dark" || (mode === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    document.documentElement.style.colorScheme = dark ? "dark" : "light";
  } catch (_) { /* ignore */ }
})();
