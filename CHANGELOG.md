# Changelog

All notable changes to JobRadar are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions will follow [Semantic Versioning](https://semver.org/) from the first release.

## [Unreleased]

### Added

**Website**
- `site/`: a one-page site published to GitHub Pages by `.github/workflows/pages.yml`, in light and dark themes (follows the system, with a switch). It tells one real night from my log (15 Oct, 22:15 to 22:25) with the real timestamps: two Telegram posts, a finance job hidden as a weak match (29), Rubrik scored 79 and emailed six seconds later, QualComm scored 92 and held for the 10 minute pause, US sales roles skipped from the company boards. Blip, the mascot, annotates each moment in handwriting. Visitors can forward a post to Blip, which highlights each line it understands by field, dims the noise and underlines the skills you have or lack (read in the browser with the post-label rules, nothing sent), and try three AI rewrites of a bullet on an example resume: Blip circles each made-up number, tool or brag word, notes why in the margin, and strikes the rewrite for your own sentence. Static HTML, CSS and JS. The numbers section plots every alert sent so far (time from post to inbox, from the local database), the share of Telegram jobs read without AI, the 58 boards and the test count; a "What's coming" board shows planned work from PLAN.md (Next, Soon, Later); the setup section shows what each command prints on a first run.

**More places to find jobs**
- Workday career sites (`sources.boards.workday`, e.g. `NVIDIA: nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite`): India postings only, read at most every 30 minutes. Only postings Workday dates within the last 3 days count as new, and at most 10 are passed on per read. A site with more postings than one read takes, in an order that changes between reads (Accenture), passes nothing on; `jobradar boards` says why.
- Oracle Recruiting Cloud career sites (`sources.boards.oracle`, e.g. `Zensar: fa-etvl-saasfaprod1.fa.ocs.oraclecloud.com/CX_1`, or the careers link): India postings only, at most every 30 minutes, the same 3-day and 10-per-read rules. Oracle lists newest first, so a site with more India postings than one read takes (Kotak, EXL) still passes on new ones from the top. Oracle job pages are JavaScript only, so JobRadar reads the job's public detail data instead: the description, experience and posting date are found with no AI call, and the still-open check covers Oracle links.
- freehire (`sources.freehire`, off by default): freehire.me's public job search across about 90 hiring systems, filtered by `countries`, `seniority` and `categories`, at most every 15 minutes. Its postings go through the normal pipeline and the still-open check.
- Hiring-drive pages (`sources.pages.watch`, `jobradar pages add <name> <url>`): pages that announce drives instead of listing jobs are read every 1 to 6 hours and new lines become a post. robots.txt is obeyed, nothing is logged into, and a page that keeps refusing is paused until `jobradar pages resume`.

**Fewer wrong alerts**
- Experience is read from the job page's own description, so "Minimum 5 years" on a board posting now counts (8 Accenture alerts for 5-year roles reached a fresher before this).
- A job whose page couldn't be read, with a post under 300 characters and no stated requirements, never alerts: a title alone isn't enough to score it.
- One role across cities is one job: the same company and role seen again within 30 days (Pune, Bengaluru, Hyderabad) joins the first job instead of alerting again.
- Job age: each job keeps its publish date from the company's page or board (never guessed by the AI). Notion gets a **Posted On** column, alerts show "Posted 2 Sep", and a posting published more than `notify.old_posting_days` (30) ago doesn't alert.

**Tailored resumes (v0.3)**
- JobRadar builds a one-page PDF resume per job from `profile.yaml`, in your own LaTeX layout (`resume.template: classic` reproduces it exactly; a path to your own `.tex.j2` works too). It's built when a job first scores at or above `scoring.auto_resume_above` (at most `resume.max_per_day`), when you set a job to Shortlisted, or with `jobradar resume <job>`.
- The writer model (`llm.writer_model`, now Gemini Flash: Pro has no free tier) only selects, orders and rephrases your facts; it never sees your name or contact details. A checker undoes any bullet that adds a number, size word ("doubled", "10x"), technology or banned phrase the original doesn't have, drops unknown bullet ids, and keeps skills to those listed in that group. Every undo is logged and stored with the resume.
- `**bold**` in profile.yaml prints in bold. A project with `primary: true` always shows first with 3+ bullets.
- No em dashes on the resume: any — or `---` (from profile.yaml or the model) becomes a comma, and titles read "Intern | Company". `on_resume: false` keeps an entry for matching but off the resume.
- The page is used in full: the model's picks come first, then the entry's other bullets, and fitting the page drops from the end.
- Always fits `resume.max_pages` (1): the lowest-ranked bullets go until it does, other entries before a primary project; each entry keeps 2 bullets (a primary project 3). `jobradar resume` (the preview) fits the page the same way and says which bullets it left out. If `writer_model` is busy (503/429), tailoring falls back to `extract_model`. Compiles with Tectonic (`--untrusted`) or pdflatex (`-no-shell-escape`), 60 s limit; the LaTeX log is kept on failure.
- PDFs and their `.tex` are saved as `{Company}_{Role}_{yyyymmdd}_v{n}.pdf` in `storage.local_dir`; Notion's Resume column links to the newest version.
- `jobradar resume` alone previews your profile as a PDF.

**Still-open check, sources and reminders**
- Before a job is matched, JobRadar asks its hiring site whether the posting is still open, for links to Greenhouse, Lever, Ashby and SmartRecruiters postings. A closed one is hidden ("posting closed on the hiring site") with no Gemini call. Only a definite answer hides a job: a 404/410, or the posting missing from an Ashby board that was read fine. Timeouts, rate limits, redirects and other sites change nothing. Requests go only to the platforms' fixed API hosts.
- New Notion column **Source**: where each job was seen ("Telegram · Freshershunt", "Greenhouse · Stripe", "Added by you"). JobRadar adds the column to your database on start and fills it in on existing pages; group a view by Source to see jobs per channel or board.
- Alerts say which board a job came from ("posted 4 min ago on Stripe's Greenhouse board"), and the digest lists new jobs from company boards in their own section.
- Closing-soon reminder: one email when a job you shortlisted closes today or tomorrow, sent between 08:00 and 22:00, once per job. A posting that has already closed on its hiring site is skipped. Turn it off with `notify.closing_reminders: false`.
- `jobradar boards add <link>` (or `<ats> <board>`) adds a board to config.yaml after checking that it exists and showing how many of its postings pass your filters; `jobradar boards remove <name>` takes one out. Your comments and layout in config.yaml are kept.

**Company job boards (job feeds)**
- JobRadar reads company job boards directly through their public APIs (Greenhouse, Lever, Ashby, SmartRecruiters), every 20 minutes per board, so new postings arrive minutes after they go up instead of when a channel reposts them. List them in `sources.boards` under their hiring system as `Name: board name`, e.g. `greenhouse: {Stripe: stripe}` (a board link works too).
- The first read of a board only records what's there: no alerts for old postings. Later reads pass new postings in India (or remote with no other country named), with a tech title and not clearly senior, into the normal pipeline as posts from "Greenhouse · Stripe": dedupe, free extraction, matching, alerts and digest work as for Telegram.
- A posting that leaves its board hides its job ("closed on the company board") if it's still untouched in your Inbox.
- Errors never look like an empty board: a 404 counts towards "board gone" (3 in a row: retried after 30 days), any other error changes nothing, and a board that suddenly lists nothing is ignored for that round.
- `jobradar boards` shows each board's health and its newest posting with what the filters made of it; `jobradar boards --check` reads them all now.
- Every new board posting is logged as `board posting passed` or `board posting skipped` with the reason (not in India, remote but not open to India, not a tech role, too senior).
- Existing databases get the new "board" source type through a one-time table upgrade, with a backup written first (`jobradar.db.before-board-sources.bak`).

**Your statuses from Notion**
- Every 2 minutes JobRadar reads the Status you set in Notion (Shortlisted, Applied, Interview, Offer, Rejected, Skipped, Hidden, or New to un-hide) and keeps a dated history of it.
- A job you've set yourself is never hidden, re-decided or alerted on by JobRadar again; its score still updates.
- The digest shows "Your applications: 15 applied · 3 interviews · 0 offers (+2 applied since then)".
- `jobradar stats` shows your funnel (shortlisted → applied → interview → offer), your reply rate and alerts sent, and suggests `scoring.alert_above` from the scores of the jobs you went for.

**Readable emails**
- Alerts, digests and the test email share a new design built for email clients: a card per job with a coloured score tile, role and company, labels for fit, eligibility, freshness and deadline, "You have / Missing" skill rows, and Apply / Open in Notion buttons. The digest opens with four numbers (new, in your Inbox, hidden, alerts) and has clear sections. Plain-text versions are tidier too.
- The digest is named by time of day (morning, afternoon, evening).

**Instant alerts**
- The first alert is emailed as soon as the job is matched, about 30 seconds after the post. Strong jobs arriving within `notify.alert_batch_minutes` of an email are grouped into one follow-up at the end of that window, so a burst of posts doesn't flood your inbox.
- Each alert says when and where the job was posted ("posted 3 min ago in Freshershunt").
- Fast lane: work for jobs first seen in the last hour runs before catch-ups, re-matches and backfills (new `task.priority` column, added automatically).
- `scoring.alert_above` defaults to 75.

**Daily digest**
- A summary email at each `notify.digest_times` slot (09:00 and 19:00 by default) covering everything since the previous one: new jobs, the top 5 still in your Inbox with score and reason, jobs closing in the next 2 days, what was hidden and why, alerts sent, failed tasks and Gemini spend. A window with nothing to report sends nothing.
- Dry run applies: digests are saved to `data/alerts/digest-*.html` instead.
- `jobradar digest --now` sends the digest for the current window right away.

**v0.2 part B2: decide and email alerts**
- After matching, each job is decided in order: not eligible or an excluded company → hidden; likely scam → flagged, never emailed; unsure → Inbox; score below `scoring.hide_below` → hidden ("weak match"); strong, clearly eligible and confident → email alert; otherwise → Inbox.
- With `matching.branch_strict: true`, jobs limited to branches that clearly exclude yours are hidden. Close branches (Electrical with Electronics, "or related branches") still count.
- Scam check (Scam Risk column): deposits, training or kit fees, "pay to confirm your offer" → high; an application fee in the post, a personal-email-only contact, pay per day or week, pressure to hurry → points. No false alarms on the first 132 real jobs.
- Email alerts over SMTP to `NOTIFY_TO`: once per job, batched within `notify.alert_batch_minutes`, at most `notify.max_alerts_per_day`. **Dry run is the default**: alerts are saved to `data/alerts/*.html` instead of sent.
- JobRadar never overwrites a Notion Status you set yourself (Shortlisted, Applied…) when it hides a job.
- Alerts only for fresh, open jobs: first seen within `notify.alert_max_age_days` (default 2) and the deadline not yet passed. Skipped alerts are logged with the reason.
- `jobradar alerts --test` sends you one test email (even in dry run) to check that Gmail sending works.
- Only a job's first match can alert: the startup catch-up and re-matching after a profile or prompt change never do, so old jobs can't flood your inbox. Changing the match prompt re-matches open jobs.

**v0.2 part B1: Match Score**
- Every read job is matched against `config/profile.yaml` and gets a 0-100 **Match Score** in Notion, with a reason line at the top of the JobRadar toggle, e.g. "Good fit · 86 · eligible ✓ · Python, SQL ✓ · missing Docker · Pune ✓".
- Batch years and maximum experience (`filters`) are checked in code first; a job that fails them is marked "Not eligible: batch 2023-2024 only" without any LLM call.
- The LLM answers fixed questions (eligible, location, role alignment, skills coverage, seniority, overall fit) as levels; code turns them into the score with `matching.weights`. Answers are stored, so new weights re-score for free at the next start, and editing the profile re-matches open jobs.
- `matching.branch_strict: true` treats "CSE/IT only" jobs as not eligible.
- Only education, skills, experience and project bullets, preferences and filters are sent for matching: never your name, email, phone, links or form answers.
- No LLM key: `skills_rule` scores by skill overlap (shown as "skills only").

**v0.2 part B4a: free extraction first, LLM as fallback**
- Job details are read from the job page's schema.org JobPosting data and the post's own "Company: / Role: / Batch: …" lines (decorative bold text included) before any LLM call. The LLM is asked only when role or company is still missing (`llm.extract_mode: fallback`, the default; `always` keeps the old behaviour of reading every job with the LLM, which also fills summaries and skills).
- Jobs are read even without an LLM key, from page data and post labels alone.
- Each job records which layer filled each field (`_sources` in its data).
- `jobradar extract --compare [--refetch] [--examples N]` checks the free layers against what the LLM extracted earlier, field by field. On the first 136 real jobs, 68% needed no LLM call; company agreed 99% and role 93%.
- New columns on existing tables are now added automatically when JobRadar starts.

**v0.2 part A: reading jobs**
- Job pages are downloaded and their main text extracted, with robots.txt respected, one request per second per site, a public-address check on every redirect, and login-wall detection. Optional JavaScript rendering with Playwright (`--extra browser`).
- Gemini (through LiteLLM) extracts company, role, location, work mode, experience, skills, salary, deadline and apply link. Answers are validated, cached, and capped by a daily budget (`llm.daily_budget_inr`).
- Notion rows get their columns filled, the title becomes "Role @ Company", and the "JobRadar" toggle shows a summary, eligibility and skills.
- `jobradar extract --missing` reads jobs that were listed before an LLM key was set.

**v0.1: Telegram to Notion**
- Read-only Telegram reader with catch-up from the last message read and live updates.
- Link cleanup: short links expanded, tracking parameters removed, non-job hosts ignored, configurable `links.deny` and `links.extra_shorteners`.
- One Notion row per job, however many groups post it, with a "Seen In" count.
- Channel ads (links attached to posts about several different jobs) hidden automatically.
- SQLite task queue with retries, backoff and crash recovery.
- Commands: `init`, `run`, `chats`, `add`, `stats`, `retry --failed`.

**Project**
- Design documents (PRD, HLD, LLD), plan, README, wiki, security policy, code of conduct and contribution guide.

### Changed
- Notifications will be sent by email instead of a Telegram bot.
- The default extraction model is `gemini/gemini-flash-lite-latest`.

### Fixed
- `jobradar stats` called every hidden job an ad. It now breaks hidden jobs down by reason (not eligible, weak match, not a job, hidden by you, ad or promo link) and lists only real ads as ads.
- An ad with a company name but no role (a "free webinar" post) no longer counts as a job; the matcher hides any that got through.
- A match whose answers contradict each other (a 99 score with the verdict "Not a fit") is marked unsure and never emailed.
- Notion rejected long posts with emoji, because it counts them as two characters.
- `go.acciojob.com` links weren't expanded, so a course ad became a fake job.
- Telegram "Apply" buttons were missed with the newer Telegram API layer.
- Configuration errors pointed at the wrong setting when a section was missing.

[Unreleased]: https://github.com/ThePlator/JobRadar/commits/main
