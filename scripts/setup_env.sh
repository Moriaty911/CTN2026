#!/usr/bin/env bash
# ============================================================================
#  CTN 2026 — one-command unified Python environment
# ============================================================================
#  Idempotent: updates an existing environment, creates one if absent. Safe to
#  re-run on every machine.
#
#      ./scripts/setup_env.sh
#
#  Requires: miniforge / miniconda / anaconda (any of them provides `conda`)
# ============================================================================
set -euo pipefail

ENV_NAME="${CTN_ENV_NAME:-ctn}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

if [ ! -f environment.yml ]; then
  echo "FAIL: environment.yml not found (expected at the repository root)" >&2
  exit 1
fi

if ! command -v conda >/dev/null 2>&1; then
  cat >&2 <<'EOF'
FAIL: conda not found.

  Install miniforge (recommended: conda-forge by default, no Anaconda
  commercial-licence concerns):
      https://conda-forge.org/download/

  Then reopen the terminal and run this script again.
EOF
  exit 1
fi

# Make conda usable in a non-interactive shell (scripts usually have no activate)
eval "$(conda shell.bash hook)"

echo "==> target environment: $ENV_NAME"

if conda env list | awk 'NF && $1 !~ /^#/ {print $1}' | grep -qx "$ENV_NAME"; then
  echo "==> environment exists, updating (--prune removes dependencies dropped from environment.yml)"
  conda env update -n "$ENV_NAME" -f environment.yml --prune
else
  echo "==> creating environment"
  conda env create -n "$ENV_NAME" -f environment.yml
fi

# ── Version guard + smoke test ─────────────────────────────────────────────
# NOTE: never feed a script to `conda run` via a heredoc — stdin is swallowed
# silently, which looks like "everything passed" while producing no output.
# Call a real file instead.
echo
echo "==> preflight and offline tests"
conda run -n "$ENV_NAME" python scripts/check_env.py --tests

cat <<EOF

Environment ready.

  conda activate $ENV_NAME

  Re-sync dependencies:  ./scripts/setup_env.sh
  Export a portable lock: conda env export -n $ENV_NAME --from-history > environment.lock.yml

EOF
