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


def stale_slots(store) -> list[str]:
    """Accounts already marked suspended that still hold an active persona slot.

    This is the state the old manual flow left behind: `managed_accounts.status` says suspended but
    `account_states.mode` still says active, so the account occupies one of the 50 slots and never acts.
    """
    out = []
    for row in store.list_managed_accounts(status="suspended"):
        account = row["username"]
        if store.get_account_state(account).mode == "active":
            out.append(account)
    return out


def orphan_bindings(store) -> list[tuple[str, str]]:
    """Personas bound to a handle that has no managed account: a failed registration left them stuck."""
    out = []
    for persona_id, account in store.persona_bindings().items():
        if account and store.get_managed_account(account) is None:
            out.append((persona_id, account))
    return out


def adopt(store, account: str, persona_id: str = "", *, now=None) -> dict:
    """Bring a live account back into service after a suspension was cleared.

    2026-11-02: a suspended account still answers `get_mentions`, so only a write proves liveness. Calling
    this on a probe that used reads brought a dead account back for one failed like, so the reserve list
    must stay empty unless a write has succeeded since the suspension.
    """
    ts = time.time() if now is None else float(now)
    row = store.get_managed_account(account)
    if row is None:
        raise ValueError("unknown account " + account)
    binding = {pid: acct for pid, acct in store.persona_bindings().items() if acct == account}
    persona = persona_id or next(iter(binding), "")
    with store.batch_transaction():
        store._conn.execute("UPDATE managed_accounts SET status='active', updated_at=? WHERE username=?", (ts, account))
        store._conn.execute(
            "UPDATE account_states SET is_banned=0, mode='active' WHERE account_id=?", (account,)
        )
    if persona:
        try:
            store.set_persona_modes([persona], "active", max_active=50)
        except ValueError:
            pass
    entry = {"at": ts, "account": account, "persona": persona, "adopted": True}
    with store.batch_transaction():
        store.set_policy("roster_log", (log(store) + [entry])[-50:])
    return entry


def reserve(store) -> list[dict]:
    return list(store.policy("roster_reserve", []) or [])


def set_reserve(store, rows) -> None:
    with store.batch_transaction():
        store.set_policy("roster_reserve", list(rows))


def active_count(store) -> int:
    return len(store.list_managed_accounts(status="active"))


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


def skipped_personas(store, *, now=None):
    """Personas whose registration failed recently, so a bad handle cannot loop forever."""
    ts = time.time() if now is None else float(now)
    out = {}
    for row in store.policy("roster_skip", []) or []:
        fails = int(row.get("fails") or 1)
        pause = min(86400.0, 900.0 * fails)
        if ts - float(row.get("at") or 0) < pause:
            out[row["persona"]] = row
    return out


def note_skip(store, persona, handle, status, *, now=None):
    ts = time.time() if now is None else float(now)
    rows = list(store.policy("roster_skip", []) or [])
    previous = next((row for row in rows if row.get("persona") == persona), None)
    fails = int((previous or {}).get("fails") or 0) + 1
    rows = [row for row in rows if row.get("persona") != persona]
    rows.append({"persona": persona, "handle": handle, "status": status, "at": ts, "fails": fails})
    with store.batch_transaction():
        store.set_policy("roster_skip", rows[-50:])


def pick_persona(store, registry, *, group: str = "", now=None) -> Any:
    """An unbound persona, preferring the dead account's group, else the thinnest group."""
    bound = set(store.persona_bindings().values())
    skip = skipped_personas(store, now=now)
    free = [p for p in registry.list_all()
            if (p.account_id not in bound or not p.account_id) and p.persona_id not in skip]
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


async def _register(store, client, candidate, *, password, pol, ts, replace, budget):
    """Register one persona-account pair and activate it. Returns (ok, entry_or_reason)."""
    from .accounts import register_one

    handle = handle_of(candidate)
    outcome = await (replace or register_one)(
        store,
        client,
        candidate.persona_id,
        handle,
        password,
        max_active=int(pol["max_undeleted"]),
        max_total=int(pol["max_undeleted"]),
        display_name=candidate.name,
    )
    if outcome.get("status") != "succeeded":
        return False, {"reason": str(outcome.get("status")), "handle": handle}
    store.mark_managed_account_status(handle, "active", now=ts)
    try:
        store.set_persona_modes([candidate.persona_id], "active", max_active=int(pol.get("active_target") or 50))
    except ValueError:
        pass  # the cap is full: the account stays standby until a slot frees, which is the correct order
    entry = {
        "at": ts,
        "persona": candidate.persona_id,
        "to": handle,
        "group": candidate.occupation_group,
    }
    with store.batch_transaction():
        store.set_policy("roster_log", (log(store) + [entry])[-50:])
    return True, entry


