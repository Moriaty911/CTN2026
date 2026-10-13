"""Compose independent JEV judgments into a bound neutral/mixed decision.

No draft generation, account assignment or platform execution. A decided reply
still needs a same-purpose MaterialBrief and all existing generation gates.
"""

from dataclasses import dataclass, field

from .actions import ActionDecision, ActionPolicy
from .evaluator import StageEvaluation


RESPONSE_SCHEMA_VERSION = "jev-response-decision.v1"
_PURPOSES = frozenset(f"P{i:02}" for i in range(2, 10))
_REASONS = frozenset({
    "response_packing_deferred", "local_injection_preflight", "response_call_failed",
    "response_metadata_requires_review", "response_risk_unavailable", "response_injection_signal",
    "response_answer_unavailable", "response_action_uncertain", "response_purpose_uncertain",
    "response_context_unclear", "response_context_insufficient", "response_requested_review",
    "response_requested_materials", "response_answers_conflict", "response_no_interaction",
    "response_reply_selected",
})


@dataclass(frozen=True)
class ChoiceGate:
    selected_probability: float
    margin: float
    passed: bool

    def to_dict(self):
        return dict(selected_probability=self.selected_probability, margin=self.margin, passed=self.passed)


@dataclass(frozen=True)
class ResponseDecision:
    status: str
    action: str | None
    prompt_type: str | None
    reason: str
    needs: tuple[str, ...]
    parent_request_id: str
    policy: ActionPolicy = field(repr=False)
    assessment: StageEvaluation = field(repr=False)
    action_gate: ChoiceGate | None = None
    purpose_gate: ChoiceGate | None = None

    def __post_init__(self):
        if (type(self.status) is not str or self.status not in {"decided", "needs_materials", "needs_review", "needs_assessment"} or
            type(self.reason) is not str or self.reason not in _REASONS or
            type(self.needs) is not tuple or any(type(n) is not str or not n for n in self.needs) or
            type(self.policy) is not ActionPolicy or self.policy.version != "jev-actions.v3" or
            type(self.assessment) is not StageEvaluation or self.assessment.stage != "response" or
            type(self.parent_request_id) is not str or len(self.parent_request_id) != 64 or
            any(c not in "0123456789abcdef" for c in self.parent_request_id)):
            raise ValueError("response: invalid decision metadata")
        if self.prompt_type is not None and (type(self.prompt_type) is not str or self.prompt_type not in _PURPOSES):
            raise ValueError("response: invalid purpose")
        if (self.status == "decided" and (self.action not in {"reply", "no_interaction"} or
                (self.action == "reply") != (self.prompt_type is not None)) or
            self.status != "decided" and self.action is not None):
            raise ValueError("response: inconsistent action")
        for gate in (self.action_gate, self.purpose_gate):
            if gate is not None and type(gate) is not ChoiceGate:
                raise ValueError("response: invalid choice gate")

    def to_dict(self):
        packed = self.assessment.parsed._packed
        return {"schema_version": RESPONSE_SCHEMA_VERSION, "status": self.status,
                "action": self.action, "prompt_type": self.prompt_type, "reason": self.reason,
                "needs": list(self.needs), "question_set_version": packed.question_set_version,
                "question_set_fingerprint": packed.question_set_fingerprint,
                "request_id": packed.request_id, "parent_request_id": self.parent_request_id,
                "targets": [t.to_dict() for t in packed.targets],
                "action_policy_fingerprint": self.policy.fingerprint,
                "thresholds": self.policy.to_dict()["response_thresholds"],
                "gates": {"action": self.action_gate.to_dict() if self.action_gate else None,
                          "purpose": self.purpose_gate.to_dict() if self.purpose_gate else None},
                "new_attempt_count": self.assessment.new_attempt_count,
                "execution_authorized": False}

    def as_action(self):
        # Account and quality requirements for neutral/mixed have not been
        # specified. Leave them unset for the account/generation owners.
        return ActionDecision(self.status, self.action, self.reason, needs=self.needs)


def _gate(answer, policy):
    probability = answer.probabilities[answer.value]
    runner_up = max(p for option, p in answer.probabilities.items() if option != answer.value)
    margin = probability - runner_up
    # Account only for floating-point representation at an exact boundary.
    passed = probability + 1e-12 >= policy.response_min_probability and margin + 1e-12 >= policy.response_min_margin
    return ChoiceGate(probability, margin, passed)


