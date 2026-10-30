"""Replace banned accounts without a human in the loop.

2026-10-30: we lost 3-4 accounts a day (52 of 102 registered were suspended) and every
replacement cost a human: register, flip the dead persona to standby, flip the new one to
active, and restart the fleet. The restart was the expensive part: a suspended account keeps
`account_states.mode='active'`, so the 50-active-persona cap rejects the replacement until the
dead persona is parked (`set_persona_modes` counts every mode='active' row).

Ban detection is passive. The actuator already writes `status='failed', detail='account
suspended'` when the platform rejects a write, and `reconcile` keeps that row. This module
turns that row into a replacement:

1. park the dead persona (`standby` + `is_banned`), which frees its active slot;
2. register an unbound persona from the same occupation group (nearest group if the same one
   is exhausted), capped per day so a mass ban cannot burn the 200-account allowance;
3. bind, mark the managed account active, and let the loop pick it up.

`PersonaRegistry.list_all()` re-reads bindings on every call and `BotLoop.refresh_accounts()`
runs each cycle, so the new account starts acting without a restart. Its first post is still
the normal `initialise` worker's job.
"""

from __future__ import annotations

import re
import time
from typing import Any

# The platform's own wording when a write is refused because the account is gone.
_SUSPENDED_DETAIL = re.compile(r"suspend|banned|deactivated|disabled|no longer active", re.I)

DEFAULT_POLICY: dict[str, Any] = {
    "enabled": True,
    "interval_seconds": 120,
    "max_per_step": 1,
    "max_per_day": 6,
    "detect_window_seconds": 21600,
    "max_undeleted": 160,
    "notes": (
        "2026-10-30: 52 of 102 registered accounts suspended; each replacement used to need a "
        "human plus a fleet restart. max_per_day keeps a mass ban from spending the 200-account "
        "allowance in one afternoon, and max_undeleted leaves room under that same ceiling "
        "(suspended accounts still count as undeleted)."
    ),
}


def policy(store) -> dict[str, Any]:
    return {**DEFAULT_POLICY, **(store.policy("roster", {}) or {})}


def suspended_details(store, *, since_s: float = 21600, now: float | None = None) -> list[dict]:
    """Accounts whose last failed action carried a suspension detail, newest first."""
    ts = time.time() if now is None else float(now)
    with store._lock:
        rows = store._conn.execute(
            """SELECT account_id, MAX(created_at) AS last_at, COUNT(*) AS n, MAX(detail) AS detail
               FROM action_intents
               WHERE status='failed' AND account_id<>'' AND created_at>?
               GROUP BY account_id ORDER BY last_at DESC, account_id""",
            (ts - float(since_s),),
        ).fetchall()
    return [dict(r) for r in rows if _SUSPENDED_DETAIL.search(str(r["detail"] or ""))]


def still_live(store, account: str) -> bool:
    """True while we still treat the account as an active, unbanned member of the fleet."""
    state = store.get_account_state(account)
    if state.is_banned or state.mode != "active":
        return False
    row = store.get_managed_account(account) or {}
    return row.get("status", "active") == "active"


def pending_replacements(store, *, since_s: float = 21600, now: float | None = None) -> list[dict]:
    """Suspension details for accounts we have not retired yet."""
    return [row for row in suspended_details(store, since_s=since_s, now=now) if still_live(store, row["account_id"])]


def retire(store, account: str, *, now: float | None = None) -> str:
    """Park a dead account: ban flag, managed status, and the persona's active slot."""
    ts = time.time() if now is None else float(now)
    persona_id = next((pid for pid, acc in store.persona_bindings().items() if acc == account), "")
    with store.batch_transaction():
        store.set_account_banned(account, True)
        store.mark_managed_account_status(account, "suspended", now=ts)
    if persona_id:
        try:
            store.set_persona_modes([persona_id], "standby")
        except ValueError:
            # Already parked, or the persona left the active catalog: the ban flags are what matter.
            pass
    return persona_id


