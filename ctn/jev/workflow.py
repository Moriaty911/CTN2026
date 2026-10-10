"""Ready-to-use JEV evaluation, action decision and selected-purpose handoff.

Uses confirmed post.v2 for new work; the v1 rubric and lower-level evaluator
remain available for historical replay. No account, LLM or platform calls.
"""

import json

from .actions import ActionPolicy
from .audit import JevAuditLog
from .client import JevClient
from .evaluator import JevEvaluator
from .handoff import HandoffPackage, build_handoff, build_comparison_handoff
from .materials import MaterialBrief, MaterialCheck, MaterialChecker
from .rubrics import load_rubric


class WorkflowValidationError(ValueError):
    """Invalid module wiring; no supplied text in errors."""


class JevWorkflow:
    def __init__(self, client: JevClient, *, evaluator: JevEvaluator | None = None,
                 action_policy: ActionPolicy | None = None, material_checker: MaterialChecker | None = None,
                 audit_log: JevAuditLog | None = None):
        if type(client) is not JevClient:
            raise WorkflowValidationError("client: expected JEV client")
        self.evaluator = evaluator if evaluator is not None else JevEvaluator(client,
            post_questions=load_rubric("post", version="v2"))
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

    def _finish(self, flow, brief=None, comparison=None, *, policy, checker, audit):
        package = build_handoff(flow)
        action = policy.decide(flow)
        data = package.to_dict()
        data["action_decision"] = action.to_dict()
        data["action_policy"] = policy.to_dict()
        data["action_policy_fingerprint"] = policy.fingerprint
        bound = HandoffPackage(json.dumps(data, ensure_ascii=False, allow_nan=False), package._materials_json)
        result, gate = self._prepare(bound, brief, comparison, checker)
        if audit is not None:
            audit.record(flow, action=action, material_check=gate, action_policy=policy)
        return result

    def _prepare(self, package, brief, comparison, checker):
        data = package.to_dict()
        action = data["action_decision"]
        data["generation_task"] = None
        gate, generation = None, None
        if brief is not None:
            if action["action"] != "reply":
                gate = MaterialCheck("blocked", brief.prompt_type, ("action_is_not_reply",))
            else:
                gate = checker.check(package, brief, comparison=comparison)
            if gate.status == "ready" and action["status"] == "decided":
                generation = gate.to_dict(include_materials=True)["task"]
                generation["action_decision"] = action
                generation["action_policy_fingerprint"] = data["action_policy_fingerprint"]
                data["generation_task"] = {"status": "ready", "prompt_type": brief.prompt_type,
                    "schema_version": generation["schema_version"], "purpose": generation["purpose"],
                    "required_account_level": action["required_account_level"], "reply_quality": action["reply_quality"]}
        data["material_check"] = gate.to_dict() if gate else None
        data["generation_needs"] = ([] if generation else
            list(gate.needs) if gate and gate.status != "ready" else
            list(action["needs"]) if action["needs"] else
            ["downstream_prompt_type_and_material_brief"] if action["action"] == "reply" else [])
        result = HandoffPackage(json.dumps(data, ensure_ascii=False, allow_nan=False), package._materials_json,
                                json.dumps(generation, ensure_ascii=False, allow_nan=False) if generation else None)
        return result, gate

    def evaluate(self, content, *, brief: MaterialBrief | None = None, comparison: HandoffPackage | None = None):
        self._check_selection(brief, comparison)
        policy, checker, audit = self.action_policy, self.material_checker, self.audit_log
        return self._finish(self.evaluator.evaluate(content), brief, comparison, policy=policy, checker=checker, audit=audit)

    def prepare(self, package: HandoffPackage, brief: MaterialBrief, *, comparison: HandoffPackage | None = None):
        """Choose a purpose after evaluation, without calling JEV again.

        Preserves the original decision and policy fingerprint. It does not
        refresh stale observations or silently apply a later action policy.
        """
        self._check_selection(brief, comparison)
        if brief is None or type(package) is not HandoffPackage:
            raise WorkflowValidationError("prepare: expected handoff and selected brief")
        data = package.to_dict()
        if data.get("kind") != "post" or "action_policy_fingerprint" not in data:
            raise WorkflowValidationError("prepare: expected workflow decision handoff")
        result, gate = self._prepare(package, brief, comparison, self.material_checker)
        if self.audit_log is not None:
            self.audit_log.record_preparation(result, gate)
        return result

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
        return tuple(self._finish(flow, brief, pair, policy=policy, checker=checker, audit=audit) for flow, brief, pair in
                     zip(self.evaluator.evaluate_batch(contents), selected, paired))

    def compare(self, current, historical):
        stage = self.evaluator.compare(current, historical)
        if self.audit_log is not None:
            self.audit_log.record_comparison(stage)
        return build_comparison_handoff(stage)
