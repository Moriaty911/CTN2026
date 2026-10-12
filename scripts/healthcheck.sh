#!/usr/bin/env bash
# ============================================================================
#  CTN 2026 — container health check
# ============================================================================
#  Two layers:
#
#    1. If $CTN_STATE_DIR/heartbeat exists, judge liveness by its freshness.
#       This is the honest signal: while the main loop runs, it touches the
#       file periodically. A process that is wedged but has not exited can only
#       be detected this way.
#
#    2. If no heartbeat exists yet (main loop not implemented), degrade to the
#       environment pre-check — which at least catches "wrong interpreter" and
#       "broken install".
#
#  Why not `pgrep python`: while the main loop is blocked on a network wait the
#  process is still alive, so pgrep reports healthy while the fleet has in fact
#  stopped producing. The heartbeat is the only signal that does not lie.
# ============================================================================
set -euo pipefail

STATE_DIR="${CTN_STATE_DIR:-/state}"
HEARTBEAT="$STATE_DIR/heartbeat"
MAX_AGE="${CTN_HEARTBEAT_MAX_AGE_S:-300}"

mtime_of() {
  # GNU stat and BSD stat differ; try both
  stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null
}

if [ -f "$HEARTBEAT" ]; then
  now="$(date +%s)"
  mtime="$(mtime_of "$HEARTBEAT" || echo 0)"
  age=$(( now - mtime ))
  if [ "$age" -le "$MAX_AGE" ]; then
    echo "healthy: heartbeat ${age}s old (max ${MAX_AGE}s)"
    exit 0
  fi
  echo "unhealthy: heartbeat stale ${age}s > ${MAX_AGE}s" >&2
  exit 1
fi

# Heartbeat not implemented yet — degrade to the environment pre-check
echo "note: no heartbeat at $HEARTBEAT (main loop not implemented yet); falling back to env preflight"
exec python scripts/check_env.py
