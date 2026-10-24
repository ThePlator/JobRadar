#!/usr/bin/env bash
# A throwaway copy of your JobRadar data for previewing the dashboard on port 8095.
# Your real database, config and profile are never touched. Run from the repo root.
set -euo pipefail
root="$(cd "$(dirname "$0")/../../.." && pwd)"
box="$root/data/sandbox"
rm -rf "$box" && mkdir -p "$box/config" "$box/data"
cp "$root/config/config.yaml" "$root/config/profile.yaml" "$box/config/"
printf '\nweb:\n  port: 8095\n' >> "$box/config/config.yaml"
: > "$box/.env"  # no secrets: the dashboard doesn't need them
"$root/.venv/bin/python" - "$root/data/jobradar.db" "$box/data/jobradar.db" <<'PY'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
src.backup(sqlite3.connect(sys.argv[2]))
PY
echo "sandbox ready: $box (start it with the dashboard-sandbox preview)"
