"""SQLite state with atomic writes, durable deduplication and append-only evidence."""

from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

from .domain import (CitizenError, IdentityEvidence, KINDS, TOPICS, dump, fingerprint,
                     number, safe_data, text, timestamp, utc_now)


class CitizenStore:
    def __init__(self, path="state/citizen.sqlite3", *, forbidden_values=(), clock=time.time):
        self.clock = clock
        self.forbidden_values = tuple(forbidden_values)
        self._lock = threading.RLock()
        if str(path) != ":memory:":
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            os.chmod(path, 0o600)
        self._db = sqlite3.connect(str(path), timeout=5, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version not in {0, 1}:
            self._db.close()
            raise CitizenError("unsupported_database_version")
        self._db.executescript(Path(__file__).with_name("schema.sql").read_text())
        self._db.execute("PRAGMA user_version=1")

    def close(self):
        with self._lock:
            self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @contextmanager
    def _write(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def _safe(self, data):
        return safe_data(data, self.forbidden_values)

    def _rows(self, sql, values=()):
        with self._lock:
            return [dict(row) for row in self._db.execute(sql, values)]

    @staticmethod
    def _ensure_account(db, account_id):
        db.execute("INSERT OR IGNORE INTO accounts(account_id) VALUES (?)", (account_id,))

    def account(self, account_id):
        rows = self._rows("SELECT * FROM accounts WHERE account_id=?", (account_id,))
        return rows[0] if rows else {"account_id": account_id, "kind": "unknown", "side": "unknown",
                                    "status": "unknown", "revision": 0}

    @staticmethod
    def _project_identity(db, account_id):
        rows = db.execute("SELECT e.* FROM identity_evidence e LEFT JOIN identity_retractions r "
                          "ON e.evidence_id=r.evidence_id WHERE e.account_id=? AND r.evidence_id IS NULL "
                          "ORDER BY e.observed_ts DESC, e.evidence_id", (account_id,)).fetchall()
        confirmed = [row for row in rows if row["status"] == "confirmed"]
        considered = confirmed or rows
        kinds = {row["kind"] for row in considered if row["kind"] != "unknown"}
        sides = {row["side"] for row in considered if row["side"] != "unknown"}
        conflict = len(kinds) > 1 or len(sides) > 1
        kind = next(iter(kinds)) if len(kinds) == 1 and not conflict else "unknown"
        side = next(iter(sides)) if len(sides) == 1 and not conflict else "unknown"
        status = "conflicted" if conflict else "confirmed" if confirmed else "inferred" if kinds else "unknown"
        db.execute("UPDATE accounts SET kind=?,side=?,status=?,revision=revision+1 WHERE account_id=?",
                   (kind, side, status, account_id))

    def record_identity(self, evidence: IdentityEvidence):
        if type(evidence) is not IdentityEvidence:
            raise CitizenError("invalid_identity_evidence")
        data = self._safe(asdict(evidence))
        eid = fingerprint(data)
        with self._write() as db:
            self._ensure_account(db, evidence.account_id)
            cursor = db.execute("INSERT OR IGNORE INTO identity_evidence VALUES (?,?,?,?,?,?,?,?,?)",
                                (eid, evidence.account_id, evidence.kind, evidence.side, evidence.status,
                                 evidence.source, evidence.reference, evidence.observed_at,
                                 timestamp(evidence.observed_at)))
            if cursor.rowcount:
                self._project_identity(db, evidence.account_id)
        return self.account(evidence.account_id)

    def retract_identity(self, evidence_id, *, reason, reference):
        self._safe({"reason": text(reason), "reference": text(reference)})
        with self._write() as db:
            row = db.execute("SELECT account_id FROM identity_evidence WHERE evidence_id=?", (evidence_id,)).fetchone()
            if row is None:
                raise CitizenError("unknown_identity_evidence")
            changed = db.execute("INSERT OR IGNORE INTO identity_retractions VALUES (?,?,?,?)",
                                 (evidence_id, reason, reference, utc_now())).rowcount
            if changed:
                self._project_identity(db, row[0])

    def identities(self, account_id):
        return self._rows("SELECT e.*, r.reference AS retraction_reference FROM identity_evidence e "
                          "LEFT JOIN identity_retractions r ON e.evidence_id=r.evidence_id WHERE account_id=?",
                          (account_id,))

    def ingest(self, content):
        """Accept a validated JevInput. Exclude metrics/observation time from model cache identity."""
        data = self._safe(content.to_dict())
        snapshot = {key: value for key, value in data.items() if key not in {"source", "metrics", "task_context"}}
        snapshot["source_kind"] = content.source.kind
        version_id = fingerprint(snapshot)
        post = content.post
        observed = content.source.observed_at
        obs_id = fingerprint({"version": version_id, "source": data["source"], "metrics": data["metrics"]})
        with self._write() as db:
            if post.author_id:
                self._ensure_account(db, post.author_id)
            row = db.execute("SELECT account_id FROM posts WHERE post_id=?", (post.post_id,)).fetchone()
            if row and row[0] and post.author_id and row[0] != post.author_id:
                raise CitizenError("post_author_mismatch")
            db.execute("INSERT OR IGNORE INTO posts VALUES (?,?)", (post.post_id, post.author_id))
            if post.author_id:
                db.execute("UPDATE posts SET account_id=COALESCE(account_id,?) WHERE post_id=?", (post.author_id, post.post_id))
            db.execute("INSERT OR IGNORE INTO post_versions VALUES (?,?,?,?,?)",
                       (version_id, post.post_id, dump(snapshot), post.created_at,
                        timestamp(post.created_at) if post.created_at else None))
            db.execute("INSERT OR IGNORE INTO post_observations VALUES (?,?,?,?,?,?)",
                       (obs_id, version_id, observed, timestamp(observed), content.source.kind,
                        dump(data["metrics"]) if data["metrics"] else None))
        return version_id

    def version(self, version_id):
        rows = self._rows("SELECT v.*,p.account_id FROM post_versions v JOIN posts p ON v.post_id=p.post_id "
                          "WHERE version_id=?", (version_id,))
        if not rows:
            raise CitizenError("unknown_post_version")
        result = rows[0]
        result["content"] = json.loads(result.pop("content_json"))
        return result

    def record_stance(self, version_id, *, topic, value, label, confidence, evidence_quote,
                      model_version, observed_at):
        """Store a textual observation, never convert it into identity evidence."""
        version = self.version(version_id)
        if not version["account_id"]:
            raise CitizenError("stance_requires_observed_author")
        if topic not in TOPICS:
            raise CitizenError("unknown_stance_topic")
        if value is not None:
            number(value, -1, 1)
        number(confidence, 0, 1)
        text(label)
        text(model_version)
        timestamp(observed_at)
        quote = text(evidence_quote)
        if quote not in version["content"]["post"]["text"]:
            raise CitizenError("stance_quote_not_in_target")
        record = self._safe({"version_id": version_id, "topic": topic, "value": value,
                             "label": label, "confidence": confidence, "evidence_quote": quote,
                             "model_version": model_version, "observed_at": observed_at})
        sid = fingerprint(record)
        with self._write() as db:
            db.execute("INSERT OR IGNORE INTO stance_observations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (sid, version_id, version["account_id"], topic, value, label, confidence, quote,
                        model_version, observed_at, timestamp(observed_at)))
        return sid

    def stance_history(self, account_id, topic="public_access"):
        return self._rows("SELECT * FROM stance_observations WHERE account_id=? AND topic=? "
                          "ORDER BY observed_ts, stance_id", (account_id, topic))

    def latest_stance(self, account_id, topic="public_access", *, min_confidence=.8):
        rows = self._rows("SELECT * FROM stance_observations WHERE account_id=? AND topic=? AND confidence>=? "
                          "ORDER BY observed_ts DESC, stance_id DESC LIMIT 1", (account_id, topic, min_confidence))
        return rows[0] if rows else None

    def begin_run(self, cache_key, version_id, config, *, retry=False):
        self._safe(config)
        with self._write() as db:
            rows = db.execute("SELECT * FROM simulation_runs WHERE cache_key=? ORDER BY started_ts DESC, rowid DESC",
                              (cache_key,)).fetchall()
            for row in rows:
                if row["status"] == "completed":
                    return dict(row), False
            if rows and (not retry or (rows[0]["status"] == "running" and self.clock() - rows[0]["started_ts"] < 1800)):
                return dict(rows[0]), False
            run_id = uuid4().hex
            db.execute("INSERT INTO simulation_runs(run_id,cache_key,version_id,status,started_ts,config_json) "
                       "VALUES (?,?,?,'running',?,?)", (run_id, cache_key, version_id, self.clock(), dump(config)))
            return dict(db.execute("SELECT * FROM simulation_runs WHERE run_id=?", (run_id,)).fetchone()), True

    def run(self, run_id):
        rows = self._rows("SELECT * FROM simulation_runs WHERE run_id=?", (run_id,))
        if not rows:
            raise CitizenError("unknown_simulation_run")
        result = rows[0]
        result["config"] = json.loads(result.pop("config_json"))
        result["result"] = json.loads(result.pop("result_json")) if result["result_json"] else None
        return result

    def reserve_call(self, run_id, daily_limit):
        campaign_date = datetime.fromtimestamp(self.clock(), ZoneInfo("Australia/Sydney")).date().isoformat()
        with self._write() as db:
            total = db.execute("SELECT COUNT(*) FROM llm_calls WHERE campaign_date=?", (campaign_date,)).fetchone()[0]
            if total >= daily_limit:
                raise CitizenError("daily_call_budget_exhausted")
            call_id = uuid4().hex
            db.execute("INSERT INTO llm_calls VALUES (?,?,?,'reserved',?)", (call_id, run_id, campaign_date, self.clock()))
        return call_id

    def finish_call(self, call_id, state):
        if state not in {"completed", "failed"}:
            raise CitizenError("invalid_call_state")
        with self._write() as db:
            db.execute("UPDATE llm_calls SET state=? WHERE call_id=?", (state, call_id))

    def save_sample(self, run_id, persona_id, repeat, branch, result):
        self._safe(result)
        with self._write() as db:
            db.execute("INSERT INTO simulation_samples VALUES (?,?,?,?,?)",
                       (run_id, persona_id, repeat, branch, dump(result)))

    def finish_run(self, run_id, *, status, result=None, error_code=None, alerts=()):
        if status not in {"completed", "needs_review", "failed", "budget_exhausted"}:
            raise CitizenError("invalid_run_status")
        self._safe({"result": result, "error": error_code, "alerts": list(alerts)})
        with self._write() as db:
            row = db.execute("SELECT version_id,status FROM simulation_runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None or row["status"] != "running":
                raise CitizenError("run_not_running")
            db.execute("UPDATE simulation_runs SET status=?,completed_ts=?,result_json=?,error_code=? WHERE run_id=?",
                       (status, self.clock(), dump(result) if result is not None else None, error_code, run_id))
            for alert in alerts:
                aid = fingerprint({"run": run_id, "kind": alert["kind"]})
                db.execute("INSERT OR IGNORE INTO alerts(alert_id,version_id,run_id,kind,mode,details_json,created_ts) "
                           "VALUES (?,?,?,?,?,?,?)", (aid, row[0], run_id, alert["kind"], alert["mode"], dump(alert), self.clock()))

    def add_alert(self, version_id, kind, mode, details):
        self._safe(details)
        aid = fingerprint({"version": version_id, "kind": kind, "mode": mode, "details": details})
        with self._write() as db:
            db.execute("INSERT OR IGNORE INTO alerts(alert_id,version_id,kind,mode,details_json,created_ts) VALUES (?,?,?,?,?,?)",
                       (aid, version_id, kind, mode, dump(details), self.clock()))
        return aid

    def enqueue(self, version_id, kind, policy_fingerprint, reason, prerequisites, *, identity_revision=None):
        version = self.version(version_id)
        account = self.account(version["account_id"]) if version["account_id"] else self.account(None)
        self._safe({"kind": text(kind), "reason": text(reason), "prerequisites": prerequisites})
        task_id = fingerprint({"version": version_id, "kind": kind, "policy": policy_fingerprint,
                               "identity_revision": account["revision"]})
        with self._write() as db:
            if version["account_id"]:
                current = db.execute("SELECT revision FROM accounts WHERE account_id=?", (version["account_id"],)).fetchone()[0]
                if current != account["revision"] or (identity_revision is not None and current != identity_revision):
                    raise CitizenError("task_identity_changed")
            db.execute("INSERT OR IGNORE INTO action_tasks(task_id,version_id,account_id,kind,policy_fingerprint,"
                       "identity_revision,reason,prerequisites_json,created_ts) VALUES (?,?,?,?,?,?,?,?,?)",
                       (task_id, version_id, version["account_id"], kind, policy_fingerprint, account["revision"],
                        reason, dump(prerequisites), self.clock()))
        return task_id

    def tasks(self, *, status=None):
        return self._rows("SELECT * FROM action_tasks" + (" WHERE status=?" if status else "") + " ORDER BY created_ts",
                          (status,) if status else ())

    def alerts(self, *, mode=None):
        return self._rows("SELECT * FROM alerts" + (" WHERE mode=?" if mode else "") + " ORDER BY created_ts",
                          (mode,) if mode else ())

    def transition_task(self, task_id, status, *, result=None):
        """Local review lifecycle only. There is no publishing/approval shortcut here."""
        transitions = {"candidate": {"reviewing", "cancelled"}, "reviewing": {"handed_off", "needs_materials", "cancelled"},
                       "needs_materials": {"reviewing", "cancelled"}, "handed_off": {"executed", "failed", "cancelled"},
                       "failed": {"reviewing", "cancelled"}}
        self._safe(result)
        with self._write() as db:
            row = db.execute("SELECT * FROM action_tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None or status not in transitions.get(row["status"], set()):
                raise CitizenError("invalid_task_transition")
            if status not in {"cancelled", "failed", "executed"} and row["account_id"]:
                revision = db.execute("SELECT revision FROM accounts WHERE account_id=?", (row["account_id"],)).fetchone()[0]
                if revision != row["identity_revision"]:
                    raise CitizenError("task_identity_changed")
            if status in {"reviewing", "handed_off"}:
                latest = db.execute("SELECT v.version_id FROM post_versions v JOIN post_observations o "
                                    "ON v.version_id=o.version_id WHERE v.post_id=(SELECT post_id FROM post_versions WHERE version_id=?) "
                                    "ORDER BY o.observed_ts DESC,o.rowid DESC LIMIT 1", (row["version_id"],)).fetchone()
                if latest and latest[0] != row["version_id"]:
                    raise CitizenError("task_post_version_changed")
            if status == "executed" and (type(result) is not dict or not result.get("platform_result_id")):
                raise CitizenError("execution_requires_platform_result")
            db.execute("UPDATE action_tasks SET status=?,result_json=? WHERE task_id=?",
                       (status, dump(result) if result is not None else None, task_id))

    def summary(self):
        tables = ("accounts", "identity_evidence", "post_versions", "stance_observations", "simulation_runs",
                  "llm_calls", "alerts", "action_tasks")
        return {table: self._rows("SELECT COUNT(*) AS n FROM " + table)[0]["n"] for table in tables}

    def backup(self, destination):
        """SQLite backup API includes committed WAL state; don't copy just the DB file."""
        target = Path(destination)
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(fd)
        db = sqlite3.connect(target)
        try:
            with self._lock:
                self._db.backup(db)
        finally:
            db.close()
