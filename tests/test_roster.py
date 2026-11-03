"""Roster automation: detect a suspended account, park its persona, register a same-group replacement.

2026-10-30: 52 of 102 accounts were suspended, 3-4 a day, and each replacement needed a human plus a
fleet restart. A suspended account keeps `mode='active'` and therefore one of the 50 active-persona
slots, so the replacement is refused with 'active persona limit exceeded' until the dead persona is
parked. These tests pin that behaviour down.
"""
import json
import time
import unittest
import uuid

from ctn import roster
from ctn.persona.router import PersonaRouter
from ctn.storage import StateStore
from tests.test_persona80 import bound_router

NOW = 1793000000.0


def store_with(ids, *, active=True):
    store = StateStore()
    router = bound_router(store, ids, active=active)
    return store, router


def failed(store, account, detail, *, at=None, kind='reply', status='failed'):
    meta = {'target_author': 'someone'}
    with store.batch_transaction():
        store._conn.execute(
            "INSERT INTO action_intents(id,action_key,kind,account_id,target_id,text,status,detail,owner,lease_until,"
            "created_at,updated_at,metadata_json) VALUES(?,?,?,?,?,'',?,?,'x',0,?,?,?)",
            (uuid.uuid4().hex, uuid.uuid4().hex, kind, account, 'p1', status, detail, at or NOW - 60, at or NOW - 60,
             json.dumps(meta)),
        )


async def fake_register(store, client, persona_id, account_id, password, **kwargs):
    """Same shape as ctn.accounts.register_one without a platform call."""
    with store.batch_transaction():
        store.bind_persona(persona_id, account_id)
        store.upsert_managed_account(account_id, status='active')
    store.initialise_registered_account(account_id)
    return {'account': account_id, 'status': 'succeeded'}


class DetectionTests(unittest.TestCase):
    def setUp(self):
        self.store, self.router = store_with(('yes_001', 'yes_002'))
        self.addCleanup(self.store.close)

    def test_suspension_detail_is_detected(self):
        account = self.store.persona_bindings()['yes_001']
        failed(self.store, account, 'account suspended')
        found = roster.pending_replacements(self.store, since_s=3600, now=NOW)
        self.assertEqual([row['account_id'] for row in found], [account])

    def test_other_failures_are_not_suspensions(self):
        account = self.store.persona_bindings()['yes_001']
        failed(self.store, account, 'post not found')
        failed(self.store, account, 'client shutting down')
        self.assertEqual(roster.pending_replacements(self.store, since_s=3600, now=NOW), [])

    def test_old_failures_outside_the_window_are_ignored(self):
        account = self.store.persona_bindings()['yes_001']
        failed(self.store, account, 'account suspended', at=NOW - 86400)
        self.assertEqual(roster.pending_replacements(self.store, since_s=3600, now=NOW), [])

    def test_retired_account_stops_being_a_candidate(self):
        account = self.store.persona_bindings()['yes_001']
        failed(self.store, account, 'account suspended')
        roster.retire(self.store, account, now=NOW)
        self.assertEqual(roster.pending_replacements(self.store, since_s=3600, now=NOW), [])


class RetireTests(unittest.TestCase):
    def setUp(self):
        self.store, self.router = store_with(('yes_001', 'yes_002', 'yes_003'), active=False)
        self.addCleanup(self.store.close)

    def test_retire_parks_persona_so_the_slot_is_free(self):
        bindings = self.store.persona_bindings()
        dead, spare, blocked = bindings['yes_001'], bindings['yes_002'], bindings['yes_003']
        self.store.set_persona_modes(['yes_001', 'yes_002'], 'active', max_active=2)
        with self.assertRaises(ValueError):
            self.store.set_persona_modes(['yes_003'], 'active', max_active=2)
        persona_id = roster.retire(self.store, dead, now=NOW)
        self.assertEqual(persona_id, 'yes_001')
        self.assertTrue(self.store.get_account_state(dead).is_banned)
        self.assertEqual(self.store.get_managed_account(dead)['status'], 'suspended')
        self.assertEqual(self.store.get_account_state(dead).mode, 'standby')
        # The slot is free again: the third persona can be activated at the same cap.
        self.assertEqual(self.store.set_persona_modes(['yes_003'], 'active', max_active=2), 1)
        self.assertEqual(self.store.get_account_state(spare).mode, 'active')
        self.assertEqual(self.store.get_account_state(blocked).mode, 'active')


