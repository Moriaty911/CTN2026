"""Offline review reproductions. Uses temporary databases and synthetic transports only.

Run from the repository root with the pinned SDK/dependencies available:
    PYTHONPATH=pypi-capturethenarrative/src <python> docs/audit-2026-10-18/verify_findings.py
No runtime code, real account, credential, or project runtime database is changed.
"""
from __future__ import annotations

import asyncio
from contextlib import closing
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from capturethenarrative import LegitSocial
from ctn.client.legit_v1 import OfficialLegitClient
from ctn.config import JevConfig
from ctn.jev import JevClient, JevEvaluator, JevPacker, JevWorkflow, load_rubric
from ctn.jev.postflight import PostflightVerdict
from ctn.macro.publisher import StrategicPublisher
from ctn.models import Post, Side
from ctn.persona import PersonaRouter
from ctn.pipeline.brain import JevLlmBrain
from ctn.rag.corpus import Corpus
from ctn.storage import StateStore
from tests.test_jev_actions import set_score
from tests.test_jev_evaluator import FakeTransport, choose, content
from tests.test_persona80 import bound_router
from tests.test_runtime_reliability import actor


def sync_snapshot_loss():
    class OfflineWire:
        async def post(self, account, text, **kwargs):
            return {'post_id': 'synthetic-publication', 'created_at': time.time()}

    class Approved:
        def check(self, **kwargs):
            return PostflightVerdict(True, 'synthetic-approved')

    with closing(StateStore()) as store:
        router = bound_router(store, ('yes_001',))
        snap = Corpus().retrieve('Public spaces and hospital capacity', purpose='P01')
        ident = store.save_rag_snapshot(snap)
        publisher = StrategicPublisher(actor(OfflineWire(), store), Approved(), router)
        args = dict(account_id='fresh_yes_001', text='I support vaccination proof for shared public spaces, while asking for clear implementation details.', rag_snapshot_id=ident)
        sync = publisher.publish_sync(**args)
        direct = asyncio.run(publisher.publish(**args))
        assert sync['reason'] == 'rag_snapshot_required', sync
        assert direct['ok'], direct
        return {'sync_status': sync['status'], 'sync_reason': sync['reason'],
                'direct_async_status': direct['status'], 'external_network_calls': 0}


def unknown_author_level():
    adapter = OfficialLegitClient(social=LegitSocial('https://example.invalid', max_retries=0))
    normalized = adapter.normalize_post({'post_id': 101, 'author': {'account_id': 202,
        'username': 'synthetic_citizen', 'verified': False},
        'body': 'I oppose requiring proof to use the ferry. What would justify it?'})

    def answer(body, data):
        choose(data, 'policy_stance', 'no')
        if 'reply_value' in data['answers']:
            set_score(data, 'reply_value', 2)

    client = JevClient(JevConfig(api_key='synthetic-audit', max_retries=0), transport=FakeTransport(answer))
    record = content()
    record['task_context'] = {'assigned_side': 'yes'}
    record['author'] = {'platform_level': normalized['platform_level']}
    package = JevWorkflow(client).evaluate(record).to_dict()
    decision = package['action_decision']
    assert decision['status'] == 'needs_account_metadata', decision
    return {'normalized_level': normalized['platform_level'], 'decision': decision,
            'note': 'Conditional on server omitting an explicit level; not a live schema observation.'}


def parent_arrival_does_not_resume():
    with closing(StateStore()) as store:
        now = time.time()
        store.upsert_observed_post(Post('child', 'synthetic_citizen', 'What about that condition?',
            now, raw={'parent_id': 'parent'}), now=now)
        task = store.claim_post_tasks(now=now + 1)[0]
        before = store.dependency_fingerprint('child', 1, 'fixed-corpus')
        store.wait_for_material(task, 'fixed-corpus', 'llm:needs_materials:synthetic-context')
        store.upsert_observed_post(Post('parent', 'other_citizen', 'A concrete policy condition.', now), now=now + 2)
        after = store.dependency_fingerprint('child', 1, 'fixed-corpus')
        store.resume_material_tasks('fixed-corpus', now=now + 3)
        status = store._conn.execute("SELECT status FROM post_tasks WHERE post_id='child'").fetchone()[0]
        assert before == after and status == 'waiting_materials'
        return {'fingerprint_changed': before != after, 'child_status_after_parent_arrival': status,
                'parent_present': store.get_observed_post('parent') is not None}


