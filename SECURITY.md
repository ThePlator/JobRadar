# Security Policy

JobRadar runs on your own machine with access to your Telegram account, your Notion workspace, your email and an LLM API key. Security problems are taken seriously, and reports are welcome.

## Supported versions

JobRadar is pre-1.0 and changes quickly. Only the latest code on `main` (and, once releases start, the latest release) receives security fixes.

| Version | Supported |
|---|---|
| `main` / latest release | ✅ |
| Anything older | ❌ Please update first and check whether the problem is still there |

## Reporting a vulnerability

**Please do not open a public issue, pull request or discussion for a security problem.**

Report it privately through GitHub:

1. Go to the repository's **Security** tab.
2. Click **Report a vulnerability**.
3. Describe the problem, how to reproduce it, and what an attacker could do with it.

It helps to include:

- the JobRadar version or commit (`jobradar --version`, `git rev-parse HEAD`)
- your OS, and whether you run it with Docker or `uv`
- a minimal example: a message, link or page that triggers it
- logs, **with every token, key, password, phone number and session file removed**

Never send real credentials or a `tg.session` file as part of a report. If a secret of yours was exposed, revoke it first (see [If a secret leaks](#if-a-secret-leaks)).

### What to expect

JobRadar is maintained by one person in their spare time, so these are targets, not guarantees:

| Step | Target |
|---|---|
| Acknowledge the report | within 7 days |
| First assessment (confirmed or not, severity) | within 14 days |
| Fix for a confirmed high-severity issue | as soon as possible, usually within 30 days |

You'll be kept updated in the private advisory. Once a fix is released, the advisory is published, and you are credited unless you ask not to be.

## Scope

These are in scope, because they would let someone harm a JobRadar user:

- **Secret exposure:** API keys, the Notion token, the email app password, or the Telegram session leaking into logs, Notion pages, LLM prompts, error messages, the Docker image or the git history.
- **Telegram account abuse:** any way to make JobRadar send messages, join chats, or do anything other than read the chats you configured.
- **Server-side request forgery:** a job link, redirect or page that makes JobRadar (or its optional browser) request `localhost`, private network ranges or cloud metadata addresses.
- **Prompt injection with effect:** text in a post or job page that makes JobRadar take an action, change other jobs, leak profile data, or write content outside a job's own Notion page.
- **Notion data loss:** anything that makes JobRadar change or delete content outside its own "JobRadar" toggle, or other users' pages.
- **Injection into generated files:** LaTeX, shell or file-path injection through job or profile data (resume generation, from v0.3).
- **Forged email input:** getting a forwarded job accepted from an address that isn't yours (email forwarding, from v0.4).
- **Supply chain:** vulnerable or malicious dependencies, GitHub Actions or Docker base images used by this repo.

These are out of scope:

- Problems that need an attacker who already controls your machine, your `.env`, or your accounts.
- Wrong or low-quality LLM output (a bad match score, a missed deadline) with no security impact. Please open a normal issue.
- A job site, Telegram channel or Notion itself being malicious or compromised, unless JobRadar makes the impact worse.
- The terms of service of Telegram, WhatsApp or job sites. Following them is the user's responsibility (see the README).
- Rate limiting or denial of service against your own self-hosted instance.

## How JobRadar protects you

Knowing what is already in place helps you judge a report:

- **Secrets stay in `.env`.** They are loaded into typed settings that never print their values. `.env`, `config/`, `data/` (including the Telegram session) and `output/` are git-ignored, and CI runs **gitleaks** on every push.
- **Read-only Telegram.** JobRadar uses your account only to read the chats you list. It never sends, joins, reacts or marks messages as read, and it obeys Telegram's flood-wait limits.
- **Session file locked down.** `data/tg.session` grants full access to your Telegram account. It is set to owner-only permissions (`0600`).
- **No requests into your network.** Every short-link redirect, page fetch and browser sub-request must resolve to a public IP. Loopback, private, link-local and metadata addresses are refused.
- **Polite fetching.** `robots.txt` is obeyed, each site gets at most one request per second, and JobRadar never logs in to job sites.
- **Untrusted text is data.** Posts and pages reach the LLM inside delimiter tags, with an instruction to treat them as data. Tags inside the text are stripped so it cannot close its block. The model has no tools and no write access, and its answer is validated against a fixed schema before use.
- **Narrow Notion writes.** JobRadar only creates and updates pages in the one database you share with it. On existing pages it only replaces its own "JobRadar" toggle, and it never deletes your notes.
- **Spending cap.** LLM use is capped per day (`llm.daily_budget_inr`), so a flood of posts cannot run up an unbounded bill.
- **Least privilege in Docker.** The container runs as a non-root user.
- **Dependency hygiene.** Dependencies are locked (`uv.lock`) and Dependabot proposes weekly updates for Python packages, GitHub Actions and the Docker image.

Planned with the features that need them:

- **Resumes (v0.3):** every value inserted into LaTeX is escaped, and Tectonic runs with shell escape disabled.
- **Email (v0.4):** an app password instead of your account password. Mail goes only to your own address, JobRadar reads only one folder, and it accepts forwards only from your own address with DKIM/SPF passing.

## Running JobRadar safely

- **Use separate, limited credentials.** Use a dedicated Gmail account (or at least an app password) for JobRadar. Share only the JobRadar database with your Notion integration, not your whole workspace.
- **Keep secrets out of chat and screenshots.** Don't paste `.env` values, API hashes, tokens or the session file into issues, AI chats or screen shares.
- **Keep it updated:** `git pull && uv sync`, or pull the latest image.
- **The WhatsApp source is experimental and off by default.** It uses an unofficial client that can get a phone number banned. Use a spare number if you enable it.

## If a secret leaks

Revoke it first, then clean up:

| Secret | Where to revoke it |
|---|---|
| Telegram session (`data/tg.session`) | Telegram app → Settings → Devices → end the JobRadar session. Then delete the file and run `jobradar init` again |
| Telegram `api_hash` | [my.telegram.org](https://my.telegram.org) → API development tools |
| Notion token | notion.so/profile/integrations → your integration → regenerate the secret |
| Gemini / Groq key | Google AI Studio / Groq console → delete the key and create a new one |
| Gmail app password | myaccount.google.com/apppasswords → delete it and create a new one |
| Google Drive token (`data/gdrive_token.json`) | myaccount.google.com/permissions → remove JobRadar's access |

If a secret was committed to git, revoking it is what matters. Rewriting history does not remove it from forks, clones or caches.
