"""Conservative, versioned bot evidence scoring; moderator decisions remain separate."""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import Any

from ..jev.postflight import check_incoming_post_language
from ..models import Post
from ..storage import StateStore

# Candidate leakage patterns. Context and independent evidence are still required.
_AI_LEAK_RE = re.compile(
    r"^\s*(?:as (?:an ai(?: language model| assistant)?|a large language model)\s*[,;:]"
    r"|i (?:am|['’]m) an ai\b(?!\s+(?:researcher|engineer|developer|artist|teacher|student)\b))",
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
    """Evidence v1. `confirmed_bot` is a legacy local eligibility label, never identity truth."""
    def __init__(self,store,*,promote_threshold=85.,suspect_threshold=50.,
                 shingle_jaccard_threshold=.75,booster_window_s=15.,scan_limit=50):
        self.store=store
        from ..storage.runtime import DEFAULT_EVIDENCE_POLICY
        if store.policy("evidence_policy") is None: store.set_policy("evidence_policy",DEFAULT_EVIDENCE_POLICY)
        self.promote_threshold=promote_threshold
        self.suspect_threshold=suspect_threshold
        self.shingle_jaccard_threshold=shingle_jaccard_threshold
        self.booster_window_s=booster_window_s
        self.scan_limit=scan_limit
        self._shingle_cache={}
        self._corpus_until=0
        self._corpus=[]

    def _shingles_for(self,post_id,text):
        key=(post_id,hashlib.sha256(text.encode()).hexdigest())
        if key not in self._shingle_cache:
            if len(self._shingle_cache)>=2048: self._shingle_cache.pop(next(iter(self._shingle_cache)))
            self._shingle_cache[key]=word_shingles(text)
        return self._shingle_cache[key]

    def _reference_corpus(self):
        if time.monotonic()>=self._corpus_until:
            self._corpus=self.store.get_confirmed_bot_posts(limit=100)
            self._corpus_until=time.monotonic()+60
        return self._corpus

    @staticmethod
    def _unquoted(text):
        text=re.sub(r'```[\s\S]*?(?:```|$)', '', text)
        text='\n'.join(line for line in text.splitlines() if not line.lstrip().startswith('>'))
        text=re.sub(r'"[^"\n]*"|“[^”\n]*”|`[^`]*`', '', text)
        # Explicit examples without quote marks: exempt just that local clause.
        text=re.sub(r'(?im)^\s*(?:example|quoted text|prompt injection example)\s*:[^\n]*', '', text)
        return text

    def inspect_post(self,post,*,record_observation=False,now=None):
        from ..pipeline.perceive import post_from_dict
        if isinstance(post,dict): post=post_from_dict(post)
        if post is None: return DetectionResult('','','unknown',0)
        ts=time.time() if now is None else float(now)
        author,pid,text=post.author,post.post_id,post.text
        prof=self.store.get_bot_profile(author)
        if not author or prof['status'] in ('own_bot','verified_npc') or prof.get('verified'):
            return DetectionResult(pid,author,prof['status'],prof['suspicion_score'])
        stored=self.store.get_observed_post(pid)
        if stored is None or stored['text']!=text:
            is_new,stored=self.store.upsert_observed_post(post,now=ts)
            if record_observation and is_new:
                self.store.record_author_observation(author,is_question=text.rstrip().endswith('?'),
                    has_valid_timestamp=post.created_at>0,now=post.created_at or ts)
        tags=[];peers=[]
        def add(family,points,key,**detail):
            tags.append(key)
            self.store.add_weighted_evidence(author,family,points,
                {'post_id':pid,'version':stored.get('version',1),'published_at':post.created_at,**detail},
                evidence_key=key,now=ts)
        visible=self._unquoted(text)
        if (_AI_LEAK_RE.search(visible) or _TEMPLATE_SLOT_RE.search(visible)):
            add('leak',50,'leak:'+pid+':'+str(stored.get('version',1)),excerpt=visible[:500])
        long_content=len(re.sub(r'[^a-z0-9]','',text.lower()))>=80
        nh=stored.get('norm_hash')
        collisions=self.store.find_norm_hash_collisions(nh,exclude_post_id=pid,limit=20) if nh and long_content else []
        if collisions:
            peers=sorted({str(c['author']) for c in collisions if c['author']!=author})
            add('repetition',35,'repetition:'+str(nh),related_posts=[c['post_id'] for c in collisions],peers=peers)
        elif long_content:
            sh=self._shingles_for(pid,text)
            for prior in self.store.get_author_posts(author,limit=20):
                if prior['post_id']!=pid and shingle_jaccard(sh,self._shingles_for(prior['post_id'],prior['text']))>=self.shingle_jaccard_threshold:
                    key='similar:'+':'.join(sorted([pid,prior['post_id']]))
                    add('repetition',35,key,related_posts=[prior['post_id']]);break
            # A flagged account is only a source of wording comparisons, never proof
            # of another account's identity. Check cached versions before using them.
            for prior in self._reference_corpus()[:20]:
                if prior['author']==author: continue
                current=self.store.get_observed_post(prior['post_id'])
                if not current or current['version']!=prior['version']: continue
                if shingle_jaccard(sh,self._shingles_for(prior['post_id'],prior['text']))>=self.shingle_jaccard_threshold:
                    add('repetition',35,'similar:'+':'.join(sorted([pid,prior['post_id']])),related_posts=[prior['post_id']])
                    break
        parent=stored.get('root_post_id') or stored.get('parent_id','')
        if parent and post.created_at>0:
            coordination=self._find_booster_peer_thread_counts(author,parent,post.created_at)
            if coordination:
                add('coordination',35,'coordination:'+author,peers=coordination,current_thread=parent,
                    related_posts=[r['post_id'] for r in self.store.get_author_posts(author,limit=25) if r.get('parent_id')])
        prof=self.store.get_bot_profile(author)
        if prof['fast_interval_count']>=3:
            add('timing',20,'sustained_timing:'+author,fast_intervals=prof['fast_interval_count'])
        if prof['post_seen_count']>=5 and prof['question_post_count']==prof['post_seen_count']:
            add('weak',5,'questions:'+author)
        if not check_incoming_post_language(text)[0]: add('weak',5,'language:'+pid)
        prof=self.store.get_bot_profile(author)
        return DetectionResult(pid,author,prof['status'],prof['suspicion_score'],
            str(prof.get('inferred_team','')),infer_post_side(text) or 'unknown',tags,peers)

    def scan_similar_to_author(self,target_author,*,now=None):
        # Bounded association search. A seed's suspicion/identity is never propagated.
        seeds=self.store.get_author_posts(target_author,limit=10)
        changed=[]
        for candidate in self.store.get_unconfirmed_candidate_posts(exclude_author=target_author,limit=self.scan_limit):
            if len(candidate['text'])<100: continue
            sh=self._shingles_for(candidate['post_id'],candidate['text'])
            for seed in seeds:
                if shingle_jaccard(sh,self._shingles_for(seed['post_id'],seed['text']))>=self.shingle_jaccard_threshold:
                    prof=self.store.add_weighted_evidence(candidate['author'],'repetition',35,
                        {'post_id':candidate['post_id'],'related_posts':[seed['post_id']]},
                        evidence_key='similar:'+':'.join(sorted([candidate['post_id'],seed['post_id']])),now=now)
                    if prof['suspicion_score']>=85 and prof.get('evidence_groups',0)>=2: changed.append(candidate['author'])
                    break
        return changed

    def _find_booster_peer_thread_counts(
        self,
        author: str,
        current_parent_id: str,
        current_ts: float,
    ) -> dict[str, int]:
        """Return `{peer_handle: shared_parent_thread_count}` for peers that replied within
        `booster_window_s` of `author` in `current_parent_id` AND at least one other parent thread.
        """
        parent_rec = self.store.get_observed_post(current_parent_id)
        current_parent_author = str(parent_rec.get("author") or "").strip() if parent_rec else ""
        if author == current_parent_author:
            return {}

        siblings = self.store.get_post_replies(current_parent_id, limit=100)
        current_peers: set[str] = set()
        for s in siblings:
            s_author = str(s.get("author") or "").strip()
            if not s_author or s_author == author or s_author == current_parent_author:
                continue
            s_ts = float(s.get("created_at") or s.get("first_seen_at") or 0.0)
            if s_ts > 0 and abs(current_ts - s_ts) <= self.booster_window_s:
                s_prof = self.store.get_bot_profile(s_author)
                if s_prof["status"] not in ("verified_npc", "own_bot"):
                    current_peers.add(s_author)

        if not current_peers:
            return {}

        # Check author's earlier comments in other parent threads
        author_posts = self.store.get_author_posts(author, limit=25)
        peer_other_threads: dict[str, set[str]] = {}
        for ap in author_posts:
            other_parent = str(ap.get("root_post_id") or ap.get("parent_id") or "")
            if not other_parent or other_parent == current_parent_id:
                continue
            other_parent_rec = self.store.get_observed_post(other_parent)
            other_parent_author = (
                str(other_parent_rec.get("author") or "").strip() if other_parent_rec else ""
            )
            if author == other_parent_author:
                continue
            ap_ts = float(ap.get("created_at") or ap.get("first_seen_at") or 0.0)
            other_siblings = self.store.get_post_replies(other_parent, limit=100)
            for osib in other_siblings:
                peer = str(osib.get("author") or "").strip()
                if peer not in current_peers or peer == other_parent_author:
                    continue
                os_ts = float(osib.get("created_at") or osib.get("first_seen_at") or 0.0)
                if ap_ts > 0 and os_ts > 0 and abs(ap_ts - os_ts) <= self.booster_window_s:
                    peer_other_threads.setdefault(peer, set()).add(other_parent)

        return {
            peer: 1 + len(threads)
            for peer, threads in peer_other_threads.items()
            if threads
        }

    def _find_repeated_booster_peers(
        self,
        author: str,
        current_parent_id: str,
        current_ts: float,
    ) -> list[str]:
        """Return peer handles that replied within `booster_window_s` of `author` in both
        `current_parent_id` AND at least one other parent thread.
        """
        return list(
            self._find_booster_peer_thread_counts(author, current_parent_id, current_ts).keys()
        )
