#!/usr/bin/env bash
# ============================================================================
#  CTN 2026 — Docker convenience entry point
# ============================================================================
#      ./scripts/docker.sh configure create missing configuration templates
#      ./scripts/docker.sh build     build fleet + Pi worker images
#      ./scripts/docker.sh verify    build the self-check image and run the full offline suite
#      ./scripts/docker.sh up        build and start fleet + Pi worker
#      ./scripts/docker.sh up all    also build and start Discord review
#      ./scripts/docker.sh down      stop
#      ./scripts/docker.sh restart   restart
#      ./scripts/docker.sh logs      follow logs
#      ./scripts/docker.sh ps        status
#      ./scripts/docker.sh shell     open a shell for debugging
#      ./scripts/docker.sh prepare   prepare mounted directories for first deploy
#      build/restart/logs also accept "all" to include Discord
# ============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

if [ "${1:-}" = configure ]; then
  umask 077
  for config in .env .env.macro .env.discord; do
    if [ ! -f "$config" ]; then
      cp "$config.example" "$config"
      echo "Created $config; fill in the required values before deployment."
    fi
    chmod 600 "$config"
  done
  mkdir -p state/pi logs
  echo "OK: existing configuration values preserved."
  exit 0
fi

SERVICES=(fleet macro-agent)
case "${2:-}" in
  "") ;;
  all) SERVICES+=(discord-review) ;;
  *) echo "FAIL: optional second argument must be 'all'" >&2; exit 1 ;;
esac

if ! command -v docker >/dev/null 2>&1; then
  echo "FAIL: docker not found" >&2
  exit 1
fi

# Prefer the compose v2 plugin, fall back to the legacy docker-compose binary
if docker compose version >/dev/null 2>&1; then
  DC=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  DC=(docker-compose)
else
  echo "FAIL: docker compose not found" >&2
  exit 1
fi

require_daemon() {
  if ! docker info >/dev/null 2>&1; then
    cat >&2 <<'EOF'
FAIL: the Docker daemon is not running.

  macOS (Docker Desktop):  open -a Docker
  macOS (OrbStack):        open -a OrbStack
  Linux:                   sudo systemctl start docker
EOF
    exit 1
  fi
}

require_env() {
  if [ ! -f .env ]; then
    cat >&2 <<'EOF'
FAIL: .env is missing (secrets are injected from it and never enter the image)

  ./scripts/docker.sh configure
  # then fill in TYPESAFE_API_KEY and the rest
EOF
    exit 1
  fi
  if [ "${2:-}" = all ] && [ ! -f .env.discord ]; then
    echo "FAIL: .env.discord is missing; run ./scripts/docker.sh configure and fill in Discord configuration." >&2
    exit 1
  fi
}

case "${1:-}" in
  build)
    require_daemon
    # The build target (runtime / verify) lives in docker-compose.yml under
    # build.target. `docker compose build` has no --target flag of its own
    # (that belongs to `docker build`).
    "${DC[@]}" build "${SERVICES[@]}"
    echo "OK: images built"
    ;;

  verify)
    require_daemon
    # Fully offline self-check: version guard plus the full test suite.
    # Needs no .env and no network.
    "${DC[@]}" --profile test build verify
    "${DC[@]}" --profile test run --rm verify
    ;;

  prepare)
    # The container runs as UID/GID 10001; if the host directories are owned by
    # anyone else it cannot write to them. This is the most common pitfall when
    # deploying with bind mounts (on Linux; macOS mount layers map ownership).
    mkdir -p state/pi logs
    if [ "$(id -u)" -eq 0 ]; then
      chown -R 10001:10001 state logs
    else
      sudo chown -R 10001:10001 state logs
    fi
    echo "OK: state/ and logs/ now owned by 10001:10001"
    ;;

  up)
    require_daemon; require_env "$@"
    "${DC[@]}" up -d --build "${SERVICES[@]}"
    "${DC[@]}" --profile discord ps
    echo
    echo "logs:  ./scripts/docker.sh logs ${2:-}"
    ;;

  down)     require_daemon; "${DC[@]}" --profile discord down ;;
  restart)  require_daemon; "${DC[@]}" restart "${SERVICES[@]}" ;;

  logs)     require_daemon; "${DC[@]}" logs -f --tail=100 "${SERVICES[@]}" ;;

  ps)       require_daemon; "${DC[@]}" --profile discord ps ;;

  shell)
    require_daemon; require_env
    # Reuse the running container when there is one; otherwise start a temporary one
    if "${DC[@]}" ps --status running --services 2>/dev/null | grep -qx fleet; then
      "${DC[@]}" exec fleet bash
    else
      "${DC[@]}" run --rm --entrypoint bash fleet
    fi
    ;;

  *)
    sed -n '3,19p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