def context_status_mapping():
    def answer(body, data):
        choose(data, 'policy_stance', 'no')
        if 'specificity_evidence' in data['answers']:
            choose(data, 'specificity_evidence', 'insufficient')
    client = JevClient(JevConfig(api_key='synthetic-context', max_retries=0), transport=FakeTransport(answer))
    record = content()
    record['task_context'] = {'assigned_side': 'yes'}
    workflow = JevWorkflow(client)
    data = workflow.evaluate(record).to_dict()
    assert data['flow']['status'] == 'needs_context'
    assert data['action_decision']['status'] == 'needs_assessment'
    from ctn.client.mock import MockLegitClient
    from ctn.pipeline.loop import BotLoop
    with closing(StateStore()) as store:
        router = bound_router(store, ('yes_001',))
        brain = JevLlmBrain(workflow, None, side=Side.YES, router=router)
        loop = BotLoop(actor(MockLegitClient(failure_rate=0), store, dry=True),
            side=Side.YES, accounts=['fresh_yes_001'], brain=brain, store=store)
        store.upsert_observed_post(Post('context-child', 'synthetic_citizen', 'What about that condition?',
            time.time(), raw={'parent_id': 'missing-parent', 'platform_level': 'standard'}))
        async def run():
            for _ in range(3):
                await loop.process_pending()
                with store.batch_transaction():
                    store._conn.execute("UPDATE post_tasks SET next_attempt_at=0 WHERE status='queued'")
        asyncio.run(run())
        task = dict(store._conn.execute("SELECT status,reason,attempts FROM post_tasks WHERE post_id='context-child'").fetchone())
        assert task['status'] == 'skipped' and task['attempts'] == 3, task
        return {'flow_status': data['flow']['status'],
                'brain_reason_from_action_status': 'jev:' + data['action_decision']['status'],
                'loop_waiting_prefix': 'jev:needs_context', 'after_three_attempts': task}


def unsupported_response_purpose():
    def answer(body, data):
        if 'policy_stance' in data['answers']:
            choose(data, 'policy_stance', 'mixed')
        if 'response_action' in data['answers']:
            choose(data, 'response_action', 'reply')
            choose(data, 'response_purpose', 'P04')
    client = JevClient(JevConfig(api_key='synthetic-purpose', max_retries=0), transport=FakeTransport(answer))
    with closing(StateStore()) as store:
        router = bound_router(store, ('yes_001',))
        brain = JevLlmBrain(JevWorkflow(client), None, side=Side.YES, router=router)
        verdict = brain.think(Post('purpose-target', 'synthetic_citizen',
            'I support requirements for hospitals but prefer voluntary rules for parks.', time.time()))
        assert verdict.reason == 'unsupported_purpose:P04', verdict
        return {'model_selected': 'P04', 'brain_action': verdict.action.value, 'brain_reason': verdict.reason}


def high_account_supply():
    with closing(StateStore()) as store:
        router = bound_router(store, ('yes_001', 'yes_002'))
        data = {'action_decision': {'required_account_level': 'high'}}
        try:
            router.route(data, 'ferry public access')
            raise AssertionError('Unexpected high-capability account')
        except ValueError as exc:
            reason = str(exc)
        return {'bound_active_accounts': 2,
                'capability_tiers': sorted({s.capability_tier for s in store.account_states_for(store.persona_bindings().values()).values()}),
                'routing_result': reason, 'classification': 'operational default, not a bypass bug'}


def opposing_question_priority():
    text = "I'm worried about the ferry rule. What do the trials actually establish?"
    data = {'answers': {'expression_function': {'value': 'question'}, 'claim_kind': {'value': 'none'}}}
    selected = JevLlmBrain._select_opposing_purpose(data, text)
    assert selected == 'P07'
    return {'central_function': 'question', 'selected_purpose': selected,
            'intended_question_purpose': 'P02', 'text': text}


