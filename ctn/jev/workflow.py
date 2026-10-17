"""Ready-to-use JEV evaluation, action decision and selected-purpose handoff.

Uses post.v3 plus the independent response.v1 neutral/mixed stage with default
actions.v3. Explicit old policies remain available for historical replay.
No account, LLM or platform calls.
"""

import json
from concurrent.futures import ThreadPoolExecutor

from .actions import ActionPolicy
from .audit import JevAuditLog
from .client import JevClient
from .evaluator import JevEvaluator
from .handoff import (HandoffPackage, build_handoff, build_comparison_handoff,
                      bind_workflow_handoff, validate_workflow_handoff)
from .materials import MaterialBrief, MaterialCheck, MaterialChecker
from .planning import build_response_planning
from .response import decide_response, validate_response_binding
from .rubrics import load_rubric, ACTIVE_POST_VERSION


class WorkflowValidationError(ValueError):
    """Invalid module wiring; no supplied text in errors."""


class JevWorkflow:
    def __init__(self, client: JevClient, *, evaluator: JevEvaluator | None = None,
                 action_policy: ActionPolicy | None = None, material_checker: MaterialChecker | None = None,
                 audit_log: JevAuditLog | None = None):
        if type(client) is not JevClient:
            raise WorkflowValidationError("client: expected JEV client")
        self.evaluator = evaluator if evaluator is not None else JevEvaluator(client,
            post_questions=load_rubric("post", version=ACTIVE_POST_VERSION))
        self.action_policy = action_policy if action_policy is not None else ActionPolicy()
        self.material_checker = material_checker if material_checker is not None else MaterialChecker()
        self.audit_log = audit_log
        if (type(self.evaluator) is not JevEvaluator or self.evaluator.client is not client or
            type(self.action_policy) is not ActionPolicy or type(self.material_checker) is not MaterialChecker or
            (audit_log is not None and type(audit_log) is not JevAuditLog)):
            raise WorkflowValidationError("workflow: incompatible components")
        self.material_checker = self.material_checker.with_forbidden_values((client.config.api_key,) if client.config.api_key else ())

    @staticmethod
    def _check_selection(brief, comparison):
        if brief is not None and type(brief) is not MaterialBrief:
            raise WorkflowValidationError("brief: expected material brief")
        if comparison is not None and type(comparison) is not HandoffPackage:
            raise WorkflowValidationError("comparison: expected comparison handoff")
        if comparison is not None and comparison.to_dict().get("kind") != "comparison":
            raise WorkflowValidationError("comparison: expected comparison assessment")

    def _finish(self, flow, brief=None, comparison=None, *, policy, checker, audit,
                response_memo=None, force_response_refresh=False):
        package = build_handoff(flow)
        action = policy.decide(flow)
        response = None
        if action.status == "needs_jev_assessment":
            stage = self.evaluator.evaluate_response(flow, batch_memo=response_memo,
                                                     force_refresh=force_response_refresh)
            response = decide_response(stage, parent_request_id=flow.deep.parsed._packed.request_id,
                                       policy=policy, injection_review_threshold=flow.policy.injection_review_threshold)
            action = response.as_action()
        data = package.to_dict()
        data["action_decision"] = action.to_dict()
        data["action_policy"] = policy.to_dict()
        data["action_policy_fingerprint"] = policy.fingerprint
        data["response_decision"] = response.to_dict() if response else None
        data["response_assessment"] = response.assessment.to_dict() if response else None
        data["new_attempt_count"] = flow.new_attempt_count + (response.assessment.new_attempt_count if response else 0)
        bound = HandoffPackage(json.dumps(data, ensure_ascii=False, allow_nan=False), package._materials_json,
                               _response_result=response)
        result, gate, planning = self._prepare(bound, brief, comparison, checker)
        if audit is not None:
            audit.record(flow, action=action, material_check=gate, action_policy=policy,
                         response_planning=planning, response_decision=response)
        return result

    def _prepare(self, package, brief, comparison, checker):
        data = package.to_dict()
        action = data["action_decision"]
        response = None
        if (package._response_result is not None or data.get("response_decision") is not None or
            data.get("action_policy", {}).get("version") == "jev-actions.v3" and
            data["flow"]["relative_stance"] in {"neutral", "mixed"} and action["action"] == "reply"):
            try:
                response = validate_response_binding(package)
            except (ValueError, KeyError, TypeError):
                raise WorkflowValidationError("prepare: invalid response decision binding") from None
        data["generation_task"] = None
        gate, generation, planning, planning_materials = None, None, None, None
        if action["status"] == "needs_llm_assessment":
            planning = build_response_planning(package, checker=checker, brief=brief, comparison=comparison)
            gate = planning.material_check
            if planning.status == "ready":
                planning_materials = planning.to_dict(include_materials=True)["task"]
        elif brief is not None:
            if action["action"] != "reply":
                gate = MaterialCheck("blocked", brief.prompt_type, ("action_is_not_reply",))
            else:
                gate = checker.check(package, brief, comparison=comparison)
                if response is not None and brief.prompt_type != response.prompt_type and gate.status != "needs_review":
                    gate = MaterialCheck("blocked", brief.prompt_type, ("response_purpose_mismatch",))
            if gate.status == "ready" and action["status"] == "decided":
                generation = gate.to_dict(include_materials=True)["task"]
                generation["action_decision"] = action
                generation["action_policy_fingerprint"] = data["action_policy_fingerprint"]
                if response is not None:
                    generation["response_decision"] = response.to_dict()
                    generation["instructions"] += ("\nFollow this JEV-selected primary purpose; do not choose a different purpose. "
                        "Preserve the parts, objections, scope and conditions of mixed positions. "
                        "Neutral wording does not establish persuadability. If the direction cannot be supported, "
                        "request missing material or review instead of inventing evidence or switching purpose.")
                data["generation_task"] = {"status": "ready", "prompt_type": brief.prompt_type,
                    "schema_version": generation["schema_version"], "purpose": generation["purpose"],
                    "required_account_level": action["required_account_level"], "reply_quality": action["reply_quality"]}
        data["material_check"] = gate.to_dict() if gate else None
        data["response_planning_task"] = planning.to_dict() if planning else None
        data["response_planning_needs"] = list(planning.needs) if planning else []
        data["generation_needs"] = ([] if generation else list(dict.fromkeys(
            list(action["needs"]) + (list(gate.needs) if gate and gate.status != "ready" else
            ["material_brief" if response else "downstream_prompt_type_and_material_brief"]
            if action["action"] == "reply" and brief is None else []))))
        result = HandoffPackage(json.dumps(data, ensure_ascii=False, allow_nan=False), package._materials_json,
                                json.dumps(generation, ensure_ascii=False, allow_nan=False) if generation else None,
                                json.dumps(planning_materials, ensure_ascii=False, allow_nan=False) if planning_materials else None,
                                package._response_result)
        return bind_workflow_handoff(result), gate, planning

    def evaluate(self, content, *, brief: MaterialBrief | None = None, comparison: HandoffPackage | None = None):
        self._check_selection(brief, comparison)
        policy, checker, audit = self.action_policy, self.material_checker, self.audit_log
        return self._finish(self.evaluator.evaluate(content), brief, comparison, policy=policy, checker=checker, audit=audit)

    def prepare(self, package: HandoffPackage, brief: MaterialBrief, *, comparison: HandoffPackage | None = None):
        """Check materials after evaluation, without calling JEV again.

        Preserves the original decision and policy fingerprint. It does not
        refresh stale observations or silently apply a later action policy.
        V3 neutral/mixed briefs must match the saved JEV purpose.
        """
        self._check_selection(brief, comparison)
        if brief is None or type(package) is not HandoffPackage:
            raise WorkflowValidationError("prepare: expected handoff and selected brief")
        try:
            validate_workflow_handoff(package)
            if comparison is not None:
                validate_workflow_handoff(comparison)
        except ValueError:
            raise WorkflowValidationError("prepare: invalid local workflow binding") from None
        data = package.to_dict()
        if data.get("kind") != "post" or "action_policy_fingerprint" not in data:
            raise WorkflowValidationError("prepare: expected workflow decision handoff")
        result, gate, planning = self._prepare(package, brief, comparison, self.material_checker)
        if self.audit_log is not None:
            self.audit_log.record_preparation(result, gate, response_planning=planning)
        return result

    def redecide(self, content, *, brief: MaterialBrief | None = None, comparison: HandoffPackage | None = None):
        """Explicitly reassess updated input and force a fresh response judgment.

        Supply the complete updated JevInput, not a saved handoff or new prompt.
        Triage/deep follow the evaluator's cache policy; response bypasses saved
        response results. Changed input is independently packed and assessed.
        """
        self._check_selection(brief, comparison)
        policy, checker, audit = self.action_policy, self.material_checker, self.audit_log
        return self._finish(self.evaluator.evaluate(content), brief, comparison, policy=policy,
                            checker=checker, audit=audit, force_response_refresh=True)

    def evaluate_batch(self, contents, *, briefs=None, comparisons=None):
        if type(contents) not in {list, tuple}:
            raise WorkflowValidationError("contents: expected list or tuple")
        for values in (briefs, comparisons):
            if values is not None and (type(values) not in {list, tuple} or len(values) != len(contents)):
                raise WorkflowValidationError("selection: length must match contents")
        selected = tuple(briefs) if briefs is not None else (None,) * len(contents)
        paired = tuple(comparisons) if comparisons is not None else (None,) * len(contents)
        for brief, pair in zip(selected, paired):
            self._check_selection(brief, pair)
        policy, checker, audit = self.action_policy, self.material_checker, self.audit_log
        flows = self.evaluator.evaluate_batch(contents)
        memo = {}
        results = []
        chunk_size = self.evaluator.client.config.triage_batch_size
        with ThreadPoolExecutor(max_workers=self.evaluator.client.config.max_concurrency) as pool:
            for offset in range(0, len(flows), chunk_size):
                futures = [pool.submit(self._finish, flows[i], selected[i], paired[i], policy=policy,
                                       checker=checker, audit=audit, response_memo=memo)
                           for i in range(offset, min(offset + chunk_size, len(flows)))]
                results.extend(future.result() for future in futures)
        return tuple(results)

    def compare(self, current, historical):
        stage = self.evaluator.compare(current, historical)
        if self.audit_log is not None:
            self.audit_log.record_comparison(stage)
        return bind_workflow_handoff(build_comparison_handoff(stage))
