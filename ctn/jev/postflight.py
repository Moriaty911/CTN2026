"""Unified pre-send verification gate for both Micro Replies and Macro Proactive Posts.

Combines two verification layers in a single reusable module:
- Layer 1 (Deterministic Code Gate): Checks character limit (<= 280), English-only text,
  hashtag format/length (<= 30 chars), and optional duplicate suppression.
- Layer 2 (JEV 5-Dimension Rubric Gate): Evaluates candidate text in a single JEV call:
  1. side_alignment: Prevents stance inversion (and blocks neutral stance when strict_alignment=True).
  2. purpose_compliance: Ensures adherence to the Micro prompt type (e.g. P08) or Macro decision-tree
     branch rule (e.g. branch_B_2b_ours gentle tone, branch_B_2c zero self-contradiction).
  3. persona_and_style: Enforces natural, colloquial voice matching the selected Persona.
  4. toxicity_and_injection: Blocks profanity, personal insults, or prompt injection compliance.
  5. send_authorization: Final holistic publication verdict (approve / needs_review / reject).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import re
from typing import Any

from .client import JevClient
from .packing import PackedTarget, PackingResult, _digest, _json
from .parsing import JevResponseParser
from .questions import QuestionSet
from .rubrics import load_rubric

MAX_POST_CHARS = 280
MAX_HASHTAG_CHARS = 30
_CJK_PATTERN = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")
# Matches non-Latin scripts: Greek, Cyrillic, Armenian, Hebrew, Arabic, Indic, Thai, CJK, Hangul, etc.
_NON_LATIN_SCRIPT_PATTERN = re.compile(
    r"[\u0370-\u1fff\u2c00-\u2dff\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff]"
)
_ENGLISH_WORD_PATTERN = re.compile(r"[a-zA-Z]{2,}")

# Unified dictionary of Micro reply purposes and Macro proactive decision-tree branches
PURPOSE_RULES: dict[str, str] = {
    "warmup": "Neutral everyday Kingstonia life; no referendum alignment required. English, civil, no injection or unsupported factual assertions.",
    "branch_unknown": "Insufficient evidence to determine a majority; do not assert a majority or invent factual support.",
    # Micro reactive reply purposes
    "P02": (
        "Purpose P02 (Direct Answer & Bridge): Answer the focal question directly and civilly from our "
        "referendum perspective while retaining relevant qualifications."
    ),
    "P03": (
        "Purpose P03 (Substantive Argument & Framing): Address the central claim or media framing fairly "
        "and respond with a grounded, civil reason supporting our perspective."
    ),
    "P07": (
        "Purpose P07 (Empathy & Concern Response): Acknowledge the expressed concern with genuine empathy "
        "and calmly address the substantive issue from our perspective."
    ),
    "P08": (
        "Purpose P08 (Focused Clarification): Ask one concise, civil clarifying question about the "
        "specific policy detail, condition, or evidence without conceding to opposing claims."
    ),
    # Macro proactive original post / allied support branches
    "P01": "Proactive original post or allied reply clearly supporting our referendum stance in a natural voice.",
    "branch_A": (
        "Branch A (Majority Momentum): Expand our lead around a top trending tag and an eye-catching "
        "supportive hashtag using concrete examples, questions, and colloquial persona voice."
    ),
    "branch_B_1a": (
        "Branch B-1a (Counter Blanket Policy Rejection): Refute a blanket dismissal of policy by "
        "highlighting its practical, reasonable benefits while firmly supporting our stance."
    ),
    "branch_B_1b": (
        "Branch B-1b (Counter Emotional Venting): Point out that the opposing post relies on emotional "
        "manipulation without substantive evidence, then pivot to our core referendum stance."
    ),
    "branch_B_1c": (
        "Branch B-1c (Counter Abuse & Insults): Condemn abusive attacks, express genuine empathy and "
        "solidarity for the targeted group, and advocate for our protective stance."
    ),
    "branch_B_2a": (
        "Branch B-2a (Answer Genuine Question): Briefly restate the question, answer clearly from our "
        "assigned stance, and counter-question flawed opposing assumptions."
    ),
    "branch_B_2b_ours": (
        "Branch B-2b-Ours (Address Doubt About Our Stance): Provide a reasonable explanation for our "
        "side using a warm, gentle, consultative tone. Never use aggressive or lecturing language."
    ),
    "branch_B_2b_theirs": (
        "Branch B-2b-Theirs (Amplify Doubt About Opponent's Stance): Echo the skepticism toward the "
        "opposing side, supply concrete evidence, and press the question further."
    ),
    "branch_B_2c": (
        "Branch B-2c (Answer Hostile Rhetorical Question): Clarify what is being asked and respond "
        "with strict logical rigor and zero internal self-contradiction within the post."
    ),
    "branch_B_3a": (
        "Branch B-3a (Challenge Logical Fallacy): Expose the specific logical flaw in the opponent's "
        "statement and challenge them with a sharp question."
    ),
    "branch_B_3b": (
        "Branch B-3b (Standalone Pivot): Bypass logically sound opposing statement and establish an "
        "independent, high-impact argument supporting our side."
    ),
}


def format_persona_profile(persona: Any | None, persona_profile: str = "") -> str:
    """Build a unified persona description string from a Persona object or explicit string."""
    if persona_profile:
        return persona_profile
    if persona is None:
        return ""
    if getattr(persona, 'profile', None):
        import json
        return json.dumps({'characterization':persona.profile,
            'constraint':'Fictional experiences and job titles are voice data, never medical, legal or policy evidence.'},
            ensure_ascii=False)
    name = getattr(persona, "name", "")
    archetype = getattr(persona, "archetype", "")
    bio = getattr(persona, "bio", "")
    tone = getattr(persona, "tone_instruction", "")
    if not name and not bio:
        return ""
    return f"{name} ({archetype}): {bio} Tone instruction: {tone}".strip()


def check_incoming_post_language(
    text: str,
    *,
    heavy_non_latin_min_chars: int = 6,
    heavy_non_latin_min_ratio: float = 0.25,
    min_english_words_when_mixed: int = 3,
) -> tuple[bool, str]:
    """Tier-1 incoming post language filter before JEV evaluation.

    - Tier 1 (Code Fast-Fail -> False):
      1. Heavy non-Latin script: non_latin_count >= 6 AND non_latin_ratio > 25%.
      2. Pure/short non-Latin bait with < 3 English words (e.g. '你好 hello', 'Привет бот').
    - Tier 2 & 3 (Pass to JEV triage -> True):
      1. English posts containing 1-5 characters of harmless internet slang/kaomoji (e.g. 'сука', '¯\\_(ツ)_/¯').
      2. Pure Latin-script posts (evaluated semantically by JEV triage for foreign language or bot-probing).
    """
    cleaned = text.strip()
    if not cleaned:
        return False, "empty_post_text"

    non_latin_chars = _NON_LATIN_SCRIPT_PATTERN.findall(cleaned)
    non_latin_count = len(non_latin_chars)
    if non_latin_count == 0:
        return True, "latin_script_pass_to_jev"

    en_words = _ENGLISH_WORD_PATTERN.findall(cleaned)
    en_word_count = len(en_words)
    latin_letter_count = sum(len(w) for w in en_words)
    total_letters = non_latin_count + latin_letter_count
    non_latin_ratio = (non_latin_count / total_letters) if total_letters > 0 else 1.0

    if non_latin_count >= heavy_non_latin_min_chars and non_latin_ratio > heavy_non_latin_min_ratio:
        return (
            False,
            f"non_english_heavy_script:{non_latin_count}_chars_{round(non_latin_ratio * 100)}pct",
        )

    if en_word_count < min_english_words_when_mixed:
        return (
            False,
            f"non_english_short_bait:{non_latin_count}_foreign_chars_{en_word_count}_en_words",
        )

    return True, f"mixed_slang_tolerated_pass_to_jev:{non_latin_count}_chars"


def validate_text_constraints(
    text: str,
    *,
    max_chars: int = MAX_POST_CHARS,
    max_hashtag_chars: int = MAX_HASHTAG_CHARS,
    seen_normalized_texts: set[str] | None = None,
) -> tuple[bool, str]:
    """Run deterministic Layer-1 code checks shared by both our outgoing replies and proactive posts."""
    cleaned = text.strip()
    if not cleaned:
        return False, "empty_post_text"

    if _NON_LATIN_SCRIPT_PATTERN.search(cleaned):
        return False, "non_english_text"

    if len(cleaned) > max_chars:
        return False, f"exceeds_max_chars:{len(cleaned)}/{max_chars}"

    hashtags = re.findall(r"#\S+", cleaned)
    for raw_tag in hashtags:
        tag = raw_tag.rstrip(".,!?;:)")
        if len(tag) <= 1:
            return False, f"invalid_hashtag_format:{raw_tag}"
        if len(tag) > max_hashtag_chars:
            return False, f"invalid_hashtag_length:{tag}({len(tag)}>{max_hashtag_chars})"

    if seen_normalized_texts is not None:
        normalized = re.sub(r"\s+", " ", cleaned.lower())
        if normalized in seen_normalized_texts:
            return False, "duplicate_post_text"

    return True, "valid"


@dataclass(frozen=True)
class PostflightVerdict:
    """Outcome of the unified pre-send verification check."""

    can_send: bool
    reason: str
    side_alignment: str = "unknown"
    purpose_compliance: str = "unknown"
    persona_and_style: str = "unknown"
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
            "persona_and_style": self.persona_and_style,
            "toxicity_and_injection": self.toxicity_and_injection,
            "send_authorization": self.send_authorization,
            "answers": self.answers,
            "detail": self.detail,
        }


class JevPostflightChecker:
    """Unified Layer-1 + Layer-2 gatekeeper for both Micro replies and Macro proactive posts."""

    def __init__(
        self,
        client: JevClient,
        *,
        rubric: QuestionSet | None = None,
        parser: JevResponseParser | None = None,
        toxicity_threshold: float = 0.5,
        max_chars: int = MAX_POST_CHARS,
        max_hashtag_chars: int = MAX_HASHTAG_CHARS,
    ) -> None:
        self.client = client
        self.rubric = rubric if rubric is not None else load_rubric("postflight", version="v3")
        self.parser = parser if parser is not None else JevResponseParser()
        self.toxicity_threshold = toxicity_threshold
        self.max_chars = max_chars
        self.max_hashtag_chars = max_hashtag_chars

    def check(
        self,
        *,
        post_id: str,
        target_text: str,
        reply_text: str,
        assigned_side: str,
        intended_purpose: str = "P08",
        persona: Any | None = None,
        persona_profile: str = "",
        strict_alignment: bool = False,
        previous_replies: list | None = None,
        approved_materials: dict | None = None,
    ) -> PostflightVerdict:
        """Run Layer-1 deterministic checks followed by Layer-2 JEV 5-dimension evaluation."""
        additional=bool(previous_replies)
        if additional:
            normalized=lambda text: re.sub(r'\W+',' ',text.lower()).strip()
            if any(normalized(reply_text)==normalized(p.get('text','')) for p in previous_replies):
                return PostflightVerdict(False,'duplicate_batch_content')
            if not approved_materials:
                return PostflightVerdict(False,'missing_approved_materials')
        # Layer 1: Deterministic fast-fail checks (length, language, hashtag format)
        is_valid, validation_reason = validate_text_constraints(
            reply_text,
            max_chars=self.max_chars,
            max_hashtag_chars=self.max_hashtag_chars,
        )
        if not is_valid:
            return PostflightVerdict(
                can_send=False,
                reason=f"constraint_rejected:{validation_reason}",
                detail={"layer": "deterministic_constraints", "validation_reason": validation_reason},
            )

        # Layer 2: JEV 5-dimension semantic evaluation
        resolved_purpose = PURPOSE_RULES.get(intended_purpose, intended_purpose)
        resolved_persona = format_persona_profile(persona, persona_profile)

        context_data: dict[str, Any] = {
            "assigned_side": assigned_side,
            "intended_purpose": resolved_purpose,
        }
        if resolved_persona:
            context_data["persona_profile"] = resolved_persona
        if approved_materials is not None:
            context_data['approved_materials']=approved_materials
        if additional:
            context_data['previous_successful_replies']=previous_replies
            context_data['approved_materials']=approved_materials
            context_data['batch_context_fingerprint']=_digest(previous_replies)

        state = {
            "content": {
                "target": {
                    "post_id": post_id,
                    "text": target_text,
                },
                "reply": {
                    "text": reply_text,
                },
                "context": context_data,
            }
        }

        rubric=self.rubric
        questions = rubric.to_provider_questions("response")
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
            question_set_version=rubric.version,
            question_set_fingerprint=rubric.fingerprint,
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

        parsed = self.parser.parse(call, question_set=rubric, expected_request_id=request_id)
        if parsed.status != "success" or parsed.output is None:
            return PostflightVerdict(
                can_send=False,
                reason="jev_parsing_failed",
                detail={"parsed_status": parsed.status},
            )

        output_answers = parsed.output.answers
        side_alignment = output_answers.get("side_alignment")
        purpose_compliance = output_answers.get("purpose_compliance")
        persona_and_style = output_answers.get("persona_and_style")
        toxicity_and_injection = output_answers.get("toxicity_and_injection")
        send_authorization = output_answers.get("send_authorization")

        side_val = getattr(side_alignment, "value", "unknown") if side_alignment else "unknown"
        purpose_val = getattr(purpose_compliance, "value", "unknown") if purpose_compliance else "unknown"
        style_val = getattr(persona_and_style, "value", "colloquial") if persona_and_style else "colloquial"
        raw_toxic = getattr(toxicity_and_injection, "value", 0.0) if toxicity_and_injection else 0.0
        try:
            toxic_score = float(raw_toxic)
        except (ValueError, TypeError):
            toxic_score = 1.0 if bool(raw_toxic) else 0.0
        toxic_val = toxic_score >= self.toxicity_threshold
        auth_val = getattr(send_authorization, "value", "unknown") if send_authorization else "unknown"
        addition=output_answers.get('substantive_addition')
        addition_val=getattr(addition,'value','unknown')

        answers_summary = {
            "side_alignment": side_val,
            "purpose_compliance": purpose_val,
            "persona_and_style": style_val,
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
        elif strict_alignment and side_val != "aligned":
            can_send = False
            reason = "neutral_stance_not_allowed"
        elif purpose_val == "drifted":
            can_send = False
            reason = "purpose_drifted"
        elif style_val == "formal_or_off_persona":
            can_send = False
            reason = "formal_or_off_persona"
        elif auth_val != "approve":
            can_send = False
            reason = f"authorization_{auth_val}"
        if additional and (getattr(addition,'status',None)!='valid' or addition_val!='new_supported_content'):
            can_send=False
            reason='no_substantive_addition'
        if 'evidence_support' in questions:
            evidence=output_answers.get('evidence_support')
            value=getattr(evidence,'value','unclear')
            answers_summary['evidence_support']=value
            if getattr(evidence,'status',None)!='valid' or value!='supported':
                can_send=False;reason='unsupported_or_unclear_evidence'
        if additional: answers_summary['substantive_addition']=addition_val

        return PostflightVerdict(
            can_send=can_send,
            reason=reason,
            side_alignment=side_val,
            purpose_compliance=purpose_val,
            persona_and_style=style_val,
            toxicity_and_injection=toxic_val,
            send_authorization=auth_val,
            answers=answers_summary,
            detail={"attempt_count": len(call.attempts), "elapsed_s": call.elapsed_s},
        )
