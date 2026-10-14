"""Rule-based persona routing engine with per-account cooldown tracking."""

from __future__ import annotations

import time
from typing import Any

from .models import AccountState, Persona, PersonaArchetype, RoutingDecision
from .registry import PersonaRegistry, get_default_registry


# Keyword heuristics to supplement structured JEV signals
_MEDICAL_KEYWORDS: tuple[str, ...] = (
    "flu", "vaccin", "virus", "infect", "health", "hospital",
    "doctor", "trial", "safe", "dose", "h2d2", "medicine", "clinical",
    "epidemi", "pathogen", "transmission", "immunity",
)

_POLICY_KEYWORDS: tuple[str, ...] = (
    "policy", "version", "rule", "ordinance", "clause", "statute",
    "regulation", "law", "mandate", "govern", "condition", "section",
    "legal", "requirement", "council", "amendment",
)

_SHARP_KEYWORDS: tuple[str, ...] = (
    "fake", "scam", "sheep", "clown", "wake up", "lie", "idiot",
    "nonsense", "tyranny", "propaganda", "brainwash", "hoax",
)

_GRASSROOTS_KEYWORDS: tuple[str, ...] = (
    "bus", "transit", "park", "store", "family", "work", "job",
    "commute", "daily", "kid", "school", "shop", "subway", "train",
    "grocery", "neighbor", "living",
)


def _extract_jev_answers(data: dict[str, Any]) -> dict[str, Any]:
    """Safely extract the deepest answers map from JEV workflow data."""
    if not isinstance(data, dict):
        return {}
    # Direct answers dict
    if "answers" in data and isinstance(data["answers"], dict):
        return data["answers"]
    flow = data.get("flow") or {}
    deep = flow.get("deep") or {}
    assessment = deep.get("assessment") or {}
    output = assessment.get("output") or {}
    answers = output.get("answers") or {}
    if isinstance(answers, dict):
        return answers
    return {}


def _get_val(ans_obj: Any) -> Any:
    """Extract scalar value from a JEV answer node."""
    if isinstance(ans_obj, dict):
        return ans_obj.get("value")
    return ans_obj


