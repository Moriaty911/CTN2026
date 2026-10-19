"""Expected-value report decisions under the 2026 moderator model.

The moderator upholds a report on a competitor bot with probability 0.75 and on
a citizen with probability 0.25, so P(upheld) = 0.25 + 0.5p where p is the
probability that the author is a bot. A genuine ruling stops all team reporting
for six hours. A report is worth sending when

    P(upheld) * R  >  P(genuine) * C

R is the expected points if upheld (first-reporter odds, daily diminishing
returns); C is the expected value lost while reporting is suspended. p is never
allowed below a hard floor, whatever the arithmetic says: reporting accounts we
believe are citizens is bad-faith reporting (LS.6.5, rule 5.3).

p is an estimate, not the detector's evidence score. Each evidence bucket has a
prior that is recalibrated from rulings (upheld rate u gives p = (u-0.25)/0.5),
then updated per author with Bayes from that author's own rulings.
"""
from __future__ import annotations

import copy
import json
import re
import time
from pathlib import Path

DEFAULT_POLICY = json.loads((Path(__file__).resolve().parents[2] / 'policies/report-v2.json').read_text())
HARD_P_FLOOR = 0.7
OVERRIDE_KEYS = ('shadow', 'auto_buckets', 'p_floor', 'max_expected_genuine')
_SUSPENSION_WORDS = re.compile(r'suspend|ban|disabled|not allowed', re.IGNORECASE)


def policy(store):
    """File defaults plus operator overrides stored in SQLite. The floor can only rise."""
    merged = copy.deepcopy(DEFAULT_POLICY)
    overrides = store.policy('report_decision', {}) or {}
    for key in OVERRIDE_KEYS:
        if key in overrides:
            merged[key] = overrides[key]
    merged['p_floor'] = max(HARD_P_FLOOR, float(merged['p_floor']))
    merged['auto_buckets'] = [b for b in merged['auto_buckets'] if b in merged['buckets']]
    return merged


def set_overrides(store, *, shadow=None, auto_buckets=None, p_floor=None, max_expected_genuine=None, actor='cli'):
    current = dict(store.policy('report_decision', {}) or {})
    if shadow is not None:
        current['shadow'] = bool(shadow)
    if auto_buckets is not None:
        unknown = set(auto_buckets) - set(DEFAULT_POLICY['buckets'])
        if unknown:
            raise ValueError('unknown report bucket: ' + ','.join(sorted(unknown)))
        current['auto_buckets'] = list(auto_buckets)
    if p_floor is not None:
        if not HARD_P_FLOOR <= float(p_floor) <= 1:
            raise ValueError(f'p_floor must be between {HARD_P_FLOOR} and 1')
        current['p_floor'] = float(p_floor)
    if max_expected_genuine is not None:
        if not 0 < float(max_expected_genuine) <= 10:
            raise ValueError('max_expected_genuine must be in (0, 10]')
        current['max_expected_genuine'] = float(max_expected_genuine)
    with store.batch_transaction():
        store.set_policy('report_decision', current)
        store.audit_operation(actor, 'report_decision_policy', 'campaign', current)
    return policy(store)


def upheld_probability(p, pol):
    lo, hi = pol['moderator']['upheld_if_citizen'], pol['moderator']['upheld_if_bot']
    return lo + (hi - lo) * p


def bucket_for(store, payload):
    if payload['subject_kind'] == 'account':
        return 'account'
    families = {e['family'] for e in payload['evidence']}
    if 'leak' in families:
        from .policy import automatic_evidence
        # Leak anywhere on the author plus independent behavior in two other threads.
        if automatic_evidence(store, {**payload, 'subject_kind': 'account'}):
            return 'leak_behavior'
        return 'leak_only'
    return 'behavior_only'


def target_has_evidence(payload):
    return any(e['post']['post_id'] == payload['subject_id'] and e['family'] in ('leak', 'repetition')
               for e in payload['evidence'])


def _calibration(store, bucket):
    with store._lock:
        row = store._conn.execute('SELECT upheld,rejected FROM report_calibration WHERE bucket=?', (bucket,)).fetchone()
    return (int(row[0]), int(row[1])) if row else (0, 0)


