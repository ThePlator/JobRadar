---
name: ship
description: Ship the current JobRadar change as one pull request - branch, lint, type-check, test on 3.14 and 3.11, optional sandbox preview, changelog, commit, PR. Use when Sameer says "ship it", "/ship", "open the PR" or "make the PR".
---

# Ship a JobRadar change

Run these in order and stop at the first failure (fix it, then carry on). Keep the report to the
user short: the PR link, what's in it, test count, anything skipped and why.

## 1. Branch
- On `main`: `git pull -q`, then `git checkout -b <feat|fix|docs>/<short-kebab-name>`.
- Never stage `.gitignore` lines or files you didn't change (Sameer keeps local edits there).

## 2. Check (all must pass)
```bash
uv run ruff format src tests && uv run ruff check src tests && uv run mypy
uv run pytest -q
UV_PROJECT_ENVIRONMENT=data/venv311 uv sync -q --locked --python 3.11 --extra web --extra postgres --extra saas && UV_PROJECT_ENVIRONMENT=data/venv311 uv run -q --python 3.11 pytest -q
```
The 3.11 run catches what CI (3.11, 3.12, 3.13) catches and the 3.14 `.venv` doesn't, e.g. a
class method named `list` shadowing the `list[...]` annotations below it.

## 3. Preview (only when the change shows on the dashboard or the site)
- Dashboard: `.claude/skills/ship/sandbox.sh`, then start the `dashboard-sandbox` preview (port
  8095). It's a copy of the real data; never run a changed build against `data/jobradar.db`.
- Site: start the `site` preview (port 8137).
- One look at desktop width and one at phone width (light and dark if colours changed). Reset the
  viewport and stop the preview afterwards. Skip this step entirely for back-end-only changes.

## 4. Changelog and docs
- Add a line under `## [Unreleased]` in `CHANGELOG.md` (Added / Changed / Fixed), written for the
  user: what it does for him, not how it's built.
- If a command or setting changed: `docs/COMMANDS.md`, `config.example.yaml`, README if it lists it.

## 5. Commit and PR
- Commit message: a short summary line, a blank line, then why. **No Co-Authored-By line.**
- `git push -u origin <branch>`, then `gh pr create --base main` with a body that says what changes
  for the user, what was checked (tests, preview) and anything left out. End the body with the
  attribution line the system reminder asks for.
- Then read the PR's CI with the ccd_pr tools; don't poll CI yourself.
