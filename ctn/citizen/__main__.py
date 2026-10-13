"""Local state CLI. Only explicit --live ingest can make LLM requests."""

import argparse
import json
from pathlib import Path
import sqlite3
import sys

from ..config import CitizenLLMConfig
from .domain import AlarmPolicy, CitizenError, IdentityEvidence, utc_now
from .monitor import CitizenMonitor
from .reader import ChatReader, DemoReader, parse_json
from .store import CitizenStore
from .calibration import calibrate_null_runs


def read_file(path):
    if Path(path).stat().st_size > 262144:
        raise CitizenError("input_file_too_large")
    return parse_json(Path(path).read_bytes())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="state/citizen.sqlite3")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("status")
    sub.add_parser("alerts")
    sub.add_parser("tasks")
    identity = sub.add_parser("identities")
    identity.add_argument("--file", required=True, help="JSON array of evidence records, no credentials")
    ingest = sub.add_parser("ingest")
    ingest.add_argument("--file", required=True, help="One JevInput or an array of JevInput records")
    ingest.add_argument("--side", required=True, choices=("yes", "no"))
    ingest.add_argument("--policy-file")
    ingest.add_argument("--live", action="store_true", help="Explicitly enable configured paid LLM calls")
    ingest.add_argument("--retry", action="store_true", help="Retry a failed run; may incur new charges")
    demo = sub.add_parser("demo")
    demo.add_argument("--side", required=True, choices=("yes", "no"))
    backup = sub.add_parser("backup")
    backup.add_argument("--destination", required=True)
    calibrate = sub.add_parser("calibrate")
    calibrate.add_argument("--run-ids-file", required=True, help="JSON array of at least 20 caller-designated null run IDs")
    calibrate.add_argument("--reference", required=True)
    calibrate.add_argument("--output", required=True, help="New policy JSON file; never overwrites an existing file")
    args = parser.parse_args(argv)
    try:
        config = CitizenLLMConfig.load()
        known_keys = (config.api_key,) if config.api_key else ()
        with CitizenStore(args.db, forbidden_values=known_keys) as store:
            if args.command == "identities":
                records = read_file(args.file)
                if type(records) is not list:
                    raise CitizenError("identity_file_requires_array")
                output = [store.record_identity(IdentityEvidence(**record)) for record in records]
            elif args.command == "ingest":
                policy = AlarmPolicy(**read_file(args.policy_file)) if args.policy_file else AlarmPolicy()
                reader = ChatReader(config) if args.live else None
                monitor = CitizenMonitor(store, assigned_side=args.side, reader=reader, policy=policy)
                records = read_file(args.file)
                records = records if type(records) is list else [records]
                output = [monitor.process(record, retry=args.retry) for record in records]
            elif args.command == "demo":
                now = utc_now()
                store.record_identity(IdentityEvidence("demo-citizen", "citizen", "unknown", "inferred",
                                                      "manual", "synthetic-demo-only", now))
                content = {"schema_version": "jev-input.v1", "post": {"post_id": "citizen-demo-v1",
                           "author_id": "demo-citizen", "text": "Keep public spaces accessible while offering voluntary vaccination.",
                           "created_at": now, "kind": "original", "text_complete": True},
                           "source": {"kind": "synthetic", "observed_at": now}}
                output = CitizenMonitor(store, assigned_side=args.side, reader=DemoReader()).process(content)
                output["demonstration_only"] = True
            elif args.command == "alerts":
                output = store.alerts()
            elif args.command == "tasks":
                output = store.tasks()
            elif args.command == "backup":
                store.backup(args.destination)
                output = {"status": "backed_up"}
            elif args.command == "calibrate":
                policy, report = calibrate_null_runs(store, read_file(args.run_ids_file), reference=args.reference)
                with Path(args.output).open("x", encoding="utf-8") as file:
                    json.dump(policy.to_dict(), file, ensure_ascii=False, indent=2, allow_nan=False)
                output = {key: report[key] for key in ("scope", "sample_count", "quantile", "threshold")}
            else:
                output = store.summary()
        # Output metadata by default; source text and provider envelopes stay in local storage.
        if args.command in {"ingest", "demo"}:
            items = output if type(output) is list else [output]
            output = [{key: item[key] for key in ("post_id", "version_id", "status", "reason", "cached",
                                                  "run_id", "task_ids", "execution_authorized", "demonstration_only")
                       if key in item} for item in items]
        print(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (ValueError, TypeError, OSError, sqlite3.Error):
        # Errors may originate from untrusted input or filesystem paths. Do not echo them.
        print("Citizen command failed; check the input contract and local configuration.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