def _author_rulings(store, author):
    with store._lock:
        rows = dict(store._conn.execute("SELECT outcome,COUNT(*) FROM report_attempts WHERE author=? AND subject_kind='post' "
                                        "AND status='ruled' GROUP BY outcome", (author,)).fetchall())
        legacy = dict(store._conn.execute("SELECT outcome,COUNT(*) FROM report_log WHERE target_author=? GROUP BY outcome",
                                          (author,)).fetchall())
    upheld = max(int(rows.get('upheld', 0)), int(legacy.get('upheld', 0)))
    rejected = max(int(rows.get('rejected', 0)), int(legacy.get('rejected', 0)))
    return upheld, rejected


def estimate_p(store, bucket, author, pol):
    lo, hi = pol['moderator']['upheld_if_citizen'], pol['moderator']['upheld_if_bot']
    prior = pol['buckets'][bucket]
    strength = float(prior['prior_strength'])
    u0 = lo + (hi - lo) * float(prior['prior_mean'])
    upheld, rejected = _calibration(store, bucket)
    u = (u0 * strength + upheld) / (strength + upheld + rejected)
    p = min(1.0, max(0.0, (u - lo) / (hi - lo)))
    k, r = _author_rulings(store, author) if author else (0, 0)
    if k or r:
        bot = p * hi ** k * (1 - hi) ** r
        citizen = (1 - p) * lo ** k * (1 - lo) ** r
        p = bot / (bot + citizen) if bot + citizen > 0 else p
    return round(p, 4)


def _looks_hidden(post):
    raw = post.get('raw') or {}
    for key in ('hidden', 'is_hidden', 'moderation_hidden', 'removed', 'deleted'):
        if raw.get(key) is True:
            return True
    return str(raw.get('visibility', '')).lower() in ('hidden', 'removed')


def _step(table, value):
    for limit, result in table:
        if value <= limit:
            return result
    return table[-1][1]


def estimate_reward(store, payload, pol, now):
    """Expected points if the report is upheld."""
    author = payload['author']
    if payload['subject_kind'] == 'account':
        activity = store.confirmed_activity(author) or {}
        eligible = activity.get('action_count', 0) >= 100 and store.get_bot_profile(author)['upheld_strikes'] >= 1
        return (float(pol['points']['account']), 'account_eligible') if eligible else (0.0, 'account_not_eligible')
    post = store.get_observed_post(payload['subject_id'])
    if not post:
        return 0.0, 'target_missing'
    if _looks_hidden(post):
        return 0.0, 'target_hidden'
    created = float(post.get('created_at') or 0) or float(post.get('first_seen_at') or now)
    age = max(0.0, now - created)
    if age > pol['max_target_age_seconds']:
        return 0.0, 'target_too_old'
    first = _step(pol['first_report_decay'], age)
    team = store.get_bot_profile(author).get('true_team_id', '')
    count = (store.count_daily_reports_against(target_team=team, now=now) if team else
             store.count_daily_reports_against(target_author=author, now=now))
    multiplier = _step(pol['daily_team_multipliers'], count + 1)
    reward = multiplier * (pol['points']['first'] * first + pol['points']['later'] * (1 - first))
    return round(reward, 3), 'reward_estimated'


def estimate_cost(store, pol, now):
    """Expected value lost during one suspension: opportunities we could actually have sent."""
    cfg = pol['genuine_cost']
    window = float(pol['suspension_seconds'])
    lookback = float(cfg['lookback_seconds'])
    with store._lock:
        # One opportunity per author: an author can only hold one unresolved report at a time.
        rows = store._conn.execute('SELECT MAX(p_upheld*reward) FROM report_decisions WHERE updated_at>=? AND reward>0 '
                                   "AND subject_kind='post' AND p>=? GROUP BY author", (now - lookback, pol['p_floor'])).fetchall()
        delays = store._conn.execute("SELECT ruled_at-submitted_at FROM report_attempts WHERE status='ruled' "
                                     'AND ruled_at>submitted_at AND ruled_at>=? ORDER BY ruled_at DESC LIMIT 20',
                                     (now - 3 * 86400,)).fetchall()
    values = [float(r[0]) for r in rows]
    if not values:
        return float(cfg['floor'])
    with store._lock:
        pu_bar = store._conn.execute("SELECT AVG(p_upheld) FROM report_decisions WHERE updated_at>=? AND reward>0 AND subject_kind='post' AND p>=?",
                                     (now - lookback, pol['p_floor'])).fetchone()[0] or upheld_probability(0.9, pol)
    delay = (sum(d[0] for d in delays) / len(delays)) if len(delays) >= 3 else float(cfg['default_ruling_delay_seconds'])
    capacity = int(store.policy('max_pending_reports', 40)) * window / max(delay, 60.0)
    expected_candidates = len(values) * window / lookback
    average = sum(values) / len(values)
    cost = min(expected_candidates, capacity) * average
    # Renewal bound: the opportunities lost in a stop carry the same genuine risk themselves, so under an
    # abundance of targets a stop costs about (P_upheld / P_genuine) average reports, not every candidate seen.
    # Without this cap, fast rulings during a bot flood would make the rule refuse every report.
    pu_bar = min(max(float(pu_bar), 0.05), 0.95)
    cost = min(cost, average * pu_bar / (1 - pu_bar))
    return round(max(float(cfg['floor']), cost), 3)


