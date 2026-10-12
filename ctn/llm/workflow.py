"""Explicit draft generation after local JEV/material checks; no publishing."""

from dataclasses import replace
import hashlib
import json

from ctn.jev.handoff import HandoffPackage, validate_workflow_handoff
from ctn.jev.materials import MaterialBrief, MaterialChecker
from ctn.jev.workflow import JevWorkflow, WorkflowValidationError
from ctn.telemetry import JsonlWriter
from .client import DeepSeekClient
from .models import DraftOptions, ReplyDraftResult, MODEL_NEEDS, LlmValidationError
from .prompts import build_messages


def _strict_json(value):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = item
        return result
    def constant(value):
        raise ValueError("nonfinite JSON value")
    return json.loads(value, object_pairs_hook=pairs, parse_constant=constant)


class ReplyGenerator:
    def __init__(self, workflow: JevWorkflow, client: DeepSeekClient, *, audit_log: JsonlWriter | None = None):
        if type(workflow) is not JevWorkflow or type(client) is not DeepSeekClient:
            raise LlmValidationError("generator: incompatible components")
        if audit_log is not None and type(audit_log) is not JsonlWriter:
            raise LlmValidationError("generator: expected JSONL writer")
        self.workflow, self.client, self.audit_log = workflow, client, audit_log
        self.checker = workflow.material_checker.with_forbidden_values(tuple(
            key for key in (workflow.evaluator.client.config.api_key, client.config.api_key) if key))

    def _finish(self, result):
        if self.audit_log is not None:
            # No body, reply, arbitrary target/model string or provider error text.
            self.audit_log.write({"event": "reply_generation", "status": result.status,
                "reason": result.reason, "prompt_type": result.prompt_type,
                "prompt_version": result.prompt_version, "material_version": result.material_version,
                "model_hash": hashlib.sha256(result.model.encode()).hexdigest(),
                "request_id": result.request_id, "parent_request_id": result.parent_request_id,
                "attempt_count": result.attempt_count, "usage": dict(result.usage),
                "need_count": len(result.needs), "execution_authorized": False})
        return result

    def generate(self, handoff: HandoffPackage, brief: MaterialBrief, comparison=None, options=None):
        """Recheck locally without JEV calls. No implicit generation on evaluate()."""
        options = DraftOptions() if options is None else options
        if type(options) is not DraftOptions or type(brief) is not MaterialBrief:
            raise LlmValidationError("generate: expected typed options and material brief")
        config = self.client.config
        max_chars = options.max_reply_chars or config.max_reply_chars
        if max_chars > config.max_reply_chars:
            raise LlmValidationError("options: character limit exceeds configured maximum")
        base = ReplyDraftResult("blocked", "handoff_unavailable", model=config.model)
        def finish(status, reason, **fields):
            return self._finish(replace(base, status=status, reason=reason, **fields))
        try:
            validate_workflow_handoff(handoff)
            if comparison is not None:
                validate_workflow_handoff(comparison)
            original = handoff.to_dict()
            if original["schema_version"] != "jev-handoff.v1" or original["kind"] != "post":
                return finish("needs_review", "handoff_version_mismatch")
            action = original["action_decision"]
            prior = original.get("generation_task")
            selected = (original.get("response_decision") or {}).get("prompt_type")
            selected = selected or (prior["prompt_type"] if prior else None)
            base = replace(base, target_post_id=original["targets"][0]["post_id"], prompt_type=selected,
                           parent_request_id=original["flow"]["deep"]["assessment"]["request"]["request_id"]
                               if original["flow"]["deep"] else None)
            if action["status"] != "decided" or action["action"] != "reply":
                return finish("needs_review" if action["status"] == "needs_review" else "blocked",
                              "action_not_ready", needs=tuple(original.get("generation_needs", ())))
            if selected and selected != brief.prompt_type:
                return finish("needs_review", "purpose_mismatch")
            base = replace(base, prompt_type=selected or brief.prompt_type)
            # prepare validates v3 response provenance and retains the saved policy.
            prepared = self.workflow.prepare(handoff, brief, comparison=comparison)
            data = prepared.to_dict(include_materials=True)
            if prepared._materials_json != handoff._materials_json:
                return finish("needs_review", "material_snapshot_mismatch")
            # Independently recheck with BOTH configured credentials forbidden.
            gate = self.checker.check(prepared, brief, comparison=comparison)
            if gate.status == "needs_review":
                return finish("needs_review", "material_requires_review", needs=gate.needs)
            if data["generation_task"] is None:
                check = data.get("material_check") or {}
                status = "needs_review" if check.get("status") == "needs_review" else "needs_materials"
                return finish(status, "generation_not_ready", needs=tuple(data["generation_needs"]))
            if gate.status != "ready":
                return finish("needs_materials", "material_not_ready", needs=gate.needs)
            material = data["generation_materials"]
            task = data["generation_task"]
            if (material["prompt_type"] != brief.prompt_type or task["prompt_type"] != brief.prompt_type or
                material["target"] != data["targets"][0] or material["evaluated_materials"] != data["materials"] or
                material["action_decision"] != action or
                material["action_policy_fingerprint"] != original["action_policy_fingerprint"] or
                material["template_version"] != "jev-briefs.v1"):
                return finish("needs_review", "generation_binding_mismatch")
            # Model instructions are constructed only from our versioned templates.
            messages = build_messages(material, options, max_chars)
        except (ValueError, KeyError, TypeError, IndexError, WorkflowValidationError):
            return finish("needs_review", "invalid_handoff_binding")
        call = self.client.call(messages)
        base = replace(base, request_id=call.request_id, attempt_count=call.attempt_count)
        if call.reason != "ok":
            return finish("needs_materials" if call.reason == "request_budget_exceeded" else "failed", call.reason)
        if any(key.encode() in call.body for key in self.checker._forbidden_values):
            return finish("needs_review", "sensitive_output")
        try:
            envelope = _strict_json(call.body.decode("utf-8"))
            if type(envelope) is not dict or type(envelope.get("choices")) is not list or len(envelope["choices"]) != 1:
                return finish("failed", "invalid_response")
            choice = envelope["choices"][0]
            if type(choice) is not dict or choice.get("finish_reason") != "stop":
                return finish("failed", "incomplete_response")
            message = choice.get("message")
            if type(message) is not dict or message.get("role") != "assistant" or message.get("tool_calls"):
                return finish("needs_review", "unexpected_model_action")
            content = message.get("content")
            if type(content) is not str or not content.strip():
                return finish("failed", "empty_response")
            output = _strict_json(content)
            if type(output) is not dict or set(output) != {"status", "reply", "needs"}:
                return finish("failed", "invalid_draft_schema")
            status, reply, needs = output["status"], output["reply"], output["needs"]
            if (type(status) is not str or status not in {"draft", "needs_materials", "needs_review"} or
                type(needs) is not list or any(type(n) is not str or n not in MODEL_NEEDS for n in needs) or
                len(needs) != len(set(needs))):
                return finish("failed", "invalid_draft_schema")
            usage = envelope.get("usage", {})
            if type(usage) is dict:
                base = replace(base, usage=tuple((key, usage[key]) for key in
                    ("prompt_tokens", "completion_tokens", "total_tokens")
                    if type(usage.get(key)) is int and usage[key] >= 0))
            if status != "draft":
                if reply is not None or not needs:
                    return finish("failed", "invalid_draft_schema")
                return finish(status, "model_requests_material" if status == "needs_materials" else "model_requests_review",
                              needs=tuple(needs))
            if type(reply) is not str or not reply.strip() or needs:
                return finish("failed", "invalid_draft_schema")
            reply = reply.strip()
            # Reject malformed Unicode rather than leak it into logs/publishing.
            reply.encode("utf-8")
            if len(reply) > max_chars:
                return finish("needs_review", "reply_length_exceeded")
            if self.checker._has_secret(reply):
                return finish("needs_review", "sensitive_output")
            if self.checker.preflight.check({"reply": reply}, path="llm_output").requires_review:
                return finish("needs_review", "output_injection_signal")
            return finish("draft", "draft_created", reply=reply)
        except (ValueError, KeyError, TypeError, IndexError, UnicodeError):
            return finish("failed", "invalid_response")
