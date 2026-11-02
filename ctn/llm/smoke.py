"""One explicit, paid DeepSeek draft from synthetic input and LOCAL JEV answers.

python -m ctn.llm.smoke [--show-text]
No real JEV calls, account operations or publishing. Retries are disabled.
"""

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import json

from ctn.config import JevConfig, LLMConfig
from ctn.jev import HttpResponse, JevClient, JevWorkflow, MaterialBrief, TextSpan
from .client import DeepSeekClient
from .workflow import ReplyGenerator


class _SyntheticJev:
    """Deterministic answers for wire/integration verification, not judgment."""
    def send(self, **kwargs):
        body = json.loads(kwargs["body"])
        answers = {}
        fixed = {"policy_stance": "neutral", "response_action": "reply", "response_purpose": "P08"}
        for key, question in body["questions"].items():
            if question["type"] == "noul":
                answers[key] = {"type": "noul", "noul": 0.0}
            elif question["type"] == "choice":
                criteria = question["criteria"]
                choice = fixed.get(key, "sufficient" if "sufficient" in criteria else next(iter(criteria)))
                answers[key] = {"type": "choice", "choice": choice, "confidence": 1.0,
                                "probabilities": {k: float(k == choice) for k in criteria}}
            else:
                criteria = question["criteria"]
                answers[key] = {"type": "score", "score": 3, "confidence": 1.0,
                    "probabilities": {str(i): float(i == 3) for i in range(len(criteria))},
                    "legend": {str(i): value for i, value in enumerate(criteria)}}
        return HttpResponse(200, body=json.dumps({"model": "synthetic-smoke-model", "answers": answers}).encode())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--show-text", action="store_true", help="Explicitly display the generated synthetic draft")
    args = parser.parse_args()
    try:
        config = replace(LLMConfig.load(), max_retries=0)
        # The transport is local; the synthetic credential is never sent over a network.
        workflow = JevWorkflow(JevClient(JevConfig(api_key="synthetic-smoke-key", max_retries=0),
                                         transport=_SyntheticJev()))
        jev_key = JevConfig.load().api_key
        if jev_key:
            workflow.material_checker = workflow.material_checker.with_forbidden_values((jev_key,))
        text = "I keep seeing people mention exceptions to the vaccination-proof proposal. The details are unclear to me."
        post = {"schema_version": "jev-input.v1", "post": {"post_id": "synthetic-llm-smoke", "text": text},
                "source": {"kind": "synthetic", "observed_at": datetime.now(timezone.utc).isoformat()},
                "task_context": {"assigned_side": "yes"}}
        brief = MaterialBrief("P08", TextSpan(("content", "post", "text"), 0, len(text), text),
                              information_gap="Which policy version and specific exceptions does the author mean?")
        package = workflow.evaluate(post, brief=brief)
        with DeepSeekClient(config) as client:
            result = ReplyGenerator(workflow, client).generate(package, brief)
        report = result.to_dict(include_text=args.show_text)
        report.update(synthetic_only=True, jev_answers_simulated=True, real_jev_calls=0,
                      business_accuracy_validated=False, recorded_at=datetime.now(timezone.utc).isoformat())
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if result.status == "draft" else 1
    except (ValueError, OSError, UnicodeError):
        print(json.dumps({"status": "failed", "reason": "invalid_local_configuration", "real_jev_calls": 0}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
