# JobRadar commands

Every command you need to set up, run, watch, tune and develop JobRadar, with what it does. Run them from the project folder (`cd JobRadar`).

- [First-time setup](#first-time-setup)
- [Start, stop, restart](#start-stop-restart)
- [JobRadar commands](#jobradar-commands)
- [Watching what happens](#watching-what-happens)
- [Changing settings](#changing-settings)
- [Updating to a new version](#updating-to-a-new-version)
- [Files and folders](#files-and-folders)
- [Fixing common problems](#fixing-common-problems)
- [Development](#development)

## First-time setup

```bash
uv sync
```
Installs JobRadar and its dependencies into `.venv/`.

```bash
uv run jobradar init
```
Copies `config.example.yaml` → `config/config.yaml` and `profile.example.yaml` → `config/profile.yaml` if they don't exist yet, logs in to Telegram (a code arrives in your Telegram app; nothing is posted), and checks that your Notion database has the right columns. Fill in `.env` first (see `.env.example`).

```bash
uv run jobradar chats
```
Lists every Telegram chat your account can read, with the ids to paste into `sources.telegram.chats` in `config/config.yaml`.

```bash
uv run jobradar alerts --test
```
Sends you one example alert email, so you know Gmail sending works.

## Start, stop, restart

JobRadar runs inside a `tmux` session called `jobradar`, so it keeps running after you close the terminal.

| What | Command |
|---|---|
| Start | `tmux new -d -s jobradar "uv run jobradar run 2>&1 \| tee -a data/run.log"` |
| Stop | `tmux kill-session -t jobradar` |
| Restart (after a config change or update) | stop, then start |
| Is it running? | `tmux ls` (shows `jobradar: 1 windows …` when it is) |
| Look at it live | `tmux attach -t jobradar`, then leave with `Ctrl+b` then `d` |

Don't press `Ctrl+c` inside the attached session: that stops JobRadar.

To run it in the foreground instead (stops when you close the terminal):

```bash
uv run jobradar run
```

On every start, JobRadar catches up on Telegram posts it missed, re-scores open jobs if you changed weights or thresholds, re-matches them if you edited your profile, and schedules the next digest.

## JobRadar commands

| Command | What it does |
|---|---|
| `jobradar run` | Start everything: Telegram reader, page reading, matching, Notion sync, alerts and digests |
| `jobradar init` | First-time setup (see above) |
| `jobradar chats` | List Telegram chats with their ids |
| `jobradar add <url>` | Process one job link by hand and add it to Notion |
| `jobradar extract --missing` | Read jobs that were only listed (from before page reading existed) |
| `jobradar extract --compare` | Check how well free reading (page data, post labels) agrees with earlier Gemini answers |
| `jobradar stats --days 7` | Posts received, jobs created, reposts merged, ads hidden, likely duplicates, failed tasks |
| `jobradar retry --failed` | Put failed tasks back in the queue; they run on the next `jobradar run` |
| `jobradar alerts --test` | Send yourself a test alert email (works even in dry run) |
| `jobradar digest --now` | Send the morning/evening digest for the current window right away |
| `jobradar resume <job_id>` | *Not built yet (v0.3)*: rebuild a tailored resume |
| `jobradar doctor` | *Not built yet*: check keys, tools and access |

Prefix each with `uv run`, e.g. `uv run jobradar stats --days 3`.

### `jobradar add <url>`

```bash
uv run jobradar add "https://careers.example.com/jobs/123"
```
Treats the link as if it was posted in a channel: cleans it, reads the page, matches it against your profile and creates or updates its Notion row. A link JobRadar already knows only gets a new sighting.

### `jobradar extract`

```bash
uv run jobradar extract --missing
```
Queues every job that has no details yet. `jobradar run` then reads them from page data and post labels, asking Gemini only when those miss role or company.

```bash
uv run jobradar extract --compare --examples 5
```
Re-reads jobs Gemini read earlier with the free layers and prints, per field, how many were found and how often they agree. `--examples 5` shows up to 5 disagreements per field. Add `--refetch` to download the pages again first (slow: one request per second per site, with no progress output).

### `jobradar stats`

```bash
uv run jobradar stats --days 3
```
A report for the last N days: posts received, posts without a job link, jobs created, reposts merged, ads hidden, likely duplicates (same job under different links), per-chat counts and recent failures.

### `jobradar retry --failed`

```bash
uv run jobradar retry --failed
```
A task fails after 5 attempts (e.g. a site down for hours). This puts them back; they run the next time JobRadar is running.

### `jobradar alerts --test` and `jobradar digest --now`

```bash
uv run jobradar alerts --test
```
Sends "[JobRadar] Test email: alerts reach you" with an example job card to `NOTIFY_TO` (or `EMAIL_ADDRESS`).

```bash
uv run jobradar digest --now
```
Builds the digest for the time since the last slot (09:00 or 19:00) and sends it, or saves a preview in dry run. Prints "nothing new… not sent" when there is nothing to report.

## Watching what happens

Everything JobRadar does is written to `data/run.log`.

| What | Command |
|---|---|
| Follow the log live (`Ctrl+c` stops watching only) | `tail -f data/run.log` |
| Follow only the important lines | `tail -f data/run.log \| grep --line-buffered -E "new job\|job extracted\|job matched\|job hidden\|alert email\|would have emailed\|digest\|budget\|ERROR\|Traceback"` |
| Watch for alerts | `tail -f data/run.log \| grep --line-buffered -E "new job\|decision=alert\|alert skipped\|alert email sent\|could not send"` |
| What was hidden today, and why | `grep "job hidden" data/run.log \| grep "$(date +%F)" \| cut -c1-160` |
| Emails sent | `grep -E "alert email sent\|digest sent" data/run.log \| tail` |
| Problems only | `grep -E "WARNING\|ERROR\|Traceback" data/run.log \| tail -20` |
| Jobs read without Gemini | `grep 'job extracted' data/run.log \| grep -c 'llm=False'` |
| Dry-run previews | `ls data/alerts/` (open any `.html` file in a browser) |

What the main log lines mean:

| Line | Meaning |
|---|---|
| `telegram listening chats=7` | Connected and waiting for posts |
| `new job` | A new job link arrived |
| `duplicate job, added sighting` | A repost of a job JobRadar already has |
| `link hidden … reason=promo` | A channel ad or promo link, hidden |
| `job extracted … llm=False` | Details read from page data or post labels, no Gemini call |
| `job matched score=86 … decision=inbox` | Scored and kept in your Notion Inbox |
| `job matched … decision=alert` | A strong match: an alert email is on its way |
| `alert skipped why=…` | Strong, but too old or its deadline passed |
| `job hidden why=not eligible: …` / `weak match (32)` | Hidden in Notion, with the reason |
| `alert email sent` / `digest sent` | An email went out |
| `dry run: would have emailed` | Dry run is on: a preview was saved instead |
| `kept your Notion status` | JobRadar wanted to hide a job you had already moved, and left it alone |
| `daily LLM budget reached` | Gemini work paused until midnight |
| `task error, will retry` | A temporary failure (site or Gemini busy); retried automatically |

## Changing settings

Settings live in `config/config.yaml`; secrets in `.env`; your profile in `config/profile.yaml`. **Restart JobRadar after any change.**

Check the config is valid before restarting:

```bash
uv run python -c "from jobradar.config import load_settings; s = load_settings(); print('config OK')"
```

Common changes (`config/config.yaml`):

| To… | Set |
|---|---|
| Get more or fewer alerts | `scoring.alert_above` (75 = more, 85 = only very strong) |
| Hide fewer or more weak jobs | `scoring.hide_below` (default 40) |
| Send real emails instead of previews | `notify.dry_run: false` |
| One email per job instead of grouping follow-ups | `notify.alert_batch_minutes: 0` |
| Limit emails per day | `notify.max_alerts_per_day` |
| Change digest times | `notify.digest_times: ["09:00", "19:00"]` |
| Turn the digest off | `notify.digest: false` |
| Let Gemini do more per day | `llm.daily_budget_inr` (an estimate at list price; ₹0 on the free tier) |
| Ask Gemini for every job (adds summaries and skills) | `llm.extract_mode: always` |
| Hide or keep "CSE/IT only" jobs | `matching.branch_strict: true` / `false` |
| Graduation years you accept | `filters.batch_years: [2025, 2026]` |
| Cities (empty = anywhere) | `filters.locations: []` |
| Never show a company | `filters.exclude_companies: ["Acme"]` |
| Re-weight the Match Score (free, no new Gemini calls) | `matching.weights` |
| Add or remove a channel | `sources.telegram.chats` (ids from `jobradar chats`) |
| Ignore a site or ad link | `links.deny: ["example.com/courses"]` |

Editing `config/profile.yaml`, `filters` or `matching.branch_strict` re-matches all open jobs at the next start: about one Gemini call per job.

## Updating to a new version

```bash
git pull
```

```bash
uv sync
```

Then restart (stop, then start). New database columns and tables are added automatically on start.

## Files and folders

| Path | What it is | Safe to delete? |
|---|---|---|
| `config/config.yaml` | Your settings | No |
| `config/profile.yaml` | Your education, skills, experience, projects | No |
| `.env` | Keys and passwords | No, and never share or commit it |
| `data/jobradar.db` | Every job, match, alert and task | No: this is JobRadar's memory |
| `data/tg.session` | Your Telegram login | Only to log out (then run `jobradar init` again); never share it |
| `data/run.log` | The log | Yes (JobRadar starts a new one) |
| `data/alerts/` | Dry-run previews of alerts and digests | Yes |
| `output/` | Generated resumes (v0.3) | Yes |

Back up before risky changes:

```bash
cp -r data data-backup
```

## Fixing common problems

| Problem | What to do |
|---|---|
| `tmux kill-session` says "no server running" | JobRadar wasn't running. Just start it |
| No alert emails | Check `notify.dry_run` is `false`, run `jobradar alerts --test`, and check the log for `decision=alert`. Each job alerts once, only if first seen in the last 2 days with its deadline not passed |
| `email login rejected` | The Gmail app password in `.env` is wrong or revoked: create a new one. JobRadar doesn't retry, so your account can't get locked |
| `daily LLM budget reached` | Wait until midnight, or raise `llm.daily_budget_inr` and restart |
| A good job was hidden | Look at its reason in the log or Notion; adjust `scoring.hide_below`, `matching.branch_strict` or `filters`, or tell us in an issue |
| An ad shows up as a job | Add its link to `links.deny`, or report it with the "Ad or link" issue template |
| Tasks failed | `jobradar retry --failed`, then make sure JobRadar is running |
| Config error on start | The message names the setting; YAML needs exactly 2 spaces per level |

## Development

```bash
uv run pytest -q
```
Runs the tests (no network, no real keys needed).

```bash
uv run ruff check .
```
```bash
uv run ruff format .
```
```bash
uv run mypy
```
Lint, format and type-check: CI runs the same.

```bash
uv run pre-commit install
```
Runs the checks (and secret scanning) on every commit.

See [CONTRIBUTING.md](../CONTRIBUTING.md) for the full workflow.
