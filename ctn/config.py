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
import math
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .models import Side


# --------------------------------------------------------------------------- #
# .env loading
# --------------------------------------------------------------------------- #
def _load_dotenv(path: Path) -> None:
    """Minimal .env reader. Existing environment variables are never overwritten."""
    if not path.is_file():
        return
    path.chmod(0o600)
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
    """Legit Social platform configuration."""

    base_url: str = ""
    api_key: str = ""
    team_id: str = ""
    bot_password: str = ""
    request_timeout_s: float = 20.0
    max_retries: int = 3

    @classmethod
    def load(cls, env_file: str | Path = ".env") -> "PlatformConfig":
        _load_dotenv(Path(env_file))
        return cls(
            base_url=_env_str("CTN_PLATFORM_URL") or _env_str("CTN_API_URL"),
            api_key=(
                _env_str("CTN_PLATFORM_API_KEY")
                or _env_str("CTN_TEAM_KEY")
                or _env_str("CTN_TEAM_CODE")
            ),
            team_id=_env_str("CTN_TEAM_ID"),
            bot_password=_env_str("CTN_BOT_PASSWORD") or _env_str("CTN_PLATFORM_PASSWORD"),
            request_timeout_s=_env_float("CTN_PLATFORM_TIMEOUT_S", 20.0),
        )


@dataclass(frozen=True)
class FleetConfig:
    """Account fleet.

    2026 allows up to 200 accounts per team in total (`max_total_accounts`) and
    at most 50 active accounts at once (`bot_accounts`). Deleting a suspended
    account frees an active slot (50) while permanently counting against the
    total allowance (200).
    """

    bot_accounts: int = 50
    max_total_accounts: int = 200
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
    api_key: str = field(default="", repr=False)
    model: str = "jev-latest"
    # Reserved for T07 scheduling. T05 sends one T04 packed body per call;
    # it does not combine posts into shared-state batches.
    triage_batch_size: int = 24
    max_concurrency: int = 4
    request_timeout_s: float = 30.0
    max_retries: int = 2
    # Local queue/backoff budget; socket/DNS operations cannot be hard-cancelled.
    total_wait_budget_s: float = 60.0
    retry_initial_s: float = 1.0
    retry_max_s: float = 8.0
    max_response_bytes: int = 1_048_576
    enabled: bool = True

    def __post_init__(self) -> None:
        try:
            url = urlsplit(self.base_url)
            valid_url = (url.scheme == "https" and bool(url.hostname) and
                         url.username is None and url.password is None and
                         not url.query and not url.fragment and url.port != 0 and
                         not any(ord(c) <= 32 or c == "\\" for c in self.base_url))
        except (ValueError, TypeError):
            valid_url = False
        if not valid_url:
            raise ValueError("JevConfig.base_url: expected an HTTPS endpoint without credentials or query")
        if type(self.api_key) is not str or any(not 33 <= ord(c) <= 126 for c in self.api_key):
            raise ValueError("JevConfig.api_key: invalid header credential")
        if type(self.model) is not str or not self.model or any(ord(c) < 32 for c in self.model):
            raise ValueError("JevConfig.model: invalid model")
        if self.api_key and (self.api_key in self.base_url or self.api_key in self.model):
            raise ValueError("JevConfig: credential outside authentication field")
        for name in ("triage_batch_size", "max_concurrency", "max_response_bytes", "max_retries"):
            value = getattr(self, name)
            if type(value) is not int or value < (0 if name == "max_retries" else 1):
                raise ValueError(f"JevConfig.{name}: invalid integer limit")
        for name in ("request_timeout_s", "total_wait_budget_s", "retry_initial_s", "retry_max_s"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"JevConfig.{name}: expected a finite positive duration")
        if self.retry_initial_s > self.retry_max_s:
            raise ValueError("JevConfig.retry_initial_s: exceeds retry_max_s")
        if type(self.enabled) is not bool:
            raise ValueError("JevConfig.enabled: expected boolean")

    @classmethod
    def load(cls, env_file: str | Path = ".env") -> "JevConfig":
        """Load only JEV configuration; create no runtime directories."""
        _load_dotenv(Path(env_file))
        def number(name, default, convert):
            try:
                return convert(os.environ.get(name, str(default)))
            except (ValueError, TypeError):
                raise ValueError(f"{name}: invalid JEV configuration") from None
        enabled = os.environ.get("CTN_JEV_ENABLED", "true").strip().lower()
        if enabled not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
            raise ValueError("CTN_JEV_ENABLED: invalid JEV configuration")
        return cls(
            base_url=_env_str("TYPESAFE_BASE_URL", cls.base_url),
            api_key=_env_str("TYPESAFE_API_KEY"), model=_env_str("TYPESAFE_MODEL", cls.model),
            triage_batch_size=number("CTN_JEV_BATCH", cls.triage_batch_size, int),
            max_concurrency=number("CTN_JEV_CONCURRENCY", cls.max_concurrency, int),
            request_timeout_s=number("CTN_JEV_TIMEOUT_S", cls.request_timeout_s, float),
            max_retries=number("CTN_JEV_MAX_RETRIES", cls.max_retries, int),
            total_wait_budget_s=number("CTN_JEV_WAIT_BUDGET_S", cls.total_wait_budget_s, float),
            retry_initial_s=number("CTN_JEV_RETRY_INITIAL_S", cls.retry_initial_s, float),
            retry_max_s=number("CTN_JEV_RETRY_MAX_S", cls.retry_max_s, float),
            max_response_bytes=number("CTN_JEV_MAX_RESPONSE_BYTES", cls.max_response_bytes, int),
            enabled=enabled in {"true", "1", "yes", "on"},
        )


