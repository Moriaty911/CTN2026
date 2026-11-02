"""Notification delivery must not wedge the outbox on rows that can never be delivered."""

import unittest

from ctn.discord_bot import MAX_OUTBOX_ATTEMPTS, deliver_one
from ctn.storage import StateStore


class OutboxDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = StateStore(":memory:")
        self.addCleanup(self.store.close)
        self.delivered: list[dict] = []

    async def transport(self, payload):
        self.delivered.append(payload)

    def statuses(self):
        return [row[0] for row in self.store._conn.execute("SELECT status FROM discord_outbox ORDER BY id")]

    async def test_normal_payload_is_sent(self):
        self.store.notify_discord("review:c1:2", {"type": "candidate", "candidate_id": "c1"})
        self.assertTrue(await deliver_one(self.store, self.transport))
        self.assertEqual(self.delivered, [{"type": "candidate", "candidate_id": "c1"}])
        self.assertEqual(self.statuses(), ["sent"])

    async def test_empty_candidate_payload_is_abandoned_not_retried(self):
        # 19 of these sat at the head of the queue on 2026-10-20, retrying forever.
        self.store.notify_discord("review::1", {"type": "candidate", "candidate_id": ""})
        self.assertTrue(await deliver_one(self.store, self.transport))
        self.assertEqual(self.delivered, [])
        self.assertEqual(self.statuses(), ["failed"])

    async def test_payload_that_keeps_failing_is_abandoned_after_the_cap(self):
        async def boom(payload):
            raise RuntimeError("discord said no")

        self.store.notify_discord("k", {"type": "candidate", "candidate_id": "c1"})
        self.store._conn.execute("UPDATE discord_outbox SET attempts=?", (MAX_OUTBOX_ATTEMPTS,))
        self.assertTrue(await deliver_one(self.store, boom))
        self.assertEqual(self.statuses(), ["failed"])

    async def test_failed_delivery_stays_queued_before_the_cap(self):
        async def boom(payload):
            raise RuntimeError("discord said no")

        self.store.notify_discord("k", {"type": "candidate", "candidate_id": "c1"})
        self.assertTrue(await deliver_one(self.store, boom))
        self.assertEqual(self.statuses(), ["queued"])


if __name__ == "__main__":
    unittest.main()
