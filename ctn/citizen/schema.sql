CREATE TABLE IF NOT EXISTS accounts (
 account_id TEXT PRIMARY KEY, kind TEXT NOT NULL DEFAULT 'unknown', side TEXT NOT NULL DEFAULT 'unknown',
 status TEXT NOT NULL DEFAULT 'unknown', revision INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS identity_evidence (
 evidence_id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(account_id),
 kind TEXT NOT NULL, side TEXT NOT NULL, status TEXT NOT NULL, source TEXT NOT NULL,
 reference TEXT NOT NULL, observed_at TEXT NOT NULL, observed_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS identity_retractions (
 evidence_id TEXT PRIMARY KEY REFERENCES identity_evidence(evidence_id), reason TEXT NOT NULL,
 reference TEXT NOT NULL, recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
 post_id TEXT PRIMARY KEY, account_id TEXT REFERENCES accounts(account_id)
);
CREATE TABLE IF NOT EXISTS post_versions (
 version_id TEXT PRIMARY KEY, post_id TEXT NOT NULL REFERENCES posts(post_id),
 content_json TEXT NOT NULL, created_at TEXT, created_ts REAL
);
CREATE TABLE IF NOT EXISTS post_observations (
 observation_id TEXT PRIMARY KEY, version_id TEXT NOT NULL REFERENCES post_versions(version_id),
 observed_at TEXT NOT NULL, observed_ts REAL NOT NULL, source_kind TEXT NOT NULL, metrics_json TEXT
);
CREATE INDEX IF NOT EXISTS observation_times ON post_observations(observed_ts);
CREATE TABLE IF NOT EXISTS stance_observations (
 stance_id TEXT PRIMARY KEY, version_id TEXT NOT NULL REFERENCES post_versions(version_id),
 account_id TEXT NOT NULL REFERENCES accounts(account_id), topic TEXT NOT NULL, value REAL,
 label TEXT NOT NULL, confidence REAL NOT NULL, evidence_quote TEXT NOT NULL,
 model_version TEXT NOT NULL, observed_at TEXT NOT NULL, observed_ts REAL NOT NULL,
 CHECK(value IS NULL OR (value >= -1 AND value <= 1)), CHECK(confidence >= 0 AND confidence <= 1)
);
CREATE INDEX IF NOT EXISTS stance_history ON stance_observations(account_id, topic, observed_ts);
CREATE TABLE IF NOT EXISTS simulation_runs (
 run_id TEXT PRIMARY KEY, cache_key TEXT NOT NULL, version_id TEXT NOT NULL REFERENCES post_versions(version_id),
 status TEXT NOT NULL, started_ts REAL NOT NULL, completed_ts REAL,
 config_json TEXT NOT NULL, result_json TEXT, error_code TEXT
);
CREATE INDEX IF NOT EXISTS simulation_cache ON simulation_runs(cache_key, started_ts);
CREATE TABLE IF NOT EXISTS simulation_samples (
 run_id TEXT NOT NULL REFERENCES simulation_runs(run_id), persona_id TEXT NOT NULL,
 repeat INTEGER NOT NULL, branch TEXT NOT NULL, result_json TEXT NOT NULL,
 PRIMARY KEY(run_id, persona_id, repeat, branch)
);
CREATE TABLE IF NOT EXISTS llm_calls (
 call_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES simulation_runs(run_id),
 campaign_date TEXT NOT NULL, state TEXT NOT NULL, created_ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS call_budget ON llm_calls(campaign_date);
CREATE TABLE IF NOT EXISTS alerts (
 alert_id TEXT PRIMARY KEY, version_id TEXT NOT NULL REFERENCES post_versions(version_id),
 run_id TEXT REFERENCES simulation_runs(run_id), kind TEXT NOT NULL, mode TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'open', details_json TEXT NOT NULL, created_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS action_tasks (
 task_id TEXT PRIMARY KEY, version_id TEXT NOT NULL REFERENCES post_versions(version_id),
 account_id TEXT REFERENCES accounts(account_id), kind TEXT NOT NULL, policy_fingerprint TEXT NOT NULL,
 identity_revision INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'candidate', reason TEXT NOT NULL,
 prerequisites_json TEXT NOT NULL, result_json TEXT, created_ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS task_queue ON action_tasks(status, created_ts);
