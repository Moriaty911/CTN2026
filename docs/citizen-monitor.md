# SQLite citizen monitor

Implemented on `robot-citizen` on 2026-10-13. This branch contains the JEV library,
not the newer stage1 perception/generation/actuator application. No stage1 code
was merged. The monitor accepts the existing `JevInput` and `Post` boundaries.

## Boundaries

This is an independent observation and simulation service. It persists identity
evidence, public expression estimates, synthetic reader experiments, local alerts,
and review tasks. It does not identify hidden NPC state, predict official points,
publish, report, fetch arbitrary URLs, or replace confirmed JEV action rules.
There is no official-platform collector in this branch. Supply records through
the existing adapter or JSON files; feed polling remains an upstream task using
documented, rate-limited platform interfaces. A bot's feed is not an NPC's feed.

## Data model

SQLite schema version 1 uses WAL, foreign keys, a 5-second busy timeout, atomic
write transactions, indexes, and private database files. Runtime databases and
their journals stay outside Git. The SQLite backup API includes committed WAL
data; copying only a live `.sqlite3` file is not a reliable backup procedure.

| Records | Meaning |
|---|---|
| `accounts` | Current identity projection and revision; unknown is a real state |
| `identity_evidence`, `identity_retractions` | Append-only evidence and explicit corrections |
| `posts`, `post_versions`, `post_observations` | Author binding, content/context versions, observation times and known metrics |
| `stance_observations` | Publicly expressed position estimates, quoted evidence, confidence, model and time |
| `simulation_runs`, `simulation_samples`, `llm_calls` | Exact experimental configuration, independent readings and persistent call budget |
| `alerts` | Shadow influence candidates, calibrated active alerts and diagnostic review records |
| `action_tasks` | Durable review candidates and downstream result records |

Account kind (`own/competitor/citizen/unknown`), team affiliation (`yes/no/unknown`),
and the author's publicly expressed stance are separate. Own accounts require
team registry evidence. Confirmation of other identities requires supplied
official moderation or verified platform evidence with a reference. These source
names are declarations by the trusted caller, not proof that an API exposing
identity exists. Manual/model guesses remain inferred. Moderation confirmation
does not establish faction. Conflicting confirmations become `conflicted` until
an explicit evidence retraction corrects them; all evidence remains auditable.

## Experiment and thresholds

Six fixed personas cover firm/soft support, two undecided perspectives and
firm/soft opposition. They are a test panel, not a population sample. Each persona
starts from the same state in each call. Two repetitions each run independent
control/read requests with alternating call order. The target is absent from
control requests. Only antecedent parent/quoted context is included in the shared
baseline; later discussion comments are excluded. This is a local hypothetical
reading context, not evidence of any NPC's actual reading history.

The reader returns three stance scales, certainty, turnout, a brief reason and a
bound target quotation. Scales are local estimates, not probabilities. Program
code computes `read - control`. Public-access direction drives the first alarm
version; general and annual vaccination changes are retained separately because
their direction does not automatically establish referendum direction.

Defaults are **exploratory engineering settings**:

- mode `shadow`;
- absolute directional delta strictly above `0.2`;
- repeat spread at most `0.15`, with both repetitions crossing the same directional threshold;
- at least two affected personas for threat/opportunity;
- opposing effects across personas also produce a polarization candidate;
- 30-minute post freshness window; historical posts only populate public observations;
- 96 model calls per Sydney calendar day; one ordinary known-author assessment costs 25 calls (24 readings plus one extraction).

Thus the default budget allows only three complete known-author assessments daily,
with remaining capacity potentially producing an incomplete run. This is a small
pilot, not a full-platform throughput setting. Configure the daily budget/panel
deliberately; use upstream priorities and caching. Each failed/reserved call still
counts, and incomplete results cannot produce an influence alarm. Unknown
publication time is reported as a limitation. Freshness defaults are not official
competition timings.

Repeated content/context snapshots reuse completed runs across restarts. Metrics
and source observation time do not invalidate model results. Text/context, source
kind, panel, reader, prompt, injection policy, team side, or alarm policy changes
do invalidate the cache. Failed runs remain visible and require explicit retry;
unexpired running jobs cannot be claimed by another worker. Retry of an abandoned
run after 30 minutes is explicit and may duplicate a paid call from the old process.
Operate one serial monitoring worker in the first deployment. SQLite coordinates
budget and claims across connections, but it is not a distributed worker manager.

## Public attitude observations

The optional extraction call quotes only the target author's public text. Missing,
neutral, ambiguous or mixed positions use null values. Confidence is a model
estimate, not calibrated accuracy. Expression changes compare chronological,
high-confidence observations from the same model version. Historical arrival
order does not overwrite newer positions. A change alert means **public expression
changed; the cause and hidden opinion are unknown**. A simulation never updates
public attitude observations or confirms an account identity.

## Run locally

Python 3.14 is the project's supported interpreter. No new dependencies are needed.
Commands run from the repository root:

```sh
python3 -m ctn.citizen init
python3 -m ctn.citizen --db state/citizen-demo.sqlite3 demo --side yes
python3 -m ctn.citizen --db state/citizen-demo.sqlite3 alerts
python3 -m ctn.citizen --db state/citizen-demo.sqlite3 tasks
```

