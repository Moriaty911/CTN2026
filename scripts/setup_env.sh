#!/usr/bin/env bash
# ============================================================================
#  CTN 2026 —— 一键统一 Python 环境
# ============================================================================
#  幂等：已存在则更新，不存在则创建。可在每台开发机上重复执行。
#
#      ./scripts/setup_env.sh
#
#  依赖：miniforge / miniconda / anaconda 任一（conda 命令可用）
# ============================================================================
set -euo pipefail

ENV_NAME="${CTN_ENV_NAME:-ctn}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

if [ ! -f environment.yml ]; then
  echo "❌ 找不到 environment.yml（应在仓库根目录）" >&2
  exit 1
fi

if ! command -v conda >/dev/null 2>&1; then
  cat >&2 <<'EOF'
❌ 找不到 conda。

  安装 miniforge（推荐，conda-forge 默认渠道，无 Anaconda 商业许可顾虑）：
      https://conda-forge.org/download/

  安装后重开终端，再运行本脚本。
EOF
  exit 1
fi

# 让 conda 在非交互 shell 中可用（脚本环境通常没有 activate）
eval "$(conda shell.bash hook)"

echo "▸ 目标环境: $ENV_NAME"

if conda env list | awk 'NF && $1 !~ /^#/ {print $1}' | grep -qx "$ENV_NAME"; then
  echo "▸ 环境已存在，执行更新（--prune 会移除 environment.yml 中已删除的依赖）"
  conda env update -n "$ENV_NAME" -f environment.yml --prune
else
  echo "▸ 创建环境"
  conda env create -n "$ENV_NAME" -f environment.yml
fi

# ── 版本守卫：与环境钉住的版本一致 ──────────────────────────────────
# ── 版本守卫 + 冒烟测试 ────────────────────────────────────────────
# 注意：不要把脚本用 heredoc 喂给 `conda run` —— stdin 会被静默吞掉，
# 表现为「什么都没输出、退出码却是 0」。所以用独立文件调用。
echo
echo "▸ 预检并跑离线测试"
conda run -n "$ENV_NAME" python scripts/check_env.py --tests

cat <<EOF

✅ 环境就绪

  conda activate $ENV_NAME

  重装/同步依赖:  ./scripts/setup_env.sh
  导出可移植锁:   conda env export -n $ENV_NAME --from-history > environment.lock.yml

EOF