def expected_genuine(store, pol=None, *, extra_p=None, now=None):
    """Expected number of distinct six-hour reporting stops among unresolved post reports.

    A genuine ruling stops reporting for six hours from the ruling, and stops that land close together
    overlap. Reports are grouped by their expected ruling time (submission + ruling delay) into
    suspension-length windows; each window contributes P(at least one genuine ruling in it).
    """
    pol = pol or policy(store)
    ts = time.time() if now is None else now
    window = float(pol['suspension_seconds'])
    delay = float(pol['genuine_cost']['default_ruling_delay_seconds'])
    with store._lock:
        rows = store._conn.execute("SELECT payload_json,submitted_at FROM report_attempts WHERE subject_kind='post' "
                                   "AND status IN ('claimed','sending','pending','unknown')").fetchall()
    survive = {}
    entries = []
    for row in rows:
        try:
            p = float(json.loads(row[0]).get('decision_p', pol['p_floor']))
        except (TypeError, ValueError):
            p = pol['p_floor']
        entries.append((p, float(row[1] or 0) or ts))
    if extra_p is not None:
        entries.append((float(extra_p), ts))
    for p, submitted in entries:
        bucket = int((submitted + delay) // window)
        survive[bucket] = survive.get(bucket, 1.0) * upheld_probability(p, pol)
    return round(sum(1 - s for s in survive.values()), 4)


def automatic_ready(store, *, now=None):
    ts = time.time() if now is None else now
    pol = policy(store)
    if store.policy('report_mode', 'off') != 'hybrid':
        return False, 'automation_requires_hybrid'
    if pol['shadow']:
        return False, 'automation_shadow'
    forbidden = store.policy('report_forbidden', {}) or {}
    if forbidden.get('at', 0) > ts - pol['forbidden_cooldown_seconds']:
        return False, 'reporting_forbidden_recently'
    return True, 'ready'


def record_report_forbidden(store, message, *, now=None):
    """A report 403 pauses automation; suspension wording also starts a local six-hour stop."""
    ts = time.time() if now is None else now
    pol = policy(store)
    with store.batch_transaction():
        store.set_policy('report_forbidden', {'at': ts, 'message': str(message)[:300]})
        store.set_policy('report_clearance', {})
        if _SUSPENSION_WORDS.search(str(message)):
            until = int((ts + pol['suspension_seconds']) * 1000)
            store.set_counter('report_suspended_until_ms', max(store.get_counter('report_suspended_until_ms'), until))


def decide(store, payload, candidate_id, *, now=None, record=True):
    ts = time.time() if now is None else now
    pol = policy(store)
    bucket = bucket_for(store, payload)
    p = estimate_p(store, bucket, payload['author'], pol)
    reward, reward_reason = estimate_reward(store, payload, pol, ts)
    cost = estimate_cost(store, pol, ts)
    pu = upheld_probability(p, pol)
    pg = 1 - pu
    ev = round(pu * reward - pg * cost, 3)
    lo, hi = pol['moderator']['upheld_if_citizen'], pol['moderator']['upheld_if_bot']
    # Break-even p: (lo + (hi-lo)p) R = (1 - lo - (hi-lo)p) C
    threshold = round(((1 - lo) * cost - lo * reward) / ((hi - lo) * (reward + cost)), 4) if reward + cost > 0 else 1.0
    automatic = bucket in pol['auto_buckets'] and not pol['shadow']
    if payload['subject_kind'] == 'account':
        # Automated account moderation dismisses ineligible reports without a reporting stop.
        action, reason = (('auto' if automatic else 'human'), 'account_eligible') if reward > 0 else ('skip', reward_reason)
    elif reward <= 0:
        action, reason = 'skip', reward_reason
    elif p < pol['p_floor']:
        action, reason = ('human', 'below_floor_needs_human') if p >= pol['human_review_min_p'] else ('skip', 'below_floor')
    elif ev <= 0:
        action, reason = 'skip', 'negative_expected_value'
    elif automatic:
        action, reason = 'auto', 'positive_expected_value'
    else:
        action, reason = 'human', 'shadow_mode' if pol['shadow'] else 'bucket_requires_human'
    result = {'candidate_id': candidate_id, 'subject_kind': payload['subject_kind'], 'subject_id': str(payload['subject_id']),
              'author': payload['author'], 'bucket': bucket, 'p': p, 'p_upheld': round(pu, 4), 'reward': reward,
              'cost': cost, 'expected_value': ev, 'threshold': threshold, 'p_floor': pol['p_floor'],
              'target_evidence': target_has_evidence(payload) if payload['subject_kind'] == 'post' else None,
              'action': action, 'reason': reason, 'shadow': bool(pol['shadow']), 'policy_version': pol['version']}
    if record:
        with store.batch_transaction():
            store._conn.execute(
                'INSERT INTO report_decisions(candidate_id,subject_kind,subject_id,author,bucket,p,p_upheld,reward,cost,'
                'expected_value,action,reason,shadow,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) '
                'ON CONFLICT(candidate_id) DO UPDATE SET p=excluded.p,p_upheld=excluded.p_upheld,reward=excluded.reward,'
                'cost=excluded.cost,expected_value=excluded.expected_value,action=excluded.action,reason=excluded.reason,'
                'shadow=excluded.shadow,updated_at=excluded.updated_at',
                (candidate_id, result['subject_kind'], result['subject_id'], result['author'], bucket, p, result['p_upheld'],
                 reward, cost, ev, action, reason, int(result['shadow']), ts, ts))
    return result


def automatic_authorized(store, kind, target, author, meta, *, now=None):
    """Recompute at claim and at the wire: the evidence, reward and policy must still say auto."""
    from .policy import candidate_payload
    subject = 'account' if kind == 'report_account' else 'post'
    payload = candidate_payload(store, subject, target, author)
    if not payload or payload['evidence_hash'] != meta.get('evidence_hash') or payload['detail'] != meta.get('detail'):
        return False
    if not automatic_ready(store, now=now)[0]:
        return False
    return decide(store, payload, meta.get('candidate_id', ''), now=now, record=False)['action'] == 'auto'


def record_ruling(store, attempt_payload, outcome):
    """Feed post rulings back into bucket calibration."""
    bucket = (attempt_payload or {}).get('decision_bucket')
    if not bucket or outcome not in ('upheld', 'rejected'):
        return
    column = 'upheld' if outcome == 'upheld' else 'rejected'
    store._conn.execute(f'INSERT INTO report_calibration(bucket,{column}) VALUES(?,1) '
                        f'ON CONFLICT(bucket) DO UPDATE SET {column}={column}+1', (bucket,))


def calibration_summary(store):
    pol = policy(store)
    out = {}
    for bucket in pol['buckets']:
        upheld, rejected = _calibration(store, bucket)
        with store._lock:
            agree = store._conn.execute('SELECT COUNT(*),SUM(action=?) FROM report_decisions WHERE bucket=?',
                                        ('auto', bucket)).fetchone()
        out[bucket] = {'upheld': upheld, 'rejected': rejected, 'p_estimate': estimate_p(store, bucket, '', pol),
                       'decisions': int(agree[0] or 0), 'would_auto': int(agree[1] or 0),
                       'automatic': bucket in pol['auto_buckets'] and not pol['shadow']}
    return {'shadow': pol['shadow'], 'auto_buckets': pol['auto_buckets'], 'p_floor': pol['p_floor'],
            'max_expected_genuine': pol['max_expected_genuine'], 'buckets': out}