class PickTests(unittest.TestCase):
    def setUp(self):
        self.store, self.router = store_with(('yes_001',))
        self.addCleanup(self.store.close)

    def test_never_picks_a_bound_persona(self):
        bound = set(self.store.persona_bindings().keys())
        picked = roster.pick_persona(self.store, self.router.registry, group='retail_food')
        self.assertIsNotNone(picked)
        self.assertNotIn(picked.persona_id, bound)
        self.assertEqual(picked.occupation_group, 'retail_food')

    def test_falls_back_to_the_thinnest_group(self):
        picked = roster.pick_persona(self.store, self.router.registry, group='law_policy')
        self.assertIsNotNone(picked)
        self.assertNotIn(picked.persona_id, set(self.store.persona_bindings().keys()))

    def test_no_free_persona_returns_none(self):
        registry = PersonaRouter(store=self.store).registry
        for persona in [p for p in registry.list_all() if not p.account_id]:
            registry._personas.pop(persona.persona_id, None)
        self.assertIsNone(roster.pick_persona(self.store, registry, group='retail_food'))


class ReplaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store, self.router = store_with(('yes_001', 'yes_002'))
        self.addCleanup(self.store.close)
        # Managed rows are what `active_count` counts; bound_router only writes bindings.
        for account in self.store.persona_bindings().values():
            self.store.upsert_managed_account(account, status='active')

    async def run_pass(self, **kwargs):
        policy = {'max_per_step': 1, 'max_per_day': 3, **kwargs.pop('policy', {})}
        return await roster.replace_suspended(
            self.store, self.router.registry, client=None, password='pw',
            max_active=kwargs.pop('max_active', 2), policy=policy, now=NOW, replace=fake_register,
        )

    async def test_replacement_is_registered_bound_and_live(self):
        dead = self.store.persona_bindings()['yes_001']
        failed(self.store, dead, 'account suspended')
        out = await self.run_pass()
        self.assertEqual(len(out['registered']), 1)
        entry = out['registered'][0]
        self.assertEqual(entry['from'], dead)
        self.assertTrue(entry['same_group'])
        # Bound and active: the loop's refresh_accounts() re-reads bindings, so no restart is needed.
        self.assertEqual(self.store.persona_bindings()[entry['persona']], entry['to'])
        self.assertEqual(self.store.get_account_state(entry['to']).mode, 'active')
        self.assertEqual(self.store.get_managed_account(entry['to'])['status'], 'active')
        live = {p.account_id for p in self.router.registry.list_all() if p.account_id}
        self.assertIn(entry['to'], live)
        # The dead account keeps its binding (history) but is parked, so it cannot act.
        self.assertEqual(self.store.get_account_state(dead).mode, 'standby')
        self.assertTrue(self.store.get_account_state(dead).is_banned)
        self.assertEqual(roster.active_count(self.store), 2)

    async def test_a_live_reserved_account_is_adopted_before_registering(self):
        # A previously suspended account that answers authenticated reads is alive again: reuse it
        # instead of burning one of the 200 registrations.
        self.store.upsert_managed_account('back_from_the_dead', status='suspended')
        roster.set_reserve(self.store, [{'account': 'back_from_the_dead', 'persona': ''}])
        out = await self.run_pass(policy={'max_per_day': 5}, max_active=3)
        self.assertEqual(roster.reserve(self.store), [])
        self.assertEqual(self.store.get_managed_account('back_from_the_dead')['status'], 'active')
        self.assertEqual(self.store.get_account_state('back_from_the_dead').mode, 'active')
        self.assertFalse(self.store.get_account_state('back_from_the_dead').is_banned)
        self.assertEqual(len(out['registered']), 1)

    async def test_adopt_clears_the_ban_flag_and_the_status(self):
        self.store.upsert_managed_account('phoenix', status='suspended')
        with self.store.batch_transaction():
            self.store._conn.execute("UPDATE account_states SET is_banned=1, mode='standby' WHERE account_id='phoenix'")
        entry = roster.adopt(self.store, 'phoenix', now=NOW)
        self.assertTrue(entry['adopted'])
        self.assertEqual(self.store.get_managed_account('phoenix')['status'], 'active')
        state = self.store.get_account_state('phoenix')
        self.assertFalse(state.is_banned)
        self.assertEqual(state.mode, 'active')

    async def test_daily_cap_stops_the_second_replacement(self):
        bindings = self.store.persona_bindings()
        failed(self.store, bindings['yes_001'], 'account suspended', at=NOW - 120)
        failed(self.store, bindings['yes_002'], 'account suspended', at=NOW - 60)
        out = await self.run_pass(policy={'max_per_step': 2, 'max_per_day': 1})
        # Newest first: yes_002 gets the only slot, yes_001 waits for the next campaign day.
        self.assertEqual(len(out['registered']), 1)
        self.assertEqual(out['registered'][0]['from'], bindings['yes_002'])
        self.assertIn({'account': bindings['yes_001'], 'reason': 'daily_cap'}, out['skipped'])

    async def test_top_up_fills_the_fleet_without_a_suspension(self):
        self.store.upsert_managed_account('seed0', status='active')  # 3 active of a 5 target
        out = await self.run_pass(max_active=5, policy={'max_per_day': 5})
        self.assertEqual(out['retired'], [])
        self.assertEqual(len(out['registered']), 2)
        self.assertTrue(all(entry['from'] == '' for entry in out['registered']))
        self.assertEqual(roster.active_count(self.store), 5)

    async def test_stale_suspended_account_is_parked_and_replaced(self):
        dead = self.store.persona_bindings()['yes_001']
        # The old manual flow's leftover: suspended on the platform, still mode='active'.
        self.store.mark_managed_account_status(dead, 'suspended', now=NOW)
        self.assertEqual(roster.stale_slots(self.store), [dead])
        out = await self.run_pass()
        self.assertEqual([row['account'] for row in out['parked']], [dead])
        self.assertEqual(self.store.get_account_state(dead).mode, 'standby')
        self.assertEqual(len(out['registered']), 1)
        self.assertEqual(roster.active_count(self.store), 2)

    async def test_failed_registration_hands_the_persona_back_and_is_skipped(self):
        async def boom(store, client, persona_id, handle, password, **kwargs):
            return {'account': handle, 'status': 'failed'}

        out = await roster.replace_suspended(
            self.store, self.router.registry, client=None, password='pw',
            max_active=4, policy={'max_per_day': 5}, now=NOW, replace=boom,
        )
        self.assertEqual(out['registered'], [])
        self.assertIn('failed', [row.get('reason') for row in out['skipped']])
        # The next pass must try a different persona instead of looping on the same one.
        blocked = {row['persona'] for row in self.store.policy('roster_skip')}
        self.assertTrue(blocked)
        self.assertNotIn(roster.pick_persona(self.store, self.router.registry, now=NOW).persona_id, blocked)

    async def test_orphan_binding_is_freed_so_the_persona_is_usable_again(self):
        # A failed registration used to keep its binding, permanently burning the persona.
        self.store.bind_persona('yes_003', 'ghost_handle')
        self.assertEqual(roster.orphan_bindings(self.store), [('yes_003', 'ghost_handle')])
        out = await self.run_pass()
        self.assertIn('ghost_handle', [row['account'] for row in out['parked']])
        self.assertEqual(roster.orphan_bindings(self.store), [])
        self.assertNotIn('yes_003', self.store.persona_bindings())  # free for a later pass

    async def test_disabled_policy_does_nothing(self):
        failed(self.store, self.store.persona_bindings()['yes_001'], 'account suspended')
        out = await roster.replace_suspended(
            self.store, self.router.registry, client=None, password='pw',
            policy={'enabled': False}, now=NOW, replace=fake_register,
        )
        self.assertTrue(out['disabled'])
        self.assertEqual(out['registered'], [])
        self.assertEqual(out['parked'], [])

    async def test_no_free_persona_is_reported_not_crashed(self):
        dead = self.store.persona_bindings()['yes_001']
        failed(self.store, dead, 'account suspended')
        registry = self.router.registry
        for persona in [p for p in registry.list_all() if not p.account_id]:
            registry._personas.pop(persona.persona_id, None)
        out = await self.run_pass()
        self.assertEqual(out['registered'], [])
        self.assertIn('no_free_persona', [row.get('reason') for row in out['skipped']])
