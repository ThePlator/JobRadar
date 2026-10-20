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

    const list = document.querySelector("[data-list]");
    const rows = () => [...(list?.querySelectorAll(".row") ?? [])].filter((r) => !r.hidden);
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

    // ---- Today: filter, find and sort, all at once ----
    const toolbar = document.querySelector("[data-toolbar]");
    if (toolbar && list) {
      let all = [...list.querySelectorAll(".row")];
      const pills = [...toolbar.querySelectorAll("[data-filter]")];
      const find = toolbar.querySelector("[data-find]");
      const sort = toolbar.querySelector("[data-sort]");
      const shown = document.querySelector("[data-count]");
      const none = document.querySelector("[data-none]");
      const why = document.querySelector("[data-none-why]");
      const NAMES = Object.fromEntries(pills.map((p) => [p.dataset.filter, p.querySelector("span").textContent]));
      const SAVE = "jobradar-today";
      const state = { tag: "all", sort: "score" };
      try { Object.assign(state, JSON.parse(sessionStorage.getItem(SAVE) || "{}")); } catch { /* fresh */ }
      const fromHash = window.location.hash.slice(1);
      if (NAMES[fromHash]) state.tag = fromHash;
      if (!NAMES[state.tag]) state.tag = "all";
      sort.value = ["score", "new", "old", "closes"].includes(state.sort) ? state.sort : "score";

      const has = (row, tag) => tag === "all" || (row.dataset.tags || "").split(" ").includes(tag);
      const matches = (row, words) => words.every((w) => row.dataset.text.includes(w));
      const order = {
        score: (a, b) => b.dataset.score - a.dataset.score || b.dataset.seen.localeCompare(a.dataset.seen),
        new: (a, b) => b.dataset.seen.localeCompare(a.dataset.seen),
        old: (a, b) => a.dataset.seen.localeCompare(b.dataset.seen),
        closes: (a, b) => a.dataset.closes - b.dataset.closes || b.dataset.score - a.dataset.score,
      };

      const apply = (animate) => {
        const words = find.value.toLowerCase().split(/\s+/).filter(Boolean);
        const found = all.filter((r) => matches(r, words));
        pills.forEach((p) => {
          const tag = p.dataset.filter;
          p.setAttribute("aria-pressed", String(tag === state.tag));
          const n = found.filter((r) => has(r, tag)).length;
          p.querySelector("[data-n]").textContent = n;
          p.classList.toggle("zero", n === 0);
        });
        const visible = [];
        all.forEach((r) => {
          r.hidden = !(has(r, state.tag) && matches(r, words));
          if (!r.hidden) visible.push(r);
        });
        [...all].sort(order[state.sort]).forEach((r) => list.append(r));
        if (animate) {
          visible.forEach((r, i) => {
            r.classList.remove("enter");
            void r.offsetWidth;
            r.style.setProperty("--i", String(Math.min(i, 12)));
            r.classList.add("enter");
          });
        }
        const filtered = state.tag !== "all" || words.length;
        shown.textContent = filtered
          ? `Showing ${visible.length} of ${all.length}` + (state.tag !== "all" ? ` · ${NAMES[state.tag]}` : "") + (words.length ? ` · “${find.value.trim()}”` : "")
          : `Showing all ${all.length} · ${sort.selectedOptions[0].text.toLowerCase()}`;
        none.hidden = visible.length > 0;
        list.hidden = visible.length === 0;
        if (!visible.length && why) {
          why.textContent = words.length ? `No job here matches “${find.value.trim()}”.` : `None of these ${all.length} is tagged ${NAMES[state.tag].toLowerCase()}.`;
        }
        try { sessionStorage.setItem(SAVE, JSON.stringify({ tag: state.tag, sort: state.sort })); } catch { /* fine */ }
        history.replaceState(null, "", state.tag === "all" ? window.location.pathname : `#${state.tag}`);
        select(-1);
      };
      pills.forEach((p) => p.addEventListener("click", () => { state.tag = p.dataset.filter; apply(true); }));
      sort.addEventListener("change", () => { state.sort = sort.value; apply(true); });
      let t = 0;
      find.addEventListener("input", () => { clearTimeout(t); t = setTimeout(() => apply(false), 80); });
      find.addEventListener("keydown", (e) => { if (e.key === "Escape") { find.value = ""; apply(false); find.blur(); } });
      document.querySelector("[data-reset]")?.addEventListener("click", () => { state.tag = "all"; find.value = ""; apply(true); });
      apply(false);
      document.addEventListener("jobradar:changed", () => {
        all = [...list.querySelectorAll(".row")];
        apply(false);
      });
    }


    // ---- buttons: change a status without leaving the page ----
    const TOKEN = document.querySelector('meta[name="jobradar-token"]')?.content || "";
    const toastEl = document.querySelector("[data-toast]");
    const toastText = document.querySelector("[data-toast-text]");
    const undoBtn = document.querySelector("[data-undo]");
    let toastTimer = 0;
    let undoing = null;
    const toast = (message, undo = null, warn = false) => {
      if (!toastEl) return;
      toastText.textContent = message;
      toastEl.classList.toggle("warn", warn);
      undoing = undo;
      undoBtn.hidden = !undo;
      toastEl.hidden = false;
      toastEl.classList.remove("show");
      void toastEl.offsetWidth;
      toastEl.classList.add("show");
      clearTimeout(toastTimer);
      toastTimer = setTimeout(() => { toastEl.hidden = true; }, undo ? 8000 : 5000);
    };
    const send = async (url, fields) => {
      const res = await fetch(url, {
        method: "POST",
        headers: { "X-JobRadar-Token": TOKEN, "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams(fields),
      });
      try { return await res.json(); } catch { return { ok: false, message: `Something went wrong (${res.status}). Reload and try again.` }; }
    };
    // After a reload (board, job page), show the toast the change left behind.
    try {
      const left = JSON.parse(sessionStorage.getItem("jobradar-toast") || "null");
      sessionStorage.removeItem("jobradar-toast");
      if (left) toast(left.message, left.undo);
    } catch { /* fine */ }
    const reloadWith = (message, undo) => {
      try { sessionStorage.setItem("jobradar-toast", JSON.stringify({ message, undo })); } catch { /* fine */ }
      window.location.reload();
    };
    undoBtn?.addEventListener("click", async () => {
      if (!undoing) return;
      const { id, status } = undoing;
      undoBtn.disabled = true;
      const out = await send(`/jobs/${encodeURIComponent(id)}/status`, { status });
      undoBtn.disabled = false;
      if (out.ok) reloadWith(`Undone. ${out.message}`, null);
      else toast(out.message, null, true);
    });

    document.addEventListener("submit", async (e) => {
      const form = e.target;
      if (!(form instanceof HTMLFormElement) || !(form.matches("[data-act]") || form.matches("[data-set]"))) return;
      e.preventDefault();
      const button = form.querySelector("button");
      button.disabled = true;
      const fields = Object.fromEntries(new FormData(form));
      // getAttribute: a field named "action" hides form.action
      const url = form.getAttribute("action");
      const id = decodeURIComponent(url.split("/jobs/")[1].split("/")[0]);
      const out = await send(url, fields);
      button.disabled = false;
      if (!out.ok) { toast(out.message, null, true); return; }
      const undo = out.undo ? { id, status: out.undo } : null;
      const row = form.closest(".row");
      if (row && document.querySelector("[data-toolbar]")) {  // Today lists only untouched jobs
        row.classList.add("leaving");
        const badge = document.querySelector('.nav a[href="/"] .badge');
        if (badge) {
          const left = Math.max(0, Number(badge.textContent) - 1);
          badge.textContent = String(left);
          badge.hidden = left === 0;
        }
        row.addEventListener("animationend", () => {
          row.remove();
          document.dispatchEvent(new Event("jobradar:changed"));
        }, { once: true });
        toast(out.message, undo);
      } else if (row && out.row) {  // All jobs: the row updates in place
        const fresh = document.createElement("template");
        fresh.innerHTML = out.row.trim();
        const next = fresh.content.firstElementChild;
        next.classList.add("flash");
        row.replaceWith(next);
        toast(out.message, undo);
      } else {
        reloadWith(out.message, undo);  // the board and the job page re-render
      }
    });

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
        const to = { t: "/", a: "/applications", j: "/jobs", d: "/digest" }[e.key];
        if (to) { e.preventDefault(); window.location.href = to; }
        return;
      }
      if (e.key === "g") { g = true; setTimeout(() => { g = false; }, 900); return; }
      if (e.key === "/") {
        e.preventDefault();
        const search = document.getElementById("q") || document.getElementById("find");
        if (search) search.focus(); else window.location.href = "/jobs#search";
      } else if (e.key === "j") { select(at + 1); }
      else if (e.key === "k") { select(at - 1); }
      else if (e.key === "Enter" && at >= 0) {
        const row = rows()[at];
        if (row?.dataset.href) window.location.href = row.dataset.href;
      }
    });
    // All jobs: changing the sort or Show applies it straight away
    document.querySelectorAll("[data-autosubmit], #show").forEach((el) => {
      el.addEventListener("change", () => el.form?.requestSubmit());
    });
    if (window.location.hash === "#search") document.getElementById("q")?.focus();
  });
})();
