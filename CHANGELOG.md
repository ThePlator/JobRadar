# Changelog

All notable changes to JobRadar are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions will follow [Semantic Versioning](https://semver.org/) from the first release.

## [Unreleased]

### Added

**Interviews by source**
- The Applications page lists each Telegram chat, company board and page you applied from, with one dot per application (filled green: reached Interview or Offer; outlined: no reply yet; grey: rejected) and "1 of 4". Open a row to see those jobs, marked where that source found a job first. Only sources you applied from are listed; the top five show, the rest fold away. `jobradar sources` prints the same list.
- A job carried by two sources counts once in each and once in the total; a repost doesn't count twice. Rejected after an interview still counts as reaching the next round.
- A status you change again within 10 minutes is taken as a misclick: Interview put back to Applied 8 seconds later isn't an interview, and Applied put back to Shortlisted isn't an application. The funnel on the Applications page follows the same rule, so it no longer counted two misclicks as an interview and an offer.

**Dashboard (read-only, first step)**
- `jobradar web` opens your jobs on a page at http://127.0.0.1:8080, read from JobRadar's own database: **Today** (good matches from the last 3 days you haven't acted on, best first), **Applications** (a board: Shortlisted, Applied, Interview, Offer or rejected), **All jobs** (search by company or role, filter by status, 50 per page) and a page for each job (score reasons, details, where it was posted, your status history, resumes and letters built, the page text JobRadar read). Install once with `uv sync --extra web` (FastAPI and Uvicorn, an optional extra). Read-only for now; statuses still change from the email buttons or Notion.
- Blip lives in the dashboard: the sidebar shows whether it's watching (it read something in the last 20 minutes, with a pulsing dot) or asleep ("wake it with `jobradar run`"), and Today opens with Blip greeting you by your profile's first name and naming the best match (poke it for tips; it hops and waves). Each job page has "Blip's take", the score reason as a sentence. Score rings, company initials, live counts in the sidebar (Today, Applications, All jobs), a numbers strip (good matches, applied, interviews, closing in 48 h), "N days quiet" on applications with no reply, a light / dark / system theme switch, and `/` to search.
- More detail, all from data JobRadar already keeps: Today shows a 14-day chart (jobs found per day, and how many scored `alert_above`+) and where your good matches came from (each Telegram chat; company boards as one line); quick filters (Eligible, Remote or hybrid, Internships, Closing this week). Applications opens with your funnel (found → scored 75+ → shortlisted → applied → interview → offer, with each step's rate; a job you set to Rejected after an interview still counts as interviewed). Each job page shows how the score was made (each weighted part as a bar with its share, whether it's open to you, how sure the AI was), the job's skills marked green when they're on your profile and red when required and missing, and a deadline countdown. `Ctrl+K` jumps to any job by company or role; `j` / `k` move down a list and `Enter` opens; `g` then `t` / `a` / `j` goes to a page.
- Today's list, reworked: a toolbar with filters (All, Eligible, Remote or hybrid, Internships, Closing soon) that each show how many jobs they'd leave, a search box that narrows the list as you type (role, company or place), and a sort (best match, newest first, closing soonest), all working together; your choice is remembered, and the filter is in the address (`/#internship`) so you can bookmark it. Cards show a coloured score stripe, a "New" badge for jobs first seen in the last 6 hours, how long ago each was seen, the deadline countdown, and **Open job** / **Details** buttons; the whole card opens the job. A filter that leaves nothing says why, with one click back to all. Fixed: after an update the browser could keep running the old `app.js` (so the filters did nothing); the CSS and JS addresses now change whenever the files do.
- **Digest** page: the morning and evening digest on the dashboard. It opens on the latest one that went out, with chips for the ones before it (8 in all) and **So far** for what the next one will say. Blip's summary, the numbers (new jobs, scored 75+, still in your inbox, alerts emailed, closing soon), new jobs best first, closing soon, jobs from company boards, skills you keep missing, what was hidden and why, AI spend and failed tasks. **See it as emailed** opens the exact email. An earlier digest leaves out jobs, alerts and status changes that came after it. `g` then `d` opens it.
- Sort by time: Today adds **Oldest first** next to Newest first; All jobs gets a sort (Newest first, Oldest first, Best match) that applies as soon as you pick it, and keeps it across pages.
- Change a status from the dashboard: **I applied**, **Shortlist** and **Not interested** on new jobs (the card slides out of Today), **Interview**, **I followed up**, **Rejected** and **Offer** on the Applications board, and on each job's page a **Your move** panel with a **Change status** menu that sets any status (for fixing a mistake). A toast confirms each change with **Undo**. Buttons only move a job forward, by the same rules as the email buttons (now one shared module); Change status goes anywhere. Every change is in your status history, Notion follows through the queue, and shortlisting a job without a resume queues one, as Notion does. Each request must carry a secret only the dashboard's own pages have, and requests from another site are refused. Without JavaScript the buttons still work as plain forms.
- When an email button can't change anything (say **Not interested** on a job you've already applied to), JobRadar now emails you back with the reason and a link to change it in the dashboard, instead of only writing a log line.
- Applications page, redesigned: a pipeline strip (found → scored 75+ → shortlisted → applied → interview → offer, with the rate at each step) and five columns (Shortlisted, Applied, Interview, Offer, Rejected), each with its own colour, icon and a line about it ("2 due for a follow-up · 1 replied · waiting 6 days on average"). Cards show when JobRadar will draft the next follow-up with a meter that fills as the 10 days pass (it restarts when you follow up, and stops once the company replies), a mark when the company replied (interview invite, online test, rejection), when you followed up, and whether a resume was built. **Drag a card to another column** to change its status (Undo in the toast); on a phone, the buttons do the same. The page uses the full screen width.
- `jobradar run` starts the dashboard too, so there's one thing to start (`web.enabled: false` leaves it out; if its port is taken or the web extra isn't installed, JobRadar runs without it and says why). Your clicks wake the worker at once, so Notion and resume builds start right away. Alert and digest emails get a **View in dashboard** button on each job.
- Resumes on a job's page: every version with **Open PDF**, the **cover letter** written next to it (`…_cover_letter.pdf`), and its resume check (reads cleanly, or the problems; the job's skills on it and the ones you have that aren't). **Build resume** / **Build a new version** queue a build and the page updates when it's ready; Shortlisted cards without a resume get a **Build resume** button. Files are found by their id in the database and only served from `storage.local_dir`.
- The follow-up email for an Applied job is on its page: the draft JobRadar would send, the recruiter's address when the job gives one, **Copy email** and **Open in mail**.
- **Skills** page: what your good matches ask for that your profile lacks and what it has, over 7, 30 or 90 days, with bars, each skill's share of jobs, the change from the window before (once there is one), and the best jobs behind each. **I know …** adds the skill to profile.yaml's one-line `additional: [...]` list: only that line changes, a backup is kept, and the file is checked after the edit and put back if it doesn't read the same.
- **The dashboard on your phone:** [docs/PHONE.md](docs/PHONE.md) shares it over your own Tailscale network in about ten minutes. Set `web.url` to the Tailscale address and the dashboard accepts requests for that name (and still refuses every other), and email links open there.
- **What exactly they want**, for each skill (`/skills/<skill>`), opening with a roll call: one tile per job, strictest first (filled when the job needs the skill, outlined when your skills already cover it), so the answer shows before you read a word. Below it: every job's own lines about it, sorted into must have, one of a few ("AWS, Azure or GCP"), nice to have, and covered ("Python, Java or C++" when you have Python or Java); what they name with it (AWS: Lambda, S3, DynamoDB, certification…; C++: STL, multithreading, Linux systems…; also Azure, Google Cloud, Linux, Kubernetes, Docker and generative AI); and the other skills asked for alongside. A missing skill's count on the Skills page now leaves out the jobs your skills already cover, so C++ no longer ranks high just because jobs list it next to Java.
- All jobs searches the **whole job**: page text, posts, skills and places, with matches highlighted in a snippet and a Most relevant sort (SQLite full-text search, brought up to date before each search). New filters: score, first seen within, place, remote or hybrid, and source (each Telegram chat, board or page).
- On phones: a bottom tab bar, bigger buttons, the Applications board swipes sideways a column at a time, and on Today you can swipe a card right to **Shortlist** or left for **Not interested** (Undo in the toast).
- Only this computer can open it: it listens on 127.0.0.1, refuses requests naming any other host (so a website can't pose as localhost), and pages can't be framed or load anything from the internet. `web.host: 0.0.0.0` is refused. If port 8080 is busy the next free one up to 8089 is used and logged; `web.port` sets another.

**Website**
- `site/`: a one-page site published to GitHub Pages by `.github/workflows/pages.yml`, in light and dark themes (follows the system, with a switch). It tells one real night from my log (15 Oct, 22:15 to 22:25) with the real timestamps: two Telegram posts, a finance job hidden as a weak match (29), Rubrik scored 79 and emailed six seconds later, QualComm scored 92 and held for the 10 minute pause, US sales roles skipped from the company boards. Blip, the mascot, annotates each moment in handwriting. Visitors can forward a post to Blip, which highlights each line it understands by field, dims the noise and underlines the skills you have or lack (read in the browser with the post-label rules, nothing sent), and try three AI rewrites of a bullet on an example resume: Blip circles each made-up number, tool or brag word, notes why in the margin, and strikes the rewrite for your own sentence. Static HTML, CSS and JS. The numbers section plots every alert sent so far (time from post to inbox, from the local database), the share of Telegram jobs read without AI, the 58 boards and the test count; "Arriving at JobRadar" shows planned work from PLAN.md on an Indian Railways departure board (feature, arriving next / expected soon / scheduled later, and the platform where you'll see it: Browser, Email, Digest, Notion…; your own dashboard is arriving next); tapping a row plays its station announcement; the night log ends with what happens after you apply (tap "I applied" in an email and Notion updates, which visitors can try; a company's reply turned into a tappable "Yes, set Interview" suggestion; the follow-up draft 10 days later; and the weekly "Skills you keep missing" digest section with the owner's real counts); the setup section shows what each command prints on a first run.

**Replies from companies**
- JobRadar reads new mail from companies you set to Applied or Interview, and only theirs: their own domain (rubrik.com) or a hiring system or test platform (Greenhouse, Workday, HackerRank…) naming the company. An interview invite, online test or rejection gets you an email like "Rubrik replied: interview invite?" with the subject quoted, a **Yes, set Interview** (or Rejected) button and a link to open their email. It never changes a status on its own, ignores "thank you for applying" and "if you're shortlisted…", and never guesses offers. A reply stops follow-up reminders for that job. `notify.company_replies: false` turns it off.

**Skills you keep missing**
- Once a week the digest lists the skills your good matches asked for that your profile doesn't, with how many jobs named each ("AWS in 7 good matches, Google Cloud in 6, Kubernetes in 5"). `jobradar skills` shows it any time. No AI call: it reads each job's stated skills and the technology names in its own page and post (words like Go or Spring only from stated lists), counting synonyms once.
- New warning: a skill in profile.yaml cut in two by a comma inside brackets (`[Machine Learning (feature engineering, model training), ...]`) made matching miss it. JobRadar now says so at start and in `jobradar skills`; quote the item to fix it.

**Update jobs from your inbox, not Notion**
- Every job card in alert, digest and closing-reminder emails has **I applied** and **Not interested** buttons. Tap one and press Send on the email it opens: within 2 minutes JobRadar sets the job to Applied (which starts follow-up reminders) or Skipped, in Notion too, and archives that email. Follow-up emails have **Send to recruiter** (the draft, ready in your mail app) and **I followed up** (sets the Followed Up date).
- JobRadar reads only these emails from your inbox over IMAP, with the login that already sends alerts. Each button's subject carries the job and a signature only your JobRadar can make, and must come from your own address, so nobody else can change your jobs. Statuses only move forward (a late "I applied" never undoes an Interview).
- `jobradar replies --check` reads the inbox once now; `notify.email_actions: false` turns the buttons off.

**Follow-up reminders**
- When a job you set to Applied in Notion has had no reply for 10 days (`notify.followup_after_days`), JobRadar emails you a short follow-up draft for the recruiter, built only from the role, company, the day you applied and skills already on your resume (no AI, nothing invented). At most two per job, between 08:00 and 22:00, several in one email; JobRadar never sends the draft itself.
- New Notion column **Followed Up** (added to your database automatically): set it to the day you send a follow-up and the next reminder counts from then. Any later Status (Interview, Rejected…) stops them; applications quiet for 60+ days are left alone. Turn them off with `notify.followups: false`.
- `jobradar followups` lists each Applied job, the day you applied and when its draft is due; `--now` sends the due ones.

**Resumes read the way hiring software reads them**
- After each build, JobRadar reads the PDF's text the way Workday, Oracle and SuccessFactors import it and checks that the email and phone are there as text, dates use a plain hyphen, no characters are unreadable, no words run together and each heading comes before its bullets. Problems are logged and kept with the resume; `jobradar resume` prints them, plus how many of the job's skills you have are on the page.
- Fixed in the `classic` layout, found on a real resume: dates are printed with a plain hyphen (Workday drops dates written with a long dash), and under pdfLaTeX the PDF has real spaces between words, so bold phrases no longer merge with their neighbours ("pipelineinPython").
- Fixed: grouped `skills:` in profile.yaml (`languages: [...]`, `frontend: [...]`) were read as their group names by the free skills rule, so it only knew your project tags.

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
- **Notion is optional:** `notion.enabled: false` stops every Notion call; no Notion key is needed, and emails link to the dashboard instead ("more in Today on the dashboard", "find them on the dashboard: All jobs, Show hidden"). Tasks already queued for Notion are dropped. On by default.
- **Telegram is optional too:** with `sources.telegram.enabled: false`, `jobradar run` and `jobradar init` no longer ask for Telegram keys or a login.
- The digest's "skills you keep missing" counts only the jobs that need a skill, like the Skills page: "Python, Java or C++" no longer counts against you for C++.
- Notifications will be sent by email instead of a Telegram bot.
- The default extraction model is `gemini/gemini-flash-lite-latest`.

### Fixed
- Security: certifi (the list of certificate authorities JobRadar trusts for HTTPS) is updated to 2026.10.23. Older versions trusted emSign roots whose intermediate keys were stolen, so someone on your network could have faked a site JobRadar talks to, such as Gemini or Notion ([GHSA-6q48-6c7w-9gw2](https://github.com/advisories/GHSA-6q48-6c7w-9gw2)).
- `jobradar run` stopped right after starting when `sources.telegram.enabled` was false: the switched-off source finished at once and ended the run.
- Handled button emails (I applied, Not interested…) stayed in the Gmail inbox, only marked read: Gmail accepts taking the Inbox label off while the inbox is open but ignores it. They're now moved to All Mail, which is what Gmail's Archive does, and a failure is logged instead of passing silently.
- Oracle career sites (Zensar, Cummins…) often state the years of experience only in their own fields ("Minimum Experience (In Years)": 4) or a short summary, not in the description, so a 4-7 year Java Developer job looked open to freshers and was emailed. Those fields are now read, and `max_experience_years` hides such jobs.
- `jobradar stats` called every hidden job an ad. It now breaks hidden jobs down by reason (not eligible, weak match, not a job, hidden by you, ad or promo link) and lists only real ads as ads.
- An ad with a company name but no role (a "free webinar" post) no longer counts as a job; the matcher hides any that got through.
- A match whose answers contradict each other (a 99 score with the verdict "Not a fit") is marked unsure and never emailed.
- Notion rejected long posts with emoji, because it counts them as two characters.
- `go.acciojob.com` links weren't expanded, so a course ad became a fake job.
- Telegram "Apply" buttons were missed with the newer Telegram API layer.
- Configuration errors pointed at the wrong setting when a section was missing.

[Unreleased]: https://github.com/ThePlator/JobRadar/commits/main
