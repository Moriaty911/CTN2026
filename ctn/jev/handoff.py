"""Bound assessments and the exact evaluated materials for downstream consumers.

The default serialization excludes text. Consumers explicitly request materials
for prompt construction; content stays data, never system instructions. This
module does not select accounts, call an LLM or authorize a platform action.
"""

from dataclasses import dataclass, field
import hashlib
import json

from .evaluator import EvaluationFlow, StageEvaluation


HANDOFF_SCHEMA_VERSION = "jev-handoff.v1"


class HandoffValidationError(ValueError):
    """Invalid local provenance; messages never echo supplied content."""


@dataclass(frozen=True)
class HandoffPackage:
    _json: str = field(repr=False)
    _materials_json: str | None = field(default=None, repr=False)
    _generation_json: str | None = field(default=None, repr=False)
    _planning_json: str | None = field(default=None, repr=False)

    def to_dict(self, *, include_materials=False):
        if type(include_materials) is not bool:
            raise HandoffValidationError("include_materials: expected boolean")
        result = json.loads(self._json)
        if include_materials:
            result["materials"] = json.loads(self._materials_json) if self._materials_json else None
            if self._generation_json is not None:
                result["generation_materials"] = json.loads(self._generation_json)
            if self._planning_json is not None:
                result["response_planning_materials"] = json.loads(self._planning_json)
        return result


def _stage_materials(stage):
    parsed = stage.parsed
    packed = parsed._packed
    if packed is None:
        raise HandoffValidationError("assessment: missing request provenance")
    if packed.status != "ready":
        return None
    wire = packed.to_provider_bytes()
    if hashlib.sha256(wire).hexdigest() != packed.request_id:
        raise HandoffValidationError("assessment: request fingerprint mismatch")
    if parsed.output is not None:
        output = parsed.output
        if (output.request_id != packed.request_id or output.stage != packed.stage or
            output.question_set_version != packed.question_set_version or not packed.targets or
            output.post_id != packed.targets[0].post_id or output.source_kind != packed.targets[0].source_kind):
            raise HandoffValidationError("assessment: target or version mismatch")
    state = packed.to_provider_body()["state"]
    # No original unassessed input, provider questions or raw answer envelope.
    return {"content": state["content"], "verified_background": state["verified_background"],
            "packing": state["packing"], "trust": "untrusted_content_as_data",
            "background_verification": "registry_match_is_not_factual_verification"}


def build_handoff(flow: EvaluationFlow) -> HandoffPackage:
    """No fallback to triage material when deep packing/evaluation fails.

    Both assessments remain available; final_stage identifies the assessment
    the consumer must use. Availability of material is not reply permission.
    """
    if type(flow) is not EvaluationFlow:
        raise HandoffValidationError("flow: expected evaluation flow")
    for stage in (flow.triage, flow.deep):
        if stage is not None:
            _stage_materials(stage)
    final = flow.deep if flow.deep is not None else flow.triage
    first, last = flow.triage.parsed._packed, final.parsed._packed
    if flow.deep is not None and (first.input_fingerprint != last.input_fingerprint or
                                 first.targets != last.targets):
        raise HandoffValidationError("flow: stages reference different input snapshots")
    materials = _stage_materials(final)
    payload = {"schema_version": HANDOFF_SCHEMA_VERSION, "kind": "post", "flow": flow.to_dict(),
               "final_stage": final.stage, "targets": [t.to_dict() for t in last.targets],
               "materials_available": materials is not None,
               "action_decision": {"status": "pending_rules", "action": None,
                                   "reason": "business_policy_not_configured"},
               "generation_task": None, "execution_authorized": False}
    return HandoffPackage(json.dumps(payload, ensure_ascii=False, allow_nan=False),
                          json.dumps(materials, ensure_ascii=False, allow_nan=False) if materials else None)


def build_comparison_handoff(stage: StageEvaluation) -> HandoffPackage:
    if type(stage) is not StageEvaluation or stage.parsed._packed is None or stage.parsed._packed.kind != "comparison":
        raise HandoffValidationError("stage: expected bound comparison")
    materials = _stage_materials(stage)
    packed = stage.parsed._packed
    local_review = packed.preflight is not None and packed.preflight.requires_review
    payload = {"schema_version": HANDOFF_SCHEMA_VERSION, "kind": "comparison",
               "assessment": stage.to_dict(), "final_stage": stage.stage,
               "targets": [t.to_dict() for t in packed.targets], "materials_available": materials is not None,
               "action_decision": {"status": "pending_rules", "action": None,
                                   "reason": "business_policy_not_configured"},
               "generation_task": None, "execution_authorized": False}
    if local_review:
        payload["action_decision"] = {"status": "needs_review", "action": None,
                                      "reason": "local_injection_preflight"}
    return HandoffPackage(json.dumps(payload, ensure_ascii=False, allow_nan=False),
                          json.dumps(materials, ensure_ascii=False, allow_nan=False) if materials else None)
