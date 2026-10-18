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
  const awakeOnes = blips.filter((b) => !b.classList.contains("bed-blip"));
  const nap = () => awakeOnes.forEach((b) => b.classList.add("sleep"));
  function awake() {
    clearTimeout(idle);
    if (awakeOnes[0].classList.contains("sleep")) awakeOnes.forEach((b) => { b.classList.remove("sleep"); wave(b); });
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
  const LINES = ["Hi, I'm Blip.", "I read every new post in your job channels.", "Same job in three channels? One row.", "Same role in five cities? One job.",
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

  // ---- every alert so far: time from post to inbox, one dot per job (from data/jobradar.db) -----
  const ALERTS = [["12 Oct, 13:52","t",6647],["12 Oct, 21:18","t",13],["12 Oct, 21:28","t",61],["12 Oct, 21:28","t",45],["12 Oct, 21:39","t",624],
    ["12 Oct, 21:39","t",614],["12 Oct, 21:39","t",600],["12 Oct, 21:39","t",596],["12 Oct, 21:39","t",584],["13 Oct, 10:09","t",27],
    ["13 Oct, 12:01","t",21],["13 Oct, 15:05","t",72],["13 Oct, 22:28","t",124],["13 Oct, 23:12","t",2779],["14 Oct, 10:47","t",44469],
    ["14 Oct, 10:48","t",44494],["14 Oct, 12:32","t",45],["14 Oct, 18:25","t",1414],["14 Oct, 22:33","t",72],["15 Oct, 12:40","t",87],
    ["15 Oct, 22:15","t",1760],["15 Oct, 22:25","t",2360],["16 Oct, 15:26","b",19610110],["16 Oct, 15:36","t",83826],["16 Oct, 17:29","b",80],
    ["16 Oct, 17:29","b",85],["16 Oct, 17:39","b",691],["16 Oct, 17:39","b",691],["16 Oct, 17:39","b",691],["16 Oct, 17:39","b",691],
    ["16 Oct, 17:39","b",691],["16 Oct, 17:39","b",691],["17 Oct, 18:35","t",2051],["18 Oct, 11:52","t",51],["18 Oct, 14:29","t",34],
    ["18 Oct, 20:04","t",52]];
  const human = (sec) => sec < 90 ? `${sec} s` : sec < 5400 ? `${Math.round(sec / 60)} min` : sec < 172800 ? `${Math.round(sec / 3600)} h` : `${Math.round(sec / 86400)} days`;
  (function swarm() {
    const svg = $("#swarm"), NS = "http://www.w3.org/2000/svg", MAX = 172800, BASE = 196;
    // Drawn at the page's own width so labels stay readable on phones.
    const W = Math.max(320, Math.min(960, Math.round(svg.getBoundingClientRect().width) || 960)), narrow = W < 600;
    const L = 22, R = W - (narrow ? 40 : 60), r = narrow ? 6 : 8;
    svg.setAttribute("viewBox", `0 0 ${W} 250`);
    const x = (sec) => L + (Math.log10(Math.max(sec, 10)) - 1) / (Math.log10(MAX) - 1) * (R - L);
    const el = (tag, attrs, text) => { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); if (text) e.textContent = text; svg.append(e); return e; };
    // The 10-minute window: alerts inside it share one email, so they wait for it to close.
    el("rect", { x: x(560), y: 8, width: x(720) - x(560), height: BASE + r - 8, class: "sw-band" });
    el("text", { x: x(640), y: 24, class: "sw-band-label", "text-anchor": "middle" }, "one email per 10 min");
    el("line", { x1: L, x2: W - 10, y1: BASE + r + 4, y2: BASE + r + 4, class: "sw-axis" });
    for (const [sec, label] of [[10, "10 s"], [60, "1 min"], [600, "10 min"], [3600, "1 h"], ...(narrow ? [] : [[43200, "12 h"]]), [86400, "1 day"]]) {
      el("line", { x1: x(sec), x2: x(sec), y1: BASE + r + 4, y2: BASE + r + 10, class: "sw-axis" });
      el("text", { x: x(sec), y: BASE + r + 28, "text-anchor": "middle", class: "sw-tick" }, label);
    }
    const stacks = {}, hover = $("#swarmHover");
    ALERTS.forEach(([when, src, sec], i) => {
      const off = sec > MAX, cx = off ? W - r - 8 : x(sec), bin = Math.round(cx / (r * 2.1));
      const level = stacks[bin] = (stacks[bin] ?? -1) + 1;
      const c = el("circle", { cx: bin * r * 2.1, cy: BASE - level * r * 2.1, r, class: `sw-dot ${src}`, tabindex: 0,
        style: `--i:${i}` });
      const say = `${when}: ${src === "t" ? "Telegram post" : "company board"}, ${off ? `the posting was already ${human(sec)} old when Blip first saw it` : `in my inbox ${human(sec)} after it was posted`}`;
      const tip = document.createElementNS(NS, "title"); tip.textContent = say; c.append(tip);
      const show = () => { hover.textContent = say; };
      c.addEventListener("pointerenter", show); c.addEventListener("focus", show); c.addEventListener("click", show);
      if (off) el("text", { x: W - r - 8, y: BASE - level * r * 2.1 - 16, "text-anchor": "end", class: "sw-tick" }, "7 months");
    });
    const secs = ALERTS.map((a) => a[2]).sort((p, q) => p - q), within = (t) => secs.filter((v) => v <= t).length;
    $("#swarmNote").textContent = `${within(120)} of ${secs.length} reached me within two minutes, and ${within(720)} within 12 minutes. ` +
      `The cluster at 10 minutes is jobs waiting for the next email. ${secs.length - within(720)} took longer, up to ${human(secs.filter((v) => v <= MAX).at(-1))}.`;
    new IntersectionObserver((es, o) => { if (es.some((e) => e.isIntersecting)) { o.disconnect(); svg.classList.add("in"); } }, { threshold: .3 }).observe(svg);
  })();
  // 138 of 181 Telegram jobs read by the free first pass (job.prompt_version = rules-v1, since 11 Oct).
  $("#squares").innerHTML = Array.from({ length: 181 }, (_, i) => `<i${i < 138 ? ' class="on"' : ""}></i>`).join("");
  $("#pyDots").textContent = ".".repeat(96);

  // ---- on Blip's workbench: pick a box, read its packing slip ----------------------------------
  (function workbench() {
    const worker = $(".worker"), boxes = $$("#coming .box"), stage = $("#slipStage");
    const belt = $("#coming .belt"), list = $("#coming .boxes");
    const fit = () => belt.style.setProperty("--belt-w", `${list.scrollWidth}px`);
    fit(); addEventListener("resize", fit, { passive: true });
    boxes.forEach((box) => box.addEventListener("click", () => {
      boxes.forEach((b) => b.setAttribute("aria-pressed", String(b === box)));
      const kind = ["next", "soon", "later"].find((k) => box.classList.contains(k));
      stage.className = `stamp ${kind}`; stage.textContent = kind;
      $("#slipTitle").textContent = box.dataset.title;
      $("#slipText").textContent = box.dataset.text;
      $("#slipNote").textContent = box.dataset.note;
      mood(worker, "happy");
    }));
  })();

  // ---- wake up your own Blip: each step shows its first-run output ------------------------------
  const bedBlip = $(".bed-blip"), steps = $$(".step");
  steps.forEach((st, i) => $(".open", st).addEventListener("click", (e) => {
    const btn = e.currentTarget, out = $(".out", st), open = btn.getAttribute("aria-expanded") !== "true";
    btn.setAttribute("aria-expanded", String(open)); out.hidden = !open;
    st.classList.toggle("done", open || st.classList.contains("done"));
    if (i === steps.length - 1 && open) { bedBlip.classList.remove("sleep"); mood(bedBlip, "happy", 0); wave(bedBlip); }
    if (i === steps.length - 1 && !open) { bedBlip.classList.remove("happy"); bedBlip.classList.add("sleep"); }
  }));

  // ---- copy buttons ------------------------------------------------------------------------------
  $$(".copy").forEach((btn) => btn.addEventListener("click", async () => {
    const code = btn.dataset.copy || btn.parentElement.querySelector("code").textContent;
    try { await navigator.clipboard.writeText(code); } catch { return; }
    btn.classList.add("done"); btn.setAttribute("aria-label", "Copied");
    setTimeout(() => { btn.classList.remove("done"); btn.setAttribute("aria-label", "Copy command"); }, 1400);
  }));
})();
