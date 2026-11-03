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

Supported reply purposes:
- P01 (support): Allied cross-persona reinforcement (gated by 3-level SQLite valve).
- P02 (answer): Direct, civil answer to neutral/mixed/opposing focal questions.
- P03 (argument): Fair representation and counter/bridge for substantive claims or media framing.
- P07 (concern): Empathetic acknowledgment and calm reassurance for personal concerns.
- P08 (clarify): Single focused clarifying question for ambiguous or venting posts.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

from ..jev import (
    ActionPolicy,
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
from ..persona.router import _extract_jev_answers, _get_val
from ..jev.expansion import expansion_decision
from ..persona.policy import REPLY_POLICY
from .targeting import CITIZEN_MAX_ID

# Generic gap used for the P08 clarification brief. It deliberately assumes no facts.
DEFAULT_INFORMATION_GAP = "Which specific policy detail, condition or evidence does the author mean?"


def _is_citizen(post) -> bool:
    """Citizens are the accounts registered before the competition opened; they are who we are persuading."""
    if hasattr(post, 'get'):
        author_id = post.get('author_id')
    else:  # a Post carries the platform payload in `.raw`
        author_id = (getattr(post, 'raw', None) or {}).get('author_id')
    try:
        return 1 <= int(author_id or 0) <= CITIZEN_MAX_ID
    except (TypeError, ValueError):
        return False

SUPPORTED_REPLY_PURPOSES: tuple[str, ...] = ("P01", "P02", "P03", "P07", "P08")

_PURPOSE_SUPPORT_USE: dict[str, str] = {
    "P01": "addition",
    "P02": "answer_basis",
    "P03": "argument_basis",
    "P07": "response_basis",
}

DEFAULT_PLATFORM_LEVELS: tuple[tuple[str, str], ...] = (
    ("standard", "standard"),
    ("citizen", "standard"),
    ("unverified", "standard"),
    ("test-standard", "standard"),
    ("high", "high"),
    ("verified", "high"),
    ("vip", "high"),
    ("doctor", "high"),
    ("official", "high"),
    ("newspaper", "high"),
    ("test-high", "high"),
)

_HIGH_LEVEL_SYNONYMS: frozenset[str] = frozenset(
    {"high", "verified", "vip", "doctor", "official", "newspaper", "test-high"}
)

_VIP_HANDLES: frozenset[str] = frozenset({"daily_kingstonia", "kingstonia_herald"})
_VIP_PREFIXES: tuple[str, ...] = (
    "mayor_",
    "dr_",
    "prof_",
    "senator_",
    "minister_",
    "official_",
    "council_",
)

_CONCERN_MARKERS: tuple[str, ...] = (
    "worried",
    "worry",
    "concerned",
    "concern",
    "afraid",
    "scared",
    "anxious",
    "nervous",
    "side effect",
    "my kids",
    "my family",
    "livelihood",
)


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
        allied_reply_every_n: int = 1,
        allied_reply_hourly_cap: int = 3,
        trusted_background: tuple = (),
        corpus=None,
    ) -> None:
        if side not in (Side.YES, Side.NO):
            raise ValueError("JevLlmBrain needs an assigned side (yes/no)")
        self.workflow = workflow
        from ..jev import JevEvaluator
        from ..jev.rubrics import load_rubric
        if type(getattr(workflow,"evaluator",None)) is JevEvaluator:
            workflow.evaluator.response_questions=load_rubric("response",version="v2")
        self._ensure_action_policy_levels()
        self.generator = generator
        self.side = side
        self.router = router if router is not None else PersonaRouter(default_side=side.value)
        self.postflight = postflight if postflight is not None else JevPostflightChecker(workflow.evaluator.client)
        self.router.store.set_policy('capabilities_version','reply-production.v2:P01,P02,P03,P07,P08')
        self.options = options or DraftOptions(language="en", max_reply_chars=280)
        self.corpus=corpus
        self.information_gap = information_gap
        self.trusted_background=trusted_background
        self.allied_reply_every_n = allied_reply_every_n
        self.allied_reply_hourly_cap = allied_reply_hourly_cap

    def _ensure_action_policy_levels(self) -> None:
        """Ensure ActionPolicy has platform_levels mappings so opposing [1, 3] posts are never dropped as unknown."""
        policy = getattr(self.workflow, "action_policy", None)
        if type(policy) is not ActionPolicy:
            return
        existing = dict(policy.platform_levels)
        merged = list(policy.platform_levels)
        for label, classification in DEFAULT_PLATFORM_LEVELS:
            if label not in existing:
                merged.append((label, classification))
        if len(merged) != len(policy.platform_levels):
            self.workflow.action_policy = replace(policy, platform_levels=tuple(merged))

    def _infer_platform_level(self, post: Post) -> str:
        """Resolve a mapped platform_level label for the post author."""
        raw = post.raw if isinstance(post.raw, dict) else {}
        explicit = str(raw.get("platform_level") or raw.get("author_level") or "").strip()
        if explicit:
            candidate = explicit
        else:
            author_obj = raw.get("author")
            is_verified = bool(raw.get("verified")) or (
                isinstance(author_obj, dict) and bool(author_obj.get("verified"))
            )
            handle_low = (post.author or "").strip().lower()
            is_verified = is_verified or bool(self.router.store.get_bot_profile(post.author).get("verified"))
            candidate = "verified" if is_verified else "unknown"

        policy = getattr(self.workflow, "action_policy", None)
        if type(policy) is ActionPolicy and policy.platform_levels:
            level_map = dict(policy.platform_levels)
            if candidate in level_map:
                return candidate
            if candidate.lower() in level_map:return candidate.lower()
        return candidate

    # ---- input mapping --------------------------------------------------- #
    def payload_for(self, post: Post) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": "jev-input.v1",
            "post": {"post_id": post.post_id, "text": post.text},
            "source": {"kind": "platform", "observed_at": datetime.fromtimestamp(
                (self.router.store.get_observed_post(post.post_id) or {}).get("last_seen_at",datetime.now(timezone.utc).timestamp()),timezone.utc).isoformat()},
            "task_context": {"assigned_side": self.side.value},
            "author": {"platform_level": self._infer_platform_level(post)},
        }
        iso=lambda ts: datetime.fromtimestamp(ts,timezone.utc).isoformat()
        observed=payload['source']['observed_at']
        presence=post.raw.get('_metric_presence',post.raw.keys())
        payload['metrics']={k:getattr(post,k) for k in ('likes','reposts','replies') if k in presence or (not post.raw.get('_metric_presence') and getattr(post,k)>0)}
        payload['metrics']['observed_at']=observed
        if post.created_at>0: payload['post']['created_at']=iso(post.created_at)
        parent_id=str(post.raw.get('parent_id') or '')
        payload['post']['kind']='reply' if parent_id else 'original'
        payload['post']['text_complete']=post.raw.get('text_complete', True)
        if parent_id: payload['post']['parent_id']=parent_id
        parent=self.router.store.get_observed_post(parent_id) if parent_id else None
        payload['conversation']={'status':'available' if parent else ('partial' if parent_id else 'not_applicable'),
            'items':([{'role':'parent','post_id':parent_id,'text':parent['text'],
                'source':{'kind':'platform','observed_at':iso(parent['last_seen_at'])},'text_complete':parent['raw'].get('text_complete',True)}] if parent else [])}
        with self.router.store._lock:
            rows=self.router.store._conn.execute("SELECT * FROM interaction_log WHERE target_post_id=? "
                "AND action IN ('reply','like','repost') ORDER BY created_at DESC LIMIT 20",(post.post_id,)).fetchall()
        payload['interaction_history']={'status':'partial','events':[{'kind':r['action'],
            'target_post_id':r['target_post_id'],'acted_at':iso(r['created_at']),'text':r['text']} for r in rows]}
        if self.trusted_background:
            payload['trusted_background']={'items':[asdict(r) for r in self.trusted_background]}
        if self.corpus is not None:
            stored=self.router.store.get_observed_post(post.post_id) or {}
            snapshot=self.corpus.retrieve(post.text,target_id=post.post_id,target_version=stored.get('version',1))
            ident=self.router.store.save_rag_snapshot(snapshot)
            self.workflow.evaluator.packer.register_campaign_snapshot(snapshot)
            payload['campaign_materials']={'snapshot_id':ident,'corpus_hash':self.corpus.hash,'items':snapshot.campaign_items()}
        return payload

    # ---- decision -------------------------------------------------------- #
    @staticmethod
    def _extract_root_post_id(post: Post) -> str:
        raw = post.raw if isinstance(post.raw, dict) else {}
        root_val = raw.get("root_post_id") or raw.get("parent_id") or ""
        return str(root_val).strip()

    @staticmethod
    def _select_opposing_purpose(data: dict[str, Any], post_text: str) -> str:
        """Select the most effective reply purpose (P02/P03/P07/P08) for an opposing post."""
        answers = _extract_jev_answers(data)
        func = str(_get_val(answers.get("expression_function")) or "").lower()
        claim_kind = str(_get_val(answers.get("claim_kind")) or "").lower()
        text_low = post_text.lower()

        if func == "question":
            return "P02"
        if func == "venting":
            return "P08"
        if func == "concern" or any(marker in text_low for marker in _CONCERN_MARKERS):
            return "P07"
        if func == "question":
            return "P02"
        if func == "argument" and claim_kind in ("policy_proposal", "factual") and "?" not in post_text:
            return "P03"
        return "P08"

    def _build_material_brief(self, active_purpose: str, post: Post, package: Any) -> MaterialBrief:
        """Construct a checked MaterialBrief for P01, P02, P03, P07, or P08."""
        text = post.text
        focus_span = TextSpan(("content", "post", "text"), 0, len(text), text)
        full_data=package.to_dict(include_materials=True)
        campaign=((full_data.get('materials') or {}).get('content') or {}).get('campaign_materials')
        if campaign:
            items=campaign['items']
            if active_purpose=='P08':
                unknown=next((r for r in items if r['kind']=='unknown'),None)
                # The target still defines the gap; the corpus supplies a related question, never its answer.
                gap=(unknown['text'].split('\n')[0] if unknown else self.information_gap)
                return MaterialBrief('P08',focus_span,information_gap=gap)
            accepted=('official_fact','attributed_claim','argument','unknown') if active_purpose in ('P02','P07') else ('official_fact','attributed_claim','argument')
            supports=tuple(MaterialUse(TextSpan(('content','campaign_materials','items',i,'text'),0,len(r['text']),r['text']),_PURPOSE_SUPPORT_USE[active_purpose]) for i,r in enumerate(items) if r['kind'] in accepted)
            if not supports:raise ValueError('no_relevant_material')
            return MaterialBrief(active_purpose,focus_span,support=supports)
        if active_purpose in _PURPOSE_SUPPORT_USE:
            use_kind = _PURPOSE_SUPPORT_USE[active_purpose]
            support_span = None
            try:
                full_data = package.to_dict(include_materials=True)
                vb_items = ((full_data.get("materials") or {}).get("verified_background") or {}).get("items") or []
                if vb_items and isinstance(vb_items[0], dict) and vb_items[0].get("text"):
                    ref_text = str(vb_items[0]["text"])
                    support_span = TextSpan(("verified_background", "items", 0, "text"), 0, len(ref_text), ref_text)
            except Exception:
                support_span = None
            if support_span is None: raise ValueError("no_verified_answer_basis")
            return MaterialBrief(active_purpose, focus_span, support=(MaterialUse(support_span, use_kind),))
        return MaterialBrief("P08", focus_span, information_gap=self.information_gap)

    def think(self, post: Post, *, commit_state: bool = False, assessment=None) -> BrainVerdict:
        if commit_state: raise ValueError("planning cannot commit execution state")
        # Tier-1 Input Language Gate: Drop heavy/pure non-Latin bait before spending JEV calls
        lang_ok, lang_reason = check_incoming_post_language(post.text)
        if not lang_ok:
            return BrainVerdict(
                ActionKind.NONE,
                reason=f"preflight:{lang_reason}",
                detail={"language_gate": lang_reason},
            )

        payload = self.payload_for(post)
        package = assessment if assessment is not None else self.workflow.evaluate(payload)
        data = package.to_dict()
        action = data["action_decision"]
        rel_stance = data["flow"].get("relative_stance")
        root_post_id = self._extract_root_post_id(post)
        detail: dict[str, Any] = {
            "language_gate": lang_reason,
            "flow_status": data["flow"]["status"],
            "action_status": action["status"],
            "needs": action.get("needs",[]),
            "action": action.get("action"),
            "relative_stance": rel_stance,
            "target_author": post.author,
            "required_account_level": "high" if "high" in (action.get("required_account_level"),action.get("minimum_account_level")) else action.get("required_account_level"),
            "root_post_id": root_post_id,
            "new_attempt_count": data.get("new_attempt_count"),
        }
        if action["status"] != "decided":
            return BrainVerdict(reason=f"jev:{action['status']}", detail=detail)
        if action["action"] == "like":
            detail["purpose"] = ""
            detail["state_committed"] = False
            return BrainVerdict(ActionKind.LIKE, reason="jev:like", detail=detail)
        if action["action"] != "reply":
            return BrainVerdict(reason=f"jev:{action['action']}", detail=detail)
        if rel_stance == "opposing":
            from .targeting import poll_window_rules
            window = poll_window_rules()
            func = str(_get_val(_extract_jev_answers(data).get("expression_function")) or "").lower()
            if window and func in window.get("skip_opposing_functions", ()):
                # Before the poll, a reply to a venting opponent risks a change of mind away from us.
                return BrainVerdict(reason=f"poll_window:opposing_{func}", detail=detail)

        selected = (data.get("response_decision") or {}).get("prompt_type")
        if selected is not None and selected not in SUPPORTED_REPLY_PURPOSES:
            return BrainVerdict(reason=f"unsupported_purpose:{selected}", prompt_type=selected, detail=detail)

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
                # A like uses its own route budget; it does not imply scoring credit.
                detail["purpose"] = "like_downgrade"
                detail["state_committed"] = False
                return BrainVerdict(
                    ActionKind.LIKE,
                    reason=f"jev:like({gate_reason})",
                    detail=detail,
                )
            active_purpose = selected or "P01"
        elif selected is not None:
            active_purpose = selected
        else:
            active_purpose = self._select_opposing_purpose(data, post.text)

        # Filter account eligibility, then match occupation and scene.
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
        detail.update(self.router.context_for(routing.persona))
        detail['topic']=str(_get_val(_extract_jev_answers(data).get('topic')) or 'unknown')

        # If all eligible accounts are cooling down, pause reply to prevent spamming
        if routing.in_cooldown:
            detail["cooldown_remaining_s"] = round(self.router.get_state(routing.account_id).remaining_cooldown(
                time.time(), routing.persona.cooldown_s), 1)
            return BrainVerdict(
                ActionKind.NONE,
                reason=f"account_cooldown:{routing.account_id}",
                prompt_type=active_purpose,
                detail=detail,
            )

        try:
            brief = self._build_material_brief(active_purpose, post, package)
        except ValueError:
            return BrainVerdict(reason="needs_materials:"+active_purpose,prompt_type=active_purpose,detail=detail)

        campaign=((package.to_dict(include_materials=True).get('materials') or {}).get('content') or {}).get('campaign_materials')
        snapshot=self.router.store.load_rag_snapshot(campaign['snapshot_id']) if campaign else None
        rag_kwargs={'rag_snapshot':snapshot} if snapshot else {}
        if snapshot:detail['rag_snapshot_id']=snapshot.snapshot_id
        # Attempt 1: generate candidate draft using selected persona
        draft = self.generator.generate(package, brief, options=self.options, persona=routing.persona, **rag_kwargs)
        detail["draft"] = draft.to_dict()
        if draft.status != "draft" or not draft.reply:
            return BrainVerdict(reason=f"llm:{draft.status}:{draft.reason}", prompt_type=draft.prompt_type, detail=detail)
        detail['draft_created_at']=time.time()

        # Postflight gate check 1 (strict_alignment=True for P01 allied support, False for P02/P03/P07/P08)
        strict_mode = (active_purpose == "P01")
        pv1 = self.postflight.check(
            post_id=post.post_id,
            target_text=post.text,
            reply_text=draft.reply,
            assigned_side=self.side.value,
            intended_purpose=draft.prompt_type or active_purpose,
            persona=routing.persona,
            strict_alignment=strict_mode,
            approved_materials=package.to_dict(include_materials=True).get("materials"),
        )
        detail["postflight"] = pv1.to_dict()

        from ..rag.corpus import too_close
        from ..jev.postflight import PostflightVerdict
        references=self.router.store.publication_references()
        if snapshot:references += [s['text'] for s in snapshot.to_dict()['styles']]
        if too_close(draft.reply,references):pv1=PostflightVerdict(False,'near_duplicate_or_style_copy')
        from ..phrasing import recent_texts,repetition_reason,unanswered_doubt
        recent=recent_texts(self.router.store)
        repeated=repetition_reason(draft.reply,recent) if pv1.can_send else ''
        if repeated:pv1=PostflightVerdict(False,repeated)
        doubt=unanswered_doubt(post.text,draft.reply) if pv1.can_send and _is_citizen(post) else ''
        if doubt:pv1=PostflightVerdict(False,doubt)
        self.router.store.record_quality(routing.account_id,post.post_id,pv1.can_send,pv1.reason,'primary')
        if pv1.can_send:
            self._batch_materials(detail,draft,data,post)
            final_purpose = draft.prompt_type or active_purpose
            detail["purpose"] = final_purpose
            detail["state_committed"] = False
            return BrainVerdict(ActionKind.REPLY, draft.reply, "approved", final_purpose, detail)

        # Option B: One targeted retry if postflight rejected attempt 1
        detail["postflight_attempt_1_rejected"] = pv1.reason
        draft_retry = self.generator.generate(
            package, brief, options=self.options, persona=routing.persona, retry_feedback=pv1.reason, **rag_kwargs,
        )
        detail["draft_retry"] = draft_retry.to_dict()
        if draft_retry.status != "draft" or not draft_retry.reply:
            return BrainVerdict(ActionKind.NONE, reason=f"postflight_rejected:{pv1.reason}", prompt_type=draft.prompt_type, detail=detail)
        detail['draft_created_at']=time.time()

        # Postflight gate check 2 on the retried draft
        pv2 = self.postflight.check(
            post_id=post.post_id,
            target_text=post.text,
            reply_text=draft_retry.reply,
            assigned_side=self.side.value,
            intended_purpose=draft_retry.prompt_type or active_purpose,
            persona=routing.persona,
            strict_alignment=strict_mode,
            approved_materials=package.to_dict(include_materials=True).get("materials"),
        )
        detail["postflight_retry"] = pv2.to_dict()

        if too_close(draft_retry.reply,references):pv2=PostflightVerdict(False,'near_duplicate_or_style_copy')
        repeated=repetition_reason(draft_retry.reply,recent) if pv2.can_send else ''
        if repeated:pv2=PostflightVerdict(False,repeated)
        doubt=unanswered_doubt(post.text,draft_retry.reply) if _is_citizen(post) else ''
        if doubt:detail['doubt_unanswered']=doubt
        self.router.store.record_quality(routing.account_id,post.post_id,pv2.can_send,pv2.reason,'primary')
        if pv2.can_send:
            self._batch_materials(detail,draft_retry,data,post)
            final_purpose = draft_retry.prompt_type or active_purpose
            detail["purpose"] = final_purpose
            detail["state_committed"] = False
            return BrainVerdict(ActionKind.REPLY, draft_retry.reply, "approved_on_retry", final_purpose, detail)

        # Abort reply after failed retry to protect account safety and write quota
        return BrainVerdict(ActionKind.NONE, reason=f"postflight_rejected:{pv2.reason}", prompt_type=draft.prompt_type, detail=detail)

    @staticmethod
    def _disposition(verdict,error):
        if verdict.action is not ActionKind.NONE:return 'execute'
        status=verdict.detail.get('flow_status')
        action_status=verdict.detail.get('action_status')
        if status=='needs_context' or action_status in ('needs_materials','needs_review','needs_account_metadata') or verdict.reason.startswith(('needs_materials:','llm:needs_materials','llm:needs_review','unsupported_purpose:')):return 'wait'
        if error or status in ('failed','deferred') or action_status in ('needs_assessment','needs_jev_assessment') or verdict.reason.startswith(('account_cooldown:','all_accounts_disabled','llm:')):return 'retry'
        return 'skip'

    def _batch_materials(self,detail,draft,data,post):
        """Only an approved primary draft can propose a persistent batch."""
        frozen=getattr(draft,'frozen_material_json','')
        if not frozen or not self.router.registry.catalog_hash:return
        expansion=expansion_decision(data,material_ready=True)
        from .targeting import launch_rule
        if expansion['allowed'] and not launch_rule(self.router.store,'reply_batches_enabled'):
            # Several of our accounts in one thread is the first thing other teams' detectors look for.
            expansion={**expansion,'allowed':False,'reason':'batches_disabled_at_launch'}
        requested=REPLY_POLICY['extra_count_max'] if expansion['allowed'] else 0
        extras=self.router.additional_candidates(data,post.text,detail['selected_account'],requested,
            purpose=draft.prompt_type,materials=json.loads(frozen)['materials'])
        detail['reply_batch_plan']={
            'rag_snapshot_id':detail.get('rag_snapshot_id',''),
            'version':'ctn-reply-batch.v1','catalog_hash':self.router.registry.catalog_hash,
            'expansion':expansion,'requested_extra_count':requested,
            'sampled_persona_ids':[p.persona_id for p in extras],
            'extra_accounts':[p.account_id for p in extras],
            'frozen_material_json':frozen,'target_text':post.text,
            'post_rubric_version':self.workflow.evaluator.post_questions.version,
            'prompt_version':draft.prompt_version,'material_version':draft.material_version}

    def draft_additional(self,plan,persona,previous,primary):
        """Generate and review one fixed slot without evaluating the target again."""
        frozen=plan['frozen_material_json'];materials=json.loads(frozen)['materials']
        if plan.get('rag_snapshot_id'):self.router.store.load_rag_snapshot(plan['rag_snapshot_id'])
        purpose=materials['prompt_type'];feedback=None
        for attempt in range(2):
            draft=self.generator.generate_frozen(frozen,persona=persona,previous_replies=previous,
                options=self.options,retry_feedback=feedback)
            if draft.status!='draft' or not draft.reply:return None,'additional_draft:'+draft.reason
            drafted_at=time.time()
            verdict=self.postflight.check(post_id=primary['post_id'],target_text=plan['target_text'],
                reply_text=draft.reply,assigned_side=self.side.value,intended_purpose=purpose,
                persona=persona,strict_alignment=(purpose=='P01'),
                previous_replies=previous,approved_materials=materials)
            from ..rag.corpus import too_close
            from ..jev.postflight import PostflightVerdict
            refs=self.router.store.publication_references()
            if too_close(draft.reply,refs):verdict=PostflightVerdict(False,'near_duplicate_publication')
            from ..phrasing import recent_texts,repetition_reason
            repeated=repetition_reason(draft.reply,recent_texts(self.router.store)) if verdict.can_send else ''
            if repeated:verdict=PostflightVerdict(False,repeated)
            self.router.store.record_quality(persona.account_id,primary['post_id'],verdict.can_send,verdict.reason,'additional')
            if verdict.can_send:
                decision=json.loads(json.dumps(primary))
                decision.update(decision_id=uuid.uuid4().hex[:12],account=persona.account_id,
                    text=draft.reply,rationale='approved_additional')
                decision['scores']['raw'].pop('reply_batch_plan',None)
                decision['scores']['raw'].update(self.router.context_for(persona))
                decision['scores']['raw'].update(draft=draft.to_dict(),postflight=verdict.to_dict())
                decision['scores']['raw']['draft_created_at']=drafted_at
                return decision,'approved'
            feedback=verdict.reason
        return None,'additional_postflight:'+feedback

    def refresh_registered_background(self):
        from ..jev import BackgroundReference,JevPacker
        data=self.router.store.policy('trusted_background',None)
        if data is None:return
        refs=tuple(BackgroundReference(**r) for r in data)
        if refs==self.trusted_background:return
        packer=self.workflow.evaluator.packer
        self.workflow.evaluator.packer=JevPacker(model=packer.model,limits=packer.limits,
            preflight=packer.preflight,forbidden_values=packer._forbidden_values,verified_references=refs)
        self.trusted_background=refs

    async def assess(self, post: Post):
        if not check_incoming_post_language(post.text)[0]:return None
        return await asyncio.to_thread(self.workflow.evaluate,self.payload_for(post))

    async def athink(self, post: Post, *, commit_state: bool = False, assessment=None) -> BrainVerdict:
        return await asyncio.to_thread(self.think, post, commit_state=commit_state,assessment=assessment)

    def commit_decision(self, decision: Decision, *, account: str | None = None) -> None:
        """Legacy API: accounting belongs to Actuator's atomic success transaction."""
        raise RuntimeError('commit via Actuator execution, never from a model decision')

    # ---- loop integration ------------------------------------------------ #
    async def decide(
        self,
        post: Post,
        *,
        account: str = "",
        commit_state: bool = False,
        assessment=None,
    ) -> Decision:
        """Same return type as `pipeline.decide.decide`, so the loop can swap them."""
        try:
            verdict = await self.athink(post, commit_state=commit_state,assessment=assessment)
            error = ""
        except Exception as exc:  # one bad post must never kill a cycle
            verdict = BrainVerdict(reason="brain_error")
            error = type(exc).__name__
        account_to_use = account or verdict.detail.get("selected_account", "bot_default")
        root_post_id = str(verdict.detail.get("root_post_id") or self._extract_root_post_id(post))
        tier = {ActionKind.LIKE: Tier.AMPLIFY, ActionKind.REPLY: Tier.PUBLISH}.get(verdict.action, Tier.OBSERVE)
        return Decision(
            decision_id=uuid.uuid4().hex[:12],
            post_id=post.post_id,
            tier=tier,
            account=account_to_use,
            scores=JevAnswers(
                answers={"relevance": 1.0 if verdict.action is not ActionKind.NONE else 0.0},
                raw={"source": "jev+llm", **verdict.detail},
                ok=not error,
                error=error,
            ),
            rationale=verdict.reason,
            action=verdict.action,
            text=verdict.text,
            root_post_id=root_post_id,
            disposition=self._disposition(verdict,error),
            needs=tuple(verdict.detail.get("needs",())),
        )
