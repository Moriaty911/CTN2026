"""Strategic proactive publisher reusing unified postflight gatekeeping and cooldown tracking."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from typing import Any

from ctn.actuator import Actuator
from ctn.jev import JevPostflightChecker
from ctn.jev.postflight import (
    MAX_HASHTAG_CHARS,
    MAX_POST_CHARS,
    PURPOSE_RULES,
    format_persona_profile,
    validate_text_constraints,
)
from ctn.persona import PersonaRouter

# Alias kept for external callers
BRANCH_RULES = PURPOSE_RULES


class StrategicPublisher:
    """Executes safe proactive posts, delegating text validation and semantic review to postflight."""

    def __init__(
        self,
        actuator: Actuator,
        postflight: JevPostflightChecker,
        router: PersonaRouter | None = None,
        *,
        side: str = "yes",
        max_post_chars: int = MAX_POST_CHARS,
        max_hashtag_chars: int = MAX_HASHTAG_CHARS,
    ) -> None:
        self.actuator = actuator
        self.postflight = postflight
        self.side = side.lower()
        self.router = router if router is not None else PersonaRouter(default_side=self.side)
        if actuator.store is None:
            actuator.store = self.router.store
        elif actuator.store is not self.router.store and actuator.store.db_path != self.router.store.db_path:
            raise ValueError("publisher and actuator must share SQLite")
        if self.side in ("yes","no"): self.router.store.bind_campaign_side(self.side)
        self.max_post_chars = max_post_chars
        self.actuator.catalog_hash=self.router.registry.catalog_hash
        self.max_hashtag_chars = max_hashtag_chars
        self._recent_normalized_posts: set[str] = set()

    def validate_post_constraints(self, post_text: str) -> tuple[bool, str]:
        """Delegate deterministic Layer-1 code checks to unified postflight validator and SQLite store."""
        is_valid, reason = validate_text_constraints(
            post_text,
            max_chars=self.max_post_chars,
            max_hashtag_chars=self.max_hashtag_chars,
            seen_normalized_texts=self._recent_normalized_posts,
        )
        if not is_valid:
            return False, reason
        if self.router.store.has_normalized_text(post_text):
            return False, "duplicate_post_text"
        return True, "valid"

    async def publish(
        self,
        account_id: str,
        text: str,
        *,
        hashtags: list[str] | None = None,
        purpose: str = "P01",
        branch: str = "",
        target_text: str = "",
        bypass_jev: bool = False,
        rag_snapshot_id: str = "",
        macro_job_id: str = "",
        macro_owner: str = "",
    ) -> dict[str, Any]:
        """Publish a proactive post after cooldown and unified Layer-1 + Layer-2 verification."""
        now = time.time()
        mock = self.actuator._client.__class__.__name__ == 'MockLegitClient'
        if bypass_jev and not (mock or self.actuator.dry_run):
            return {"ok":False,"status":"blocked","reason":"live_jev_bypass_forbidden"}
        if self.side not in ('yes','no'):
            return {"ok":False,"status":"blocked","reason":"side_unassigned"}
        persona = self.router.registry.get(account_id)
        if persona is None:
            return {
                "ok": False,
                "status": "rejected",
                "reason": f"unknown_account:{account_id}",
                "account_id": account_id,
            }
        if not persona.account_id:
            return {'ok':False,'status':'blocked','reason':'persona_unbound'}
        account_id=persona.account_id
        ready,reason=self.router.store.persona_action_ready(account_id,self.router.context_for(persona))
        if not ready:return {'ok':False,'status':'blocked','reason':reason}

        # 1. Suspension / Ban & Cooldown verification
        state = self.router.get_state(account_id)
        if persona.assigned_side != self.side:
            return {"ok":False,"status":"blocked","reason":"side_mismatch"}
        if state.mode != 'active':
            return {"ok":False,"status":"blocked","reason":"account_"+state.mode}
        if purpose not in ('warmup','initialise') and state.original_locked_until > now:
            return {"ok":False,"status":"blocked","reason":"original_locked"}
        if state.is_banned:
            return {
                "ok": False,
                "status": "blocked",
                "reason": "account_banned",
                "account_id": account_id,
            }
        if state.is_frozen:
            return {
                "ok": False,
                "status": "blocked",
                "reason": "account_frozen",
                "account_id": account_id,
            }
        if state.is_in_cooldown(now, persona.cooldown_s):
            rem = state.remaining_cooldown(now, persona.cooldown_s)
            return {
                "ok": False,
                "status": "blocked",
                "reason": f"account_in_cooldown:{round(rem, 1)}s",
                "account_id": account_id,
                "remaining_cooldown_s": round(rem, 1),
            }

        # Append hashtags if specified and not already in text
        post_text = text.strip()
        if hashtags:
            for tag in hashtags:
                formatted_tag = tag if tag.startswith("#") else f"#{tag}"
                if formatted_tag.lower() not in post_text.lower():
                    post_text = f"{post_text} {formatted_tag}"

        # 2. Layer-1 Deterministic Code Gate (shared with micro replies)
        is_valid, validation_reason = self.validate_post_constraints(post_text)
        if not is_valid:
            return {
                "ok": False,
                "status": "rejected",
                "reason": f"constraint_rejected:{validation_reason}",
                "account_id": account_id,
                "text": post_text,
            }

        approved_materials=None
        if purpose not in ('warmup','initialise'):
            from ctn.rag.corpus import Corpus
            if rag_snapshot_id:
                try:snapshot=self.router.store.load_rag_snapshot(rag_snapshot_id)
                except ValueError:return {'ok':False,'status':'blocked','reason':'invalid_rag_snapshot'}
            else:
                if not (mock or self.actuator.dry_run):
                    return {'ok':False,'status':'blocked','reason':'rag_snapshot_required'}
                snapshot=Corpus().retrieve(target_text or text,purpose=purpose)
                rag_snapshot_id=self.router.store.save_rag_snapshot(snapshot)
            data=snapshot.to_dict()
            if data['purpose']!=purpose:return {'ok':False,'status':'blocked','reason':'rag_purpose_mismatch'}
            approved_materials={'campaign_materials':snapshot.campaign_items()}
            from ctn.rag.corpus import too_close
            if too_close(post_text,[r['text'] for r in data['styles']]):
                return {'ok':False,'status':'rejected','reason':'style_copy'}
            from ctn.phrasing import recent_texts,repetition_reason
            repeated=repetition_reason(post_text,recent_texts(self.router.store))
            if repeated:
                return {'ok':False,'status':'rejected','reason':repeated}
        # 3. Layer-2 JEV Postflight Gatekeeper (shared with micro replies, with strict_alignment=True)
        active_key = branch if branch else purpose
        resolved_purpose = PURPOSE_RULES.get(active_key, active_key)
        persona_profile = format_persona_profile(persona)
        # A first or warm-up post is everyday life: judged against the referendum it reads as off-purpose
        # (live 2026-10-19: every bakery post of one persona was rejected as purpose_drifted).
        default_target = ("Everyday life in Kingstonia (no referendum context)" if purpose in ('warmup', 'initialise')
                          else "Kingstonia referendum public discussion")
        effective_target = target_text.strip() or default_target

        postflight_dict: dict[str, Any] = {}
        if not bypass_jev:
            import asyncio
            verdict = await asyncio.to_thread(self.postflight.check,
                post_id="proactive_publish",
                target_text=effective_target,
                reply_text=post_text,
                assigned_side=self.side,
                intended_purpose=resolved_purpose,
                persona_profile=persona_profile,
                strict_alignment=(purpose not in ('warmup','initialise','P00')),
                approved_materials=approved_materials,
            )
            postflight_dict = verdict.to_dict()
            self.router.store.record_quality(account_id,'proactive_publish',verdict.can_send,verdict.reason,'initialise' if purpose in ('warmup','initialise') else 'original')
            if not verdict.can_send:
                return {
                    "ok": False,
                    "status": "rejected",
                    "reason": f"jev_rejected:{verdict.reason}",
                    "account_id": account_id,
                    "branch": active_key,
                    "postflight": postflight_dict,
                }
        else:
            postflight_dict = {"can_send": True, "bypassed": True}

        # 4. Execution via Actuator
        material_context={'target_text':effective_target,'hashtags':hashtags or [],
            'source':'supplied_macro_input','background_version':self.router.store.policy('background_version')}
        material_version='macro-input.v1:'+hashlib.sha256(json.dumps(material_context,
            sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        res = await self.actuator.post(account_id, post_text,context={"engine":"macro","macro_job_id":macro_job_id,"macro_owner":macro_owner,
            **self.router.context_for(persona),'prompt_version':'pi-campaign.v3',
            'material_version':material_version,'material_context':material_context,
            'topic':branch or purpose,
            "purpose":("initialise" if purpose in ("warmup","initialise") else active_key),"rag_snapshot_id":rag_snapshot_id,"assigned_side":self.side,"cooldown_s":persona.cooldown_s,"draft_expires_at":now+300})
        if res.ok:
            normalized = re.sub(r"\s+", " ", post_text.lower())
            self._recent_normalized_posts.add(normalized)
            post_id = res.response.get("post_id") if isinstance(res.response, dict) else ""
            final_id = post_id or res.decision_id
            return {
                "ok": True,
                "status": "published",
                "post_id": final_id,
                "account_id": account_id,
                "branch": active_key,
                "text": post_text,
                "postflight": postflight_dict,
            }

        return {
            "ok": False,
            "status": res.status,
            "action_id": res.action_id,
            "reason": f"actuator_failed:{res.detail}",
            "account_id": account_id,
        }

    def publish_sync(
        self,
        account_id: str,
        text: str,
        *,
        hashtags: list[str] | None = None,
        purpose: str = "P01",
        branch: str = "",
        target_text: str = "",
        bypass_jev: bool = False,
        rag_snapshot_id: str = "",
        macro_job_id: str = "",
        macro_owner: str = "",
    ) -> dict[str, Any]:
        """Synchronous wrapper for publish."""
        return asyncio.run(
            self.publish(
                account_id,
                text,
                hashtags=hashtags,
                purpose=purpose,
                branch=branch,
                target_text=target_text,
                bypass_jev=bypass_jev,
                rag_snapshot_id=rag_snapshot_id,
                macro_job_id=macro_job_id,macro_owner=macro_owner,
            )
        )