def pick_persona(store, registry, *, group: str = "") -> Any:
    """An unbound persona, preferring the dead account's group, else the thinnest group."""
    bound = set(store.persona_bindings().values())
    free = [p for p in registry.list_all() if p.account_id not in bound or not p.account_id]
    if not free:
        return None
    same = [p for p in free if p.occupation_group == group]
    if same:
        return same[0]
    counts: dict[str, int] = {}
    for p in registry.list_all():
        if p.account_id:
            counts[p.occupation_group] = counts.get(p.occupation_group, 0) + 1
    return sorted(free, key=lambda p: (counts.get(p.occupation_group, 0), p.persona_id))[0]


def handle_of(persona) -> str:
    """The platform handle the catalog reserves for a persona (`candidate_username`)."""
    return str((persona.profile or {}).get("candidate_username") or persona.account_id or "").strip()


def log(store) -> list[dict]:
    rows = store.policy("roster_log", []) or []
    return list(rows)


def _count_today(store, *, now: float) -> int:
    start = now - (now % 86400) + 11 * 3600  # Sydney midnight, since the campaign day does the same
    return sum(1 for row in log(store) if float(row.get("at") or 0) >= start)


async def replace_suspended(
    store,
    registry,
    client,
    *,
    password: str,
    max_active: int = 50,
    policy: dict | None = None,
    now: float | None = None,
    replace= None,
) -> dict:
    """Register a replacement for every newly suspended account this step allows.

    `replace` is `ctn.accounts.register_one`; it is a parameter so tests can run without a
    platform client.
    """
    from .accounts import register_one

    pol = {**DEFAULT_POLICY, **(policy or {})}
    ts = time.time() if now is None else float(now)
    out = {"retired": [], "registered": [], "skipped": [], "disabled": not pol["enabled"]}
    if not pol["enabled"]:
        return out
    found = pending_replacements(store, since_s=float(pol["detect_window_seconds"]), now=ts)
    if not found:
        return out
    budget = int(pol["max_per_day"]) - _count_today(store, now=ts)
    for row in found[: int(pol["max_per_step"])]:
        account = row["account_id"]
        if budget <= 0:
            out["skipped"].append({"account": account, "reason": "daily_cap"})
            continue
        persona_id = retire(store, account, now=ts)
        group = ""
        dead = registry.get(persona_id) if persona_id else None
        if dead is not None:
            group = dead.occupation_group
        candidate = pick_persona(store, registry, group=group)
        if candidate is None:
            out["retired"].append({"account": account, "persona": persona_id})
            out["skipped"].append({"account": account, "reason": "no_free_persona"})
            continue
        outcome = await (replace or register_one)(
            store,
            client,
            candidate.persona_id,
            handle_of(candidate),
            password,
            max_active=int(pol["max_undeleted"]),
            max_total=int(pol["max_undeleted"]),
            display_name=candidate.name,
        )
        out["retired"].append({"account": account, "persona": persona_id})
        if outcome.get("status") != "succeeded":
            out["skipped"].append({"account": account, "reason": str(outcome.get("status")), "handle": handle_of(candidate)})
            continue
        handle = handle_of(candidate)
        store.mark_managed_account_status(handle, "active", now=ts)
        try:
            store.set_persona_modes([candidate.persona_id], "active", max_active=max_active)
        except ValueError:
            pass  # already active from registration
        entry = {
            "at": ts,
            "from": account,
            "persona": candidate.persona_id,
            "to": handle,
            "group": candidate.occupation_group,
            "same_group": candidate.occupation_group == group,
        }
        with store.batch_transaction():
            store.set_policy("roster_log", (log(store) + [entry])[-50:])
        out["registered"].append(entry)
        budget -= 1
    return out


async def roster_step(store, registry, client, *, password: str, max_active: int = 50, now: float | None = None) -> dict:
    """Worker body: one bounded replacement pass."""
    pol = policy(store)
    return await replace_suspended(
        store, registry, client, password=password, max_active=max_active, policy=pol, now=now
    )
