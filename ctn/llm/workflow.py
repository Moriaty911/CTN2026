"""Explicit draft generation after local JEV/material checks; no publishing."""

from dataclasses import replace
import hashlib
import json

from ctn.jev.handoff import HandoffPackage, validate_workflow_handoff
from ctn.jev.materials import MaterialBrief, MaterialChecker
from ctn.jev.workflow import JevWorkflow, WorkflowValidationError
from ctn.persona.models import Persona
from ctn.telemetry import JsonlWriter
from .client import DeepSeekClient
from .models import DraftOptions, ReplyDraftResult, MODEL_NEEDS, LlmValidationError,PROMPT_VERSION
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


def _clean_natural_reply(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if len(lines) >= 2 and lines[0].startswith("```") and lines[-1].strip() == "```":
            cleaned = "\n".join(lines[1:-1]).strip()
    if len(cleaned) >= 2 and cleaned.startswith("`") and cleaned.endswith("`"):
        cleaned = cleaned[1:-1].strip()
    if len(cleaned) >= 2:
        if (cleaned[0] == '"' and cleaned[-1] == '"') or \
           (cleaned[0] == "'" and cleaned[-1] == "'") or \
           (cleaned[0] == '“' and cleaned[-1] == '”'):
            cleaned = cleaned[1:-1].strip()
    return cleaned

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

    def generate(self, handoff: HandoffPackage, brief: MaterialBrief, comparison=None, options=None, *, persona: Persona | None = None, retry_feedback: str | None = None):
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
            frozen = {"version":"ctn-frozen-reply.v1", "materials":material,
                      'prompt_version':PROMPT_VERSION,
                      "parent_request_id":base.parent_request_id}
            frozen["fingerprint"] = hashlib.sha256(json.dumps(frozen,sort_keys=True,
                ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()).hexdigest()
            base=replace(base,frozen_material_json=json.dumps(frozen,ensure_ascii=False,allow_nan=False))
            messages = build_messages(material, options, max_chars, persona=persona, retry_feedback=retry_feedback)
        except (ValueError, KeyError, TypeError, IndexError, WorkflowValidationError):
            return finish("needs_review", "invalid_handoff_binding")
        return self._call(messages,base,max_chars)

    def generate_frozen(self, frozen_json, *, persona, previous_replies, options=None, retry_feedback=None):
        """Resume a locally persisted, checked batch. Never accepts a provider handoff."""
        options=options or DraftOptions()
        max_chars=options.max_reply_chars or self.client.config.max_reply_chars
        base=ReplyDraftResult('needs_review','invalid_frozen_material',model=self.client.config.model)
        try:
            frozen=_strict_json(frozen_json)
            fingerprint=frozen.pop('fingerprint')
            if (set(frozen)!={'version','materials','parent_request_id','prompt_version'} or
                frozen['version']!='ctn-frozen-reply.v1' or
                frozen['prompt_version']!=PROMPT_VERSION or
                hashlib.sha256(json.dumps(frozen,sort_keys=True,ensure_ascii=False,
                    allow_nan=False,separators=(',',':')).encode()).hexdigest()!=fingerprint):
                raise ValueError('invalid snapshot')
            material=frozen['materials'];action=material['action_decision']
            if (material['schema_version']!='jev-generation-material.v1' or action['status']!='decided'
                or action['action']!='reply' or not material.get('action_policy_fingerprint')
                or max_chars>self.client.config.max_reply_chars or not previous_replies):
                raise ValueError('invalid scope')
            if self.checker._has_secret(frozen_json):raise ValueError('sensitive snapshot')
            messages=build_messages(material,options,max_chars,persona=persona,
                retry_feedback=retry_feedback,previous_replies=previous_replies)
            base=replace(base,target_post_id=material['target']['post_id'],prompt_type=material['prompt_type'],
                parent_request_id=frozen['parent_request_id'],frozen_material_json=frozen_json)
        except (ValueError,TypeError,KeyError,IndexError):
            return self._finish(base)
        return self._call(messages,base,max_chars)

    def _call(self,messages,base,max_chars):
        def finish(status,reason,**fields):
            return self._finish(replace(base,status=status,reason=reason,**fields))
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
            usage = envelope.get("usage", {})
            if type(usage) is dict:
                base = replace(base, usage=tuple((key, usage[key]) for key in
                    ("prompt_tokens", "completion_tokens", "total_tokens")
                    if type(usage.get(key)) is int and usage[key] >= 0))
            reply = None
            trimmed = content.strip()
            # If the response happens to be JSON (e.g. from structured test fixtures or fallback),
            # handle it gracefully.
            if trimmed.startswith("{") and trimmed.endswith("}"):
                try:
                    output = _strict_json(trimmed)
                    if isinstance(output, dict) and "status" in output:
                        if set(output) != {"status", "reply", "needs"}:
                            return finish("failed", "invalid_draft_schema")
                        status = output["status"]
                        raw_needs = output.get("needs") or []
                        if status in {"needs_materials", "needs_review"}:
                            if (output.get("reply") is None and isinstance(raw_needs, list)
                                    and all(isinstance(n, str) and n in MODEL_NEEDS for n in raw_needs)
                                    and len(raw_needs) == len(set(raw_needs)) and raw_needs):
                                return finish(status,
                                              "model_requests_material" if status == "needs_materials" else "model_requests_review",
                                              needs=tuple(raw_needs))
                            return finish("failed", "invalid_draft_schema")
                        elif status == "draft":
                            if "reply" in output and isinstance(output["reply"], str) and output["reply"].strip() and not raw_needs:
                                reply = _clean_natural_reply(output["reply"])
                            else:
                                return finish("failed", "invalid_draft_schema")
                        else:
                            return finish("failed", "invalid_draft_schema")
                    elif isinstance(output, dict) and "reply" in output and isinstance(output["reply"], str):
                        reply = _clean_natural_reply(output["reply"])
                except ValueError as err:
                    if "duplicate JSON key" in str(err) or "nonfinite" in str(err):
                        return finish("failed", "invalid_response")

            if reply is None:
                reply = _clean_natural_reply(content)

            if not reply:
                return finish("failed", "empty_response")

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
