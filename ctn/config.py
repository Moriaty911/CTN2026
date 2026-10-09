"""Central configuration.

Everything tunable lives here and is loaded once at startup. Nothing else in the
codebase reads os.environ directly, so there is exactly one place to audit for
secrets handling (competition rule 5.6: never share keys beyond the team).

Design notes
------------
* Stdlib only. During a two-week live event we want as few moving parts as
  possible; a config layer that cannot fail is worth more than one that is pretty.
* Real environment variables always win over the .env file, so the same code runs
  under systemd (EnvironmentFile=), Docker, and a laptop without changes.
* Rate limits are configuration, not constants, because the organisers have not
  published 2026 numbers. We ship conservative defaults derived from the 2025
  published telemetry and make them adjustable without a redeploy.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .models import Side


# --------------------------------------------------------------------------- #
# .env loading
# --------------------------------------------------------------------------- #
def _load_dotenv(path: Path) -> None:
    """Minimal .env reader. Existing environment variables are never overwritten."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def _env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_side(name: str) -> Side:
    try:
        return Side(_env_str(name).lower())
    except ValueError:
        return Side.UNASSIGNED


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PlatformConfig:
    """Legit Social. The concrete shapes are unknown until the official client
    template is released, so the adapter -- not this config -- absorbs the churn."""

    base_url: str = ""
    api_key: str = ""
    team_id: str = ""
    request_timeout_s: float = 20.0
    max_retries: int = 3


@dataclass(frozen=True)
class FleetConfig:
    """Account fleet.

    2025 allowed 41 accounts per team (1 human-operated + 40 bots). The 2026
    number is unconfirmed, so it is config rather than a hard-coded assumption.
    Accounts are consumables: assume some get suspended mid-run.
    """

    bot_accounts: int = 40
    primary_accounts: int = 1
    # A suspended account is parked for this long before we probe it again.
    suspend_cooldown_s: float = 3600.0


@dataclass(frozen=True)
class RateConfig:
    """Two-level, read/write separated rate limiting.

    The read/write split is the single most important limit decision: a burst of
    reading must never consume the budget needed to publish into a trending
    window. They therefore get independent token buckets, and the team-level and
    per-account buckets are independent too.

    Defaults are deliberately below the 2025 observed per-team average
    (~0.61 req/s across all teams over four weeks) so we have headroom to
    accelerate when the platform tolerates it.
    """

    read_rps: float = 0.35          # team-level reads
    write_rps: float = 0.15         # team-level writes (posts/replies/likes/...)
    per_account_read_rps: float = 0.05
    per_account_write_rps: float = 0.02
    # Burst capacity, in seconds' worth of tokens.
    burst_s: float = 20.0
    # Multiplier applied to every bucket when the platform returns 429.
    penalty_factor: float = 2.0
    penalty_max_s: float = 900.0


@dataclass(frozen=True)
class BudgetConfig:
    """Request accounting.

    Reads are metered by the platform, so perception is a spend decision, not a
    free operation. The ledger lets the perception loop self-throttle instead of
    discovering the ceiling by getting rate-limited.
    """

    total_requests: int = 700_000       # rough 2-week allocation, tune per organisers
    read_share: float = 0.75            # fraction of budget reserved for reading
    warn_at: float = 0.80               # start shedding low-value reads at 80%


@dataclass(frozen=True)
class JevConfig:
    """TypeSafe "System One" decision model.

    Jev does not generate text. It answers typed questions about a state and
    returns calibrated probabilities. It is our fast, cheap perception and
    safety layer -- never our content layer.
    """

    base_url: str = "https://api.typesafe.ai/v1/systemone"
    api_key: str = ""
    model: str = "jev-latest"
    # Posts per batched triage request. One state, N questions, evaluated in
    # parallel. Larger batches amortise better but degrade instruction
    # following, so this is measured, not guessed.
    triage_batch_size: int = 24
    max_concurrency: int = 8
    request_timeout_s: float = 30.0
    max_retries: int = 3
    enabled: bool = True


@dataclass(frozen=True)
class LLMConfig:
    """Generative layer. External models are explicitly permitted by rule 5.1
    for creative content generation, subject to the provider's own licence."""

    base_url: str = ""
    api_key: str = ""
    model: str = ""
    max_concurrency: int = 4
    request_timeout_s: float = 60.0
    max_retries: int = 2
    temperature: float = 0.9        # high: we want surface-form diversity


