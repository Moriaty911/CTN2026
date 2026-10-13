"""Local observations and simulations; none of these values are NPC ground truth."""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import re

TOPICS = ("public_access", "vaccination_general", "annual_vaccination")
KINDS = {"own", "competitor", "citizen", "unknown"}
SIDES = {"yes", "no", "unknown"}


class CitizenError(ValueError):
    """Fixed messages only, without supplied text or provider exceptions."""


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(",", ":"))


def fingerprint(value):
    return hashlib.sha256(dump(value).encode()).hexdigest()


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.timestamp()
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise CitizenError("invalid_timestamp") from None


def text(value, *, limit=4096):
    if type(value) is not str or not value.strip() or len(value) > limit:
        raise CitizenError("invalid_text_field")
    return value


def number(value, low, high):
    if type(value) not in {int, float} or not math.isfinite(value) or not low <= value <= high:
        raise CitizenError("invalid_numeric_field")
    return float(value)


def safe_data(value, forbidden=()):
    """Reject recognizable/known credentials before persistence or model use.

    This is a bounded local check, not a general secret or injection detector.
    """
    encoded = dump(value)
    if len(encoded.encode()) > 262144:
        raise CitizenError("input_too_large")
    pattern = re.compile(r"\b(?:api[_ -]?key|access[_ -]?token|client[_ -]?secret|password|authorization)"
                         r"\s*[:=]\s*[\"']?[^\s\"',;]{8,}|\bsk-[A-Za-z0-9_-]{16,}", re.I)

    def walk(item):
        if type(item) is str:
            if pattern.search(item) or any(secret and secret in item for secret in forbidden):
                raise CitizenError("credential_in_data")
        elif type(item) is dict:
            for key, child in item.items():
                walk(key)
                walk(child)
        elif type(item) in {list, tuple}:
            for child in item:
                walk(child)
    walk(value)
    return value


@dataclass(frozen=True)
class IdentityEvidence:
    account_id: str
    kind: str
    side: str
    status: str
    source: str
    reference: str
    observed_at: str

    def __post_init__(self):
        text(self.account_id)
        text(self.reference)
        timestamp(self.observed_at)
        if (self.kind not in KINDS or self.side not in SIDES or
                self.status not in {"confirmed", "inferred"} or
                self.source not in {"team_registry", "official_moderation", "verified_platform", "manual"}):
            raise CitizenError("invalid_identity_evidence")
        if self.kind in {"citizen", "unknown"} and self.side != "unknown":
            raise CitizenError("identity_is_not_public_stance")
        if self.status == "confirmed":
            if self.kind == "unknown" or self.source == "manual":
                raise CitizenError("confirmation_requires_evidence")
            if self.source == "team_registry" and self.kind != "own":
                raise CitizenError("registry_confirms_only_own_accounts")
            if self.source == "official_moderation" and (
                    self.kind not in {"competitor", "citizen"} or self.side != "unknown"):
                raise CitizenError("moderation_does_not_confirm_faction")
        if self.kind == "own" and (self.source != "team_registry" or self.status != "confirmed"):
            raise CitizenError("own_account_requires_registry")


@dataclass(frozen=True)
class Persona:
    persona_id: str
    description: str
    public_access: float
    vaccination_general: float = 0.4
    annual_vaccination: float = 0.0
    certainty: float = 0.5
    turnout: float = 0.7

    def __post_init__(self):
        text(self.persona_id)
        text(self.description)
        for key in TOPICS:
            number(getattr(self, key), -1, 1)
        number(self.certainty, 0, 1)
        number(self.turnout, 0, 1)

    def to_dict(self):
        return asdict(self)


def default_panel():
    return (
        Persona("yes_firm", "Strongly supports access requirements; values hospital capacity.", .8, certainty=.9),
        Persona("yes_soft", "Qualified supporter; wants proportionate and practical measures.", .3),
        Persona("undecided_health", "Undecided; weighs health evidence cautiously.", 0, certainty=.3),
        Persona("undecided_livelihood", "Undecided; weighs work access and household costs.", 0, certainty=.3),
        Persona("no_soft", "Opposes access requirements mildly; accepts voluntary vaccination.", -.3),
        Persona("no_firm", "Strongly opposes access restrictions; values medical choice.", -.8, certainty=.9),
    )


@dataclass(frozen=True)
class AlarmPolicy:
    version: str = "citizen-alarm.v1"
    mode: str = "shadow"
    delta_threshold: float = .2
    max_repeat_spread: float = .15
    min_profiles: int = 2
    repeats: int = 2
    min_observation_confidence: float = .8
    calibration_reference: str | None = None
    calibration_scope: str | None = None
    max_post_age_s: float = 1800
    daily_call_limit: int = 96

    def __post_init__(self):
        text(self.version)
        if self.mode not in {"shadow", "active"}:
            raise CitizenError("invalid_alarm_mode")
        number(self.delta_threshold, .000001, 2)
        number(self.max_repeat_spread, 0, 2)
        number(self.min_observation_confidence, 0, 1)
        number(self.max_post_age_s, 1, 86400 * 30)
        for key, minimum in (("min_profiles", 1), ("repeats", 2), ("daily_call_limit", 1)):
            if type(getattr(self, key)) is not int or getattr(self, key) < minimum:
                raise CitizenError("invalid_policy_limit")
        if self.mode == "active" and (not self.calibration_reference or not self.calibration_scope):
            raise CitizenError("active_mode_requires_calibration_reference")
        if self.calibration_reference is not None:
            text(self.calibration_reference)
        if self.calibration_scope is not None:
            text(self.calibration_scope)

    def to_dict(self):
        return asdict(self)

    @property
    def fingerprint(self):
        return fingerprint(self.to_dict())


def validate_reading(data, *, request_id, branch, target_text, forbidden=()):
    """A valid JSON response is a simulation, not a probability or safety approval."""
    safe_data(data, forbidden)
    expected = {"request_id", "stances", "certainty", "turnout", "reason", "evidence_quote"}
    if type(data) is not dict or set(data) != expected or data["request_id"] != request_id:
        raise CitizenError("invalid_reading_binding")
    stances = data["stances"]
    if type(stances) is not dict or set(stances) != set(TOPICS):
        raise CitizenError("invalid_reading_topics")
    for value in stances.values():
        number(value, -1, 1)
    number(data["certainty"], 0, 1)
    number(data["turnout"], 0, 1)
    text(data["reason"], limit=2000)
    quote = data["evidence_quote"]
    if type(quote) is not str or len(quote) > 2000:
        raise CitizenError("invalid_reading_quote")
    if (branch == "control" and quote) or (branch == "read" and (not quote or quote not in target_text)):
        raise CitizenError("unbound_reading_quote")
    return data
