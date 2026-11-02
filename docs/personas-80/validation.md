# Validation record — 2026-10-18

This delivery is offline. No real platform account was registered or published to. The 80 source profiles remain unbound by default; binding fixtures used only temporary databases or the isolated mock container. Raw RAG content and reporting thresholds/weights were not changed. The reporter constructor now permits an empty roster, and an empty roster submits nothing.

## Regression and fault scenarios

The original 534 tests plus 37 new tests passed: **571 / 571**, locally and in the Docker verify stage. Local execution used Python 3.14.6, SQLite 3.53.3 and official SDK 0.5.0. Provider responses are scripted; these tests verify wiring and invariants, not the actual quality of JEV or generated replies.

Commands:

```bash
python -m unittest discover -s tests
python -m compileall -q ctn tests scripts
python -m ctn.ops persona validate
git diff --check
docker build --target verify -t ctn2026/verify:persona80-20261018 .
docker build --target runtime -t ctn2026/fleet:persona80-20261018 .
```

New coverage includes the exact occupation counts, 80 distinct names/handles and 320 distinct samples; invalid catalog rejection; binding/activation/capability rules; catalog mismatch; .90 expansion boundary and invalid optional answers; unchanged prior questions; compatible pools and repeated occupations with distinct accounts; four fixed slots; primary failure and unknown pauses; edit/expiry cancellation; owner leases, account occupancy and restart; duplicate-draft cancellation without refill; dry-run isolation; real two-process contention through a local HTTP server; publication and metric idempotency; unknown resolution; missing fields, negative changes and sampled ranking scope; sampling lease recovery; v3 migration failure/backup recovery; actual unbound service startup; simulated queued-draft reassessment; and conflicting platform post attribution.

Existing assertions changed for explicit reasons:

- Production defaults now contain 80 unbound YES profiles and no default NO/four-role fleet. Voice, harness and CLI tests reflect that. Old identities are injected only through `tests/legacy_personas.py` for historical fixtures.
- Fixed cross-archetype complementarity assertions now check occupation/scene routing. Cooldown, freeze, ban, quota and successful-execution accounting checks remain.
- Capability is explicitly assigned in fixtures; a medical profession no longer grants high capability.
- Production workflow tests expect `post.v3` and 19 deep questions. Explicit v1/v2 tests retain their previous questions and 0–4 boundaries. Invalid-version tests use v99 because v3 is now supported.
- Database assertions and migration-backup names move from v3 to v4. Tests verify old states and prior migration history survive.
- Unknown warmup personas fail instead of receiving a generic fallback identity. CLI simulation fixtures bind and activate a fresh account explicitly.

## Runtime and recovery

The actual service completed an offline one-cycle run with all 80 profiles unbound. A separate runtime container used `--network none`, `CTN_MODE=mock`, `CTN_DRY_RUN=true`, `CTN_SIDE=yes`, and fresh anonymous state/log volumes. Docker's automatic health status reached `healthy`; the health script also passed before and after restart.

A local-only binding `yes_001 → smoke_unused` remained `standby` with `standard` capability across SIGTERM shutdown and restart. Shutdown exit code was 0, OOM false, and the final heartbeat recorded `alive=false` with no error. Consistent backup and restoration passed `PRAGMA integrity_check`. The container contained **0 action intents, 0 publications and 0 HTTP write count**. Its temporary container and anonymous volumes were removed after validation.

A separate backup/restore test preserved a binding, freeze, counter, phase and schema v4. Migration failure tests left v3 and the original account state intact, with a consistent `.before-v4.bak`.

Images checked:

- `ctn2026/verify:persona80-20261018`: `sha256:de66e42911d79957d5877255a1a22dbf71c15c84d8148c933bc2467c70254c97`
- `ctn2026/fleet:persona80-20261018`: `sha256:8980e7a0364a6734bc7cf4903cdd881629622ddcf4a8d2490af5bb8412cbc1ab`

## Measured performance

Same machine, same synthetic distributions and benchmark script; previous comparison is `docs/repair-2026-10-17/benchmark.json`. Both use local temporary file databases and warm caches. Model latency, network latency, behavior quality and competition results are outside this measurement.

| Dataset | Previous bulk fill | Current bulk fill | Previous ingest 30 with detector | Current ingest 30 |
|---|---:|---:|---:|---:|
| 100,000 posts | 3.422 s | 3.363 s | 3.547 ms | 3.390 ms |
| 1,000,000 posts | 40.996 s | 43.886 s | 3.520 ms | 3.673 ms |

The million-row bulk fill was about 7% slower and the complete 30-post ingest about 4% slower, within the prior two-times ceiling. Common indexed query timings did not show a material regression in this run. First task-recovery scans still depend on the backlog size; warm-cache numbers do not promise all workloads will stay constant.

The added synthetic benchmark with all 80 profiles bound measured occupation routing at 0.523 ms median / 0.644 ms p95. A report over 10,000 publications and 50,000 post samples took 179.949 ms median. This synthetic roster explicitly allows 80 active accounts only in that temporary benchmark; the service's configured default cap remains 50.

Full data: [benchmark.json](benchmark.json). Machine-readable checks: [validation.json](validation.json).

## Remaining empirical questions

No live model or platform behavior was used to calibrate the .90 expansion gate, judge complementarity quality or estimate persona popularity. The 80 profiles are an initial controlled roster. Real engagement, suitable activation batches, additional-reply value and provider judgment reliability require later observations. Reports expose coverage and comparable sample ages; they do not update weights automatically or infer NPC-specific engagement, vote changes or competition revenue.
