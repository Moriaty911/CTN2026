"""Confirmed action rules; account requirements are handed off, never executed.

Aligned: response_specificity > 2.5 replies, otherwise likes.
Opposing: reply_value < 1 skips, [1, 3] replies normally, > 3 requests a
high-level account. A mapped high-level target upgrades a qualifying reply.
Unknown grades never become low grades. V3 requests a separate JEV decision
for neutral/mixed posts. V2 retains historical downstream LLM planning.
"""

from dataclasses import dataclass
import hashlib
import json
import math

from .evaluator import EvaluationFlow
from .handoff import build_handoff


class ActionValidationError(ValueError):
    """Invalid local policy without supplied values in messages."""


@dataclass(frozen=True)
class ActionDecision:
    status: str
    action: str | None
    reason: str
    signal_id: str | None = None
    signal_value: float | None = None
    required_account_level: str | None = None
    minimum_account_level: str | None = None
    reply_quality: str | None = None
    target_level_class: str | None = None
    needs: tuple[str, ...] = ()

    def __post_init__(self):
        def option(value, allowed):
            if value is not None and (type(value) is not str or value not in allowed):
                raise ActionValidationError("decision: invalid option")
        option(self.status, {"decided", "needs_review", "needs_assessment", "needs_materials", "pending_rules", "needs_account_metadata", "needs_llm_assessment", "needs_jev_assessment"})
        if self.status is None:
            raise ActionValidationError("decision: status required")
        option(self.action, {"reply", "like", "no_interaction"})
        option(self.signal_id, {"reply_value", "response_specificity"})
        option(self.required_account_level, {"standard", "high", "unknown"})
        option(self.minimum_account_level, {"standard", "high"})
        option(self.reply_quality, {"ordinary", "high", "unresolved"})
        option(self.target_level_class, {"standard", "high", "unknown"})
        if type(self.reason) is not str or not self.reason.strip() or type(self.needs) is not tuple or any(type(n) is not str or not n.strip() for n in self.needs):
            raise ActionValidationError("decision: invalid reason or needs")
        if self.signal_value is not None:
            if type(self.signal_value) not in {int, float} or not 0 <= self.signal_value <= 4 or self.signal_id is None:
                raise ActionValidationError("decision: invalid signal")
        if (self.status == "decided" and self.action is None or
            self.status in {"needs_review", "needs_assessment", "needs_materials", "pending_rules", "needs_llm_assessment", "needs_jev_assessment"} and self.action is not None or
            self.status == "needs_account_metadata" and self.action != "reply"):
            raise ActionValidationError("decision: inconsistent action status")
        if self.action != "reply" and any(v is not None for v in (self.required_account_level, self.minimum_account_level, self.reply_quality, self.target_level_class)):
            raise ActionValidationError("decision: account requirements require reply")

    def to_dict(self):
        return {name: list(value) if name == "needs" else value for name, value in vars(self).items()}


