// Apply the stored theme and language before first paint: the theme to
// avoid a flash, the language because <html lang> decides which CJK face the
// font stacks fall through to.
(() => {
  try {
    const t = localStorage.getItem("agentgui-theme");
    if (t === "light" || (t === null && matchMedia("(prefers-color-scheme: light)").matches)) {
      document.documentElement.dataset.theme = "light";
    }
    const l = localStorage.getItem("agentgui-lang");
    if (l === "zh" || l === "en") document.documentElement.lang = l;
  } catch { /* storage may be unavailable; the defaults are fine */ }
})();
