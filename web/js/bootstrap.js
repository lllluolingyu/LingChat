// Apply the stored theme before first paint to avoid a flash.
(() => {
  try {
    const t = localStorage.getItem("lingchat-theme");
    if (t === "light" || (t === null && matchMedia("(prefers-color-scheme: light)").matches)) {
      document.documentElement.dataset.theme = "light";
    }
  } catch { /* storage may be unavailable; default theme is fine */ }
})();
