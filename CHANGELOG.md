# Changelog

All notable changes to JobRadar are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions will follow [Semantic Versioning](https://semver.org/) from the first release.

## [Unreleased]

### Added

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
- Notion rejected long posts with emoji, because it counts them as two characters.
- `go.acciojob.com` links weren't expanded, so a course ad became a fake job.
- Telegram "Apply" buttons were missed with the newer Telegram API layer.
- Configuration errors pointed at the wrong setting when a section was missing.

[Unreleased]: https://github.com/ThePlator/JobRadar/commits/main
