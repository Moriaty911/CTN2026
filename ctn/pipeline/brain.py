"""The brain: JEV decides, DeepSeek drafts.

`JevLlmBrain.think(post)` is the single seam between the bot loop and the two
heavyweight subsystems:

    Post --JevWorkflow.evaluate--> action_decision
         like  -> ActionKind.LIKE
         reply -> MaterialBrief -> ReplyGenerator.generate -> ActionKind.REPLY + text
         other -> ActionKind.NONE (reason recorded; never guessed)

Everything here is synchronous and blocking (both clients use blocking I/O), so
the loop calls `athink`, which runs it in a worker thread. A draft is only a
draft: `execution_authorized` stays False inside the JEV/LLM modules, and it is
the loop's pre-publish check plus the Actuator's gate that decide what is sent.

Limitation (stage 1): the only purpose we can brief without a trusted evidence
store is P08 (ask one focused clarifying question). When JEV itself selects a
different purpose for a neutral/mixed post we do not have the materials for it
yet, so the post is skipped with an explicit reason instead of faking evidence.
The RAG/talking-point store is what will unlock P01/P02/P03/P07.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..jev import (
    JevPostflightChecker,
    JevWorkflow,
    MaterialBrief,
    MaterialUse,
    TextSpan,
    check_incoming_post_language,
)
from ..llm import DraftOptions, ReplyGenerator
from ..models import ActionKind, Decision, JevAnswers, Post, Side, Tier
from ..persona import PersonaRouter

# Generic gap used for the P08 clarification brief. It deliberately assumes no facts.
DEFAULT_INFORMATION_GAP = "Which specific policy detail, condition or evidence does the author mean?"


@dataclass
class BrainVerdict:
    action: ActionKind = ActionKind.NONE
    text: str = ""
    reason: str = ""
    prompt_type: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)   # metadata only, never post text


class JevLlmBrain:
    def __init__(
        self,
        workflow: JevWorkflow,
        generator: ReplyGenerator,
        *,
        side: Side,
        router: PersonaRouter | None = None,
        postflight: JevPostflightChecker | None = None,
        options: DraftOptions | None = None,
        information_gap: str = DEFAULT_INFORMATION_GAP,
        allied_reply_every_n: int = 3,
        allied_reply_hourly_cap: int = 3,
    ) -> None:
        if side not in (Side.YES, Side.NO):
            raise ValueError("JevLlmBrain needs an assigned side (yes/no)")
        self.workflow = workflow
        self.generator = generator
        self.side = side
        self.router = router if router is not None else PersonaRouter(default_side=side.value)
        self.postflight = postflight if postflight is not None else JevPostflightChecker(workflow.evaluator.client)
        self.options = options or DraftOptions()
        self.information_gap = information_gap
        self.allied_reply_every_n = allied_reply_every_n
        self.allied_reply_hourly_cap = allied_reply_hourly_cap

    # ---- input mapping --------------------------------------------------- #
    def payload_for(self, post: Post) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": "jev-input.v1",
            "post": {"post_id": post.post_id, "text": post.text},
            "source": {"kind": "platform", "observed_at": datetime.now(timezone.utc).isoformat()},
            "task_context": {"assigned_side": self.side.value},
        }
        level = post.raw.get("platform_level") or post.raw.get("author_level")
        if level:
            payload["author"] = {"platform_level": str(level)}
        return payload

    # ---- decision -------------------------------------------------------- #
    def think(self, post: Post) -> BrainVerdict:
        # Tier-1 Input Language Gate: Drop heavy/pure non-Latin bait before spending JEV calls
        lang_ok, lang_reason = check_incoming_post_language(post.text)
        if not lang_ok:
            return BrainVerdict(
                ActionKind.NONE,
                reason=f"preflight:{lang_reason}",
                detail={"language_gate": lang_reason},
            )

        payload = self.payload_for(post)
        package = self.workflow.evaluate(payload)
        data = package.to_dict()
        action = data["action_decision"]
        rel_stance = data["flow"].get("relative_stance")
        detail = {
            "language_gate": lang_reason,
            "flow_status": data["flow"]["status"],
            "action_status": action["status"],
            "action": action.get("action"),
            "relative_stance": rel_stance,
            "new_attempt_count": data.get("new_attempt_count"),
        }
        if action["status"] != "decided":
            return BrainVerdict(reason=f"jev:{action['status']}", detail=detail)
        if action["action"] == "like":
            self.router.store.log_interaction(
                engine="micro",
                account_id="bot_default",
                action="like",
                target_post_id=post.post_id,
                target_author=post.author,
                target_stance=str(rel_stance or ""),
            )
            return BrainVerdict(ActionKind.LIKE, reason="jev:like", detail=detail)
        if action["action"] != "reply":
            return BrainVerdict(reason=f"jev:{action['action']}", detail=detail)

        selected = (data.get("response_decision") or {}).get("prompt_type")
        if selected not in (None, "P08", "P01"):
            return BrainVerdict(reason=f"needs_materials:{selected}", prompt_type=selected, detail=detail)

        # Allied (same-camp) reply strategy: P01 cross-persona support gated by 3-level SQLite valve
        is_aligned = (rel_stance == "aligned")
        if is_aligned:
            bot_ids = {p.account_id for p in self.router.registry.list_all()}
            can_reply_allied, gate_reason = self.router.store.check_allied_reply_gate(
                target_post_id=post.post_id,
                target_author=post.author,
                bot_account_ids=bot_ids,
                every_n=self.allied_reply_every_n,
                hourly_cap=self.allied_reply_hourly_cap,
            )
            detail["allied_reply_gate"] = gate_reason
            if not can_reply_allied:
                # Downgrade throttled allied replies to a cost-free LIKE
                self.router.store.log_interaction(
                    engine="micro",
                    account_id="bot_default",
                    action="like",
                    target_post_id=post.post_id,
                    target_author=post.author,
                    target_stance="aligned",
                    purpose="like_downgrade",
                )
                return BrainVerdict(
                    ActionKind.LIKE,
                    reason=f"jev:like({gate_reason})",
                    detail=detail,
                )
            active_purpose = selected or "P01"
        else:
            active_purpose = selected or "P08"

        # Select persona and account via PersonaRouter (uses cross-persona complementarity for P01)
        try:
            routing = self.router.route(
                data=data,
                post_text=post.text,
                side=self.side.value,
                prompt_type=active_purpose,
            )
        except ValueError as exc:
            detail["routing_error"] = str(exc)
            return BrainVerdict(
                ActionKind.NONE,
                reason="all_accounts_disabled",
                prompt_type=active_purpose,
                detail=detail,
            )
        detail["routing"] = routing.to_dict()
        detail["selected_account"] = routing.account_id

        # If all eligible accounts are cooling down, pause reply to prevent spamming
        if routing.in_cooldown:
            return BrainVerdict(
                ActionKind.NONE,
                reason=f"account_cooldown:{routing.account_id}",
                prompt_type=active_purpose,
                detail=detail,
            )

        text = post.text
        span = TextSpan(("content", "post", "text"), 0, len(text), text)
        if active_purpose == "P01":
            brief = MaterialBrief("P01", span, support=(MaterialUse(span, "addition"),))
        else:
            brief = MaterialBrief("P08", span, information_gap=self.information_gap)

        # Attempt 1: generate candidate draft using selected persona
        draft = self.generator.generate(package, brief, options=self.options, persona=routing.persona)
        detail["draft"] = draft.to_dict()
        if draft.status != "draft" or not draft.reply:
            return BrainVerdict(reason=f"llm:{draft.status}:{draft.reason}", prompt_type=draft.prompt_type, detail=detail)

        # Postflight gate check 1 (strict_alignment=True for P01 allied support, False for P08 clarification)
        strict_mode = (active_purpose == "P01")
        pv1 = self.postflight.check(
            post_id=post.post_id,
            target_text=post.text,
            reply_text=draft.reply,
            assigned_side=self.side.value,
            intended_purpose=draft.prompt_type or active_purpose,
            persona=routing.persona,
            strict_alignment=strict_mode,
        )
        detail["postflight"] = pv1.to_dict()

        if pv1.can_send:
            self.router.mark_used(routing.account_id, target_stance=str(rel_stance or ""))
            self.router.store.log_interaction(
                engine="micro",
                account_id=routing.account_id,
                action="reply",
                target_post_id=post.post_id,
                target_author=post.author,
                target_stance=str(rel_stance or ""),
                purpose=draft.prompt_type or active_purpose,
                text=draft.reply,
            )
            return BrainVerdict(ActionKind.REPLY, draft.reply, "approved", draft.prompt_type or active_purpose, detail)

        # Option B: One targeted retry if postflight rejected attempt 1
        detail["postflight_attempt_1_rejected"] = pv1.reason
        draft_retry = self.generator.generate(
            package, brief, options=self.options, persona=routing.persona, retry_feedback=pv1.reason,
        )
        detail["draft_retry"] = draft_retry.to_dict()
        if draft_retry.status != "draft" or not draft_retry.reply:
            return BrainVerdict(ActionKind.NONE, reason=f"postflight_rejected:{pv1.reason}", prompt_type=draft.prompt_type, detail=detail)

        # Postflight gate check 2 on the retried draft
        pv2 = self.postflight.check(
            post_id=post.post_id,
            target_text=post.text,
            reply_text=draft_retry.reply,
            assigned_side=self.side.value,
            intended_purpose=draft_retry.prompt_type or active_purpose,
            persona=routing.persona,
            strict_alignment=strict_mode,
        )
        detail["postflight_retry"] = pv2.to_dict()

        if pv2.can_send:
            self.router.mark_used(routing.account_id, target_stance=str(rel_stance or ""))
            self.router.store.log_interaction(
                engine="micro",
                account_id=routing.account_id,
                action="reply",
                target_post_id=post.post_id,
                target_author=post.author,
                target_stance=str(rel_stance or ""),
                purpose=draft_retry.prompt_type or active_purpose,
                text=draft_retry.reply,
            )
            return BrainVerdict(ActionKind.REPLY, draft_retry.reply, "approved_on_retry", draft_retry.prompt_type or active_purpose, detail)

        # Abort reply after failed retry to protect account safety and write quota
        return BrainVerdict(ActionKind.NONE, reason=f"postflight_rejected:{pv2.reason}", prompt_type=draft.prompt_type, detail=detail)

    async def athink(self, post: Post) -> BrainVerdict:
        return await asyncio.to_thread(self.think, post)

    # ---- loop integration ------------------------------------------------ #
    async def decide(self, post: Post, *, account: str = "") -> Decision:
        """Same return type as `pipeline.decide.decide`, so the loop can swap them."""
        try:
            verdict = await self.athink(post)
            error = ""
        except Exception as exc:  # one bad post must never kill a cycle
            verdict = BrainVerdict(reason="brain_error")
            error = type(exc).__name__
        account_to_use = account or verdict.detail.get("selected_account", "bot_default")
        tier = {ActionKind.LIKE: Tier.AMPLIFY, ActionKind.REPLY: Tier.PUBLISH}.get(verdict.action, Tier.OBSERVE)
        return Decision(
            decision_id=uuid.uuid4().hex[:12], post_id=post.post_id, tier=tier, account=account_to_use,
            scores=JevAnswers(answers={"relevance": 1.0 if verdict.action is not ActionKind.NONE else 0.0},
                              raw={"source": "jev+llm", **verdict.detail}, ok=not error, error=error),
            rationale=verdict.reason, action=verdict.action, text=verdict.text,
        )
