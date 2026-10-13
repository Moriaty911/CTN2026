"""Independent monitor: matching makes review tasks, never platform actions."""

from dataclasses import asdict
import json
import statistics
from uuid import uuid4

from ..jev.inputs import JevInput
from ..jev.injection import InjectionPreflight
from .domain import (AlarmPolicy, CitizenError, TOPICS, default_panel, fingerprint,
                     number, safe_data, text, timestamp, validate_reading)
from .reader import PROMPT_VERSION


class CitizenMonitor:
    def __init__(self, store, *, assigned_side, reader=None, policy=None, panel=None,
                 forbidden_values=(), observe_stances=True):
        if assigned_side not in {"yes", "no"}:
            raise CitizenError("assigned_side_required")
        self.store = store
        self.side = assigned_side
        self.reader = reader
        self.policy = policy if policy is not None else AlarmPolicy()
        self.panel = tuple(panel) if panel is not None else default_panel()
        if not self.panel or len({p.persona_id for p in self.panel}) != len(self.panel):
            raise CitizenError("invalid_simulation_panel")
        if len(self.panel) < self.policy.min_profiles:
            raise CitizenError("panel_smaller_than_alarm_requirement")
        self.forbidden = tuple(dict.fromkeys(tuple(forbidden_values) + store.forbidden_values +
                                             tuple(getattr(reader, "forbidden_values", ()))))
        self.preflight = InjectionPreflight()
        self.observe_stances = observe_stances
        safe_data({"policy": self.policy.to_dict(), "panel": [p.to_dict() for p in self.panel]}, self.forbidden)
        self.calibration_scope = fingerprint({"panel": [p.to_dict() for p in self.panel],
                                              "reader": getattr(reader, "fingerprint", None),
                                              "prompt": PROMPT_VERSION, "preflight": self.preflight.fingerprint,
                                              "repeats": self.policy.repeats})
        self.task_policy_fingerprint = fingerprint({"side": self.side, "policy": self.policy.to_dict()})
        if self.policy.mode == "active" and (reader is None or self.policy.calibration_scope != self.calibration_scope):
            raise CitizenError("calibration_scope_mismatch")

    def _call(self, run_id, method, request):
        safe_data(request, self.forbidden)
        call_id = self.store.reserve_call(run_id, self.policy.daily_call_limit)
        try:
            result = getattr(self.reader, method)(request)
            safe_data(result, self.forbidden)
            if self.preflight.check(result).requires_review:
                raise CitizenError("reader_output_requires_review")
        except CitizenError:
            self.store.finish_call(call_id, "failed")
            raise
        except Exception:
            self.store.finish_call(call_id, "failed")
            raise CitizenError("reader_failed") from None
        self.store.finish_call(call_id, "completed")
        return result

    def _observe(self, run_id, version_id, content):
        if not self.observe_stances or not content.post.author_id or not hasattr(self.reader, "observe"):
            return
        request = {"request_id": uuid4().hex, "target": asdict(content.post),
                   "conversation": asdict(content.conversation) if content.conversation else None}
        result = self._call(run_id, "observe", request)
        if (type(result) is not dict or set(result) != {"request_id", "observations"} or
                result["request_id"] != request["request_id"] or type(result["observations"]) is not list or
                len(result["observations"]) > len(TOPICS)):
            raise CitizenError("invalid_stance_response")
        seen = set()
        validated = []
        for record in result["observations"]:
            if (type(record) is not dict or set(record) != {"topic", "value", "label", "confidence", "evidence_quote"} or
                    record["topic"] not in TOPICS or record["topic"] in seen or
                    record["label"] not in {"yes", "no", "mixed", "neutral", "unclear"}):
                raise CitizenError("invalid_stance_record")
            seen.add(record["topic"])
            value, label = record["value"], record["label"]
            if value is not None:
                number(value, -1, 1)
                if (label == "yes" and value <= 0) or (label == "no" and value >= 0) or label not in {"yes", "no"}:
                    raise CitizenError("stance_value_label_mismatch")
            if label in {"yes", "no"} and value is None:
                raise CitizenError("stance_value_missing")
            number(record["confidence"], 0, 1)
            quote = text(record["evidence_quote"])
            if quote not in content.post.text:
                raise CitizenError("stance_quote_not_in_target")
            validated.append(record)
        for record in validated:
            self.store.record_stance(version_id, **record, model_version=self.reader.fingerprint,
                                     observed_at=content.post.created_at or content.source.observed_at)
        # Chronological neighboring posts, not processing order, determine observed changes.
        for topic in seen:
            history = [r for r in self.store.stance_history(content.post.author_id, topic)
                       if r["confidence"] >= self.policy.min_observation_confidence and r["value"] is not None]
            for before, after in zip(history, history[1:]):
                if before["model_version"] != after["model_version"] or before["observed_ts"] >= after["observed_ts"]:
                    continue
                delta = after["value"] - before["value"]
                if abs(delta) > self.policy.delta_threshold:
                    self.store.add_alert(after["version_id"], "observed_expression_shift", "review", {
                        "account_id": content.post.author_id, "topic": topic, "delta": delta,
                        "before_stance_id": before["stance_id"], "after_stance_id": after["stance_id"],
                        "policy": self.policy.fingerprint, "scope": "public_expression_only_cause_unknown"})

    def _summarize(self, samples):
        profiles = []
        sign = 1 if self.side == "yes" else -1
        for persona in self.panel:
            deltas = {topic: [samples[(persona.persona_id, repeat, "read")]["stances"][topic] -
                              samples[(persona.persona_id, repeat, "control")]["stances"][topic]
                              for repeat in range(self.policy.repeats)] for topic in TOPICS}
            main = deltas["public_access"]
            stable = max(main) - min(main) <= self.policy.max_repeat_spread
            toward = stable and all(sign * d > self.policy.delta_threshold for d in main)
            away = stable and all(sign * d < -self.policy.delta_threshold for d in main)
            profiles.append({"persona_id": persona.persona_id, "deltas": deltas,
                             "median_delta": statistics.median(main), "stable": stable,
                             "toward_team": toward, "away_from_team": away})
        toward = sum(p["toward_team"] for p in profiles)
        away = sum(p["away_from_team"] for p in profiles)
        kinds = []
        if toward and away:
            kinds.append("polarization")
        if away >= self.policy.min_profiles:
            kinds.append("threat")
        if toward >= self.policy.min_profiles:
            kinds.append("opportunity")
        if any(not p["stable"] for p in profiles):
            kinds.append("unstable_simulation")
        return {"scope": "synthetic_panel_not_population_or_official_score", "assigned_side": self.side,
                "policy": self.policy.to_dict(), "profiles": profiles, "toward_profiles": toward,
                "away_profiles": away, "alarm_kinds": kinds,
                "auxiliary_topics": "recorded_separately_not_assumed_referendum_direction"}

    def _route(self, version_id):
        version = self.store.version(version_id)
        account = self.store.account(version["account_id"])
        tasks = []
        if account["status"] == "conflicted" or (account["kind"] == "own" and account["side"] not in {"unknown", self.side}):
            kind, reason = "identity_review", "conflicting_identity_evidence"
        elif account["status"] == "confirmed" and account["kind"] == "own":
            kind, reason = "own_content_review", "registered_own_account"
        elif account["status"] == "confirmed" and account["kind"] == "competitor":
            kind, reason = "competitor_content_review", "confirmed_competitor_account"
        elif account["kind"] == "citizen" and account["status"] in {"confirmed", "inferred"}:
            kind, reason = "citizen_dialogue_review", "observed_citizen_candidate"
        else:
            kind, reason = "content_review", "identity_unknown_or_unconfirmed"
        tasks.append(self.store.enqueue(version_id, kind, self.task_policy_fingerprint, reason,
                     ["current_content_and_context", "existing_JEV_workflow", "materials_and_output_review",
                      "platform_rate_limits_and_deduplication"], identity_revision=account["revision"]))
        # Reporting is only a REVIEW candidate. No report action exists in this branch.
        if (account["status"] == "confirmed" and account["kind"] == "competitor" and
                account["side"] in {"yes", "no"} and account["side"] != self.side):
            tasks.append(self.store.enqueue(version_id, "report_review", self.task_policy_fingerprint,
                         "confirmed_opposing_competitor", ["current_identity_evidence", "reportable_post",
                         "team_reporting_suspension", "already_reported_or_hidden", "official_report_adapter"],
                         identity_revision=account["revision"]))
        return tasks

    def process(self, content, *, retry=False):
        content = content if type(content) is JevInput else JevInput.from_dict(content)
        if getattr(self.reader, "synthetic_only", False) and content.source.kind != "synthetic":
            raise CitizenError("demo_reader_requires_synthetic_input")
        if content.task_context and content.task_context.assigned_side not in {None, "unknown", self.side}:
            raise CitizenError("assigned_side_mismatch")
        safe_data(content.to_dict(), self.forbidden)
        version_id = self.store.ingest(content)
        base = {"version_id": version_id, "post_id": content.post.post_id,
                "identity": self.store.account(content.post.author_id), "execution_authorized": False,
                "limitations": [{"path": issue.path, "reason": issue.reason} for issue in content.context_issues]}
        check = self.preflight.check(content.to_dict())
        if check.requires_review or content.post.text_complete is False:
            reason = "input_requires_review" if check.requires_review else "incomplete_target_text"
            self.store.add_alert(version_id, reason, "review", {"reason": reason})
            task = self.store.enqueue(version_id, "input_review", self.task_policy_fingerprint, reason, ["review_untrusted_input"])
            return {**base, "status": "needs_review", "task_ids": [task]}
        history_only = False
        if content.post.created_at:
            age = self.store.clock() - timestamp(content.post.created_at)
            if age < -60:
                return {**base, "status": "needs_review", "reason": "future_post_timestamp", "task_ids": []}
            if age > self.policy.max_post_age_s:
                history_only = True
        tasks = [] if history_only else self._route(version_id)
        if self.reader is None:
            return {**base, "status": "stored_only", "reason": "reader_not_configured", "task_ids": tasks}
        if content.post.kind in {"reply", "quote", "repost"} and (
                content.conversation is None or content.conversation.status not in {"available", "partial"} or
                not content.conversation.items):
            self.store.add_alert(version_id, "missing_conversation", "review", {"reason": "missing_conversation"})
            return {**base, "status": "needs_context", "task_ids": tasks}
        config = {"policy": self.policy.to_dict(), "panel": [p.to_dict() for p in self.panel],
                  "reader": self.reader.fingerprint, "prompt": PROMPT_VERSION, "side": self.side,
                  "preflight": self.preflight.fingerprint, "observe_stances": self.observe_stances,
                  "calibration_scope": self.calibration_scope, "history_only": history_only}
        cache_key = fingerprint({"version_id": version_id, "config": config})
        run, acquired = self.store.begin_run(cache_key, version_id, config, retry=retry)
        if not acquired:
            return {**base, "status": run["status"], "cached": True, "run_id": run["run_id"],
                    "result": json.loads(run["result_json"]) if run["result_json"] else None, "task_ids": tasks}
        run_id = run["run_id"]
        samples = {}
        try:
            self._observe(run_id, version_id, content)
            if history_only:
                result = {"scope": "historical_public_expression_only", "alarm_kinds": []}
                self.store.finish_run(run_id, status="completed", result=result)
                return {**base, "status": "completed", "reason": "historical_observation_only", "run_id": run_id,
                        "result": result, "cached": False, "task_ids": []}
            # Context is identical, and excludes the target post in both branches.
            # Only preceding parent context belongs to this experiment's baseline.
            conversation = asdict(content.conversation) if content.conversation else None
            if conversation:
                conversation["items"] = [item for item in conversation["items"]
                                         if item["role"] in {"parent", "quoted"} and item["post_id"] != content.post.post_id
                                         and (not item["created_at"] or not content.post.created_at or
                                              timestamp(item["created_at"]) <= timestamp(content.post.created_at))]
            context = {"conversation": conversation,
                       "knowledge_limit": "No hidden NPC state or unobserved feed is available."}
            for persona in self.panel:
                for repeat in range(self.policy.repeats):
                    # Alternate call order to reduce a systematic order effect. Calls have no shared chat history.
                    branches = ("control", "read") if repeat % 2 == 0 else ("read", "control")
                    for branch in branches:
                        request = {"request_id": uuid4().hex, "persona": persona.to_dict(), "context": context,
                                   "branch": branch, "target": asdict(content.post) if branch == "read" else None}
                        data = self._call(run_id, "read", request)
                        data = validate_reading(data, request_id=request["request_id"], branch=branch,
                                                target_text=content.post.text, forbidden=self.forbidden)
                        self.store.save_sample(run_id, persona.persona_id, repeat, branch, data)
                        samples[(persona.persona_id, repeat, branch)] = data
            result = self._summarize(samples)
            alerts = [{"kind": kind, "mode": "review" if kind == "unstable_simulation" else self.policy.mode,
                       "policy": self.policy.fingerprint, "profiles": result["profiles"]}
                      for kind in result["alarm_kinds"]]
            self.store.finish_run(run_id, status="completed", result=result, alerts=alerts)
            if self.policy.mode == "active" and any(kind != "unstable_simulation" for kind in result["alarm_kinds"]):
                tasks.append(self.store.enqueue(version_id, "alarm_response_review", self.task_policy_fingerprint,
                             "stable_simulation_alarm", ["alarm_evidence", "existing_JEV_workflow", "output_review"]))
            return {**base, "status": "completed", "cached": False, "run_id": run_id,
                    "result": result, "task_ids": tasks}
        except CitizenError as error:
            code = str(error)
        except Exception:
            code = "monitor_failed"
        status = "budget_exhausted" if code == "daily_call_budget_exhausted" else "needs_review"
        self.store.finish_run(run_id, status=status, error_code=code)
        self.store.add_alert(version_id, "assessment_unavailable", "review", {"reason": code, "run_id": run_id})
        return {**base, "status": status, "run_id": run_id, "reason": code, "task_ids": tasks}

    def evaluate_with_jev(self, workflow, content, **kwargs):
        """Composition entry point; preserve the existing JEV handoff and its decisions."""
        package = workflow.evaluate(content, **kwargs)
        try:
            monitor = self.process(content)
        except Exception:
            monitor = {"status": "needs_review", "reason": "monitor_failed", "execution_authorized": False}
        return {"jev": package, "monitor": monitor}
