// JobRadar dashboard: theme switch, Blip's reactions, quick filters, keyboard moves and the
// Ctrl+K palette. Loaded in <head> so the saved theme applies before the page paints.
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
    "Press Ctrl K to jump to any job I've found.",
    "Try j and k to move down the list, Enter to open.",
    "Boop. That tickles.",
  ];
  let line = 0;
  const typing = (t) =>
    t instanceof HTMLInputElement || t instanceof HTMLTextAreaElement || t instanceof HTMLSelectElement;

  document.addEventListener("DOMContentLoaded", () => {
    // ---- theme ----
    const themeBtn = document.getElementById("theme");
    const showTheme = (mode) => {
      if (!themeBtn) return;
      themeBtn.querySelector("use").setAttribute("href", ICONS[mode]);
      themeBtn.querySelector("span").textContent = LABELS[mode];
      themeBtn.setAttribute("aria-label", `Theme: ${LABELS[mode]}`);
    };
    showTheme(read());
    themeBtn?.addEventListener("click", () => {
      const next = MODES[(MODES.indexOf(read()) + 1) % MODES.length];
      try { localStorage.setItem(KEY, next); } catch { /* private window: this page only */ }
      apply(next);
      showTheme(next);
    });

    // ---- Blip ----
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

    // ---- quick filters (Today) ----
    const list = document.querySelector("[data-list]");
    const rows = () => [...(list?.querySelectorAll(".row") ?? [])].filter((r) => !r.hidden);
    const count = document.querySelector("[data-count]");
    document.querySelectorAll("[data-filter]").forEach((pill) => {
      pill.addEventListener("click", () => {
        const tag = pill.dataset.filter;
        document.querySelectorAll("[data-filter]").forEach((p) => p.setAttribute("aria-pressed", String(p === pill)));
        list?.querySelectorAll(".row").forEach((r) => {
          r.hidden = tag !== "all" && !(r.dataset.tags || "").split(" ").includes(tag);
        });
        if (count) count.textContent = `${rows().length} shown`;
        select(-1);
      });
    });

    // ---- j / k / Enter on lists ----
    let at = -1;
    const select = (i) => {
      const all = rows();
      all.forEach((r) => r.classList.remove("is-active"));
      at = all.length ? Math.max(-1, Math.min(i, all.length - 1)) : -1;
      if (at >= 0) {
        all[at].classList.add("is-active");
        all[at].scrollIntoView({ block: "nearest", behavior: "smooth" });
      }
    };

    // ---- Ctrl+K palette ----
    const palette = document.getElementById("palette");
    const q = document.getElementById("palette-q");
    const results = document.getElementById("palette-list");
    const hint = document.querySelector("[data-palette-hint]");
    let picks = [];
    let pick = 0;
    let timer = 0;
    const draw = () => {
      results.replaceChildren(...picks.map((job, i) => {
        const li = document.createElement("li");
        li.setAttribute("role", "option");
        li.setAttribute("aria-selected", String(i === pick));
        const a = document.createElement("a");
        a.href = `/jobs/${encodeURIComponent(job.id)}`;
        const score = document.createElement("b");
        score.className = "p-score";
        score.textContent = job.score ?? "–";
        const text = document.createElement("span");
        text.className = "p-text";
        const role = document.createElement("strong");
        role.textContent = job.role;
        const sub = document.createElement("small");
        sub.textContent = [job.company, job.status].filter(Boolean).join(" · ");
        text.append(role, sub);
        a.append(score, text);
        li.append(a);
        return li;
      }));
      results.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: "nearest" });
    };
    const find = async () => {
      const words = q.value.trim();
      if (!words) { picks = []; draw(); hint.hidden = false; return; }
      try {
        const res = await fetch(`/api/search?q=${encodeURIComponent(words)}`);
        picks = (await res.json()).jobs;
      } catch { picks = []; }
      pick = 0;
      hint.hidden = picks.length > 0;
      if (!picks.length) hint.textContent = `No job matches “${words}”.`;
      draw();
    };
    const openPalette = () => {
      if (!palette || palette.open) return;
      palette.showModal();
      q.value = "";
      picks = [];
      draw();
      hint.hidden = false;
      hint.textContent = "Type a company or role. ↑ ↓ to pick, Enter to open.";
      q.focus();
    };
    document.querySelectorAll("[data-palette-open]").forEach((b) => b.addEventListener("click", openPalette));
    q?.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(find, 120); });
    q?.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        if (picks.length) { pick = (pick + (e.key === "ArrowDown" ? 1 : picks.length - 1)) % picks.length; draw(); }
      } else if (e.key === "Enter") {
        e.preventDefault();
        if (picks[pick]) window.location.href = `/jobs/${encodeURIComponent(picks[pick].id)}`;
      }
    });
    palette?.addEventListener("click", (e) => { if (e.target === palette) palette.close(); });

    // ---- keys ----
    let g = false;
    document.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openPalette(); return; }
      if (e.ctrlKey || e.metaKey || e.altKey || typing(e.target) || palette?.open) return;
      if (g) {
        g = false;
        const to = { t: "/", a: "/applications", j: "/jobs" }[e.key];
        if (to) { e.preventDefault(); window.location.href = to; }
        return;
      }
      if (e.key === "g") { g = true; setTimeout(() => { g = false; }, 900); return; }
      if (e.key === "/") {
        e.preventDefault();
        const search = document.getElementById("q");
        if (search) search.focus(); else window.location.href = "/jobs#search";
      } else if (e.key === "j") { select(at + 1); }
      else if (e.key === "k") { select(at - 1); }
      else if (e.key === "Enter" && at >= 0) {
        const row = rows()[at];
        if (row?.dataset.href) window.location.href = row.dataset.href;
      }
    });
    if (window.location.hash === "#search") document.getElementById("q")?.focus();
  });
})();
