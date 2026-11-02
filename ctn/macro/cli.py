"""Command-line interface for macro campaign state sensing and proactive publishing."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

from ctn.actuator import Actuator
from ctn.client.base import LegitClient
from ctn.client.legit_v1 import OfficialLegitClient
from ctn.client.mock import MockLegitClient, MockPost
from ctn.config import BudgetConfig, JevConfig, PlatformConfig, RateConfig, Settings, _load_dotenv
from ctn.jev import JevClient, JevPostflightChecker
from ctn.persona import PersonaRouter, PersonaRegistry,get_warmup_seed
from ctn.ratelimit import build_gate, build_ledger
from .publisher import StrategicPublisher
from .state import MacroStateCollector

from ctn.storage import DEFAULT_DB_PATH, StateStore

DEFAULT_STATE_FILE = DEFAULT_DB_PATH


def _load_router(side: str, state_path: Path) -> PersonaRouter:
    """Instantiate a PersonaRouter backed by SQLite (or JSON state file if .json is specified)."""
    registry=PersonaRegistry()
    if side in ('yes','no') and side!=registry.catalog['side']:
        raise ValueError('assigned side conflicts with persona catalog')
    store = StateStore(state_path)
    if side in ("yes","no"): store.bind_campaign_side(side)
    router = PersonaRouter(registry=registry,default_side=side, store=store)
    store.seed_campaign_policy(router.registry.list_all())
    return router


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


def _build_mock_platform() -> MockLegitClient:
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
    return mock_platform


def _setup_environment(
    dry_run: bool = False,
    *,
    store: StateStore | None = None,
    force_mock: bool = False,
    live: bool = False,
    side: str | None = None,
) -> tuple[Actuator, JevPostflightChecker]:
    """Set up Actuator and JevPostflightChecker using OfficialLegitClient when configured, or MockLegitClient offline."""
    settings=Settings.load()
    if side in ('yes','no'):
        from dataclasses import replace
        from ctn.models import Side
        settings=replace(settings,side=Side(side))
    mode=settings.execution_mode('mock' if force_mock else ('live' if live else ('dry-run' if dry_run else None)))
    if store is not None and mode!='mock': store.bind_data_source('real')
    if mode=='live': settings.require_live_writes()
    jev_client=JevClient(settings.jev)
    postflight=JevPostflightChecker(jev_client)
    if mode != 'mock':
        if not settings.platform.api_key:
            raise ValueError('platform credentials required; use --mock for offline operation')
        platform_client=OfficialLegitClient(url=settings.platform.base_url or None,
            team_key=settings.platform.api_key,default_password=settings.platform.bot_password,
            timeout=settings.platform.request_timeout_s,auto_register=False,store=store)
        platform_client.configure_pacing(settings.rate,settings.budget)
    else:
        platform_client=_build_mock_platform()
    actuator=Actuator(platform_client,build_gate(settings.rate),build_ledger(settings.budget,store=store),
        dry_run=(mode!='live'),store=store)
    return actuator,postflight


def cmd_status(args: argparse.Namespace) -> int:
    """Collect and print current macro state."""
    state_path = Settings.load().database_path(args.state_file)
    router = _load_router(args.side, state_path)
    actuator, _ = _setup_environment(
        dry_run=None, store=router.store, force_mock=getattr(args, "mock", False),side=args.side
    )

    collector = MacroStateCollector(actuator, router, side=args.side)
    snapshot = collector.collect_sync()
    print(json.dumps(snapshot, indent=2, ensure_ascii=False))
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    """Execute a strategic proactive post with JEV safety audit."""
    state_path = Settings.load().database_path(args.state_file)
    router = _load_router(args.side, state_path)
    actuator, postflight = _setup_environment(
        dry_run=args.dry_run, store=router.store, force_mock=getattr(args, "mock", False),live=args.live,side=args.side
    )

    publisher = StrategicPublisher(actuator, postflight, router, side=args.side)
    tags = [t.strip() for t in args.tag.split(",") if t.strip()] if args.tag else None

    # Offline safety bypass if JEV key is not set
    bypass_jev = args.bypass_jev or isinstance(actuator._client,MockLegitClient)

    result = publisher.publish_sync(
        args.account,
        args.text,
        hashtags=tags,
        purpose=args.purpose,
        branch=args.branch,
        target_text=args.target_text,
        bypass_jev=bypass_jev,
        rag_snapshot_id=args.rag_snapshot,
    )

    if result.get("ok"):
        _save_router(router, state_path)

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") or result.get("status")=="simulated" else 1


def cmd_warmup(args: argparse.Namespace) -> int:
    """Publish a pre-vetted Kingstonia native seed post for account warming."""
    state_path = Settings.load().database_path(args.state_file)
    router = _load_router(args.side, state_path)
    actuator, postflight = _setup_environment(
        dry_run=args.dry_run, store=router.store, force_mock=getattr(args, "mock", False),live=args.live,side=args.side
    )

    publisher = StrategicPublisher(actuator, postflight, router, side=args.side)
    seed_text = get_warmup_seed(args.account, index=args.index,registry=router.registry)

    # Warmup seeds are pre-vetted non-controversial life posts; JEV is bypassed or soft-checked
    result = publisher.publish_sync(
        args.account,
        seed_text,
        purpose="warmup",
        bypass_jev=isinstance(actuator._client,MockLegitClient),
    )

    if result.get("ok"):
        _save_router(router, state_path)

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") or result.get("status")=="simulated" else 1


def main(argv: list[str] | None = None) -> int:
    _load_dotenv(Path(".env"))
    env_side = os.environ.get("CTN_SIDE", "").strip().lower()
    default_side = env_side if env_side in ("yes", "no") else "unassigned"

    parser = argparse.ArgumentParser(description="CTN 2026 Macro Campaign Publisher CLI")
    parser.add_argument(
        "--side",
        choices=["yes", "no", "unassigned"],
        default=default_side,
        help="Assigned team side (defaults to CTN_SIDE or 'yes')",
    )
    parser.add_argument("--state-file", default=None, help="Path to state file")
    parser.add_argument("--mock", action="store_true", help="Force offline MockLegitClient")
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
    sub_pub.add_argument("--rag-snapshot",default="",help="ID from ctn.rag query; required for live strategic posts")
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
    modes=sub_pub.add_mutually_exclusive_group()
    modes.add_argument("--dry-run", action="store_true",default=None, help="Simulate without posting")
    modes.add_argument("--live",action="store_true")
    sub_pub.add_argument("--bypass-jev", action="store_true", help="Bypass JEV postflight check")
    sub_pub.set_defaults(func=cmd_publish)

    # Subcommand: warmup
    sub_warm = subparsers.add_parser("warmup", help="Publish a pre-vetted Kingstonia warmup post")
    sub_warm.add_argument("--account", required=True, help="Account ID to warm up")
    sub_warm.add_argument("--index", type=int, default=0, help="Index of seed post (default: 0)")
    modes=sub_warm.add_mutually_exclusive_group()
    modes.add_argument("--dry-run", action="store_true",default=None, help="Simulate without posting")
    modes.add_argument("--live",action="store_true")
    sub_warm.set_defaults(func=cmd_warmup)

    # Also accept global options after the subcommand (Pi's historical order).
    for sub in (sub_status,sub_pub,sub_warm):
        sub.add_argument('--side',choices=['yes','no','unassigned'],default=argparse.SUPPRESS)
        sub.add_argument('--state-file',default=argparse.SUPPRESS)
        sub.add_argument('--mock',action='store_true',default=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.mock and getattr(args,"live",False): parser.error("--mock and --live conflict")
    try:
        return args.func(args)
    except ValueError as exc:
        print(json.dumps({'ok':False,'status':'blocked','reason':str(exc)}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
