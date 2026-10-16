"""Command-line interface for macro campaign state sensing and proactive publishing."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
from typing import Any

from ctn.actuator import Actuator
from ctn.client.mock import MockLegitClient, MockPost
from ctn.config import BudgetConfig, JevConfig, RateConfig
from ctn.jev import JevClient, JevPostflightChecker
from ctn.persona import PersonaRouter, get_warmup_seed
from ctn.ratelimit import build_gate, build_ledger
from .publisher import StrategicPublisher
from .state import MacroStateCollector

from ctn.storage import DEFAULT_DB_PATH, StateStore

DEFAULT_STATE_FILE = DEFAULT_DB_PATH


def _load_router(side: str, state_path: Path) -> PersonaRouter:
    """Instantiate a PersonaRouter backed by SQLite (or JSON state file if .json is specified)."""
    if state_path.suffix.lower() == ".json":
        router = PersonaRouter(default_side=side)
        if state_path.is_file():
            try:
                data = json.loads(state_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    router.load_states(data)
            except Exception:
                pass
        return router
    store = StateStore(state_path)
    return PersonaRouter(default_side=side, store=store)


def _save_router(router: PersonaRouter, state_path: Path) -> None:
    """Save account runtime states to disk (automatic for SQLite; explicit dump for .json)."""
    if state_path.suffix.lower() == ".json":
        try:
            data = router.dump_states()
            state_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass
    else:
        router.dump_states()


def _setup_environment(dry_run: bool = False) -> tuple[Actuator, JevPostflightChecker]:
    """Set up Actuator and JevPostflightChecker with available credentials or offline fallbacks."""
    jev_cfg = JevConfig.load()
    jev_client = JevClient(jev_cfg)
    postflight = JevPostflightChecker(jev_client)

    # Setup platform client (Mock platform initialized with Kingstonia baseline items)
    mock_platform = MockLegitClient(failure_rate=0.0)
    t = time.time()
    mock_platform.posts["news_001"] = MockPost(
        post_id="news_001",
        author="Daily Kingstonia",
        text="Editorial: Tourism economy faces fragile recovery. Balancing public health with business freedom.",
        created_at=t - 3600,
        likes=145,
        reposts=42,
        hashtags=["#KingstoniaEconomy", "#TourismRecovery"],
    )
    mock_platform.posts["news_002"] = MockPost(
        post_id="news_002",
        author="Kingstonia Herald",
        text="Investigation: Bus drivers and clinic staff voice concerns over peak hour crowding on ring road.",
        created_at=t - 1800,
        likes=210,
        reposts=68,
        hashtags=["#PublicTransit", "#WorkerSafety"],
    )
    mock_platform.posts["trend_001"] = MockPost(
        post_id="trend_001",
        author="citizen_marcus",
        text="Crowded morning buses on the coastal ring road. We need safe rules for public transit! #KingstoniaVotes",
        created_at=t - 600,
        likes=89,
        reposts=19,
        hashtags=["#KingstoniaVotes", "#PublicTransit"],
    )

    rate_cfg = RateConfig(read_rps=10.0, write_rps=10.0)
    actuator = Actuator(
        mock_platform,
        build_gate(rate_cfg),
        build_ledger(BudgetConfig()),
        dry_run=dry_run,
    )
    return actuator, postflight


def cmd_status(args: argparse.Namespace) -> int:
    """Collect and print current macro state."""
    state_path = Path(args.state_file)
    router = _load_router(args.side, state_path)
    actuator, _ = _setup_environment(dry_run=True)

    collector = MacroStateCollector(actuator, router, side=args.side)
    snapshot = collector.collect_sync()
    print(json.dumps(snapshot, indent=2, ensure_ascii=False))
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    """Execute a strategic proactive post with JEV safety audit."""
    state_path = Path(args.state_file)
    router = _load_router(args.side, state_path)
    actuator, postflight = _setup_environment(dry_run=args.dry_run)

    publisher = StrategicPublisher(actuator, postflight, router, side=args.side)
    tags = [t.strip() for t in args.tag.split(",") if t.strip()] if args.tag else None

    # Offline safety bypass if JEV key is not set
    bypass_jev = args.bypass_jev or (not JevConfig.load().api_key)

    result = publisher.publish_sync(
        args.account,
        args.text,
        hashtags=tags,
        purpose=args.purpose,
        branch=args.branch,
        target_text=args.target_text,
        bypass_jev=bypass_jev,
    )

    if result.get("ok"):
        _save_router(router, state_path)

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") else 1


def cmd_warmup(args: argparse.Namespace) -> int:
    """Publish a pre-vetted Kingstonia native seed post for account warming."""
    state_path = Path(args.state_file)
    router = _load_router(args.side, state_path)
    actuator, postflight = _setup_environment(dry_run=args.dry_run)

    publisher = StrategicPublisher(actuator, postflight, router, side=args.side)
    seed_text = get_warmup_seed(args.account, index=args.index)

    # Warmup seeds are pre-vetted non-controversial life posts; JEV is bypassed or soft-checked
    result = publisher.publish_sync(
        args.account,
        seed_text,
        bypass_jev=True,
    )

    if result.get("ok"):
        _save_router(router, state_path)

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CTN 2026 Macro Campaign Publisher CLI")
    parser.add_argument("--side", choices=["yes", "no"], default="yes", help="Assigned team side (default: yes)")
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE, help="Path to state file")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Subcommand: status
    sub_status = subparsers.add_parser("status", help="Collect macro environment status")
    sub_status.set_defaults(func=cmd_status)

    # Subcommand: publish
    sub_pub = subparsers.add_parser("publish", help="Publish a proactive post")
    sub_pub.add_argument("--account", required=True, help="Account ID to publish from")
    sub_pub.add_argument("--text", required=True, help="Post content to publish")
    sub_pub.add_argument("--tag", default="", help="Optional comma-separated hashtags")
    sub_pub.add_argument("--purpose", default="P01", help="Strategic purpose code")
    sub_pub.add_argument(
        "--branch",
        default="",
        help="Decision tree branch code (e.g. branch_A, branch_B_1a, branch_B_2b_ours, branch_B_2c)",
    )
    sub_pub.add_argument(
        "--target-text",
        default="",
        help="Optional opposing trending post text being countered in Branch B",
    )
    sub_pub.add_argument("--dry-run", action="store_true", help="Simulate without posting")
    sub_pub.add_argument("--bypass-jev", action="store_true", help="Bypass JEV postflight check")
    sub_pub.set_defaults(func=cmd_publish)

    # Subcommand: warmup
    sub_warm = subparsers.add_parser("warmup", help="Publish a pre-vetted Kingstonia warmup post")
    sub_warm.add_argument("--account", required=True, help="Account ID to warm up")
    sub_warm.add_argument("--index", type=int, default=0, help="Index of seed post (default: 0)")
    sub_warm.add_argument("--dry-run", action="store_true", help="Simulate without posting")
    sub_warm.set_defaults(func=cmd_warmup)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