@dataclass(frozen=True)
class ActionPolicy:
    aligned_reply_above: float = 2.5
    opposing_reply_from: float = 1.0
    opposing_high_above: float = 3.0
    # Explicit platform labels only. User-provided mapping, empty by default.
    platform_levels: tuple[tuple[str, str], ...] = ()
    version: str = "jev-actions.v3"
    response_min_probability: float = 0.7
    response_min_margin: float = 0.15

    def __post_init__(self):
        for name in ("aligned_reply_above", "opposing_reply_from", "opposing_high_above"):
            value = getattr(self, name)
            try:
                valid = type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 4
            except OverflowError:
                valid = False
            if not valid:
                raise ActionValidationError(f"{name}: invalid score threshold")
        if self.opposing_reply_from > self.opposing_high_above:
            raise ActionValidationError("thresholds: inconsistent order")
        for name in ("response_min_probability", "response_min_margin"):
            value = getattr(self, name)
            if type(value) not in {int, float} or not 0 <= value <= 1:
                raise ActionValidationError(f"{name}: invalid probability threshold")
        if type(self.version) is not str or self.version not in {"jev-actions.v1", "jev-actions.v2", "jev-actions.v3"} or type(self.platform_levels) is not tuple:
            raise ActionValidationError("policy: unsupported configuration")
        seen = set()
        for pair in self.platform_levels:
            if type(pair) is not tuple or len(pair) != 2:
                raise ActionValidationError("platform_levels: expected immutable label/class pairs")
            label, classification = pair
            if type(label) is not str or not label.strip() or type(classification) is not str or classification not in {"standard", "high"}:
                raise ActionValidationError("platform_levels: invalid mapping")
            if label in seen:
                raise ActionValidationError("platform_levels: duplicate label")
            seen.add(label)

    def to_dict(self):
        data = {"version": self.version, "aligned_reply_above": self.aligned_reply_above,
                "opposing_reply_from": self.opposing_reply_from, "opposing_high_above": self.opposing_high_above,
                "platform_levels": dict(self.platform_levels),
                "engagement_policy": "visible_signals_in_reply_value_no_standalone_trigger",
                "pending_rules": (["mixed_neutral_actions"] if self.version == "jev-actions.v1" else []) + ["engagement_trigger_thresholds"]}
        if self.version == "jev-actions.v2":
            data["mixed_neutral_policy"] = "downstream_response_planning"
        elif self.version == "jev-actions.v3":
            data["mixed_neutral_policy"] = "jev_response_decision"
            data["response_thresholds"] = {"min_probability": self.response_min_probability,
                                           "min_margin": self.response_min_margin}
        return data

    @property
    def fingerprint(self):
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def decide(self, flow: EvaluationFlow) -> ActionDecision:
        package = build_handoff(flow)  # Validate exact input/stage bindings first.
        final = flow.deep if flow.deep is not None else flow.triage
        route = flow.policy.decide(final.parsed)
        if route.route == "review" or flow.status == "needs_review":
            return ActionDecision("needs_review", None, "assessment_requires_review", needs=("assessment_review",))
        if flow.status in {"failed", "deferred", "needs_context"} or route.route == "failed":
            return ActionDecision("needs_assessment", None, "assessment_unavailable", needs=(flow.status,))
        if route.route == "observe" and flow.status == "observe":
            return ActionDecision("decided", "no_interaction", "clearly_unrelated")
        if flow.status not in {"evaluated", "partial"} or flow.deep is None:
            return ActionDecision("needs_assessment", None, "deep_assessment_required", needs=("deep",))
        if self.version == "jev-actions.v2" and flow.relative_stance in {"neutral", "mixed"}:
            return ActionDecision("needs_llm_assessment", None, "downstream_response_planning_required",
                                  needs=("downstream_response_planning",))
        if self.version == "jev-actions.v3" and flow.relative_stance in {"neutral", "mixed"}:
            return ActionDecision("needs_jev_assessment", None, "jev_response_decision_required",
                                  needs=("jev_response_decision",))
        if flow.relative_stance not in {"aligned", "opposing"}:
            return ActionDecision("pending_rules", None, "alignment_action_rules_pending", needs=("alignment_action_rules",))
        key = "response_specificity" if flow.relative_stance == "aligned" else "reply_value"
        answer = final.parsed.output.usable_answers.get(key)
        if (answer is None or answer.type != "score" or answer.legend is None or
            set(answer.legend) != {"0", "1", "2", "3", "4"}):
            return ActionDecision("needs_assessment", None, "action_score_unavailable", signal_id=key, needs=(key,))
        score = answer.value
        if flow.relative_stance == "aligned":
            if score > self.aligned_reply_above:
                return ActionDecision("decided", "reply", "aligned_specificity_above_threshold", key, score,
                                      reply_quality="ordinary")
            return ActionDecision("decided", "like", "aligned_specificity_not_above_threshold", key, score)
        if score < self.opposing_reply_from:
            return ActionDecision("decided", "no_interaction", "opposing_value_below_threshold", key, score)
        materials = package.to_dict(include_materials=True)["materials"]
        author = materials["content"].get("author") or {}
        label = author.get("platform_level")
        grade = dict(self.platform_levels).get(label, "unknown")
        high = score > self.opposing_high_above or grade == "high"
        if high:
            return ActionDecision("decided", "reply", "opposing_high_account_required", key, score,
                                  "high", "high", "high", grade)
        if grade == "unknown":
            return ActionDecision("needs_account_metadata", "reply", "opposing_target_level_unknown", key, score,
                                  "unknown", "standard", "unresolved", grade,
                                  ("author.platform_level" if label is None else "platform_level_mapping",))
        return ActionDecision("decided", "reply", "opposing_standard_reply", key, score,
                              "standard", "standard", "ordinary", grade)
