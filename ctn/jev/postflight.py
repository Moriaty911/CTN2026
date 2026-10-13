"""Pre-send verification gate evaluating candidate replies using JEV.

Evaluates candidate replies against the target post, checking:
1. side_alignment: Prevents stance inversion or conceding to opponents.
2. purpose_compliance: Ensures adherence to the selected strategy (e.g. P08 clarification).
3. toxicity_and_injection: Blocks profanity, abuse, or compliance with prompt injections.
4. send_authorization: The holistic gate decision (approve / needs_review / reject).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any

from .client import JevClient
from .packing import PackedTarget, PackingResult, _digest, _json
from .parsing import JevResponseParser
from .questions import QuestionSet
from .rubrics import load_rubric


@dataclass(frozen=True)
class PostflightVerdict:
    """Outcome of the JEV pre-send verification check."""

    can_send: bool
    reason: str
    side_alignment: str = "unknown"
    purpose_compliance: str = "unknown"
    toxicity_and_injection: bool = False
    send_authorization: str = "unknown"
    answers: dict[str, Any] = field(default_factory=dict)
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "can_send": self.can_send,
            "reason": self.reason,
            "side_alignment": self.side_alignment,
            "purpose_compliance": self.purpose_compliance,
            "toxicity_and_injection": self.toxicity_and_injection,
            "send_authorization": self.send_authorization,
            "answers": self.answers,
            "detail": self.detail,
        }


class JevPostflightChecker:
    """Evaluates candidate replies in a single JEV call across 4 dimensions."""

    def __init__(
        self,
        client: JevClient,
        *,
        rubric: QuestionSet | None = None,
        parser: JevResponseParser | None = None,
        toxicity_threshold: float = 0.5,
    ) -> None:
        self.client = client
        self.rubric = rubric if rubric is not None else load_rubric("postflight", version="v1")
        self.parser = parser if parser is not None else JevResponseParser()
        self.toxicity_threshold = toxicity_threshold

    def check(
        self,
        *,
        post_id: str,
        target_text: str,
        reply_text: str,
        assigned_side: str,
        intended_purpose: str = "P08",
    ) -> PostflightVerdict:
        """Run postflight check on a candidate reply."""
        state = {
            "content": {
                "target": {
                    "post_id": post_id,
                    "text": target_text,
                },
                "reply": {
                    "text": reply_text,
                },
                "context": {
                    "assigned_side": assigned_side,
                    "intended_purpose": intended_purpose,
                },
            }
        }

        questions = self.rubric.to_provider_questions("response")
        body = {
            "model": self.client.config.model,
            "state": state,
            "questions": questions,
        }
        body_json = _json(body)
        body_bytes = body_json.encode("utf-8")
        request_id = hashlib.sha256(body_bytes).hexdigest()

        target = PackedTarget(
            role="target",
            source_kind="platform",
            post_id=post_id,
            content_path="content",
        )

        packed = PackingResult(
            status="ready",
            kind="postflight",
            stage="response",
            targets=(target,),
            issues=(),
            question_ids=tuple(questions),
            question_set_version=self.rubric.version,
            question_set_fingerprint=self.rubric.fingerprint,
            packing_fingerprint=_digest(body),
            input_fingerprint=_digest(state),
            request_id=request_id,
            state_bytes=len(_json(state).encode("utf-8")),
            request_bytes=len(body_bytes),
            _body_json=body_json,
        )

        call = self.client.evaluate(packed)
        if call.status != "success":
            return PostflightVerdict(
                can_send=False,
                reason=f"jev_call_failed:{call.status}:{call.error_code}",
                detail={"call_status": call.status, "error_code": call.error_code},
            )

        parsed = self.parser.parse(call, question_set=self.rubric, expected_request_id=request_id)
        if parsed.status != "success" or parsed.output is None:
            return PostflightVerdict(
                can_send=False,
                reason="jev_parsing_failed",
                detail={"parsed_status": parsed.status},
            )

        output_answers = parsed.output.answers
        side_alignment = output_answers.get("side_alignment")
        purpose_compliance = output_answers.get("purpose_compliance")
        toxicity_and_injection = output_answers.get("toxicity_and_injection")
        send_authorization = output_answers.get("send_authorization")

        side_val = getattr(side_alignment, "value", "unknown") if side_alignment else "unknown"
        purpose_val = getattr(purpose_compliance, "value", "unknown") if purpose_compliance else "unknown"
        raw_toxic = getattr(toxicity_and_injection, "value", 0.0) if toxicity_and_injection else 0.0
        try:
            toxic_score = float(raw_toxic)
        except (ValueError, TypeError):
            toxic_score = 1.0 if bool(raw_toxic) else 0.0
        toxic_val = toxic_score >= self.toxicity_threshold
        auth_val = getattr(send_authorization, "value", "unknown") if send_authorization else "unknown"

        answers_summary = {
            "side_alignment": side_val,
            "purpose_compliance": purpose_val,
            "toxicity_and_injection": toxic_val,
            "toxicity_score": toxic_score,
            "send_authorization": auth_val,
        }

        # Multi-factor publication gate check
        can_send = (auth_val == "approve")
        reason = "approved"

        if toxic_val:
            can_send = False
            reason = "toxic_or_injection"
        elif side_val == "opposing_or_inversion":
            can_send = False
            reason = "stance_inversion"
        elif purpose_val == "drifted":
            can_send = False
            reason = "purpose_drifted"
        elif auth_val != "approve":
            can_send = False
            reason = f"authorization_{auth_val}"

        return PostflightVerdict(
            can_send=can_send,
            reason=reason,
            side_alignment=side_val,
            purpose_compliance=purpose_val,
            toxicity_and_injection=toxic_val,
            send_authorization=auth_val,
            answers=answers_summary,
            detail={"attempt_count": len(call.attempts), "elapsed_s": call.elapsed_s},
        )