async def replace_suspended(
    store,
    registry,
    client,
    *,
    password: str,
    max_active: int = 50,
    policy: dict | None = None,
    now: float | None = None,
    replace=None,
) -> dict:
    """One bounded pass: park stale slots, replace new suspensions, top the fleet back up.

    `replace` is `ctn.accounts.register_one`; it is a parameter so tests run without a platform client.
    """
    pol = {**DEFAULT_POLICY, **(policy or {})}
    pol["active_target"] = max_active
    ts = time.time() if now is None else float(now)
    out = {"parked": [], "retired": [], "registered": [], "skipped": [], "disabled": not pol["enabled"]}
    if not pol["enabled"]:
        return out

    for persona_id, account in orphan_bindings(store):
        with store.batch_transaction():
            store._conn.execute("DELETE FROM persona_bindings WHERE persona_id=? AND account_id=?", (persona_id, account))
        out["parked"].append({"account": account, "persona": persona_id, "orphan": True})

    # A suspended account that still holds an active slot blocks its replacement (the 50-persona cap
    # counts every mode='active' row), so park those first; this is idempotent.
    for account in stale_slots(store):
        out["parked"].append({"account": account, "persona": retire(store, account, now=ts)})

    budget = int(pol["max_per_day"]) - _count_today(store, now=ts)
    for row in pending_replacements(store, since_s=float(pol["detect_window_seconds"]), now=ts)[
        : int(pol["max_per_step"])
    ]:
        account = row["account_id"]
        if budget <= 0:
            out["skipped"].append({"account": account, "reason": "daily_cap"})
            continue
        persona_id = retire(store, account, now=ts)
        out["retired"].append({"account": account, "persona": persona_id})
        dead = registry.get(persona_id) if persona_id else None
        group = dead.occupation_group if dead is not None else ""
        candidate = pick_persona(store, registry, group=group, now=ts)
        if candidate is None:
            out["skipped"].append({"account": account, "reason": "no_free_persona"})
            continue
        ok, entry = await _register(
            store, client, candidate, password=password, pol=pol, ts=ts, replace=replace, budget=budget
        )
        if not ok:
            note_skip(store, candidate.persona_id, handle_of(candidate), str(entry.get("reason")), now=ts)
            out["skipped"].append({"account": account, **entry})
            continue
        out["registered"].append({**entry, "from": account, "same_group": entry["group"] == group})
        budget -= 1

    # Top up: a ban wave or a manual retirement can leave the fleet below the active target, and the
    # campaign window does not wait for the next suspension to be noticed.
    if pol.get("top_up", True):
        held = reserve(store)
        while held and active_count(store) < int(max_active):
            candidate = held.pop(0)
            account = candidate.get("account", "")
            if store.get_managed_account(account) is None:
                continue
            out["registered"].append(adopt(store, account, candidate.get("persona", ""), now=ts))
        set_reserve(store, held)
        while budget > 0 and active_count(store) < int(max_active):
            candidate = pick_persona(store, registry, now=ts)
            if candidate is None:
                out["skipped"].append({"reason": "no_free_persona", "where": "top_up"})
                break
            ok, entry = await _register(
                store, client, candidate, password=password, pol=pol, ts=ts, replace=replace, budget=budget
            )
            if not ok:
                note_skip(store, candidate.persona_id, handle_of(candidate), str(entry.get("reason")), now=ts)
                out["skipped"].append({"where": "top_up", **entry})
                break
            out["registered"].append({**entry, "from": "", "same_group": False})
            budget -= 1
    return out


async def roster_step(store, registry, client, *, password: str, max_active: int = 50, now: float | None = None) -> dict:
    """Worker body: one bounded pass. Never raises into the service loop."""
    try:
        return await replace_suspended(
            store, registry, client, password=password, max_active=max_active, policy=policy(store), now=now
        )
    except Exception as exc:  # registration is a real action; a failure must not stop the fleet
        return {"error": type(exc).__name__, "detail": str(exc)[:200], "registered": []}