class PersonaRouter:
    """Routes target posts to the most suitable account persona based on JEV signals."""

    def __init__(
        self,
        registry: PersonaRegistry | None = None,
        *,
        default_side: str = "yes",
    ) -> None:
        self.registry = registry if registry is not None else get_default_registry()
        self.default_side = default_side
        self._states: dict[str, AccountState] = {}

    def get_state(self, account_id: str) -> AccountState:
        """Return or create the runtime state for an account."""
        if account_id not in self._states:
            self._states[account_id] = AccountState(account_id=account_id)
        return self._states[account_id]

    def mark_used(self, account_id: str, now: float | None = None) -> None:
        """Record that an account just published a reply."""
        current_time = time.monotonic() if now is None else now
        state = self.get_state(account_id)
        state.mark_used(current_time)

    def reset_states(self) -> None:
        """Reset all account runtime states (cooldowns and counters)."""
        self._states.clear()

    def load_states(self, states_data: dict[str, dict[str, Any]]) -> None:
        """Load account states from dictionary."""
        for acc_id, d in states_data.items():
            self._states[acc_id] = AccountState(
                account_id=acc_id,
                last_posted_at=float(d.get("last_posted_at", 0.0)),
                post_count=int(d.get("post_count", 0)),
            )

    def dump_states(self) -> dict[str, dict[str, Any]]:
        """Dump all account states to a dictionary."""
        return {acc_id: s.to_dict() for acc_id, s in self._states.items()}

    def score_archetypes(
        self,
        data: dict[str, Any],
        post_text: str,
        prompt_type: str | None = None,
    ) -> dict[PersonaArchetype, float]:
        """Compute match scores for all archetypes given JEV data and post text."""
        answers = _extract_jev_answers(data)
        text_lower = post_text.lower()

        # Extract normalized JEV signals
        topic = str(_get_val(answers.get("topic")) or "").lower()
        func = str(_get_val(answers.get("expression_function")) or "").lower()
        claim_kind = str(_get_val(answers.get("claim_kind")) or "").lower()
        try:
            emotion_val = float(_get_val(answers.get("emotion_intensity")) or 0.0)
        except (ValueError, TypeError):
            emotion_val = 0.0
        try:
            fact_check_val = float(_get_val(answers.get("fact_check_needed")) or 0.0)
        except (ValueError, TypeError):
            fact_check_val = 0.0

        scores: dict[PersonaArchetype, float] = {
            "sharp_debater": 0.1,
            "medical_expert": 0.1,
            "policy_analyst": 0.1,
            "grassroots_worker": 0.25,  # Baseline working-class default
        }

        # 1. Sharp debater heuristics
        if func == "venting":
            scores["sharp_debater"] += 0.6
        if emotion_val >= 2.0:
            scores["sharp_debater"] += 0.3
        if any(w in text_lower for w in _SHARP_KEYWORDS):
            scores["sharp_debater"] += 0.4

        # 2. Medical expert heuristics
        if topic in ("public_health", "vaccine_evidence"):
            scores["medical_expert"] += 0.6
        if fact_check_val >= 0.5:
            scores["medical_expert"] += 0.3
        if any(w in text_lower for w in _MEDICAL_KEYWORDS):
            scores["medical_expert"] += 0.3

        # 3. Policy analyst heuristics
        if prompt_type in ("P04", "P05", "P08"):
            scores["policy_analyst"] += 0.3
        if topic in ("public_access", "economic_effects", "voting_process"):
            scores["policy_analyst"] += 0.4
        if claim_kind == "policy_proposal":
            scores["policy_analyst"] += 0.3
        if any(w in text_lower for w in _POLICY_KEYWORDS):
            scores["policy_analyst"] += 0.4

        # 4. Grassroots worker heuristics
        if func in ("experience", "call_to_action"):
            scores["grassroots_worker"] += 0.5
        if topic in ("personal_experience", "personal_choice"):
            scores["grassroots_worker"] += 0.4
        if any(w in text_lower for w in _GRASSROOTS_KEYWORDS):
            scores["grassroots_worker"] += 0.3

        return scores

    def route(
        self,
        data: dict[str, Any],
        post_text: str,
        side: str | None = None,
        prompt_type: str | None = None,
        now: float | None = None,
    ) -> RoutingDecision:
        """Match target post to best available persona, handling cooldown and fallbacks."""
        current_time = time.monotonic() if now is None else now
        target_side = (side or self.default_side).lower()

        # Retrieve candidate personas assigned to the side
        candidates = self.registry.list_by_side(target_side)
        if not candidates:
            raise ValueError(f"No registered personas for side '{target_side}'")

        # Derive prompt_type if not passed explicitly
        if prompt_type is None:
            resp_dec = data.get("response_decision") or {}
            gen_task = data.get("generation_task") or {}
            prompt_type = resp_dec.get("prompt_type") or gen_task.get("prompt_type")

        # Score archetypes
        archetype_scores = self.score_archetypes(data, post_text, prompt_type=prompt_type)

        # Sort candidates by (score * weight) descending
        scored_candidates: list[tuple[Persona, float]] = []
        for p in candidates:
            base_score = archetype_scores.get(p.archetype, 0.1)
            final_score = base_score * p.weight
            scored_candidates.append((p, final_score))

        scored_candidates.sort(key=lambda item: item[1], reverse=True)
        top_persona, top_score = scored_candidates[0]
        target_archetype = top_persona.archetype

        # Check cooldown status for candidates
        available: list[tuple[Persona, float]] = []
        in_cooldown_list: list[tuple[Persona, float, float]] = []  # persona, score, remaining_time

        for p, score in scored_candidates:
            st = self.get_state(p.account_id)
            if st.is_in_cooldown(current_time, p.cooldown_s):
                rem = st.remaining_cooldown(current_time, p.cooldown_s)
                in_cooldown_list.append((p, score, rem))
            else:
                available.append((p, score))

        # Case 1: Ideal match is ready
        if available and available[0][0].account_id == top_persona.account_id:
            return RoutingDecision(
                account_id=top_persona.account_id,
                persona=top_persona,
                target_archetype=target_archetype,
                score=round(top_score, 3),
                reason=f"matched_{target_archetype}",
                fallback_used=False,
                in_cooldown=False,
            )

        # Case 2: Ideal match is cooling down, but an alternative persona is ready
        if available:
            fallback_persona, fallback_score = available[0]
            return RoutingDecision(
                account_id=fallback_persona.account_id,
                persona=fallback_persona,
                target_archetype=target_archetype,
                score=round(fallback_score, 3),
                reason=f"fallback_from_{top_persona.account_id}_cooling_down",
                fallback_used=True,
                in_cooldown=False,
            )

        # Case 3: All accounts are in cooldown; select one with shortest remaining wait
        in_cooldown_list.sort(key=lambda item: item[2])  # sort by remaining wait time
        best_cool_persona, best_cool_score, _ = in_cooldown_list[0]
        return RoutingDecision(
            account_id=best_cool_persona.account_id,
            persona=best_cool_persona,
            target_archetype=target_archetype,
            score=round(best_cool_score, 3),
            reason=f"all_accounts_in_cooldown_earliest_{best_cool_persona.account_id}",
            fallback_used=(best_cool_persona.account_id != top_persona.account_id),
            in_cooldown=True,
        )