def decide_response(stage: StageEvaluation, *, parent_request_id: str, policy: ActionPolicy,
                    injection_review_threshold: float) -> ResponseDecision:
    if (type(stage) is not StageEvaluation or stage.stage != "response" or
        type(policy) is not ActionPolicy or policy.version != "jev-actions.v3" or
        type(injection_review_threshold) not in {int, float} or not 0 <= injection_review_threshold <= 1):
        raise ValueError("response: incompatible decision components")
    parsed, packed = stage.parsed, stage.parsed._packed
    action_gate, purpose_gate = None, None

    def result(status, reason, *needs, action=None, prompt_type=None):
        return ResponseDecision(status, action, prompt_type, reason, tuple(needs), parent_request_id,
                                policy, stage, action_gate, purpose_gate)

    if packed.preflight is not None and packed.preflight.requires_review:
        return result("needs_review", "local_injection_preflight", "assessment_review")
    if packed.status != "ready":
        return result("needs_assessment", "response_packing_deferred", "response_packing")
    if parsed.error is not None or parsed.output is None or parsed.output.error is not None:
        if parsed.error is not None and parsed.error.code == "response_metadata_requires_review":
            return result("needs_review", "response_metadata_requires_review", "assessment_review")
        return result("needs_assessment", "response_call_failed", "response_assessment")
    answers = parsed.output.usable_answers
    risk = answers.get("prompt_injection")
    if risk is None or risk.type != "noul":
        return result("needs_review", "response_risk_unavailable", "prompt_injection")
    if risk.value >= injection_review_threshold:
        return result("needs_review", "response_injection_signal", "assessment_review")
    required = ("response_action", "response_purpose", "response_context")
    missing = tuple(k for k in required if k not in answers or answers[k].type != "choice")
    if missing:
        return result("needs_review", "response_answer_unavailable", *missing)
    action, purpose, context = (answers[k] for k in required)
    action_gate, purpose_gate = _gate(action, policy), _gate(purpose, policy)
    retained_purpose = purpose.value if purpose.value in _PURPOSES and purpose_gate.passed else None
    if action.value == "needs_review":
        return result("needs_review", "response_requested_review", "response_review")
    if not action_gate.passed:
        return result("needs_review", "response_action_uncertain", "response_action")
    if context.value == "unclear":
        return result("needs_review", "response_context_unclear", "response_context")
    if context.value == "insufficient":
        return result("needs_materials", "response_context_insufficient", "response_context", prompt_type=retained_purpose)
    if action.value == "needs_materials":
        if purpose.value == "none":
            return result("needs_review", "response_answers_conflict", "response_review")
        return result("needs_materials", "response_requested_materials", "response_context", prompt_type=retained_purpose)
    if not purpose_gate.passed or purpose.value == "unclear":
        return result("needs_review", "response_purpose_uncertain", "response_purpose")
    if action.value == "no_interaction" and purpose.value == "none":
        return result("decided", "response_no_interaction", action="no_interaction")
    if action.value == "reply" and retained_purpose is not None:
        return result("decided", "response_reply_selected", action="reply", prompt_type=retained_purpose)
    return result("needs_review", "response_answers_conflict", "response_review")


def validate_response_binding(package):
    """Check immutable local decision provenance before material preparation.

    HandoffPackage has no arbitrary dictionary-import API. A caller-created or
    altered v3 package cannot turn a saved selection into a new selection.
    """
    decision = package._response_result
    data = package.to_dict(include_materials=True)
    if type(decision) is not ResponseDecision or data.get("response_decision") != decision.to_dict():
        raise ValueError("response: missing or changed decision binding")
    packed = decision.assessment.parsed._packed
    if (data.get("response_assessment") != decision.assessment.to_dict() or
        data["action_policy_fingerprint"] != decision.policy.fingerprint or
        data["action_policy"] != decision.policy.to_dict() or
        data["action_decision"] != decision.as_action().to_dict() or
        data["flow"]["deep"]["assessment"]["request"]["request_id"] != decision.parent_request_id or
        data["targets"] != decision.to_dict()["targets"]):
        raise ValueError("response: inconsistent decision provenance")
    if packed.status == "ready":
        state = packed.to_provider_body()["state"]
        materials = data["materials"]
        if materials is None or any(materials[k] != state[k] for k in ("content", "verified_background", "packing")):
            raise ValueError("response: material snapshot changed")
    return decision
