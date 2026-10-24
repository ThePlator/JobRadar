# JobRadar: notes for Claude

A self-hosted job-search agent (Python 3.11+, asyncio). It reads company boards, hiring pages,
Telegram channels and forwarded email; extracts and scores each job against `config/profile.yaml`;
emails strong matches; builds LaTeX resumes; and shows everything on a local FastAPI dashboard
(Notion is optional). Owner: Sameer (GitHub ThePlator). `PLAN.md` is the roadmap; `docs/COMMANDS.md`
documents every command and setting.

## Commands
```bash
uv sync --extra web                     # install (the dashboard needs the web extra)
uv run ruff format src tests && uv run ruff check src tests && uv run mypy
uv run pytest -q                        # about 680 tests, ~30 s; target one file while iterating
uv run jobradar run                     # the agent + dashboard (Sameer runs it in his own terminal)
```
CI runs Python 3.11, 3.12 and 3.13 (`.venv` is 3.14): test 3.11 before a PR, see the `ship` skill.

## Layout (src/jobradar)
- `sources/`: Telegram, boards (Greenhouse, Lever, Ashby, Workday, Oracle, SmartRecruiters, freehire), pages, email forward
- `pipeline/`: ingest -> fetch -> extract -> match/score -> decide; `status_rules.py` (who may change a status), `skill_gaps.py` + `skill_detail.py`
- `sinks/`: email alerts, digest, reminders, follow-ups, company replies, email buttons, Notion
- `web/`: FastAPI app (`app.py` routes), `views.py` (page data), Jinja `templates/`, `static/app.css` + `app.js`
- `db/repo.py`: every SQL query (SQLite + SQLModel); `queue/`: the durable task queue and worker
- `resume/`: LaTeX build, grounding check, ATS check; `by_source.py`: applications per source

## Rules
- Commits and PR bodies: **no Co-Authored-By line**. One PR per step; Sameer merges.
- Never touch `data/jobradar.db`, `config/*` or `.env` with a changed build: preview on the sandbox
  (`.claude/skills/ship/sandbox.sh`, preview `dashboard-sandbox`, port 8095).
- Dashboard pages: strict CSP (`script-src 'self'`, no inline styles or scripts), so bar widths are
  SVG attributes; every POST needs the page token; the server only binds 127.0.0.1 (`web.url` for
  the phone via Tailscale).
- Facts on a resume come only from `profile.yaml`; never invent skills, numbers or claims.
- A status changed back within 10 minutes is a misclick and isn't counted.
- User-facing text: plain sentences, no em dashes, no jargon.
- Short answers; reuse saved plans from memory instead of re-planning.
