"""User-confirmed evaluation routing; never authorizes a platform action."""

from dataclasses import dataclass
import hashlib
import json
import math

from .parsing import ParsingResult


class RoutingValidationError(ValueError):
    """Local policy error without supplied values."""


@dataclass(frozen=True)
class RouteDecision:
    route: str
    reason: str
    answer_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {"route": self.route, "reason": self.reason, "answer_ids": list(self.answer_ids)}


@dataclass(frozen=True)
class RoutingPolicy:
    """D09 defaults, 2026-10-10; probabilities are not competition scores.

    Only relevance and prompt_injection are required to select this evaluation
    route. Missing topic/stance does not prevent deeper classification. Risk is
    checked before relevance, including before dropping unrelated material.
    """

    unrelated_min_probability: float = 0.8
    injection_review_threshold: float = 0.5
    unclear_to_deep: bool = True
    version: str = "jev-routing.v1"

    def __post_init__(self):
        for name in ("unrelated_min_probability", "injection_review_threshold"):
            value = getattr(self, name)
            try:
                valid = type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1
            except OverflowError:
                valid = False
            if not valid:
                raise RoutingValidationError(f"{name}: expected a finite probability")
        if type(self.unclear_to_deep) is not bool:
            raise RoutingValidationError("unclear_to_deep: expected boolean")
        if self.version != "jev-routing.v1":
            raise RoutingValidationError("version: unsupported routing version")

    def to_dict(self) -> dict:
        return {"version": self.version, "unrelated_min_probability": self.unrelated_min_probability,
                "injection_review_threshold": self.injection_review_threshold, "unclear_to_deep": self.unclear_to_deep}

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def decide(self, parsed: ParsingResult) -> RouteDecision:
        if parsed.status == "failed" or parsed.error is not None or parsed.output is None or parsed.output.error is not None:
            return RouteDecision("failed", "evaluation_failed")
        answers = parsed.output.usable_answers
        risk = answers.get("prompt_injection")
        if risk is None or risk.type != "noul":
            return RouteDecision("review", "risk_unavailable", ("prompt_injection",))
        if risk.value >= self.injection_review_threshold:
            return RouteDecision("review", "injection_signal", ("prompt_injection",))
        relevance = answers.get("relevance")
        if relevance is None or relevance.type != "choice" or relevance.value not in {"relevant", "unrelated", "unclear"}:
            return RouteDecision("failed", "relevance_unavailable", ("relevance",))
        if relevance.value == "unclear":
            return RouteDecision("deep" if self.unclear_to_deep else "review", "relevance_unclear", ("relevance",))
        if relevance.value == "relevant":
            return RouteDecision("deep", "relevant", ("relevance",))
        if relevance.probabilities["unrelated"] >= self.unrelated_min_probability:
            return RouteDecision("observe", "clearly_unrelated", ("relevance",))
        return RouteDecision("deep", "weak_unrelated_signal", ("relevance",))
