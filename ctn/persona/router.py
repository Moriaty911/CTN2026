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


# Cross-persona complementarity matrix for allied (P01) support replies:
# Maps the detected topic archetype of an allied post to (primary_complement, secondary_complement).
_COMPLEMENTARY_ARCHETYPES: dict[PersonaArchetype, tuple[PersonaArchetype, PersonaArchetype]] = {
    "grassroots_worker": ("medical_expert", "policy_analyst"),
    "medical_expert": ("grassroots_worker", "policy_analyst"),
    "policy_analyst": ("grassroots_worker", "sharp_debater"),
    "sharp_debater": ("policy_analyst", "medical_expert"),
}


class PersonaRouter:
    """Routes target posts to the most suitable account persona, backed by SQLite StateStore."""

    def __init__(
        self,
        registry: PersonaRegistry | None = None,
        *,
        default_side: str = "yes",
        store: Any | None = None,
        db_path: str = ":memory:",
    ) -> None:
        from ctn.storage import StateStore

        self.registry = registry if registry is not None else get_default_registry()
        self.default_side = default_side
        self.store: StateStore = store if store is not None else StateStore(db_path)
        self._states: dict[str, AccountState] = {}

    def get_state(self, account_id: str) -> AccountState:
        """Return or create the runtime state for an account, synchronized with SQLite."""
        if account_id not in self._states:
            self._states[account_id] = self.store.get_account_state(account_id)
        return self._states[account_id]

    def mark_used(
        self,
        account_id: str,
        now: float | None = None,
        *,
        target_stance: str = "",
    ) -> None:
        """Record that an account just published a post or reply and persist to SQLite."""
        current_time = time.monotonic() if now is None else now
        state = self.get_state(account_id)
        state.mark_used(current_time)
        self.store.save_account_state(state, target_stance=target_stance)

    def set_account_frozen(self, account_id: str, frozen: bool = True) -> AccountState:
        """Mark an account as temporarily frozen (or unfreeze it) in memory and SQLite."""
        st = self.store.set_account_frozen(account_id, frozen=frozen)
        self._states[account_id] = st
        return st

    def set_account_banned(self, account_id: str, banned: bool = True) -> AccountState:
        """Mark an account as permanently banned (or unban it) in memory and SQLite."""
        st = self.store.set_account_banned(account_id, banned=banned)
        self._states[account_id] = st
        return st

    def reset_states(self) -> None:
        """Reset all account runtime states in memory and SQLite."""
        self._states.clear()
        self.store.reset_accounts()

    def load_states(self, states_data: dict[str, dict[str, Any]]) -> None:
        """Load account states from dictionary and persist into SQLite."""
        for acc_id, d in states_data.items():
            st = AccountState(
                account_id=acc_id,
                last_posted_at=float(d.get("last_posted_at", 0.0)),
                post_count=int(d.get("post_count", 0)),
                is_frozen=bool(d.get("is_frozen", False)),
                is_banned=bool(d.get("is_banned", False)),
            )
            self._states[acc_id] = st
            self.store.save_account_state(st)

    def dump_states(self) -> dict[str, dict[str, Any]]:
        """Dump all account states to a dictionary."""
        for acc_id, st in list(self._states.items()):
            self.store.save_account_state(st)
        all_states = self.store.load_all_account_states()
        self._states.update(all_states)
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
        *,
        complementary: bool | None = None,
    ) -> RoutingDecision:
        """Match target post to best available persona, handling disabled accounts, cooldown, fallbacks, and allied complementarity."""
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

        # Score archetypes based on topic/emotion
        archetype_scores = self.score_archetypes(data, post_text, prompt_type=prompt_type)
        detected_archetype = max(archetype_scores.items(), key=lambda item: item[1])[0]

        # Determine whether to apply Cross-Persona Complementarity for allied P01 statements
        if complementary is None:
            flow = data.get("flow") or {}
            rel_stance = flow.get("relative_stance")
            answers = _extract_jev_answers(data)
            func = str(_get_val(answers.get("expression_function")) or "").lower()
            is_question = (func == "question") or ("?" in post_text)
            use_complementary = (rel_stance == "aligned" and not is_question) or (prompt_type == "P01" and not is_question)
        else:
            use_complementary = complementary

        if use_complementary and detected_archetype in _COMPLEMENTARY_ARCHETYPES:
            primary_comp, secondary_comp = _COMPLEMENTARY_ARCHETYPES[detected_archetype]
            base_max = archetype_scores[detected_archetype]
            archetype_scores = dict(archetype_scores)
            archetype_scores[primary_comp] = base_max + 0.5
            archetype_scores[secondary_comp] = base_max + 0.25

        # Sort candidates by (score * weight) descending
        scored_candidates: list[tuple[Persona, float]] = []
        for p in candidates:
            base_score = archetype_scores.get(p.archetype, 0.1)
            final_score = base_score * p.weight
            scored_candidates.append((p, final_score))

        scored_candidates.sort(key=lambda item: item[1], reverse=True)
        top_persona, top_score = scored_candidates[0]
        target_archetype = top_persona.archetype
        match_reason = (
            f"complementary_{target_archetype}_for_{detected_archetype}"
            if use_complementary and target_archetype != detected_archetype
            else f"matched_{target_archetype}"
        )

        # Check suspension/ban and cooldown status for candidates
        available: list[tuple[Persona, float]] = []
        in_cooldown_list: list[tuple[Persona, float, float]] = []  # persona, score, remaining_time
        top_state = self.get_state(top_persona.account_id)

        for p, score in scored_candidates:
            st = self.get_state(p.account_id)
            if st.is_disabled():
                continue
            if st.is_in_cooldown(current_time, p.cooldown_s):
                rem = st.remaining_cooldown(current_time, p.cooldown_s)
                in_cooldown_list.append((p, score, rem))
            else:
                available.append((p, score))

        if not available and not in_cooldown_list:
            raise ValueError(f"All registered accounts for side '{target_side}' are frozen or banned")

        # Case 1: Ideal match is active and ready
        if available and available[0][0].account_id == top_persona.account_id:
            return RoutingDecision(
                account_id=top_persona.account_id,
                persona=top_persona,
                target_archetype=target_archetype,
                score=round(top_score, 3),
                reason=match_reason,
                fallback_used=False,
                in_cooldown=False,
            )

        # Case 2: Ideal match is cooling down or disabled, but an alternative active persona is ready
        if available:
            fallback_persona, fallback_score = available[0]
            if top_state.is_banned:
                fb_reason = f"fallback_from_{top_persona.account_id}_banned"
            elif top_state.is_frozen:
                fb_reason = f"fallback_from_{top_persona.account_id}_frozen"
            else:
                fb_reason = f"fallback_from_{top_persona.account_id}_cooling_down"
            return RoutingDecision(
                account_id=fallback_persona.account_id,
                persona=fallback_persona,
                target_archetype=target_archetype,
                score=round(fallback_score, 3),
                reason=fb_reason,
                fallback_used=True,
                in_cooldown=False,
            )

        # Case 3: All active accounts are in cooldown; select one with shortest remaining wait
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