@dataclass(frozen=True)
class LLMConfig:
    """Generative layer. External models are explicitly permitted by rule 5.1
    for creative content generation, subject to the provider's own licence."""

    base_url: str = "https://api.deepseek.com"
    api_key: str = field(default="", repr=False)
    model: str = "deepseek-flash"
    max_concurrency: int = 4
    request_timeout_s: float = 60.0
    max_retries: int = 2
    temperature: float = 0.5
    total_wait_budget_s: float = 60.0
    retry_initial_s: float = 1.0
    retry_max_s: float = 8.0
    max_tokens: int = 1536
    max_reply_chars: int = 280
    max_request_bytes: int = 262_144
    max_response_bytes: int = 1_048_576

    def to_dict(self):
        """Metadata configuration; credentials never enter common serialization."""
        return {name: value for name, value in vars(self).items() if name != "api_key"}

    def __post_init__(self):
        try:
            url = urlsplit(self.base_url)
            valid = (url.scheme == "https" and bool(url.hostname) and
                     url.username is None and url.password is None and
                     not url.query and not url.fragment and url.port != 0 and
                     not any(ord(c) <= 32 or c == "\\" for c in self.base_url))
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise ValueError("LLMConfig.base_url: expected HTTPS base URL")
        if type(self.api_key) is not str or any(not 33 <= ord(c) <= 126 for c in self.api_key):
            raise ValueError("LLMConfig.api_key: invalid credential")
        if type(self.model) is not str or not self.model or any(not 33 <= ord(c) <= 126 for c in self.model):
            raise ValueError("LLMConfig.model: invalid model")
        if self.api_key and (self.api_key in self.base_url or self.api_key in self.model):
            raise ValueError("LLMConfig: credential outside authentication")
        for name in ("max_concurrency", "max_retries", "max_tokens", "max_reply_chars", "max_request_bytes", "max_response_bytes"):
            value = getattr(self, name)
            if type(value) is not int or value < (0 if name == "max_retries" else 1):
                raise ValueError(f"LLMConfig.{name}: invalid integer limit")
        if self.max_tokens > 393216:
            raise ValueError("LLMConfig.max_tokens: exceeds provider limit")
        for name in ("request_timeout_s", "total_wait_budget_s", "retry_initial_s", "retry_max_s"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"LLMConfig.{name}: invalid duration")
        if self.retry_initial_s > self.retry_max_s:
            raise ValueError("LLMConfig: invalid retry bounds")
        if type(self.temperature) not in (int, float) or not math.isfinite(self.temperature) or not 0 <= self.temperature <= 2:
            raise ValueError("LLMConfig.temperature: invalid temperature")

    @classmethod
    def load(cls, env_file: str | Path = ".env") -> "LLMConfig":
        """Load generation settings without creating directories."""
        _load_dotenv(Path(env_file))
        values = {"base_url": _env_str("CTN_LLM_BASE_URL") or cls.base_url,
                  "api_key": _env_str("CTN_LLM_API_KEY"),
                  "model": _env_str("CTN_LLM_MODEL") or cls.model}
        names = {"max_concurrency": "CONCURRENCY", "request_timeout_s": "TIMEOUT_S",
                 "max_retries": "MAX_RETRIES", "temperature": "TEMPERATURE",
                 "total_wait_budget_s": "WAIT_BUDGET_S", "retry_initial_s": "RETRY_INITIAL_S",
                 "retry_max_s": "RETRY_MAX_S", "max_tokens": "MAX_TOKENS",
                 "max_reply_chars": "MAX_REPLY_CHARS", "max_request_bytes": "MAX_REQUEST_BYTES",
                 "max_response_bytes": "MAX_RESPONSE_BYTES"}
        for field_name, suffix in names.items():
            default = getattr(cls, field_name)
            try:
                values[field_name] = type(default)(os.environ.get("CTN_LLM_" + suffix, str(default)))
            except (ValueError, TypeError):
                raise ValueError("CTN_LLM_" + suffix + ": invalid configuration") from None
        return cls(**values)


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
    mode: str = "mock"
    state_file: str = ""
    auto_reports: bool = False
    report_mode: str = "off"
    rag_corpus: str = ""
    max_pending_reports: int = 5
    max_pending_per_author: int = 1

    # ---- lifecycle ------------------------------------------------------- #
    @classmethod
    def load(cls, env_file: str | Path = ".env") -> "Settings":
        _load_dotenv(Path(env_file))

        state_dir = Path(_env_str("CTN_STATE_DIR", "state"))
        log_dir = Path(_env_str("CTN_LOG_DIR", "logs"))
        state_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)

        settings = cls(
            platform=PlatformConfig.load(env_file),
            fleet=FleetConfig(
                bot_accounts=_env_int("CTN_BOT_ACCOUNTS", 50),
                max_total_accounts=_env_int("CTN_MAX_TOTAL_ACCOUNTS", 200),
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
            jev=JevConfig.load(env_file),
            llm=LLMConfig.load(env_file),
            side=_env_side("CTN_SIDE"),
            state_dir=state_dir,
            log_dir=log_dir,
            dry_run=_env_bool("CTN_DRY_RUN", True),
            log_level=_env_str("CTN_LOG_LEVEL", "INFO").upper(),
            mode=_env_str("CTN_MODE", "dry-run" if _env_bool("CTN_DRY_RUN", True) else "live"),
            state_file=_env_str("CTN_STATE_FILE"),
            auto_reports=_env_bool("CTN_AUTO_REPORTS", False),
            report_mode=_env_str("CTN_REPORT_MODE", "off"),
            rag_corpus=_env_str("CTN_RAG_CORPUS"),
            max_pending_reports=_env_int("CTN_MAX_PENDING_REPORTS",5),
            max_pending_per_author=_env_int("CTN_MAX_PENDING_PER_AUTHOR",1),
        )
        if settings.report_mode not in {"off","manual","hybrid"}:
            raise ValueError("CTN_REPORT_MODE must be off, manual or hybrid")
        return settings

    def execution_mode(self, override: str | None = None) -> str:
        mode = override or self.mode
        if mode not in {"mock", "dry-run", "live"}:
            raise ValueError("CTN_MODE must be mock, dry-run or live")
        # CTN_MODE is authoritative when explicitly configured. Conflicting
        # legacy configuration is rejected rather than enabling writes.
        if override is None and "CTN_MODE" in os.environ and "CTN_DRY_RUN" in os.environ:
            if (mode == "live") == self.dry_run:
                raise ValueError("CTN_MODE conflicts with CTN_DRY_RUN")
        return mode

    def require_live_writes(self, *, replies: bool = False) -> None:
        missing=[]
        if self.side is Side.UNASSIGNED: missing.append("CTN_SIDE")
        if not self.platform.api_key: missing.append("CTN_PLATFORM_API_KEY")
        if not self.platform.bot_password: missing.append("CTN_BOT_PASSWORD")
        if not self.jev.enabled or not self.jev.api_key: missing.append("enabled TYPESAFE_API_KEY")
        if replies and not self.llm.api_key: missing.append("CTN_LLM_API_KEY")
        if missing: raise ValueError("live writes require: " + ", ".join(missing))

    def database_path(self, explicit: str | Path | None = None) -> Path:
        """One path for both processes. Legacy migration is explicit in stderr."""
        import sqlite3
        from contextlib import closing
        import sys
        path=Path(explicit or self.state_file or self.state_dir / "ctn.sqlite3").expanduser().resolve()
        if path.suffix == ".json":
            raise ValueError("JSON is not shared runtime state; import it into SQLite first")
        if explicit or self.state_file or path.exists():
            return path
        legacy=[p for p in (Path(".ctn_state.db"),self.state_dir/"ctn.db") if p.is_file() and p.resolve()!=path]
        if len(legacy)>1:
            raise ValueError("multiple legacy databases; select one with --state-file")
        if legacy:
            path.parent.mkdir(parents=True,exist_ok=True)
            with closing(sqlite3.connect(legacy[0])) as source, closing(sqlite3.connect(path)) as dest:
                source.backup(dest)
            path.chmod(0o600)
            print(f"Migrated legacy SQLite {legacy[0].resolve()} -> {path}; original preserved",file=sys.stderr)
        return path

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
