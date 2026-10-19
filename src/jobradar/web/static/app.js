// JobRadar dashboard: the theme switch, Blip's reactions and "/" to search. Loaded in <head>
// so the saved theme applies before the page paints.
(() => {
  const KEY = "jobradar-theme";
  const MODES = ["system", "light", "dark"];
  const LABELS = { system: "System", light: "Light", dark: "Dark" };
  const ICONS = { system: "#i-auto", light: "#i-sun", dark: "#i-moon" };
  const read = () => {
    try { return localStorage.getItem(KEY) || "system"; } catch { return "system"; }
  };
  const apply = (mode) => {
    if (mode === "system") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", mode);
  };
  apply(read());

  const LINES = [
    "Beep. I read every channel and board so you don't have to.",
    "Applied somewhere? Tap “I applied” in my email and I'll keep track.",
    "Ten quiet days after you apply, I'll draft a follow-up for you.",
    "Press / to search every job I've found.",
    "Boop. That tickles.",
  ];
  let line = 0;

  document.addEventListener("DOMContentLoaded", () => {
    const button = document.getElementById("theme");
    const show = (mode) => {
      if (!button) return;
      button.querySelector("use").setAttribute("href", ICONS[mode]);
      button.querySelector("span").textContent = LABELS[mode];
      button.setAttribute("aria-label", `Theme: ${LABELS[mode]}`);
    };
    show(read());
    button?.addEventListener("click", () => {
      const next = MODES[(MODES.indexOf(read()) + 1) % MODES.length];
      try { localStorage.setItem(KEY, next); } catch { /* private window: this page only */ }
      apply(next);
      show(next);
    });

    const say = document.querySelector("[data-say]");
    document.querySelectorAll(".blip-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const blip = btn.querySelector(".blip");
        blip.classList.remove("poke");
        void blip.getBoundingClientRect(); // restart the animation
        blip.classList.add("poke");
        if (say && btn.classList.contains("hello-blip")) {
          say.textContent = LINES[line++ % LINES.length];
        }
      });
    });

    document.addEventListener("keydown", (e) => {
      if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey) return;
      const t = e.target;
      if (t instanceof HTMLInputElement || t instanceof HTMLTextAreaElement || t instanceof HTMLSelectElement) return;
      e.preventDefault();
      const q = document.getElementById("q");
      if (q) q.focus();
      else window.location.href = "/jobs#search";
    });
    if (window.location.hash === "#search") document.getElementById("q")?.focus();
  });
})();
