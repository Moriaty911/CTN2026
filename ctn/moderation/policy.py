"""Evidence-bound candidate payloads and the narrow v1 automatic whitelist."""
import json
from pathlib import Path
POLICY=json.loads((Path(__file__).resolve().parents[2]/"policies/report-v1.json").read_text())
from ctn.rag.corpus import digest

PREFIXES={'post':'I think that this content is inauthentic (bot) activity because ',
          'account':'I think that this account is conducting inauthentic (bot) activity because '}


def candidate_payload(store, kind, target, author):
    prof=store.get_bot_profile(author)
    if (kind not in PREFIXES or prof['status'] in ('own_bot','verified_npc') or prof.get('verified')
        or not store.report_evidence_ready(prof)):
        return None
    evidence=[]
    for row in store.evidence_for(author):
        if row['family'] in ('weak','moderator'):continue
        detail=json.loads(row['detail_json'])
        post=store.get_observed_post(detail.get('post_id',''))
        if not post or post['version']!=detail.get('version'):continue
        related=[]
        for pid in detail.get('related_posts',[]):
            other=store.get_observed_post(pid)
            if other: related.append({k:other[k] for k in ('post_id','version','text','created_at','parent_id','root_post_id','author')})
        evidence.append({'family':row['family'],'key':row['evidence_key'],
            'post':{k:post[k] for k in ('post_id','version','text','created_at','parent_id','root_post_id','author')},
            'related':related,'detail':detail})
    if not evidence:return None
    if kind=='post':
        post=store.get_observed_post(target)
        if not post or post['author']!=author:return None
        target_version=post['version']
    else:target_version=None
    eligibility={'activity':store.confirmed_activity(author),'upheld_posts':prof['upheld_strikes']} if kind=='account' else None
    evidence_hash=digest({'evidence':evidence,'target_version':target_version,'eligibility':eligibility,'policy':POLICY,'scoring_policy':store.policy('evidence_policy',{})})
    detail=PREFIXES[kind]+json.dumps(evidence,sort_keys=True,ensure_ascii=True,separators=(',',':'))
    return dict(subject_kind=kind,subject_id=str(target),author=author,reason='inauthentic',detail=detail,
        evidence=evidence,evidence_hash=evidence_hash,target_version=target_version,policy_version=POLICY['version'],
        automatic_policy=POLICY,scoring_policy=store.policy('evidence_policy',{}),suspicion_score=prof['suspicion_score'])


def automatic_evidence(store, payload):
    """Leak on target plus disjoint authored behavior in >=2 other threads.

    Wording comparisons against another flagged account never establish identity.
    Repetition of the same leak does not create an independent second group.
    """
    from .detector import BotDetector, _AI_LEAK_RE, _TEMPLATE_SLOT_RE, word_shingles, shingle_jaccard
    leaks=[e for e in payload['evidence'] if e['family']=='leak' and
        (payload['subject_kind']=='account' or e['post']['post_id']==payload['subject_id'])]
    for leak in leaks:
        visible=BotDetector._unquoted(leak['post']['text'])
        if not (_AI_LEAK_RE.search(visible) or _TEMPLATE_SLOT_RE.search(visible)):continue
        for behavior in payload['evidence']:
            if behavior['family'] not in POLICY['automatic_behavior_groups']:continue
            rows=[behavior['post']]+behavior['related']
            rows={r['post_id']:r for r in rows if r['author']==payload['author'] and
                r['post_id']!=leak['post']['post_id'] and r['created_at']>0}
            rows=[r for r in rows.values() if not (_AI_LEAK_RE.search(BotDetector._unquoted(r['text'])) or
                _TEMPLATE_SLOT_RE.search(BotDetector._unquoted(r['text'])))]
            threads={r['root_post_id'] or r['parent_id'] or r['post_id'] for r in rows}
            if len(rows)<POLICY['minimum_other_authored_posts'] or len(threads)<POLICY['minimum_other_threads']:continue
            if behavior['family']=='repetition':
                # Long original content, excluding quotations; short slogans are insufficient.
                texts=[BotDetector._unquoted(r['text']) for r in rows]
                texts=[t for t in texts if len(''.join(t.split()))>=POLICY['minimum_repeated_content_characters']]
                if not any(shingle_jaccard(word_shingles(a),word_shingles(b))>=POLICY['repetition_similarity_min']
                    for i,a in enumerate(texts) for b in texts[i+1:]):continue
            else:
                peers=behavior['detail'].get('peers') or {}
                shared={peer:set() for peer in peers}
                for row in rows:
                    root=row['root_post_id'] or row['parent_id'] or row['post_id']
                    for sibling in store.get_post_replies(root,limit=100):
                        peer=sibling['author']
                        if peer in shared and sibling['created_at']>0 and abs(row['created_at']-sibling['created_at'])<=POLICY['coordination_window_seconds']:
                            shared[peer].add(root)
                if not any(len(threads)>=POLICY['minimum_other_threads'] for threads in shared.values()):continue
            return True
    return False
