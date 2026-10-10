"""Metadata-only JEV audit events using the project's JSONL writer.

Identifiers and variable strings are hashed; no text, raw envelopes, API key,
probabilities, score legends or arbitrary caller fields reach this sink.
Explicit record calls are synchronous: I/O failures propagate to the caller.
"""

from collections import Counter
import hashlib
from pathlib import Path

from ctn.telemetry import JsonlWriter
from .evaluator import EvaluationFlow, StageEvaluation


def _hash(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest() if value is not None else None


def _option(value, allowed):
    return value if type(value) is str and value in allowed else "unknown"


def _fingerprint(value):
    if type(value) is not str or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("audit: invalid policy fingerprint")
    return value


def _stage(stage):
    parsed, packed = stage.parsed, stage.parsed._packed
    output = parsed.output
    call = stage.to_dict()["original_call"]
    return {"stage": _option(packed.stage, {"triage", "deep"}), "kind": _option(packed.kind, {"post", "comparison"}),
            "source": _option(stage.source, {"network", "cache", "shared", "not_sent"}),
            "status": parsed.status, "request_hash": _hash(packed.request_id),
            "input_hash": _hash(packed.input_fingerprint), "questions_hash": _hash(packed.question_set_fingerprint),
            "packing_hash": _hash(packed.packing_fingerprint),
            "question_version_hash": _hash(packed.question_set_version),
            "targets": [{"role": _option(t.role, {"target", "current", "historical"}), "post_hash": _hash(t.post_id),
                         "source_kind": _option(t.source_kind, {"platform", "synthetic", "manual"})} for t in packed.targets],
            "evaluation_hash": _hash(output.evaluation_id) if output else None,
            "model_hash": _hash(output.model) if output else None,
            "answer_status_counts": dict(Counter(a.status for a in output.answers.values())) if output else {},
            "issue_count": len(parsed.issues), "error_hash": _hash(parsed.error.code) if parsed.error else None,
            "new_attempt_count": stage.new_attempt_count, "cache_age_s": stage.cache_age_s,
            "elapsed_s": call["elapsed_s"] if call else None,
            # Usage belongs to the original response, including cached results.
            "original_usage": output.usage.to_dict() if output and output.usage else None}


class JevAuditLog:
    def __init__(self, path: Path):
        self._writer = JsonlWriter(Path(path))

    def record(self, flow: EvaluationFlow, *, action=None, material_check=None, action_policy=None):
        if type(flow) is not EvaluationFlow:
            raise ValueError("audit: expected evaluation flow")
        from .actions import ActionDecision, ActionPolicy
        from .materials import MaterialCheck
        if action is not None and type(action) is not ActionDecision:
            raise ValueError("audit: expected action decision")
        if material_check is not None and type(material_check) is not MaterialCheck:
            raise ValueError("audit: expected material check")
        if action_policy is not None and type(action_policy) is not ActionPolicy:
            raise ValueError("audit: expected action policy")
        self._writer.write({"schema_version": "jev-audit.v1", "event": "post_evaluation",
            "run_hash": _hash(flow.run_id), "status": _option(flow.status, {"evaluated", "partial", "observe", "needs_context", "needs_review", "failed", "deferred"}),
            "batch_index": flow.batch_index if type(flow.batch_index) is int and flow.batch_index >= 0 else None,
            "route": _option(flow.decision.route, {"deep", "failed", "observe", "context", "complete", "review", "partial"}),
            "reason_hash": _hash(flow.decision.reason),
            "policy_hash": flow.policy.fingerprint, "new_attempt_count": flow.new_attempt_count,
            "action_policy_hash": action_policy.fingerprint if action_policy else None,
            "stages": [_stage(s) for s in (flow.triage, flow.deep) if s is not None],
            "action": {"status": action.status, "action": action.action, "reason_hash": _hash(action.reason),
                "signal_id_hash": _hash(action.signal_id), "signal_value": action.signal_value,
                "required_account_level": action.required_account_level, "reply_quality": action.reply_quality,
                "need_count": len(action.needs)} if action else None,
            "material_check": {"status": material_check.status, "prompt_type": material_check.prompt_type,
                "need_count": len(material_check.needs)} if material_check else None})

    def record_preparation(self, package, material_check):
        from .handoff import HandoffPackage
        from .materials import MaterialCheck
        if type(package) is not HandoffPackage or type(material_check) is not MaterialCheck:
            raise ValueError("audit: expected preparation result")
        data = package.to_dict()
        self._writer.write({"schema_version": "jev-audit.v1", "event": "material_preparation",
            "run_hash": _hash(data["flow"]["run_id"]), "new_attempt_count": 0,
            "action_policy_hash": _fingerprint(data["action_policy_fingerprint"]), "status": material_check.status,
            "prompt_type": material_check.prompt_type, "need_count": len(material_check.needs),
            "generation_ready": data["generation_task"] is not None})

    def record_comparison(self, stage: StageEvaluation):
        if type(stage) is not StageEvaluation or stage.parsed._packed.kind != "comparison":
            raise ValueError("audit: expected comparison stage")
        self._writer.write({"schema_version": "jev-audit.v1", "event": "comparison_evaluation", "stage": _stage(stage)})

    def close(self):
        self._writer.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
