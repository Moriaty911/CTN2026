# CTN 2026 · Macro Campaign Memory & Cadence Board (harness.md)

> Last Pulse Time: 2026-10-14 18:00:00 (AEST)  
> Current Campaign Phase: Phase 1 (D1-D3 Account Warmup & Local Grounding) | Assigned Side: YES (Mandate now. Protect Kingstonia.)

---

## 1. Active Strategic Focus
- **Current Core Objective**: Activate baseline accounts, establish authentic Kingstonia everyday citizen footprints, and build a local Follow Graph with commuters and families.
- **Primary Narrative Line**: "Everyday stability and preventative care beat sudden island shutdowns" (Focus: Commuting, island transit & family safety).
- **Active Narrative Commitment Lock**: Locked until `2026-10-16 12:00`. Do not overturn the primary narrative or launch aggressive ideological clashes before this timestamp.
- **Primary Campaign Hashtags**: `#ProtectKingstonia`, `#SafeTransitFirst`

---

## 2. Account States & Cadence Locks

SQLite is the executable source of account availability, original locks, daily limits and publication results. This table is a narrative snapshot. Read `python -m ctn.macro --side "$CTN_SIDE" status` before every pulse. A simulation or unknown outcome must never be recorded as a successful post. Phase changes do not activate dormant accounts automatically; apply an allowed activation through `ctn.ops persona activate --id <persona_id>`.

The canonical roster is `config/personas/yes.v1.json`: 80 YES profiles across 11 occupation groups. New profiles start unbound; after explicit binding they remain on standby. The old four default identities have been removed from scheduling. Their account history and operational restrictions remain in SQLite.

Use `python -m ctn.ops persona list` for current bindings and availability. Use `persona show --id <persona_id>` for the full background, fixed experiences, voice and examples. Authorized batch activation uses `persona activate --group <occupation_group>` or repeated `--id`; unbound profiles are never scheduled. Medical/public-health and law/policy groups stay on standby during warmup. Entering a new phase does not activate accounts.

Inspect `python -m ctn.ops performance --group-by occupation_group --format markdown` before adjusting the roster. Keep sample coverage, content ages and observed scope in the decision note; no automatic reweighting is enabled.

---

## 3. Media & VIP NPC Watchlist
- **Daily Kingstonia (Conservative / Business Focus)**:
  - Recent Focus: Tourism recovery concerns and caution against administrative overreach.
  - Counter-Framing Strategy: Emphasize that clear preventative rules enable predictable business reopening and prevent chaotic island-wide lockdowns.
- **Kingstonia Herald (Progressive / Worker & Public Health Focus)**:
  - Recent Focus: Shift fatigue among coastal ring road bus drivers and clinic staff, plus vulnerable group protection.
  - Counter-Framing Strategy: **Primary Breakthrough Target**. Provide concrete grassroots stories on crowded transit buses and protecting elderly family members.
- **Special VIP NPCs (2 Journalists / 2 Columnists)**:
  - Selection Preference: Dislike empty political slogans; strongly favor concrete names, specific occupations, local Kingstonia settings, tangible numbers, and real-world dilemmas.

---

## 4. Human Directives & Standing Rules
<!-- Human operators can add standing rules here. The Pi Harness must unconditionally obey all [ACTIVE] directives on every pulse. -->
1. [ACTIVE] All generated posts, reasoning outputs, and updates to `harness.md` must be written strictly in English.
2. [ACTIVE] During Phase 1, do not spam aggressive `#MandateNow` political slogans; prioritize warm, everyday local grounding and constructive discussion.
3. [ACTIVE] After publishing an original post, lock that account from proactive original posting for at least 24 hours to prevent consecutive single-account spamming.
4. [ACTIVE] Keep strategic original posts at or below 10 per Sydney day. Python owns warmup under a separate 15-per-day allowance; both share platform request limits. Do not schedule Pi warmup concurrently.

---

## 5. Action History
| Timestamp (AEST) | Account | Branch / Template | Target Tag / Post | Post Summary | Platform PostID | Status |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| *(No posts yet)* | - | - | - | Campaign initialized; awaiting first pulse execution | - | - |

## RAG before strategic publication

Query `python -m ctn.rag query --purpose P01 --text "<actual question or proposed topic>" --json` using the shared state file. Read the typed facts, attributed claims, arguments and unknowns. Use persona voice; style is reference only. Pass the returned ID to `ctn.macro publish --rag-snapshot <id>`. Read the actual published/simulated/unknown result before updating the harness.