`demo` uses an explicit synthetic fixture, makes no network calls, and proves
wiring only. It cannot be used to assess platform content.

`ingest` accepts a `jev-input.v1` object or an array; this command stores and routes
without LLM calls:

```sh
python3 -m ctn.citizen ingest --file incoming-posts.json --side yes
```

Register known accounts using a JSON array passed to `identities --file`:

```json
[
  {
    "account_id": "actual-platform-account-id",
    "kind": "own",
    "side": "yes",
    "status": "confirmed",
    "source": "team_registry",
    "reference": "local-team-account-registration-record",
    "observed_at": "2026-10-13T20:00:00+11:00"
  }
]
```

For live simulations, populate the value-free `.env.example` fields in the local
environment: `CTN_CITIZEN_LLM_URL` (full HTTPS chat-completions endpoint),
`CTN_CITIZEN_LLM_API_KEY`, and `CTN_CITIZEN_LLM_MODEL`. The provider must support
the OpenAI-compatible chat JSON envelope, `response_format=json_object`, and the
configured `max_tokens`. No provider endpoint, model or credential is assumed.
Compatibility still needs a real-service smoke test; none was performed here.

```sh
python3 -m ctn.citizen ingest --file incoming-posts.json --side yes --live
```

Only explicit `--live` or direct construction of `ChatReader` enables paid calls.
There are no automatic HTTP retries. Errors are recorded as fixed codes; raw
provider error bodies and credentials are not printed or stored. Known credentials
and recognizable credential patterns are rejected before input persistence/model
use. Reuse existing `InjectionPreflight` for input and model-output review. These
local checks have the same heuristic limitations as the existing JEV defenses.

## Calibrate before active alarms

Collect at least 20 distinct, caller-verified null/control posts under the same
reader, panel and repetition count. Retain the resulting completed run IDs in a
JSON array, then run:

```sh
python3 -m ctn.citizen calibrate --run-ids-file null-run-ids.json \
  --reference local-reviewed-null-set-v1 --output citizen-policy.local.json
```

The helper uses the empirical 99th percentile of per-post maximum absolute panel
delta and writes a **shadow** policy. Twenty runs is a minimum wiring guard, not a
statistical guarantee. Caller-designated null labels must be reviewed. This only
estimates local noise; it does not validate NPC prediction or response benefit.
Review false positives and compare with JEV-only priorities and whatever official
feedback is actually available. Aggregate official logs cannot prove per-post
causality. After review, explicitly set `mode` to `active` in the generated policy:

```sh
python3 -m ctn.citizen ingest --file incoming-posts.json --side yes --live \
  --policy-file citizen-policy.local.json
```

Active mode requires a calibration reference and an exact reader/panel/prompt/
preflight/repetition fingerprint match. A changed model cannot silently reuse old
calibration. Active alarms enter the local `alerts` table; external notification
and autonomous publishing/reporting are separate integrations.

## Integrate with existing code

```python
from ctn.citizen import CitizenMonitor, CitizenStore, content_from_post

with CitizenStore("state/citizen.sqlite3") as store:
    monitor = CitizenMonitor(store, assigned_side="yes")
    # Existing pipeline Post; stable ID is supplied by the real adapter.
    content = content_from_post(post, source_kind="platform", author_id=stable_author_id)
    result = monitor.process(content)
    combined = monitor.evaluate_with_jev(jev_workflow, content)
    handoff = combined["jev"]  # unchanged existing HandoffPackage
```

Use one path for a given event; the example illustrates two alternative entry
points. For the stage1 perception loop, pass each collected Post into this bridge;
fetch direct parents/quoted context upstream and attach it to a validated JevInput
before evaluating replies. Display handles, `author_is_npc`, mock `stance`, and
mock profile `kind` are not used to establish identity. Missing stable ID stays
unknown. The composition helper runs JEV first, then bounded monitoring; use a
separate worker/queue for live monitoring so its 25 calls do not delay posting.

Matching creates `own_content_review`, `competitor_content_review`,
`citizen_dialogue_review`, `identity_review`, or generic `content_review`. Confirmed
opposing competitors also get `report_review` with explicit missing reporting
prerequisites. Active stable alarms add `alarm_response_review`. These tasks do
not bypass JEV, material/output review, account allocation, platform limits or
downstream action deduplication.

Task lifecycle: `candidate -> reviewing -> handed_off -> executed/failed`.
`needs_materials` can return to `reviewing`; cancellation is explicit. Identity or
post-version changes block stale handoff. Executed status requires a downstream
platform result ID; an already-performed result can still be recorded if later
observations arrived. This is local result accounting, not execution permission.
Policy/identity revisions may create a new review task, so the final executor must
still enforce its own semantic action deduplication.

```sh
python3 -m ctn.citizen status
python3 -m ctn.citizen backup --destination /tmp/citizen-backup.sqlite3
python3 -m unittest tests.test_citizen_monitor -q
python3 scripts/check_env.py --tests
```

Do not commit the live database, local corpora, credentials or calibration files.
Real NPC prediction accuracy, live provider compatibility, official platform
collection and final execution integration remain unvalidated.