@dataclass(frozen=True)
class PolicyConfig:
    """Decision thresholds for the action ladder.

    Keeping these here (rather than inline in the pipeline) is what makes the
    closed calibration loop possible: we can retune thresholds between polls and
    record exactly which values were live at any moment.
    """

    # --- triage gate ---
    triage_min_interest: float = 0.55      # below this a post is observe-only
    # --- escalation to a scarce write action ---
    reply_upside_min: float = 3.4          # Score 0..5
    persuadable_min: float = 3.0           # Score 0..5
    needs_nuance_min: float = 0.5          # Noul
    # --- amplification (cheap action, Story-score channel) ---
    amplify_min_alignment: float = 0.70
    amplify_min_visibility: float = 3.0
    # --- safety gates (any hit blocks the action) ---
    risk_block: float = 0.60
    injection_block: float = 0.70
    hate_block: float = 0.40
    # --- targeting ---
    # We would rather be a large share of a few citizens' feeds than a rounding
    # error in everyone's: story-score credit is shared among all teams whose
    # posts a citizen consumed.
    target_feed_share_gain: float = 0.06   # fallback gain when unmeasured
    max_concurrent_targets: int = 120
    # --- anti-collision ---
    own_team_similarity_max: float = 0.82  # reject drafts too close to our own


@dataclass(frozen=True)
class Settings:
    platform: PlatformConfig = field(default_factory=PlatformConfig)
    fleet: FleetConfig = field(default_factory=FleetConfig)
    rate: RateConfig = field(default_factory=RateConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    jev: JevConfig = field(default_factory=JevConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)

    # Not yet assigned by the organisers. UNASSIGNED keeps the loop observe-only.
    side: Side = Side.UNASSIGNED

    state_dir: Path = Path("state")
    log_dir: Path = Path("logs")
    dry_run: bool = True            # never write to the platform unless False
    log_level: str = "INFO"

    # ---- lifecycle ------------------------------------------------------- #
    @classmethod
    def load(cls, env_file: str | Path = ".env") -> "Settings":
        _load_dotenv(Path(env_file))

        state_dir = Path(_env_str("CTN_STATE_DIR", "state"))
        log_dir = Path(_env_str("CTN_LOG_DIR", "logs"))
        state_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)

        settings = cls(
            platform=PlatformConfig(
                base_url=_env_str("CTN_PLATFORM_URL"),
                api_key=_env_str("CTN_PLATFORM_API_KEY"),
                team_id=_env_str("CTN_TEAM_ID"),
                request_timeout_s=_env_float("CTN_PLATFORM_TIMEOUT_S", 20.0),
            ),
            fleet=FleetConfig(
                bot_accounts=_env_int("CTN_BOT_ACCOUNTS", 40),
                primary_accounts=_env_int("CTN_PRIMARY_ACCOUNTS", 1),
            ),
            rate=RateConfig(
                read_rps=_env_float("CTN_READ_RPS", 0.35),
                write_rps=_env_float("CTN_WRITE_RPS", 0.15),
                per_account_read_rps=_env_float("CTN_ACCT_READ_RPS", 0.05),
                per_account_write_rps=_env_float("CTN_ACCT_WRITE_RPS", 0.02),
            ),
            budget=BudgetConfig(
                total_requests=_env_int("CTN_TOTAL_REQUESTS", 700_000),
                read_share=_env_float("CTN_READ_SHARE", 0.75),
            ),
            jev=JevConfig(
                base_url=_env_str("TYPESAFE_BASE_URL", JevConfig.base_url),
                api_key=_env_str("TYPESAFE_API_KEY"),
                model=_env_str("TYPESAFE_MODEL", "jev-latest"),
                triage_batch_size=_env_int("CTN_JEV_BATCH", 24),
                max_concurrency=_env_int("CTN_JEV_CONCURRENCY", 8),
                enabled=_env_bool("CTN_JEV_ENABLED", True),
            ),
            llm=LLMConfig(
                base_url=_env_str("CTN_LLM_BASE_URL"),
                api_key=_env_str("CTN_LLM_API_KEY"),
                model=_env_str("CTN_LLM_MODEL"),
                max_concurrency=_env_int("CTN_LLM_CONCURRENCY", 4),
            ),
            side=_env_side("CTN_SIDE"),
            state_dir=state_dir,
            log_dir=log_dir,
            dry_run=_env_bool("CTN_DRY_RUN", True),
            log_level=_env_str("CTN_LOG_LEVEL", "INFO").upper(),
        )
        return settings

    # ---- guardrails ------------------------------------------------------ #
    def validate(self) -> list[str]:
        """Return human-readable warnings. Never raises on a missing key: a dry
        run against the mock client must work with an empty .env."""
        problems: list[str] = []
        if not self.dry_run:
            if not self.platform.base_url:
                problems.append("CTN_PLATFORM_URL is empty but CTN_DRY_RUN=false")
            if not self.platform.api_key:
                problems.append("CTN_PLATFORM_API_KEY is empty but CTN_DRY_RUN=false")
        if self.jev.enabled and not self.jev.api_key:
            problems.append("TYPESAFE_API_KEY missing; Jev calls will be skipped")
        if self.side is Side.UNASSIGNED:
            problems.append("CTN_SIDE not set (yes/no); the bot loop will only observe")
        if self.rate.read_rps + self.rate.write_rps > 1.0:
            problems.append(
                "team read+write rate exceeds 1 req/s; 2025 per-team average was "
                "~0.61 req/s - expect throttling"
            )
        return problems
