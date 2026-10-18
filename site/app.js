// JobRadar site: Blip (eyes, moods, naps), the theme switch, the log ticker, the chat that
// reads a forwarded post, and the red pen. No libraries; nothing is sent anywhere.
(() => {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const esc = (s) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

  // ---- Blip: one inline copy per spot, so each can have its own mood --------------------
  const blipSvg = $("#blip").innerHTML;
  const blips = $$("svg.blip").map((svg) => { svg.innerHTML = blipSvg; return svg; });
  function mood(svg, name, ms = 1400) {
    svg.classList.remove("happy", "alarm");
    if (name) svg.classList.add(name);
    clearTimeout(svg._mood);
    if (name && ms) svg._mood = setTimeout(() => svg.classList.remove(name), ms);
  }
  // Eyes follow the pointer.
  let px = innerWidth / 2, py = innerHeight / 3, queued = false;
  addEventListener("pointermove", (e) => {
    px = e.clientX; py = e.clientY;
    if (!queued) { queued = true; requestAnimationFrame(look); }
  }, { passive: true });
  function look() {
    queued = false;
    for (const svg of blips) {
      if (svg.closest(".reading")) continue;
      const r = svg.getBoundingClientRect();
      if (r.bottom < 0 || r.top > innerHeight) continue;
      const dx = px - (r.left + r.width / 2), dy = py - (r.top + r.height * 0.4);
      const d = Math.hypot(dx, dy) || 1, k = Math.min(1, d / 300);
      $(".b-eyes", svg).style.transform = `translate(${(dx / d) * 9 * k}px, ${(dy / d) * 6 * k}px)`;
    }
  }
  look();
  // Blinks, at a natural random pace.
  function blink() {
    const svg = blips[Math.floor(Math.random() * blips.length)];
    svg.classList.add("blink");
    setTimeout(() => svg.classList.remove("blink"), 130);
    setTimeout(blink, 1200 + Math.random() * 2600);
  }
  if (!still) setTimeout(blink, 1500);

  // Waves hello; hearts when you click; dozes off when you leave it alone.
  function wave(svg) {
    if (still) return;
    svg.classList.remove("wave"); void svg.getBoundingClientRect(); svg.classList.add("wave");
    clearTimeout(svg._wave); svg._wave = setTimeout(() => svg.classList.remove("wave"), 2100);
  }
  function hearts(host, x, y) {
    if (still) return;
    for (let i = 0; i < 6; i++) {
      const h = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      h.setAttribute("class", "heart"); h.setAttribute("viewBox", "0 0 24 24"); h.setAttribute("aria-hidden", "true");
      h.innerHTML = '<use href="#heart"/>';
      h.style.left = `${x - 11 + (Math.random() - .5) * 40}px`; h.style.top = `${y - 11}px`;
      h.style.setProperty("--dx", `${(Math.random() - .5) * 90}px`);
      h.style.setProperty("--rot", `${(Math.random() - .5) * 60}deg`);
      h.style.animationDelay = `${i * 70}ms`;
      host.append(h);
      setTimeout(() => h.remove(), 1400 + i * 70);
    }
  }
  for (const svg of blips) {
    const host = svg.closest(".blip-btn");
    if (!host) continue;
    host.addEventListener("pointerenter", () => wave(svg));
    host.addEventListener("click", (e) => {
      const r = host.getBoundingClientRect();
      hearts(host, e.clientX - r.left || r.width / 2, e.clientY - r.top || r.height / 3);
      svg.classList.add("blush"); setTimeout(() => svg.classList.remove("blush"), 1200);
    });
  }
  let idle;
  const nap = () => blips.forEach((b) => b.classList.add("sleep"));
  function awake() {
    clearTimeout(idle);
    if (blips[0].classList.contains("sleep")) blips.forEach((b) => { b.classList.remove("sleep"); wave(b); });
    idle = setTimeout(nap, 25000);
  }
  for (const ev of ["pointermove", "keydown", "scroll", "touchstart"]) addEventListener(ev, awake, { passive: true });
  awake();
  setTimeout(() => wave(blips[0]), 600);

  // ---- light / dark -----------------------------------------------------------------------
  const themeBtn = $("#themeBtn"), root = document.documentElement;
  const dark = () => root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  const syncTheme = () => { themeBtn.setAttribute("aria-pressed", String(dark())); themeBtn.title = dark() ? "Switch to light" : "Switch to dark"; };
  themeBtn.addEventListener("click", () => {
    root.dataset.theme = dark() ? "light" : "dark";
    try { localStorage.setItem("jr-theme", root.dataset.theme); } catch { /* private mode: just this visit */ }
    syncTheme();
    say(dark() ? "Night mode. My eyes glow now." : "Good morning!");
    wave(blips[0]);
  });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", syncTheme);

  // ---- hero speech bubble -----------------------------------------------------------------
  const heroBlip = $("#heroBlip svg"), bubble = $("#heroBubble");
  function say(text) {
    bubble.textContent = text;
    bubble.classList.remove("pop"); void bubble.offsetWidth; bubble.classList.add("pop");
  }
  const LINES = ["Hi, I'm Blip.", "I read every new post in your job channels.", "Same job in three channels? One row.",
    "Senior and sales roles never reach you.", "I'll never call you an expert.", "Click me again. I like it."];
  let line = 0;
  $("#heroBlip").addEventListener("click", () => { mood(heroBlip, "happy"); say(LINES[++line % LINES.length]); });


  syncTheme();

  const top = $(".top");
  addEventListener("scroll", () => top.classList.toggle("scrolled", scrollY > 8), { passive: true });

  // ---- hero: the real log lines from 15 Oct, scrolling under Blip ---------------------------
  const LOG = [
    ["22:15:12", "new job", "job4freshers.co.in/apprentice-analyst-eclerx-walk-in"],
    ["22:15:13", "new job", "freshershunt.in/akamai-technical-academy-2026"],
    ["22:15:15", "new job", "rubrik.com/company/careers/departments/job.8256407"],
    ["22:15:17", "new job", "careers.qualcomm.com/careers/job/446719784235"],
    ["22:15:18", "job hidden", "eClerx: weak match (29)", true],
    ["22:15:43", "job matched", "Rubrik score=79 decision=alert"],
    ["22:15:49", "alert email sent", "jobs=1"],
    ["22:15:56", "job matched", "QualComm score=92 decision=alert"],
    ["22:20:24", "board posting skipped", "druva: Regional Vice President, not in India", true],
    ["22:22:25", "board posting skipped", "stripe: Account Executive, not in India", true],
    ["22:23:25", "board posting skipped", "okta: Senior Software Engineer, not in India", true],
    ["22:25:55", "alert email sent", "jobs=1"],
  ];
  const ticker = $("#ticker");
  let li = 0;
  function tick() {
    const [t, what, rest, dim] = LOG[li++ % LOG.length];
    const d = document.createElement("div");
    d.innerHTML = `<span class="t">${t}</span><span class="${dim ? "hid" : "k"}">${esc(what)}</span> ${esc(rest)}`;
    ticker.append(d);
    while (ticker.children.length > 8) ticker.firstElementChild.remove();
    if (!dim && what === "alert email sent") { mood(heroBlip, "alarm", 1500); }
    if (what === "job matched") { mood(heroBlip, "happy", 1200); }
  }
  for (let i = 0; i < 7; i++) tick();
  if (!still) setInterval(tick, 1600);

  // ---- forward a post: the reader, in a chat --------------------------------------------------
  const LABELS = {
    company: ["company", "company name", "organization", "organisation", "hiring company", "employer"],
    role: ["role", "job role", "post", "post name", "position", "job title", "designation", "profile", "job profile"],
    location: ["location", "locations", "job location", "work location"],
    batch: ["batch", "batches", "eligible batch", "passing year", "year of passing", "graduation year", "passout", "pass out"],
    eligibility: ["eligibility", "eligibility criteria", "qualification", "qualifications", "education", "degree"],
    experience: ["experience", "exp", "experience required", "work experience"],
    salary: ["salary", "ctc", "stipend", "package", "compensation"],
    deadline: ["last date", "last date to apply", "apply by", "apply before", "deadline", "closing date"],
  };
  const KIND = {};
  for (const [kind, words] of Object.entries(LABELS)) for (const w of words) KIND[w] = kind;
  const labelRe = new RegExp(
    "^(" + Object.keys(KIND).sort((a, b) => b.length - a.length).map((w) => w.replace(/[-/\\^$*+?.()|[\]{}]/g, "\\$&")).join("|") +
    ")(?:\\s*:|\\s+[-\\u2013\\u2014])\\s*(.+)$", "i");
  const plain = (line) => line.normalize("NFKC").replace(/[\p{Extended_Pictographic}️‍]/gu, "").replace(/^[\W_]+/u, "").replace(/\s+/g, " ").trim();
  const TECH = ["Python", "Java", "JavaScript", "TypeScript", "C++", "C#", "Go", "Rust", "Kotlin", "SQL", "NoSQL", "React", "Angular", "Vue", "Next.js",
    "Node.js", "Express", "Django", "Flask", "FastAPI", "Spring", "Spring Boot", "AWS", "Azure", "GCP", "Docker", "Kubernetes", "Git", "Linux",
    "PostgreSQL", "MySQL", "MongoDB", "Redis", "Kafka", "GraphQL", "REST", "Machine Learning", "Deep Learning", "TensorFlow", "PyTorch",
    "Pandas", "NumPy", "Power BI", "Tableau", "Excel", "HTML", "CSS", "Selenium", "DSA", "Data Structures"];
  const key = (s) => s.toLowerCase().replace(/\([^)]*\)/g, "").replace(/[^a-z0-9+#]/g, "")
    .replace(/^(js)$/, "javascript").replace(/^(ts)$/, "typescript").replace(/^(node|nodejs)$/, "nodejs")
    .replace(/^(reactjs)$/, "react").replace(/^(postgres)$/, "postgresql").replace(/^(golang)$/, "go");

  function read(text) {
    const found = {};
    const lines = text.split(/\n/).map(plain).filter(Boolean);
    for (const l of lines) {
      const m = l.match(labelRe);
      if (m && !/^http/i.test(m[2])) found[KIND[m[1].toLowerCase()]] ??= m[2].trim();
    }
    for (const l of lines.slice(0, 3)) {  // "Epicor is hiring for Product Developer"
      const m = l.match(/^(.+?)\s+is\s+hiring\s+(?:for\s+)?(?:an?\s+)?(.+)$/i);
      if (m && !/http/i.test(l) && !/^(in|at|for|freshers|graduates|candidates)\b/i.test(m[2])) {
        found.company ??= m[1]; found.role ??= m[2].replace(/[!.]+$/, "");
      }
    }
    const years = [...(found.batch || found.eligibility || text).matchAll(/\b(20[1-3]\d)\b/g)].map((m) => +m[1]);
    const exp = found.experience || (text.match(/\b(\d+\s*(?:-|to)\s*\d+|\d+\+?)\s*(?:years?|yrs?)\b/i) || [])[0] || (/\bfreshers?\b/i.test(text) ? "Freshers" : "");
    const link = (text.match(/https?:\/\/\S+/) || [])[0] || "";
    const tech = TECH.filter((t) => new RegExp(`(^|[^A-Za-z0-9+#.])${t.replace(/[+.#]/g, "\\$&")}(?![A-Za-z0-9+#])`, "i").test(text));
    // "Spring Boot" also matches "Spring": keep the longer name only.
    const uniq = tech.filter((t) => !tech.some((o) => o !== t && o.toLowerCase().includes(t.toLowerCase())));
    return { found, years: [...new Set(years)].sort(), exp, link, tech: uniq };
  }

  const SAMPLES = [
    "🔥 Hiring Alert 🔥\n\nCompany: Acme Fintech\nRole: Software Engineer Intern\nLocation: Bengaluru (Hybrid)\nBatch: 2025 / 2026\nEligibility: B.Tech / BE (CS, IT, ECE, EEE)\nSkills: Python, SQL, REST APIs, Git\nStipend: 40k per month\nLast date: 30 Oct\n\nApply: https://jobs.example.com/acme/12345",
    "Northwind is hiring for Backend Developer!\n\nFreshers can apply. Work with Java, Spring Boot, MySQL and Docker.\nLocation: Pune\n\nhttps://careers.example.com/northwind/backend-dev",
    "𝗚𝗿𝗲𝗮𝘁 𝗢𝗽𝗽𝗼𝗿𝘁𝘂𝗻𝗶𝘁𝘆 🚀\n➡️ 𝗣𝗼𝘀𝘁 𝗡𝗮𝗺𝗲 : Associate Data Analyst\n➡️ 𝗢𝗿𝗴𝗮𝗻𝗶𝘀𝗮𝘁𝗶𝗼𝗻 - Contoso Analytics\n📍 Job Location : Remote, India\n🎓 Passout : 2024, 2025, 2026\n💼 Exp : 0-1 years\nTools: Excel, SQL, Power BI, Python\n👉 https://example.com/apply/contoso",
  ];
  // Which field a single line gives, so the line can get that field's highlighter colour.
  function lineKind(raw) {
    const l = plain(raw);
    if (!l) return null;
    const m = l.match(labelRe);
    if (m && !/^http/i.test(m[2])) return KIND[m[1].toLowerCase()];
    if (/^(.+?)\s+is\s+hiring\b/i.test(l) && !/http/i.test(l)) return "hiring";
    if (/https?:\/\//.test(raw)) return "link";
    return null;
  }
  const NAMES = { hiring: "company, role", company: "company", role: "role", location: "place", batch: "batch", eligibility: "eligibility",
    experience: "experience", salary: "pay", deadline: "last date", link: "apply link" };
  const lines = $("#chatLog"), ta = $("#postText"), readout = $("#readout"), desk = $(".postdesk"), peek = $(".blip.peek");
  let current = SAMPLES[0], run = 0;
  function show(text) {
    current = text;
    const r = read(text);
    const mine = new Set($("#mySkills").value.split(",").map((s) => key(s.trim())).filter(Boolean));
    const batch = +$("#myBatch").value, batchOk = r.years.length ? r.years.includes(batch) : null;
    const techRe = r.tech.length ? new RegExp(`(^|[^A-Za-z0-9+#.])(${[...r.tech].sort((x, y) => y.length - x.length)
      .map((t) => t.replace(/[+.#]/g, "\\$&")).join("|")})(?![A-Za-z0-9+#])`, "gi") : null;
    const raw = text.split("\n").slice(0, 24);
    lines.innerHTML = raw.map((l) => {
      const k = lineKind(l);
      let h = esc(l.length > 140 ? l.slice(0, 140) + "…" : l) || "&nbsp;";
      if (techRe) h = h.replace(techRe, (_, pre, t) => `${pre}<span class="sk ${mine.has(key(t)) ? "have" : "miss"}">${t}</span>`);
      const years = k === "batch" && batchOk !== null && /\b20[1-3]\d\b/.test(l), skills = !k && h.includes('class="sk ');
      const tag = k ? `<span class="tag">${NAMES[k]}${years ? (batchOk ? " ✓" : " ✗") : ""}</span>` : skills ? '<span class="tag">skills</span>' : "";
      const cls = k ? "hl k-" + k : skills ? "skills" : l.trim() ? "skip" : "gap";
      return `<p class="ln ${cls}${years && !batchOk ? " bad" : ""}"><span class="tx">${h}</span>${tag}</p>`;
    }).join("");
    // The highlighter goes down the post one line at a time; Blip's eyes follow it.
    const me = ++run, marked = $$(".ln.hl, .ln.skip, .ln.skills", lines);
    desk.classList.add("reading");
    marked.forEach((ln, i) => {
      if (still) { ln.classList.add("on"); return; }
      setTimeout(() => {
        if (me !== run) return;
        ln.classList.add("on");
        const pr = peek.getBoundingClientRect(), lr = ln.getBoundingClientRect();
        const dy = lr.top - (pr.top + pr.height * .4), dx = lr.left + 120 - (pr.left + pr.width / 2), d = Math.hypot(dx, dy) || 1;
        $(".b-eyes", peek).style.transform = `translate(${(dx / d) * 9}px, ${(dy / d) * 6}px)`;
      }, 160 + i * 170);
    });
    const have = r.tech.filter((t) => mine.has(key(t))), miss = r.tech.filter((t) => !mine.has(key(t)));
    const got = [r.found.company, r.found.role, r.found.location, r.years.length, r.exp, r.link].filter(Boolean).length;
    let say = r.tech.length ? `You have ${have.length} of the ${r.tech.length} skills it names.` + (miss.length ? ` Missing: ${miss.join(", ")}.` : "") : "It doesn't name any skills I know.";
    say += batchOk === null ? " No batch stated." : batchOk ? ` Your ${batch} batch is in.` : ` ${batch} isn't listed, so I'd hide this one.`;
    readout.classList.remove("on");
    setTimeout(() => {
      if (me !== run) return;
      desk.classList.remove("reading");
      readout.innerHTML = `${esc(say)}<small>${got} of 6 details found without any AI.</small>`;
      readout.classList.add("on");
      mood(peek, batchOk === false ? "alarm" : "happy", 1600);
    }, still ? 0 : 260 + marked.length * 170);
  }
  const pick = (i) => $$(".samples button").forEach((b) => b.setAttribute("aria-pressed", String(+b.dataset.sample === i)));
  $$(".samples button").forEach((b) => b.addEventListener("click", () => { pick(+b.dataset.sample); show(SAMPLES[+b.dataset.sample]); }));
  $("#chatForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const text = ta.value.trim();
    if (!text) { ta.focus(); ta.placeholder = "Paste a job post first"; return; }
    pick(-1); show(text);
  });
  ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) $("#chatForm").requestSubmit(); });
  let typingTimer;
  for (const id of ["#mySkills", "#myBatch"]) $(id).addEventListener("input", () => { clearTimeout(typingTimer); typingTimer = setTimeout(() => show(current), 400); });
  // The first post is read on load, so the desk is never empty; the highlighter replays once
  // when the section first scrolls into view.
  show(current);
  new IntersectionObserver((es, o) => { if (es.some((e) => e.isIntersecting)) { o.disconnect(); show(current); } }, { threshold: .15 }).observe(desk);

  // ---- the red pen (the rules in resume/validate.py, simplified) ------------------------------
  const ORIGINAL = $("#orig").textContent;
  const BANNED = ["expert", "world-class", "guru", "ninja", "rockstar", "cutting-edge", "passionate"];
  const SIZE = /\b(doubl\w*|tripl\w*|halv\w*|\d+(\.\d+)?\s?x)\b/gi;
  const nums = (s) => new Set((s.match(/\d[\d,]*(?:\.\d+)?\+?%?/g) || []).map((x) => x.replace(/,/g, "")));
  const techIn = (s) => new Set(TECH.filter((t) => new RegExp(`(^|[^A-Za-z0-9+#.])${t.replace(/[+.#]/g, "\\$&")}(?![A-Za-z0-9+#])`, "i").test(s)).map((t) => t));
  // Each flag is the words to circle and Blip's note in the margin.
  function check(text) {
    const flags = [], low = ORIGINAL.toLowerCase();
    const s0 = new Set((ORIGINAL.match(SIZE) || []).map((x) => x.toLowerCase()));
    for (const x of text.match(SIZE) || []) if (!s0.has(x.toLowerCase())) flags.push({ t: x, note: `${x}? You never measured that.` });
    const n0 = nums(ORIGINAL);
    for (const x of nums(text)) if (!n0.has(x) && !flags.some((f) => f.t.includes(x))) flags.push({ t: x, note: `${x}? Not in your bullet.` });
    const t0 = techIn(ORIGINAL);
    for (const x of techIn(text)) if (!t0.has(x)) flags.push({ t: x, note: `${x}? You never used it here.` });
    for (const b of BANNED) {
      const m = text.match(new RegExp(`\\b${b}\\b`, "i"));
      if (m && !low.includes(b)) flags.push({ t: m[0], note: `"${m[0]}" is a brag word.` });
    }
    return flags;
  }
  const ring = (t) => `<span class="ring">${t}<svg viewBox="0 0 100 40" preserveAspectRatio="none" aria-hidden="true"><path pathLength="1" d="M8 24 C2 8 60 1 94 12 C102 22 70 39 30 36 C12 34 6 26 14 16"/></svg></span>`;
  const draft = $("#draft"), marks = $("#marks"), verdict = $("#verdict"), proof = $(".proof");
  let penRun = 0;
  function proofread(i) {
    const text = $$(".drafts button")[i].textContent, flags = check(text), me = ++penRun;
    $$(".drafts button").forEach((b, j) => b.setAttribute("aria-pressed", String(j === i)));
    let h = esc(text);
    for (const f of flags) h = h.replace(new RegExp(`(^|[^A-Za-z0-9.])(${f.t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})(?![A-Za-z0-9])`, "i"), (_, pre, w) => pre + ring(w));
    draft.innerHTML = h;
    marks.innerHTML = flags.length ? flags.map((f) => `<li>${esc(f.note)}</li>`).join("") : '<li class="ok">✓ Same 9 s and 1.2 s.</li>';
    // Notes start level with the bullet they are about (stacked under it on phones).
    marks.style.paddingTop = matchMedia("(max-width: 520px)").matches ? "" : `${$(".pr-target").offsetTop - 4}px`;
    proof.classList.remove("judged", "kept", "sent-back", "go");
    verdict.textContent = "";
    void proof.offsetWidth;
    proof.classList.add("go");
    // Circles first, then the notes, then the verdict.
    $$(".ring", draft).forEach((r, k) => r.style.setProperty("--d", `${300 + k * 450}ms`));
    $$("li", marks).forEach((r, k) => r.style.setProperty("--d", `${550 + k * 450}ms`));
    setTimeout(() => {
      if (me !== penRun) return;
      proof.classList.add("judged", flags.length ? "sent-back" : "kept");
      verdict.innerHTML = flags.length ? "Struck out. Your own sentence goes back in." : "Same facts, new words. Kept.";
    }, still ? 0 : 600 + flags.length * 450);
  }
  $$(".drafts button").forEach((b, i) => b.addEventListener("click", () => proofread(i)));
  proofread(1);
  new IntersectionObserver((es, o) => { if (es.some((e) => e.isIntersecting)) { o.disconnect(); proofread(1); } }, { threshold: .15 }).observe(proof);

  // ---- copy buttons ------------------------------------------------------------------------------
  $$(".copy").forEach((btn) => btn.addEventListener("click", async () => {
    const code = btn.parentElement.querySelector("code").textContent;
    try { await navigator.clipboard.writeText(code); } catch { return; }
    btn.classList.add("done"); btn.setAttribute("aria-label", "Copied");
    setTimeout(() => { btn.classList.remove("done"); btn.setAttribute("aria-label", "Copy command"); }, 1400);
  }));
})();
