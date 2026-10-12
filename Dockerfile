# ============================================================================
#  CTN 2026 —— 运行镜像
# ============================================================================
#  为什么用 Docker，而不是在服务器上装 conda：
#
#      解释器版本决定这份代码能否运行。2026-10 实测同一份代码：
#          Python 3.14.8   → 319/319 测试通过
#          Python 3.13.13  → 229 failures + 130 errors
#
#      根因是 ctn/jev/inputs.py 的 Optional 解包依赖
#      `typing.Union is types.UnionType`，而 Python 3.14 才统一两者。
#
#      镜像把解释器钉死，服务器上就不存在「版本恰好不对」这个变量。
#      代码本身也应修成版本无关（见 origin is UnionType or origin is Union），
#      但那是纵深防御的第二层，不是第一层。
#
#  目录约定（与架构设计一致）：
#      /app     代码（进镜像，来自 git）
#      /state   运行时状态（挂卷，绝不进镜像）—— sqlite / 曝光日志 / 检查点
#      /logs    遥测 JSONL（挂卷，绝不进镜像）
#      .env     密钥（绝不进镜像，由 compose 的 env_file 注入）
# ============================================================================

# syntax=docker/dockerfile:1


# ---------------------------------------------------------------------------
# 阶段 1 —— 依赖（独立层，代码改动不会让依赖重装）
# ---------------------------------------------------------------------------
FROM python:3.14-slim AS deps

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY requirements.txt ./
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install -r requirements.txt


# ---------------------------------------------------------------------------
# 阶段 2 —— 自检（CI / 发布前，用 --target verify 构建）
# ---------------------------------------------------------------------------
FROM deps AS verify

WORKDIR /app
COPY . .
# 解释器版本守卫 + 完整离线测试
RUN /opt/venv/bin/python scripts/check_env.py --tests


# ---------------------------------------------------------------------------
# 阶段 3 —— 运行时
# ---------------------------------------------------------------------------
FROM python:3.14-slim AS runtime

# 与其他容器/宿主的时间对齐；主办方公告使用 AEST
ENV TZ=Australia/Sydney \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    CTN_STATE_DIR=/state \
    CTN_LOG_DIR=/logs

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && ln -snf "/usr/share/zoneinfo/$TZ" /etc/localtime \
 && echo "$TZ" > /etc/timezone \
 && rm -rf /var/lib/apt/lists/*

# 固定 UID/GID：宿主挂载目录的属主必须是这个数字，否则写不进 state/logs
RUN groupadd --gid 10001 ctn \
 && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin ctn

COPY --from=deps /opt/venv /opt/venv
WORKDIR /app
COPY --chown=ctn:ctn . .

# 状态与日志必须是卷，镜像内只留空目录占位
RUN mkdir -p /state /logs && chown -R ctn:ctn /state /logs
VOLUME ["/state", "/logs"]

USER ctn

# 健康检查：优先看主循环的心跳文件，没有则退化为环境预检
HEALTHCHECK --interval=60s --timeout=15s --start-period=20s --retries=3 \
  CMD ["bash", "scripts/healthcheck.sh"]

# 说明：主循环 ctn/main.py 尚未实现（Jev-API 分支目前只有决策层）。
# 因此默认命令先做环境预检，保证镜像可启动、可被 compose 覆盖。
# 主循环就位后改成：
#     CMD ["python", "-m", "ctn.main"]
CMD ["python", "scripts/check_env.py", "--tests"]
