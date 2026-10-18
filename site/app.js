// JobRadar showcase: Blip's eyes and moods, the radar, the scroll story, the post reader
// demo and the resume checker. No libraries, nothing sent anywhere.
(() => {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const still = matchMedia("(prefers-reduced-motion: reduce)").matches;

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

  // ---- top bar ------------------------------------------------------------------------
  const top = $(".top");
  addEventListener("scroll", () => top.classList.toggle("scrolled", scrollY > 8), { passive: true });

  // ---- hero radar: postings appear as the sweep passes; filtered ones dim ---------------
  const POSTS = [
    ["Software Engineer, Intern", true], ["Backend Engineer, New Grad", true],
    ["Account Executive", false], ["Associate Software Engineer", true],
    ["Senior SRE", false], ["Data Analyst, Fresher", true], ["Sales Manager", false],
    ["Full Stack Developer Intern", true], ["Staff Engineer", false], ["ML Engineer, Graduate", true],
  ];
  const pings = $("#pings"), heroBlip = $("#heroBlip svg"), bubble = $("#heroBubble");
  let n = 0;
  function ping() {
    const [title, good] = POSTS[n++ % POSTS.length];
    const a = Math.random() * Math.PI * 2, r = 18 + Math.random() * 22;
    const el = document.createElement("div");
    el.className = "ping" + (good ? "" : " dim");
    el.textContent = title;
    el.style.left = `${50 + Math.cos(a) * r}%`;
    el.style.top = `${44 + Math.sin(a) * r * 0.8}%`;
    pings.append(el);
    if (pings.children.length > 4) {
      const old = pings.firstElementChild;
      old.classList.add("out");
      setTimeout(() => old.remove(), 350);
    }
    if (good && Math.random() < 0.5) say(`${title}? Checking your profile…`);
  }
  const LINES = [
    "Hi! I'm Blip. I read job channels so you don't have to.",
    "I skip senior and sales roles for you.",
    "Same job in 3 channels? One row.",
    "Strong match: emailing you now.",
    "I never add a skill you don't have.",
  ];
  let line = 0;
  function say(text) {
    bubble.textContent = text;
    bubble.classList.remove("pop"); void bubble.offsetWidth; bubble.classList.add("pop");
  }
  $("#heroBlip").addEventListener("click", () => { mood(heroBlip, "happy"); say(LINES[++line % LINES.length]); });
  if (!still) { setInterval(ping, 1700); ping(); ping(); } else { for (let i = 0; i < 4; i++) ping(); }

  // ---- the scroll story ---------------------------------------------------------------
  const stage = $("#stage"), stageBlip = $(".stage-blip svg"), stageBubble = $("#stageBubble");
  const SAY = {
    1: ["New post in a channel!", null],
    2: ["Same job, 3 channels. Merged.", null],
    3: ["92. That's a good one.", "happy"],
    4: ["Sent! It's in your inbox.", "alarm"],
    5: ["One page. Only true things.", "happy"],
  };
  const steps = $$(".step");
  const io = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (!e.isIntersecting) continue;
      const s = e.target.dataset.step;
      if (stage.dataset.step === s) continue;
      stage.dataset.step = s;
      steps.forEach((x) => x.classList.toggle("on", x === e.target));
      stageBubble.textContent = SAY[s][0];
      stageBubble.classList.remove("pop"); void stageBubble.offsetWidth; stageBubble.classList.add("pop");
      mood(stageBlip, SAY[s][1], 1800);
    }
  }, {
    // On phones the pinned picture covers the top half, so a step switches lower down.
    rootMargin: matchMedia("(max-width: 900px)").matches ? "-68% 0px -27% 0px" : "-45% 0px -45% 0px",
  });
  steps.forEach((s) => io.observe(s));
  steps[0].classList.add("on");

  // ---- the post reader (the same label idea as pipeline/post_labels.py) ------------------
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
  const ta = $("#postText"), say2 = $("#demoSay"), dBlip = $("#demoBlip"), reader = $(".reader");
  const sampleBtns = $$(".samples button");
  function loadSample(i) {
    ta.value = SAMPLES[i];
    sampleBtns.forEach((b, j) => b.setAttribute("aria-pressed", String(i === j)));
  }
  sampleBtns.forEach((b, i) => b.addEventListener("click", () => { loadSample(i); run(); }));
  loadSample(0);

  function row(dl, label, value, i) {
    const dt = document.createElement("dt"), dd = document.createElement("dd");
    dt.textContent = label;
    dd.textContent = value || "not stated";
    if (!value) dd.className = "none";
    for (const el of [dt, dd]) { el.classList.add("row-in"); el.style.animationDelay = `${i * 70}ms`; }
    dl.append(dt, dd);
  }
  function chip(text, have) {
    const s = document.createElement("span");
    s.className = "chip" + (have ? " have" : "");
    s.innerHTML = have ? '<svg class="i" aria-hidden="true"><use href="#i-check"/></svg>' : "";
    s.append(text);
    return s;
  }
  function run() {
    const text = ta.value.trim();
    if (!text) { say2.textContent = "Paste a job post first, then press Read this post."; return; }
    reader.classList.add("reading");
    say2.textContent = "Reading…";
    setTimeout(() => {
      reader.classList.remove("reading");
      const r = read(text), dl = $("#fields");
      dl.replaceChildren();
      const rows = [["Company", r.found.company], ["Role", r.found.role], ["Location", r.found.location],
        ["Batch", r.years.join(", ")], ["Experience", r.exp], ["Apply link", r.link]];
      rows.forEach(([l, v], i) => row(dl, l, v, i));
      const mine = $("#mySkills").value.split(",").map((s) => s.trim()).filter(Boolean);
      const mineKeys = new Set(mine.map(key));
      const out = $("#skillOut");
      out.replaceChildren(...r.tech.map((t) => chip(t, mineKeys.has(key(t)))));
      const have = r.tech.filter((t) => mineKeys.has(key(t))).length;
      const batch = +$("#myBatch").value;
      const batchOk = !r.years.length ? null : r.years.includes(batch);
      const p = document.createElement("p");
      p.textContent = (r.tech.length ? `You have ${have} of the ${r.tech.length} skills this post names. ` : "This post names no skills I know. ")
        + (batchOk === null ? "It doesn't state a batch." : batchOk ? `Your ${batch} batch is eligible.` : `Your ${batch} batch isn't listed, so I'd hide it.`);
      out.append(p);
      const got = rows.filter(([, v]) => v).length;
      say2.textContent = batchOk === false ? "Not your batch. I'd hide this one." : `Found ${got} of 6 details without asking any AI.`;
      mood(dBlip, batchOk === false ? null : "happy", 1600);
    }, still ? 0 : 900);
  }
  $("#readBtn").addEventListener("click", run);

  // ---- the resume checker (the rules in resume/validate.py, simplified) -----------------
  const ORIGINAL = $("#origText").textContent;
  const BANNED = ["expert", "world-class", "guru", "ninja", "rockstar", "cutting-edge", "passionate"];
  const SIZE = /\b(doubl\w*|tripl\w*|halv\w*|\d+(\.\d+)?\s?x)\b/gi;
  const nums = (s) => new Set((s.match(/\d[\d,]*(?:\.\d+)?\+?%?/g) || []).map((x) => x.replace(/,/g, "")));
  const techIn = (s) => new Set(TECH.filter((t) => new RegExp(`(^|[^A-Za-z0-9+#.])${t.replace(/[+.#]/g, "\\$&")}(?![A-Za-z0-9+#])`, "i").test(s)).map((t) => t));
  function check(text) {
    const why = [];
    const n0 = nums(ORIGINAL), extraN = [...nums(text)].filter((x) => !n0.has(x));
    if (extraN.length) why.push(`a number that isn't in your bullet: ${extraN.join(", ")}`);
    const s0 = new Set((ORIGINAL.match(SIZE) || []).map((x) => x.toLowerCase()));
    const extraS = (text.match(SIZE) || []).filter((x) => !s0.has(x.toLowerCase()));
    if (extraS.length) why.push(`a size word you didn't use: ${extraS.join(", ")}`);
    const t0 = techIn(ORIGINAL), extraT = [...techIn(text)].filter((x) => !t0.has(x));
    if (extraT.length) why.push(`a technology your bullet doesn't mention: ${extraT.join(", ")}`);
    const ban = BANNED.filter((b) => text.toLowerCase().includes(b) && !ORIGINAL.toLowerCase().includes(b));
    if (ban.length) why.push(`a banned word: ${ban.join(", ")}`);
    return why;
  }
  const verdict = $("#verdict");
  $$(".rewrites button").forEach((b, _, all) => b.addEventListener("click", () => {
    all.forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    const why = check(b.textContent);
    verdict.className = "verdict " + (why.length ? "undone" : "kept");
    verdict.innerHTML = why.length
      ? `<div class="v-title"><svg class="i" aria-hidden="true"><use href="#i-x"/></svg>Undone. Your wording goes back in.</div><p class="struck"></p><ul>${why.map((w) => `<li>Adds ${w}</li>`).join("")}</ul>`
      : `<div class="v-title"><svg class="i" aria-hidden="true"><use href="#i-check"/></svg>Kept. Same facts, same numbers.</div>`;
    const struck = $(".struck", verdict);
    if (struck) struck.textContent = b.textContent;
  }));

  // ---- copy buttons --------------------------------------------------------------------
  $$(".copy").forEach((btn) => btn.addEventListener("click", async () => {
    const code = btn.parentElement.querySelector("code").textContent;
    try { await navigator.clipboard.writeText(code); } catch { return; }
    btn.classList.add("done"); btn.setAttribute("aria-label", "Copied");
    setTimeout(() => { btn.classList.remove("done"); btn.setAttribute("aria-label", "Copy command"); }, 1400);
  }));
})();
