# JobRadar: notes for Claude Code

The rule book for any AI (what JobRadar is, where the work stands, conventions, decisions,
roadmap) is AGENTS.md, imported here:

@AGENTS.md

## Claude Code specifics

- **Ship a change** with the `ship` skill ("ship it"): branch, ruff, mypy, tests on 3.14 and
  3.11, optional preview, changelog, commit, PR.
- **Previews**: `.claude/skills/ship/sandbox.sh` copies the real data to `data/sandbox`; then start
  the `dashboard-sandbox` preview (port 8095). The `site` preview serves `site/` on port 8137.
- **Memory**: agreed plans are saved as project memories (`plan-<feature>`); check them before
  planning anything new and build from them.
- Keep answers short; don't spawn subagents unless asked.
