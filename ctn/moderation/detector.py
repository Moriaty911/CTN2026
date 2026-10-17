"""Blue-Team Bot Detector: S/A two-tier evidence scoring and virtual team clustering.

Design principles (CTN 2026 rules):
- Reporting a genuine NPC citizen suspends the entire team from reporting for 6 hours.
  Therefore, we never report on a single weak heuristic.
- Tier-S (Smoking-Gun) signals are impossible for genuine Kingstonia NPCs and immediately
  promote an account to `confirmed_bot` (score >= 90):
    * S1: Explicit AI/prompt-injection leak, unexpanded template slots (`{topic}`), or
          heavy non-English script (Kingstonia NPCs only post in English).
    * S2: Exact `norm_hash` copy-paste collision across accounts (matrix botnet) or
          repeated across posts/threads by the same account.
- Tier-A (Behavioral Accumulation) signals place an account into `suspected_bot` (score >= 50)
  on first occurrence and only promote to `confirmed_bot` (score >= 90) when multiple
  independent signals or sustained patterns accumulate:
    * A1: Near-duplicate template paraphrasing (3-gram word shingle Jaccard >= 0.75).
    * A2: Sustained rapid-fire posting intervals (<= 15s between posts, 2x -> +35, 3x -> +90).
    * A3: Pure question-machine `P08` pattern (100% '?' endings across >= 5 posts -> +90).
    * A4: Multi-thread coordinated booster co-occurrence (same pair of accounts replying
          within 15s of each other across >= 2 different parent threads).
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import Any

from ..jev.postflight import check_incoming_post_language
from ..models import Post
from ..storage import StateStore

# Explicit LLM / prompt-injection / template failure patterns that genuine NPCs never emit.
_AI_LEAK_RE = re.compile(
    r"(?:"
    r"\bas an ai\b"
    r"|\bas a large language model\b"
    r"|\bi am an ai\b"
    r"|\bignore (?:all )?previous instructions\b"
    r"|\bdisregard (?:all )?(?:prior|previous) instructions\b"
    r"|\bsystem prompt\b"
    r"|\byou are a helpful assistant\b"
    r"|\bi cannot fulfill this request\b"
    r")",
    re.IGNORECASE,
)

_TEMPLATE_SLOT_RE = re.compile(
    r"(?:"
    r"\{(?:topic|hashtag|persona|stance|target|author|claim|post_text|insert_[a-z0-9_]+)\}"
    r"|\[(?:insert[\s_][a-z0-9_\s]+|your[\s_](?:hashtag|stance|response))\]"
    r"|<insert[\s_][a-z0-9_\s]+>"
    r")",
    re.IGNORECASE,
)

# Simple stance keyword cues to infer whether a bot belongs to the YES or NO campaign.
_YES_CUES = ("mandate now", "protect kingstonia", "#mandatenow", "#protectkingstonia")
_NO_CUES = ("protect choice", "keep kingstonia open", "#mybodymychoice", "#keepkingstoniaopen")


def infer_post_side(text: str) -> str:
    """Best-effort lexical inference of campaign side ('yes', 'no', or '')."""
    low = (text or "").lower()
    yes_hits = sum(1 for c in _YES_CUES if c in low)
    no_hits = sum(1 for c in _NO_CUES if c in low)
    if yes_hits > no_hits:
        return "yes"
    if no_hits > yes_hits:
        return "no"
    return ""


def word_shingles(text: str, k: int = 3) -> set[tuple[str, ...]]:
    """Extract normalized k-word shingles (stripping URLs, mentions, hashtags, and punctuation)."""
    if not text:
        return set()
    stripped = re.sub(r"https?://\S+|#\S+|@\S+", " ", text.lower())
    words = re.findall(r"[a-z0-9]+", stripped)
    if len(words) < max(k, 6):
        return set()
    return {tuple(words[i : i + k]) for i in range(len(words) - k + 1)}


def shingle_jaccard(a: set[tuple[str, ...]], b: set[tuple[str, ...]]) -> float:
    """Compute Jaccard similarity between two shingle sets."""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / float(len(a | b))


@dataclass
class DetectionResult:
    post_id: str
    author: str
    status: str
    suspicion_score: float
    inferred_team: str = ""
    inferred_side: str = "unknown"
    triggered_tags: list[str] = field(default_factory=list)
    colliding_authors: list[str] = field(default_factory=list)

    @property
    def is_confirmed_bot(self) -> bool:
        return self.status == "confirmed_bot"

    @property
    def is_suspected_bot(self) -> bool:
        return self.status == "suspected_bot"


class BotDetector:
    """Evaluates observed posts/comments and updates `bot_registry` with S/A tier evidence."""

    def __init__(
        self,
        store: StateStore,
        *,
        promote_threshold: float = 90.0,
        suspect_threshold: float = 50.0,
        shingle_jaccard_threshold: float = 0.60,
        booster_window_s: float = 15.0,
    ) -> None:
        self.store = store
        self.promote_threshold = promote_threshold
        self.suspect_threshold = suspect_threshold
        self.shingle_jaccard_threshold = shingle_jaccard_threshold
        self.booster_window_s = booster_window_s

    def inspect_post(
        self,
        post: Post | dict[str, Any],
        *,
        record_observation: bool = False,
        now: float | None = None,
    ) -> DetectionResult:
        """Evaluate a post/comment against S-tier and A-tier bot signatures and update `bot_registry`.

        If `record_observation=True`, also upserts the post into `observed_posts` and increments the
        base behavioral counters (`post_seen_count`, `question_post_count`, `fast_interval_count`).
        When called after `FrontlineCrawler` / `CommentCrawler` `_ingest()`, leave `record_observation=False`
        so base counters are not double-counted.
        """
        ts = time.time() if now is None else float(now)
        if isinstance(post, dict):
            post_id = str(post.get("post_id", "")).strip()
            author = str(post.get("author", "")).strip()
            text = str(post.get("text", ""))
            created_at = float(post.get("created_at", 0.0) or 0.0)
        else:
            post_id = str(post.post_id).strip()
            author = str(post.author).strip()
            text = str(post.text)
            created_at = float(post.created_at or 0.0)

        if not post_id or not author:
            return DetectionResult(post_id=post_id, author=author, status="unknown", suspicion_score=0.0)

        # 0. Protected whitelist short-circuit (never flag verified NPCs or our own bots)
        prof = self.store.get_bot_profile(author)
        if prof["status"] in ("verified_npc", "own_bot"):
            return DetectionResult(
                post_id=post_id,
                author=author,
                status=prof["status"],
                suspicion_score=0.0,
                inferred_team=str(prof.get("inferred_team") or ""),
                inferred_side=str(prof.get("inferred_side") or "unknown"),
            )

        obs_ts = created_at if created_at > 0 else ts
        if record_observation:
            _, stored_post = self.store.upsert_observed_post(post, now=ts)
            prof = self.store.record_author_observation(
                author,
                platform_account_id=int(stored_post.get("author_id") or 0),
                is_question=text.rstrip().endswith("?"),
                inferred_side=infer_post_side(text),
                promote_threshold=self.promote_threshold,
                suspect_threshold=self.suspect_threshold,
                now=obs_ts,
            )
        else:
            stored_post = self.store.get_observed_post(post_id)
            if stored_post is None:
                _, stored_post = self.store.upsert_observed_post(post, now=ts)

        norm_hash = str(stored_post.get("norm_hash") or "")
        parent_id = str(stored_post.get("parent_id") or "")
        side_hint = infer_post_side(text)

        triggered_tags: list[str] = []
        colliding_authors: list[str] = []
        total_delta = 0.0
        force_status = ""
        inferred_team = str(prof.get("inferred_team") or "")

        # ---- Tier-S1: AI Leak / Prompt Injection / Template Slot / Non-English Script ---- #
        if _AI_LEAK_RE.search(text):
            triggered_tags.append("s1_ai_or_injection_leak")
            total_delta += 100.0
            force_status = "confirmed_bot"

        if _TEMPLATE_SLOT_RE.search(text):
            triggered_tags.append("s1_unexpanded_template_slot")
            total_delta += 100.0
            force_status = "confirmed_bot"

        lang_ok, lang_reason = check_incoming_post_language(text)
        if not lang_ok:
            triggered_tags.append(f"s1_{lang_reason.split(':', 1)[0]}")
            total_delta += 100.0
            force_status = "confirmed_bot"

        # ---- Tier-S2: Exact norm_hash Copy-Paste Collision (Cross-Account or Self) ------- #
        if norm_hash:
            collisions = self.store.find_norm_hash_collisions(
                norm_hash, exclude_post_id=post_id, limit=10
            )
            cross_peers: list[str] = []
            self_collision = False
            for c in collisions:
                c_author = str(c.get("author") or "").strip()
                if not c_author:
                    continue
                if c_author == author:
                    self_collision = True
                else:
                    peer_prof = self.store.get_bot_profile(c_author)
                    if peer_prof["status"] not in ("verified_npc", "own_bot"):
                        cross_peers.append(c_author)

            if cross_peers:
                cluster_team = f"cluster_hash_{norm_hash[:6]}"
                inferred_team = inferred_team or cluster_team
                triggered_tags.append("s2_cross_account_copy_paste")
                total_delta += 95.0
                force_status = "confirmed_bot"
                for peer in cross_peers:
                    if peer not in colliding_authors:
                        colliding_authors.append(peer)
                    self._apply_bonus(
                        peer,
                        delta=95.0,
                        tag="s2_cross_account_copy_paste",
                        inferred_team=cluster_team,
                        inferred_side=side_hint,
                        force_status="confirmed_bot",
                        now=obs_ts,
                    )
            elif self_collision:
                triggered_tags.append("s2_self_copy_paste")
                total_delta += 95.0
                force_status = "confirmed_bot"

        # ---- Tier-S3 & Tier-A1: Reincarnated Bot Phrasing & Near-Duplicate Template ------ #
        shingles = word_shingles(text, k=3)
        if shingles:
            # S3: Compare against phrasing of already-confirmed or suspended/deleted bots.
            # When a rival team deletes a 3-striked bot and spawns a replacement account using
            # the same prompt/template, this catches the reincarnated bot on its very first post.
            confirmed_corpus = self.store.get_confirmed_bot_posts(
                exclude_author=author, limit=150
            )
            for cp in confirmed_corpus:
                cp_shingles = word_shingles(str(cp.get("text") or ""), k=3)
                if shingle_jaccard(shingles, cp_shingles) >= self.shingle_jaccard_threshold:
                    cp_author = str(cp.get("author") or "").strip()
                    is_dead = bool(cp.get("author_is_suspended"))
                    tag = (
                        "s3_reincarnated_bot_phrasing"
                        if is_dead
                        else "s3_confirmed_bot_phrasing_match"
                    )
                    if tag not in triggered_tags:
                        triggered_tags.append(tag)
                    total_delta += 95.0
                    force_status = "confirmed_bot"
                    cp_team = str(cp.get("author_inferred_team") or "")
                    if not cp_team and cp_author:
                        cp_team = f"cluster_phrasing_{hashlib.sha256(cp_author.encode()).hexdigest()[:6]}"
                        self._apply_bonus(cp_author, delta=0.0, inferred_team=cp_team, now=obs_ts)
                    inferred_team = inferred_team or cp_team
                    cp_side = str(cp.get("author_inferred_side") or "")
                    if not side_hint and cp_side in ("yes", "no"):
                        side_hint = cp_side
                    if cp_author and cp_author not in colliding_authors:
                        colliding_authors.append(cp_author)
                    break

            # A1: Self near-duplicate template check across author's own history
            if "a1_near_duplicate_template" not in prof["evidence"]:
                history = self.store.get_author_posts(author, limit=10)
                for prev in history:
                    if str(prev.get("post_id")) == post_id:
                        continue
                    if norm_hash and str(prev.get("norm_hash") or "") == norm_hash:
                        continue
                    prev_shingles = word_shingles(str(prev.get("text") or ""), k=3)
                    if shingle_jaccard(shingles, prev_shingles) >= self.shingle_jaccard_threshold:
                        triggered_tags.append("a1_near_duplicate_template")
                        total_delta += 55.0
                        break

        # ---- Tier-A2 & Tier-A3: Sustained Rapid-Fire (3x) & Sustained All-Questions (5x) -- #
        prof = self.store.get_bot_profile(author)
        if int(prof["fast_interval_count"]) >= 3 and "a2_sustained_rapid_fire_3x" not in prof["evidence"]:
            triggered_tags.append("a2_sustained_rapid_fire_3x")
            total_delta += 55.0

        seen_cnt = int(prof["post_seen_count"])
        q_cnt = int(prof["question_post_count"])
        if seen_cnt >= 5 and q_cnt == seen_cnt and "a3_sustained_all_questions_5x" not in prof["evidence"]:
            triggered_tags.append("a3_sustained_all_questions_5x")
            total_delta += 45.0

        # ---- Tier-A4: Multi-Thread Coordinated Booster Co-occurrence --------------------- #
        if parent_id:
            co_peers = self._find_repeated_booster_peers(author, parent_id, obs_ts)
            if co_peers:
                pair_key = ":".join(sorted([author, *co_peers]))
                cluster_team = f"cluster_boost_{hashlib.sha256(pair_key.encode()).hexdigest()[:6]}"
                inferred_team = inferred_team or cluster_team
                if "a4_multi_thread_booster" not in prof["evidence"]:
                    triggered_tags.append("a4_multi_thread_booster")
                    total_delta += 60.0
                for peer in co_peers:
                    if peer not in colliding_authors:
                        colliding_authors.append(peer)
                    peer_prof = self.store.get_bot_profile(peer)
                    if "a4_multi_thread_booster" not in peer_prof["evidence"]:
                        self._apply_bonus(
                            peer,
                            delta=60.0,
                            tag="a4_multi_thread_booster",
                            inferred_team=cluster_team,
                            inferred_side=side_hint,
                            now=obs_ts,
                        )

        # Persist any triggered tags / deltas / side / team onto `bot_registry`
        if triggered_tags or total_delta > 0 or force_status or side_hint or inferred_team:
            prof = self._apply_bonus(
                author,
                delta=total_delta,
                tags=triggered_tags,
                inferred_team=inferred_team,
                inferred_side=side_hint,
                force_status=force_status,
                now=obs_ts,
            )

        return DetectionResult(
            post_id=post_id,
            author=author,
            status=str(prof["status"]),
            suspicion_score=float(prof["suspicion_score"]),
            inferred_team=str(prof.get("inferred_team") or ""),
            inferred_side=str(prof.get("inferred_side") or "unknown"),
            triggered_tags=triggered_tags,
            colliding_authors=colliding_authors,
        )

    def scan_similar_to_author(
        self,
        target_author: str,
        *,
        now: float | None = None,
    ) -> list[str]:
        """Scan recent unconfirmed posts in `observed_posts` for phrasing highly similar to `target_author`.

        Called during reporting sweeps so that when a bot is confirmed or suspended/deleted, any
        other account on the platform using highly repetitive/paraphrased phrasing from the same
        template is immediately scored +95 (`confirmed_bot`) and linked to the same team cluster.
        """
        clean_target = str(target_author or "").strip()
        if not clean_target:
            return []
        ts = time.time() if now is None else float(now)
        target_prof = self.store.get_bot_profile(clean_target)
        if target_prof["status"] != "confirmed_bot" and not target_prof.get("is_suspended"):
            return []

        target_posts = self.store.get_author_posts(clean_target, limit=20)
        if not target_posts:
            return []

        target_signatures: list[tuple[str, set[tuple[str, ...]]]] = []
        for tp in target_posts:
            nh = str(tp.get("norm_hash") or "")
            sh = word_shingles(str(tp.get("text") or ""), k=3)
            if nh or sh:
                target_signatures.append((nh, sh))
        if not target_signatures:
            return []

        cluster_team = str(target_prof.get("inferred_team") or "")
        if not cluster_team:
            cluster_team = (
                f"cluster_phrasing_{hashlib.sha256(clean_target.encode()).hexdigest()[:6]}"
            )
            self._apply_bonus(clean_target, delta=0.0, inferred_team=cluster_team, now=ts)

        target_side = str(target_prof.get("inferred_side") or "")
        if target_side not in ("yes", "no"):
            target_side = ""
        tag = (
            "s3_reincarnated_bot_phrasing"
            if target_prof.get("is_suspended")
            else "s3_confirmed_bot_phrasing_match"
        )

        candidates = self.store.get_unconfirmed_candidate_posts(
            exclude_author=clean_target, limit=200
        )
        promoted: list[str] = []
        for cand in candidates:
            cand_author = str(cand.get("author") or "").strip()
            if not cand_author or cand_author in promoted:
                continue
            cand_nh = str(cand.get("norm_hash") or "")
            cand_sh = word_shingles(str(cand.get("text") or ""), k=3)
            matched = False
            for t_nh, t_sh in target_signatures:
                if cand_nh and t_nh and cand_nh == t_nh:
                    matched = True
                    break
                if cand_sh and t_sh and shingle_jaccard(cand_sh, t_sh) >= self.shingle_jaccard_threshold:
                    matched = True
                    break
            if matched:
                self._apply_bonus(
                    cand_author,
                    delta=95.0,
                    tag=tag,
                    inferred_team=cluster_team,
                    inferred_side=target_side,
                    force_status="confirmed_bot",
                    now=ts,
                )
                promoted.append(cand_author)
        return promoted

    def _find_repeated_booster_peers(
        self,
        author: str,
        current_parent_id: str,
        current_ts: float,
    ) -> list[str]:
        """Return peer handles that replied within `booster_window_s` of `author` in both
        `current_parent_id` AND at least one other parent thread.
        """
        siblings = self.store.get_post_replies(current_parent_id, limit=100)
        current_peers: set[str] = set()
        for s in siblings:
            s_author = str(s.get("author") or "").strip()
            if not s_author or s_author == author:
                continue
            s_ts = float(s.get("created_at") or s.get("first_seen_at") or 0.0)
            if s_ts > 0 and abs(current_ts - s_ts) <= self.booster_window_s:
                s_prof = self.store.get_bot_profile(s_author)
                if s_prof["status"] not in ("verified_npc", "own_bot"):
                    current_peers.add(s_author)

        if not current_peers:
            return []

        # Check author's earlier comments in other parent threads
        author_posts = self.store.get_author_posts(author, limit=25)
        repeated_peers: list[str] = []
        for ap in author_posts:
            other_parent = str(ap.get("parent_id") or "")
            if not other_parent or other_parent == current_parent_id:
                continue
            ap_ts = float(ap.get("created_at") or ap.get("first_seen_at") or 0.0)
            other_siblings = self.store.get_post_replies(other_parent, limit=100)
            for osib in other_siblings:
                peer = str(osib.get("author") or "").strip()
                if peer not in current_peers or peer in repeated_peers:
                    continue
                os_ts = float(osib.get("created_at") or osib.get("first_seen_at") or 0.0)
                if ap_ts > 0 and os_ts > 0 and abs(ap_ts - os_ts) <= self.booster_window_s:
                    repeated_peers.append(peer)
        return repeated_peers

    def _apply_bonus(
        self,
        account_id: str,
        *,
        delta: float = 0.0,
        tag: str = "",
        tags: list[str] | None = None,
        inferred_team: str = "",
        inferred_side: str = "",
        force_status: str = "",
        now: float | None = None,
    ) -> dict[str, Any]:
        """Apply suspicion score delta and evidence tags to an existing profile without
        artificially inflating `post_seen_count`.
        """
        all_tags = list(tags or ([] if not tag else [tag]))
        prof = self.store.get_bot_profile(account_id)
        if prof["status"] in ("verified_npc", "own_bot"):
            return prof

        # If the profile has never been created in bot_registry yet, initialize it via record_author_observation.
        if int(prof["post_seen_count"]) == 0:
            first_tag = all_tags[0] if all_tags else ""
            prof = self.store.record_author_observation(
                account_id,
                inferred_side=inferred_side,
                inferred_team=inferred_team,
                suspicion_delta=delta,
                evidence_tag=first_tag,
                force_status=force_status,
                promote_threshold=self.promote_threshold,
                suspect_threshold=self.suspect_threshold,
                now=now,
            )
            all_tags = all_tags[1:]
            delta = 0.0
            if not all_tags and not force_status and not inferred_team and not inferred_side:
                return prof

        return self.store.add_bot_evidence(
            account_id,
            suspicion_delta=delta,
            evidence_tags=all_tags,
            inferred_side=inferred_side,
            inferred_team=inferred_team,
            force_status=force_status,
            promote_threshold=self.promote_threshold,
            suspect_threshold=self.suspect_threshold,
            now=now,
        )
