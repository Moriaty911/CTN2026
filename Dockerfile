# syntax=docker/dockerfile:1
# ============================================================================
#  CTN 2026 — runtime image
# ============================================================================
#  Why Docker rather than installing conda on the server:
#
#      The interpreter version decides whether this code runs at all.
#      Measured 2026-10 on the same unchanged code:
#          Python 3.14.8   -> 319/319 tests pass
#          Python 3.13.13  -> 229 failures + 130 errors
#
#      Root cause: the Optional unwrapping in ctn/jev/inputs.py depends on
#      `typing.Union is types.UnionType`, and Python 3.14 is the first release
#      where the two are unified.
#
#      The image pins the interpreter, so "the server happens to have the wrong
#      version" stops being a variable. The code should also be made
#      version-agnostic (`origin is UnionType or origin is Union`), but that is
#      the second layer of defence, not the first.
#
#  Directory contract (matches the architecture design):
#      /app     code            (in the image, from git)
#      /state   runtime state   (volume, never in the image)
#               sqlite / exposure log / checkpoints
#      /logs    telemetry JSONL (volume, never in the image)
#      .env     secrets         (never in the image; injected via compose)
# ============================================================================


# ---------------------------------------------------------------------------
# Stage 1 — dependencies (separate layer; code changes do not reinstall)
# ---------------------------------------------------------------------------
FROM python:3.14.6-slim AS deps

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY requirements.txt ./
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install -r requirements.txt


# ---------------------------------------------------------------------------
# Stage 2 — self-check (CI / pre-release; build with --target verify)
# ---------------------------------------------------------------------------
FROM deps AS verify

WORKDIR /app
COPY . .
# Include optional UI dependencies in offline verification.
RUN /opt/venv/bin/pip install -r requirements-discord.lock
# Interpreter version guard plus the full offline test suite
RUN /opt/venv/bin/python scripts/check_env.py --tests


# ---------------------------------------------------------------------------
# Stage 3 — runtime
# ---------------------------------------------------------------------------
FROM python:3.14.6-slim AS runtime

# Align clocks with containers and the host; organisers publish times in AEST.
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

# Fixed UID/GID: the owner of the host-mounted directories must match this
# number, otherwise the container cannot write to state/ and logs/.
RUN groupadd --gid 10001 ctn \
 && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin ctn

COPY --from=deps /opt/venv /opt/venv
WORKDIR /app
COPY --chown=ctn:ctn . .

# State and logs must be volumes; the image only carries empty placeholders.
RUN mkdir -p /state /logs && chown -R ctn:ctn /state /logs
VOLUME ["/state", "/logs"]

USER ctn

# Health check requires a fresh service heartbeat without a recorded error.
HEALTHCHECK --interval=60s --timeout=15s --start-period=20s --retries=3 \
  CMD ["bash", "scripts/healthcheck.sh"]

CMD ["python", "-m", "ctn.main"]

# Optional reporting UI. Fleet runtime dependencies remain separate.
FROM runtime AS discord-runtime
USER root
RUN /opt/venv/bin/pip install -r /app/requirements-discord.lock
USER ctn
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
 CMD ["python", "-m", "ctn.discord_bot", "--healthcheck"]
CMD ["python", "-m", "ctn.discord_bot"]

# Pi is pinned to the installed and inspected CLI contract.
FROM node:24-bookworm-slim AS pi-runtime
RUN npm install --global @earendil-works/pi-coding-agent@1.3.0
FROM runtime AS macro-runtime
# Match the team's local Pi selection; credentials arrive through the Pi mount.
ENV CTN_PI_MODEL=deepseek/deepseek-flash \
    CTN_PI_THINKING=high
USER root
COPY --from=pi-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=pi-runtime /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js /usr/local/bin/pi
USER ctn
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
 CMD ["python", "-m", "ctn.macro.worker", "--healthcheck"]
CMD ["python", "-m", "ctn.macro.worker", "--harness", "/state/harness.md"]
