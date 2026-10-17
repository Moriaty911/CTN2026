"""Read -> react -> act against the mock platform.

Run with `python -m pytest tests` or `python tests/test_pipeline.py`.
"""

from __future__ import annotations

import asyncio
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ctn.actuator import Actuator
from ctn.client.mock import MockLegitClient
from ctn.config import BudgetConfig, RateConfig
from ctn.models import ActionKind, Post, Side, Tier
from ctn.pipeline import BotLoop, decide
from ctn.ratelimit import build_gate, build_ledger

ACCOUNTS = ["bot_001", "bot_002", "bot_003"]
FAST = RateConfig(read_rps=100, write_rps=100, per_account_read_rps=100, per_account_write_rps=100)


def make_actuator(client: MockLegitClient, *, dry_run: bool = False, max_retries: int = 0) -> Actuator:
    return Actuator(
        client, build_gate(FAST), build_ledger(BudgetConfig()),
        dry_run=dry_run, max_retries=max_retries, backoff_s=0.01,
    )


def make_client(**kwargs) -> MockLegitClient:
    kwargs.setdefault("failure_rate", 0.0)
    kwargs.setdefault("latency_ms", (0.0, 1.0))
    return MockLegitClient(**kwargs)


def run(coro):
    return asyncio.run(coro)


# ---- actions ------------------------------------------------------------- #
def test_actions_reach_platform():
    client = make_client()
    actuator = make_actuator(client)
    target = next(iter(client.posts))

    async def go():
        return [
            await actuator.like("bot_001", target),
            await actuator.retweet("bot_001", target),
            await actuator.comment("bot_001", target, "Interesting point."),
            await actuator.post("bot_001", "Hello Kingstonia #KingstoniaVotes"),
            await actuator.follow("bot_001", "citizen_0001"),
            await actuator.unfollow("bot_001", "citizen_0001"),
        ]

    results = run(go())
    assert all(r.ok for r in results), [r.detail for r in results]
    assert [r.kind for r in results] == ["like", "repost", "reply", "post", "follow", "unfollow"]
    assert client.write_count == 6
    assert actuator.ledger.writes == 6


def test_dry_run_sends_nothing():
    client = make_client()
    actuator = make_actuator(client, dry_run=True)
    result = run(actuator.like("bot_001", next(iter(client.posts))))
    assert result.status == "simulated" and not result.ok and "dry run" in result.detail
    assert client.write_count == 0


def test_throttle_slows_gate_instead_of_crashing():
    client = make_client(throttle_after=0)
    actuator = make_actuator(client)
    result = run(actuator.like("bot_001", next(iter(client.posts))))
    assert not result.ok and result.detail == "throttled"
    assert actuator._gate.stats["write"].slow_downs == 1


def test_gate_recovers_after_clean_cycles():
    client = make_client(throttle_after=0)
    actuator = make_actuator(client)
    loop = BotLoop(actuator, side=Side.YES, accounts=ACCOUNTS)
    run(loop.run_cycle())                     # every call throttled
    slowed = actuator._gate._team["read"].rate
    assert slowed < FAST.read_rps

    client._throttle_after = 10_000           # platform calms down
    run(loop.run_cycle())
    assert actuator._gate._team["read"].rate > slowed


def test_suspended_account_is_retired():
    client = make_client()
    client.suspend("bot_001")
    actuator = make_actuator(client)
    result = run(actuator.like("bot_001", next(iter(client.posts))))
    assert not result.ok and "bot_001" in actuator.suspended


def test_failed_read_returns_default():
    client = make_client(throttle_after=0)
    actuator = make_actuator(client)
    assert run(actuator.search("kingstonia")) == []


# ---- react --------------------------------------------------------------- #
def _post(text: str, **kw) -> Post:
    return Post(post_id="p1", author="citizen_0001", text=text, **kw)


def test_unassigned_side_only_observes():
    d = decide(_post("kingstonia #MandateNow"), side=Side.UNASSIGNED, keywords=["kingstonia"])
    assert d.tier is Tier.OBSERVE and d.action is ActionKind.NONE


def test_decision_rules():
    kw = ["kingstonia"]
    assert decide(_post("kingstonia #MandateNow"), side=Side.YES, keywords=kw).action is ActionKind.LIKE
    assert decide(_post("kingstonia #MandateNow", likes=10), side=Side.YES, keywords=kw).action is ActionKind.REPOST
    assert decide(_post("kingstonia #MandateNow"), side=Side.NO, keywords=kw).action is ActionKind.NONE
    counter = decide(_post("kingstonia #MandateNow", replies=5), side=Side.NO, keywords=kw)
    assert counter.action is ActionKind.REPLY and counter.text
    assert decide(_post("kingstonia today"), side=Side.YES, keywords=kw).action is ActionKind.REPLY
    assert decide(_post("weather"), side=Side.YES, keywords=kw).action is ActionKind.NONE


# ---- full cycle ---------------------------------------------------------- #
def test_cycle_acts_and_rotates_accounts():
    client = make_client()
    loop = BotLoop(make_actuator(client), side=Side.YES, accounts=ACCOUNTS, rng=random.Random(0))
    report = run(loop.run_cycle())
    assert report.status == "acted"
    assert report.observed > 0
    assert 0 < len(report.results) <= loop.max_actions_per_cycle
    used = [r.account for r in report.results]
    assert len(set(used)) == len(used)          # one bot per action


def test_cycle_never_acts_twice_on_same_post():
    client = make_client()
    loop = BotLoop(make_actuator(client), side=Side.YES, accounts=ACCOUNTS, rng=random.Random(0))

    async def go():
        first = await loop.run_cycle()
        second = await loop.run_cycle()
        return first, second

    first, second = run(go())
    targets = [r.target_id for r in first.results + second.results if r.ok]
    assert len(targets) == len(set(targets))


def test_cycle_without_side_observes():
    client = make_client()
    loop = BotLoop(make_actuator(client), side=Side.UNASSIGNED, accounts=ACCOUNTS)
    report = run(loop.run_cycle())
    assert report.status == "observed" and not report.results
    assert client.write_count == 0


def load_tests(loader, tests, pattern):
    import unittest
    for name,fn in list(globals().items()):
        if name.startswith('test_') and callable(fn): tests.addTest(unittest.FunctionTestCase(fn))
    return tests


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
