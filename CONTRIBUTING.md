# Contributing to JobRadar

Thanks for helping. JobRadar is a small project maintained in spare time, so clear, focused contributions get merged fastest.

By taking part you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md). Security problems go through the [security policy](SECURITY.md), never a public issue.

## Ways to help

- **Report a bug** or **suggest a feature** with the issue templates.
- **Add link rules.** Missed ads, new link shorteners, or job sites that need special handling are common, small and useful fixes (`src/jobradar/pipeline/links.py`).
- **Label job postings** for the extraction accuracy test (see [Test data](#test-data)).
- **Improve the docs or the [wiki](https://github.com/ThePlator/JobRadar/wiki).**
- **Pick up a task** from [PLAN.md](PLAN.md). Please open an issue first for anything bigger than a bug fix, so we can agree on the approach before you spend time on it.

## Development setup

You need Python 3.11+, git and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/ThePlator/JobRadar.git
cd JobRadar
uv sync
uv run pre-commit install
```

You don't need any accounts or API keys to develop or run the tests. Telegram, Notion, the LLM and websites are all faked in tests. To try a change end to end, follow the [Getting Started](https://github.com/ThePlator/JobRadar/wiki/Getting-Started) guide with your own keys.

## Before you open a pull request

Run the same checks as CI:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

`uv run ruff format .` fixes formatting. CI also runs gitleaks (secret scanning) and a Docker build.

## Pull requests

1. Branch from `main`: `feat/…`, `fix/…` or `docs/…`.
2. Keep one change per pull request. A refactor and a feature are two PRs.
3. **Add or update tests** for any behaviour you change. Bug fixes should include a test that fails without the fix.
4. **Update the docs** when behaviour or configuration changes: `config.example.yaml`, `README.md`, `docs/LLD.md`, and a line in [CHANGELOG.md](CHANGELOG.md) under *Unreleased*.
5. Write commit messages in the imperative ("Hide ad links attached to many jobs") and explain *why* in the body when it isn't obvious.
6. Fill in the pull request template. CI must be green before review.

## Project conventions

- **Code style:** ruff (lint and format, 100 columns) and mypy in strict mode for `src/`. Add type hints to everything.
- **Layout:** follows [docs/LLD.md](docs/LLD.md). Sources in `sources/`, pipeline steps in `pipeline/`, outputs in `sinks/`, all database queries in `db/repo.py`.
- **Tasks must be idempotent.** Every pipeline step runs as a queued task that may be retried or run twice after a crash. Use the `(type, key)` uniqueness of tasks and `ON CONFLICT` writes. Never assume a step runs exactly once.
- **Errors:** raise `RetryableError` for temporary failures, `PermanentError` when retrying won't help, and `Defer` to wait without using up an attempt (see `queue/tasks.py`).
- **Prompts** live in `src/jobradar/prompts/*.md`. If you change a prompt, **bump its `version:` line**. That clears cached answers. Untrusted text (posts, pages) must stay inside its delimiter tags.
- **Database:** there are no migrations yet; new tables are created automatically, but new *columns* on existing tables are not. Open an issue before changing an existing table.
- **Notion:** JobRadar only writes the properties listed in `sinks/notion.py` (`SCHEMA`) and its own "JobRadar" toggle. Never touch other blocks on a user's page.

## Tests

- Tests live in `tests/`. Shared fixtures (temporary database, fake clock, queue) are in `tests/conftest.py`; fakes for the LLM are in `tests/fakes.py`.
- HTTP is mocked with [respx](https://lundberg.github.io/respx/). **Tests must never reach the network.**
- Tests are isolated from your real `.env` automatically. Never put real tokens, phone numbers or session files in tests or fixtures.

### Test data

The v0.2 accuracy check needs real job posts with hand-checked answers in `tests/fixtures/`. To contribute one:

- Use only **public** job posts, and remove names, phone numbers and personal email addresses.
- Save the post text, the job link, and the correct company, role, location, deadline and batch years.

## Security and privacy

- Never commit `.env`, `config/`, `data/`, `output/`, `*.session` or credentials. They are git-ignored; keep it that way.
- New outbound requests must go through the public-address check (`jobradar.net.is_public_host`), as the fetcher and link expander do.
- If you find a vulnerability, report it privately as described in [SECURITY.md](SECURITY.md).

## License

By contributing, you agree that your contributions are licensed under the project's [MIT License](LICENSE).
