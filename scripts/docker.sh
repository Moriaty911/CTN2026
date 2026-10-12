#!/usr/bin/env bash
# ============================================================================
#  CTN 2026 —— Docker 便捷入口
# ============================================================================
#      ./scripts/docker.sh build     构建运行时镜像
#      ./scripts/docker.sh verify    构建自检镜像并跑全部测试（离线）
#      ./scripts/docker.sh up        后台启动舰队
#      ./scripts/docker.sh down      停止
#      ./scripts/docker.sh restart   重启
#      ./scripts/docker.sh logs      跟随日志
#      ./scripts/docker.sh ps        状态
#      ./scripts/docker.sh shell     进容器排查
#      ./scripts/docker.sh prepare   修好 state/logs 的属主（首次部署必做）
# ============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

if ! command -v docker >/dev/null 2>&1; then
  echo "❌ 找不到 docker" >&2
  exit 1
fi

# compose v2 插件优先，回退到老版 docker-compose
if docker compose version >/dev/null 2>&1; then
  DC=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  DC=(docker-compose)
else
  echo "❌ 找不到 docker compose" >&2
  exit 1
fi

require_daemon() {
  if ! docker info >/dev/null 2>&1; then
    cat >&2 <<'EOF'
❌ Docker daemon 未运行。

  macOS:  打开 Docker Desktop，或执行  open -a Docker
  Linux:  sudo systemctl start docker
EOF
    exit 1
  fi
}

require_env() {
  if [ ! -f .env ]; then
    cat >&2 <<'EOF'
❌ 缺少 .env（密钥从这里注入容器，不进镜像）

  cp -n .env.example .env
  chmod 600 .env
  # 然后填入 TYPESAFE_API_KEY 等
EOF
    exit 1
  fi
}

case "${1:-}" in
  build)
    require_daemon
    "${DC[@]}" build --target runtime fleet
    echo "✅ 镜像已构建"
    ;;

  verify)
    require_daemon
    # 纯离线自检：版本守卫 + 全部测试。不需要 .env，不需要网络。
    "${DC[@]}" --profile test build --target verify verify
    "${DC[@]}" --profile test run --rm verify
    ;;

  prepare)
    # 容器以 UID/GID 10001 运行；宿主目录属主不对就写不进去。
    # 这是挂卷部署最常见的坑。
    mkdir -p state logs
    if [ "$(id -u)" -eq 0 ]; then
      chown -R 10001:10001 state logs
    else
      sudo chown -R 10001:10001 state logs
    fi
    echo "✅ state/ 与 logs/ 属主已设为 10001:10001"
    ;;

  up)
    require_daemon; require_env
    "${DC[@]}" up -d fleet
    "${DC[@]}" ps
    echo
    echo "日志:  ./scripts/docker.sh logs"
    ;;

  down)     require_daemon; "${DC[@]}" down ;;
  restart)  require_daemon; "${DC[@]}" restart fleet ;;

  logs)     require_daemon; "${DC[@]}" logs -f --tail=100 fleet ;;

  ps)       require_daemon; "${DC[@]}" ps ;;

  shell)
    require_daemon; require_env
    # 复用运行中的容器；没有则临时起一个
    if "${DC[@]}" ps --status running --services 2>/dev/null | grep -qx fleet; then
      "${DC[@]}" exec fleet bash
    else
      "${DC[@]}" run --rm --entrypoint bash fleet
    fi
    ;;

  *)
    sed -n '3,16p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