def configurable_model_wiring():
    config = JevConfig(model='synthetic-alternate-model', api_key='synthetic-key')
    client = JevClient(config, transport=FakeTransport())
    try:
        JevEvaluator(client, packer=JevPacker(verified_references=()),
            post_questions=load_rubric('post', version='v3'))
        raise AssertionError('Unexpected model match')
    except ValueError as exc:
        return {'configured_model': config.model, 'default_packer_model': JevPacker().model,
                'result': type(exc).__name__, 'reason': str(exc)}


def incomplete_text_flag_overridden():
    with closing(StateStore()) as store:
        router = bound_router(store, ('yes_001',))
        workflow = JevWorkflow(JevClient(JevConfig(api_key='synthetic-completeness'), transport=FakeTransport()))
        brain = JevLlmBrain(workflow, None, side=Side.YES, router=router)
        store.upsert_observed_post(Post('parent', 'other', 'An incomplete parent excerpt', time.time(),
            raw={'text_complete': False}))
        post = Post('child', 'citizen', 'An incomplete child excerpt', time.time(),
            raw={'text_complete': False, 'parent_id': 'parent'})
        payload = brain.payload_for(post)
        result = {'input_flag': False, 'target_payload_flag': payload['post']['text_complete'],
            'parent_payload_flag': payload['conversation']['items'][0]['text_complete']}
        assert result['target_payload_flag'] is True and result['parent_payload_flag'] is True
        return result


def active_batch_ingestion():
    from ctn.moderation import BotDetector
    from ctn.pipeline.crawl import _ingest
    results = []
    with tempfile.TemporaryDirectory() as directory:
        for count in (0, 50, 200, 500):
            with closing(StateStore(Path(directory) / f'batches-{count}.db')) as store:
                now = time.time()
                store.set_policy('active_persona_catalog', '')
                with store.batch_transaction():
                    for i in range(count):
                        pid = f'old-{i}'
                        store.upsert_observed_post(Post(pid, f'author-{i}', 'A concrete ferry access question.', now), now=now)
                        store.create_reply_batch(pid, 1, {'catalog_hash': ''},
                            [{'persona_id': 'synthetic', 'persona_version': 1, 'account_id': f'account-{i % 50}'}],
                            expires_at=now + 1800, now=now)
                detector = BotDetector(store)
                measurements = []
                sql_count = 0
                for repeat in range(5):
                    statements = []
                    store._conn.set_trace_callback(statements.append)
                    started = time.perf_counter()
                    with store.batch_transaction():
                        for i in range(30):
                            _ingest(store, Post(f'new-{repeat}-{i}', f'new-author-{repeat}-{i}',
                                f'Question {repeat} {i}: what do the ferry entry details mean?', now), now=now, detector=detector)
                    measurements.append((time.perf_counter() - started) * 1000)
                    store._conn.set_trace_callback(None)
                    sql_count = len(statements)
                results.append({'active_batches': count,
                    'ingest30_p50_ms': round(statistics.median(measurements), 3),
                    'ingest30_max_ms': round(max(measurements), 3),
                    'sql_statements_last_repeat': sql_count})
    return results


if __name__ == '__main__':
    print(json.dumps({
        'sync_rag_snapshot_lost': sync_snapshot_loss(),
        'unknown_author_level': unknown_author_level(),
        'parent_arrival_not_a_dependency': parent_arrival_does_not_resume(),
        'context_status_mapping': context_status_mapping(),
        'unsupported_response_purpose': unsupported_response_purpose(),
        'high_account_supply': high_account_supply(),
        'opposing_question_priority': opposing_question_priority(),
        'configurable_model_wiring': configurable_model_wiring(),
        'incomplete_text_flag_overridden': incomplete_text_flag_overridden(),
        'active_batch_ingestion': active_batch_ingestion(),
    }, ensure_ascii=False, indent=2))
