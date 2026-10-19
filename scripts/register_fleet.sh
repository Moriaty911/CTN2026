#!/usr/bin/env bash
# Register the personas in docs/personas-80/handles-2026-10-19.tsv that are not registered yet, one by one.
# Run by the operator (registration is a real competition action). Stops at the first failed/unknown result;
# check it with `ops fleet check --account <name>` before running again. Already registered accounts are skipped.
# The platform allows 5 registrations a minute per address; the client paces itself, so 47 take about 10 minutes.
set -euo pipefail
cd "$(dirname "$0")/.."
TSV=docs/personas-80/handles-2026-10-19.tsv
ops() { docker compose exec -T fleet python -m ctn.ops "$@"; }

grep -v '^#' "$TSV" | while IFS=$'\t' read -r persona account; do
  [ -n "$persona" ] || continue
  out=$(ops fleet register --size 50 --persona-id "$persona" --account "$account" --execute 2>&1) || true
  status=$(printf '%s' "$out" | python3 -c 'import json,sys
try: r=json.load(sys.stdin)["results"]; print(r[0]["status"] if r else "not_in_plan")
except Exception: print("error")')
  printf '%s\t%s\t%s\n' "$persona" "$account" "$status"
  case "$status" in
    succeeded|exists) ;;
    *) echo "STOP: $account -> $status"; printf '%s\n' "$out" | tail -20; exit 1 ;;
  esac
done
echo "done; restart fleet so the new accounts' notifications are polled: docker compose --profile discord up -d fleet macro-agent discord-review"
