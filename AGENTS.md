# AGENTS.md: the rule book for any AI working on JobRadar

> **On the `saas/main` branch:** this branch turns JobRadar into a multi-user SaaS. Read
> [`SAASPLAN.md`](SAASPLAN.md) first: it is the plan, the decisions and the PR order for this
> branch. SaaS pull requests go into `saas/main`, never into `main`.

Read this first. It says what JobRadar is, where the work stands, the conventions to follow, the
decisions already made (don't reopen them without the owner), and what comes next.
Last updated: 2026-10-24.

## 1. What this is

JobRadar is an open-source (MIT), self-hosted job-search agent built and used daily by its owner,
Sameer (GitHub `ThePlator`), an Electrical Engineering fresher in India applying to Backend / SWE
roles. It:

1. **Finds jobs**: company career boards (Greenhouse, Lever, Ashby, SmartRecruiters, Workday,
   Oracle, freehire.me's search), watched hiring pages, Telegram job channels, and emails the user
   forwards.
2. **Understands them**: reads each job page, extracts details with free rules first and an LLM
   (Gemini or Groq via LiteLLM) only when needed, merges duplicates, hides ads and scams.
3. **Scores them** against `config/profile.yaml` (Match Score 0-100, eligibility: batch years,
   branch, experience).
4. **Acts**: emails strong matches within minutes, sends a morning and evening digest, reminders for
   closing jobs, follow-up drafts, and reads company replies. Emails have "I applied" /
   "Not interested" buttons.
5. **Builds a one-page LaTeX resume** per job, using only facts from `profile.yaml`.
6. **Shows everything on a local dashboard** (FastAPI + Jinja, http://127.0.0.1:8080), optionally
   mirrored to Notion.

JobRadar never applies for the user. The user reviews and submits every application.

## 2. Where we are (status)

| Release | Scope | State |
|---|---|---|
| v0.1 Ingest | Telegram, links, dedupe, ads, SQLite queue, Notion rows | done |
| v0.2 Read and match | page reading, free extraction, Match Score, decide, scams, email alerts, digest | done |
| v0.2.5 Statuses and feeds | Notion/email statuses, company boards, still-open check, reminders | done |
| v0.3 Resume | profile.yaml, LaTeX templates, grounding checker, ATS check, auto + Shortlisted triggers | done |
| v0.4 Apply kit | form answers, cover letter, forward-to-add, follow-ups, company replies | done |
| v0.5 Dashboard | Today, Applications board, All jobs (full-text search), Skills (what jobs really want), Digest, job pages with resumes; interviews by source; phone via Tailscale; Notion and Telegram optional | done (PRs #68-#93) |
| Matching with Jev | TypeSafe's Jev answers match questions with calibrated confidence, beside Gemini first | **next**, waiting for the owner's TypeSafe key |
| Resume review panel | designed, see section 7 | planned |
| v1.0 Public release | Docker image, PyPI `jobradar-agent`, setup guide, WhatsApp experimental | planned |

Open decision: after a week of using the dashboard (around 2026-10-29) the owner decides whether
to switch Notion off (`notion.enabled: false`).

`PLAN.md` is the detailed roadmap with checkboxes; `CHANGELOG.md` (`[Unreleased]`) lists what has
shipped. Keep both current when you finish a step.

## 3. How it works

```
sources (boards, pages, Telegram, email)
  -> ingest: save raw message, expand and clean links, dedupe, hide ads      (pipeline/ingest.py)
  -> fetch the job page (robots.txt respected)                                (pipeline/fetch.py)
  -> extract: structured data, rules first, LLM fallback                      (pipeline/extract.py)
  -> match + score against profile.yaml, eligibility                          (pipeline/match.py, score.py)
  -> decide: hide / unsure / alert, scam check                                (pipeline/decide.py, scam.py)
  -> sinks: email alert, digest, reminders, Notion; resume build              (sinks/, resume/)
dashboard reads the same SQLite database                                      (web/)
```

Everything runs as tasks on a durable SQLite-backed queue (`queue/`), so a restart loses nothing.
`jobradar run` starts the sources, the worker, the scheduler and the dashboard in one process.

## 4. Code map (`src/jobradar`)

- `__main__.py`: the `jobradar` CLI (run, web, add, boards, resume, digest, skills, sources, stats, ...)
- `app.py`: wiring for `jobradar run` (pipeline, worker, schedules, dashboard)
- `config.py`: `config/config.yaml` + `.env`, validated with pydantic; fails fast with clear messages
- `db/repo.py`: **every SQL query lives here**, portable between SQLite and Postgres;
  `db/models.py` the SQLModel tables; on `saas/main` also `db/migrations/` (Alembic, Postgres),
  `db/textsearch.py` (search queries for both databases), `db/transfer.py` (`saas-import`),
  `db/types.py` (times and JSON: `timestamptz` / `jsonb` on Postgres, read back as text)
  and `job` (the posting, for everyone) apart from `user_jobs` (one user's score and status),
  joined into `Job` by `JOB_VIEW`; `Repo.for_user(id)` scopes per-user queries (also status
  history, alerts, resumes, and `user_settings` for one user's notes; `setting` is the app's);
  `Repo.scoped(id)` adds Postgres row-level security, for everything a signed-in user asks
- `sources/`: Telegram, `boards/` (one reader per hiring system), `pages/`, email forward
- `pipeline/`: ingest -> extract -> match -> decide; `status_rules.py` (what a status change may do,
  shared by email buttons and the dashboard); `skill_gaps.py` + `skill_detail.py` (Skills page)
- `sinks/`: email alerts, digest, reminders, follow-ups, company replies, email buttons, Notion
- `resume/`: tailor, LaTeX render, grounding check, page fitting, ATS check
- `services/`: the one way in for reads (jobs, applications, home, digests, skills, stats,
  search) and changes (statuses, resumes, profile, intake, tasks, notify); each takes a `Ctx`
  (who is asking, and when) and raises `NotFound` / `Refused`
- `web/`: `app.py` (routes, security), `templates/` (Jinja), `static/app.css` + `app.js`; `signin.py` (SaaS
  sign-in page, sessions, CSRF)
- `saas/`: SaaS-only parts; `auth.py` is the only code that talks to Firebase
- `profile.py`: the profile; on `saas/main`, `user_profile` / `user_preferences` read the user's
  rows (the owner's are copied from profile.yaml and config.yaml when they change)
- `by_source.py`: applications and next rounds per source
- `tests/`: pytest, about 680 tests, fakes in `tests/fakes.py`

## 5. Conventions

**Workflow**
- One pull request per step. The owner reviews and merges; never push to `main`.
- Branch names: `feat/...`, `fix/...`, `docs/...`, `chore/...`.
- Commit messages: a short summary line, a blank line, then why. **No Co-Authored-By lines.**
- Before a PR: `uv run ruff format src tests && uv run ruff check src tests && uv run mypy`,
  `uv run pytest -q`, and the tests on Python 3.11 too (CI runs 3.11, 3.12 and 3.13; a local
  3.14 venv misses some failures). Claude Code users: the `ship` skill does all of this.
- Update `CHANGELOG.md` (written for the user, not the developer), and `PLAN.md`,
  `docs/COMMANDS.md`, `config.example.yaml`, README when commands or settings change.

**Code**
- Python 3.11+, asyncio, type hints everywhere (mypy `strict = true`), ruff, line length 100.
- SQL only in `db/repo.py`. New settings get defaults in `config.py` and a line in
  `config.example.yaml`.
- Rules before LLM calls; cache LLM answers; respect `llm.daily_budget_inr`. The free Gemini tier
  is the target, so count calls.
- Network work goes through tasks with retry/backoff; errors are logged with context, never
  swallowed.
- Tests come with every change, using fakes rather than real services.

**Safety and privacy**
- Tests never touch the real `data/jobradar.db`: `tests/conftest.py` points the default database
  at a temp file. Keep it that way for any new command a test runs.
- Never run a changed build against the real `data/jobradar.db`, `config/` or `.env`: use a copy
  (`.claude/skills/ship/sandbox.sh` makes one in `data/sandbox`).
- Never commit secrets, `config/*`, `data/*` or personal details (the profile has the owner's
  phone and email).
- The dashboard binds 127.0.0.1 only (phone access via Tailscale `serve` and `web.url`, never
  `0.0.0.0` or `funnel`); strict CSP (`script-src 'self'`, no inline styles, so chart widths are
  SVG attributes); every POST carries the page token; foreign Host headers are refused.
- LLM prompts treat job text as data; outputs are schema-validated; models get no tools. Send
  providers no name, email or phone.
- JobRadar never applies, never messages recruiters, never posts on Telegram.

**Resume rules** (from the owner)
- Exactly one page. No em dashes anywhere.
- Every fact comes from `profile.yaml`: never invent skills, tools, numbers or achievements; verify
  any metric against code, tests or the database first. Experiences with `on_resume: false` never
  show.
- JobRadar is the primary project for Backend / SWE roles.

**Data rules**
- A status changed back within 10 minutes is a misclick and isn't counted anywhere.
- Quick actions (buttons) only move a job forward; "Change status" can set anything.
- A missing skill counts only jobs that need it: "Python, Java or C++" doesn't count against C++
  when the user has Python.

**Writing (UI, emails, docs)**
- Plain sentences, sentence case, active voice, no jargon, no em dashes, no hype.
- Name things by what they do for the user. Errors say what happened and what to do.

**Dashboard design**
- Light and dark themes, phone layout (bottom tab bar), keyboard shortcuts.
- Blip is the mascot (moods via CSS variables). The Skills pages use "one dot / tile per job"
  coloured by how the job asks (red must have, blue any one of a few, amber nice to have, green
  outlined covered); reuse that language rather than generic stat cards.

## 6. Decisions already made (don't reopen without the owner)

- MIT license; PyPI name `jobradar-agent`, CLI and import name `jobradar`.
- LLMs: hosted Gemini or Groq through LiteLLM; extraction model `gemini-flash-lite-latest`;
  writer model `gemini-flash-latest` (Gemini Pro has no free quota for the owner).
- Notifications by email (SMTP), not a Telegram bot. Telegram is a job source only.
- Owner's filters: batch years 2025-2026, anywhere in India plus remote, `branch_strict: true`
  (CSE/IT-only posts are hidden), alert at score 75.
- Resumes are local PDFs (no Google Drive for now), built from the owner's LaTeX layout.
- Dashboard: server-rendered Jinja pages with one small vanilla `app.js` (no Node build, so the
  PyPI package ships everything); React only if it outgrows that.
- Jev (TypeSafe) will judge matches behind a swappable provider interface, beside Gemini first;
  for extraction it selects among candidates code found, it never generates text.

## 7. What comes next

1. **Jev matching** (needs the owner's TypeSafe key): run Jev beside the current matcher, compare,
   then switch. Read the live docs at https://docs.typesafe.ai/llms.txt first.
2. **Resume review panel** (designed, not built): on a click on a job page, code pre-checks run
   (keyword gap, ATS check, one page), then 5 reviewer LLM calls in parallel (recruiter 0.20,
   tech lead 0.30, ATS/keywords 0.20, devil's advocate 0.15, impact editor 0.15), merged in code.
   Findings must quote the resume exactly or are dropped. "Apply fixes" re-tailors, re-runs the
   grounding check (always code, never an LLM), recompiles and re-reviews: at most 2 rounds, stop
   when there are no blockers.
3. **Notion decision** around 2026-10-29.
4. **More sources**: SuccessFactors readers starting with Wipro's sitemap; finding a company's
   boards from its name.
5. **v1.0**: Docker image, PyPI release, a setup guide that gets a new user running in under 15
   minutes; WhatsApp as an experimental source.

## 8. Working with the owner

- Short answers; plans as compact lists; ask only when the choice is really his.
- When he asks for a plan, save it and reuse it later instead of re-planning.
- Show UI changes once at desktop and phone width (and light and dark when colours change) before
  the PR; skip previews for back-end-only work.
- He runs `jobradar run` in his own terminal; after a merge he does `git pull` and restarts it.

## 9. More documentation

- `README.md`: overview, setup, configuration, privacy
- `docs/COMMANDS.md`: every command, setting and fix
- `docs/PRD.md`, `docs/HLD.md`, `docs/LLD.md`: requirements, architecture, module specs
- `docs/PHONE.md`: the dashboard on a phone via Tailscale
- `PLAN.md`: roadmap with checkboxes; `CHANGELOG.md`: what shipped
