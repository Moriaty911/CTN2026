#!/usr/bin/env bash
# ============================================================================
#  CTN 2026 —— 容器健康检查
# ============================================================================
#  两层判断：
#
#    1. 若 $CTN_STATE_DIR/heartbeat 存在，以它的新鲜度为准 —— 这才是真正的
#       **存活**检查：主循环在跑，就会定期 touch 它。进程卡死但没退出时，
#       只有心跳能发现。
#
#    2. 心跳不存在（主循环尚未实现），退化为环境预检 —— 至少能抓住
#       「解释器版本不对 / 依赖装坏」这类问题。
#
#  为什么不用 `pgrep python`：主循环卡在网络等待里时进程仍然活着，
#  pgrep 会报健康，而实际上舰队已经停止产出。心跳是唯一诚实的信号。
# ============================================================================
set -euo pipefail

STATE_DIR="${CTN_STATE_DIR:-/state}"
HEARTBEAT="$STATE_DIR/heartbeat"
MAX_AGE="${CTN_HEARTBEAT_MAX_AGE_S:-300}"

mtime_of() {
  # GNU stat 与 BSD stat 的取法不同，两者都试
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

# 心跳尚未实现 —— 退化为环境预检
echo "note: no heartbeat at $HEARTBEAT (main loop not implemented yet); falling back to env preflight"
exec python scripts/check_env.py
