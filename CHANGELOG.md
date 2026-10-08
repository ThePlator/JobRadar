# Changelog

All notable changes to JobRadar are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions will follow [Semantic Versioning](https://semver.org/) from the first release.

## [Unreleased]

### Added

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
