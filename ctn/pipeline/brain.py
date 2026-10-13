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

from ..jev import JevWorkflow, MaterialBrief, TextSpan
from ..llm import DraftOptions, ReplyGenerator
from ..models import ActionKind, Decision, JevAnswers, Post, Side, Tier

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
        options: DraftOptions | None = None,
        information_gap: str = DEFAULT_INFORMATION_GAP,
    ) -> None:
        if side not in (Side.YES, Side.NO):
            raise ValueError("JevLlmBrain needs an assigned side (yes/no)")
        self.workflow = workflow
        self.generator = generator
        self.side = side
        self.options = options or DraftOptions()
        self.information_gap = information_gap

    # ---- input mapping --------------------------------------------------- #
    def payload_for(self, post: Post) -> dict[str, Any]:
        return {
            "schema_version": "jev-input.v1",
            "post": {"post_id": post.post_id, "text": post.text},
            "source": {"kind": "platform", "observed_at": datetime.now(timezone.utc).isoformat()},
            "task_context": {"assigned_side": self.side.value},
        }

    # ---- decision -------------------------------------------------------- #
    def think(self, post: Post) -> BrainVerdict:
        payload = self.payload_for(post)
        package = self.workflow.evaluate(payload)
        data = package.to_dict()
        action = data["action_decision"]
        detail = {
            "flow_status": data["flow"]["status"],
            "action_status": action["status"],
            "action": action.get("action"),
            "relative_stance": data["flow"].get("relative_stance"),
            "new_attempt_count": data.get("new_attempt_count"),
        }
        if action["status"] != "decided":
            return BrainVerdict(reason=f"jev:{action['status']}", detail=detail)
        if action["action"] == "like":
            return BrainVerdict(ActionKind.LIKE, reason="jev:like", detail=detail)
        if action["action"] != "reply":
            return BrainVerdict(reason=f"jev:{action['action']}", detail=detail)

        selected = (data.get("response_decision") or {}).get("prompt_type")
        if selected not in (None, "P08"):
            return BrainVerdict(reason=f"needs_materials:{selected}", prompt_type=selected, detail=detail)

        text = post.text
        brief = MaterialBrief(
            "P08", TextSpan(("content", "post", "text"), 0, len(text), text),
            information_gap=self.information_gap,
        )
        # generate() completes local preparation from the supplied brief itself.
        draft = self.generator.generate(package, brief, options=self.options)
        detail["draft"] = draft.to_dict()
        if draft.status != "draft" or not draft.reply:
            return BrainVerdict(reason=f"llm:{draft.status}:{draft.reason}", prompt_type=draft.prompt_type, detail=detail)
        return BrainVerdict(ActionKind.REPLY, draft.reply, "llm:draft", draft.prompt_type, detail)

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
        tier = {ActionKind.LIKE: Tier.AMPLIFY, ActionKind.REPLY: Tier.PUBLISH}.get(verdict.action, Tier.OBSERVE)
        return Decision(
            decision_id=uuid.uuid4().hex[:12], post_id=post.post_id, tier=tier, account=account,
            scores=JevAnswers(answers={"relevance": 1.0 if verdict.action is not ActionKind.NONE else 0.0},
                              raw={"source": "jev+llm", **verdict.detail}, ok=not error, error=error),
            rationale=verdict.reason, action=verdict.action, text=verdict.text,
        )
