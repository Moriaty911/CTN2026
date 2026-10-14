"""Curated native Kingstonia everyday life seed posts for account warmup."""

from __future__ import annotations


WARMUP_SEEDS: dict[str, tuple[str, ...]] = {
    # --- YES side personas ---
    "bot_worker_yes": (
        "Traffic on the coastal ring road was brutal this morning. At least Crown Harbour has a nice ocean breeze today.",
        "Heading down to New Haven for the Mango & Spice Festival with the kids this weekend. Anyone know if the ferries are running on schedule?",
        "Early morning bus shift done. Coffee at the terminal cafe never tasted so good.",
    ),
    "bot_medic_yes": (
        "Finished a 12-hour shift at Kingstonia Central Hospital. Nothing beats that first cup of black coffee. Stay warm tonight, everyone.",
        "Seasonal flu numbers always creep up around this time of year in the islands. Keep those windows ventilated and stay hydrated.",
        "A quiet evening walk along the marina after rounds. Grateful for our dedicated nursing team today.",
    ),
    "bot_policy_yes": (
        "Browsing through the new municipal archive additions at Kingstonia University library. Always fascinating to see how local ordinances evolved.",
        "Catching up on National Assembly gazette notices from the 2024 session. The public transit planning records are particularly detailed.",
        "Quiet afternoon on campus before the semester seminar series kicks off.",
    ),
    "bot_debater_yes": (
        "Watching the National Assembly debate live stream. Some of these policy arguments could really use a basic logic check.",
        "Great turnout at the Crown Harbour community forum yesterday. Civic participation is what keeps Kingstonia moving forward.",
        "Reading through the local op-eds over breakfast. Healthy debate is the cornerstone of any democracy.",
    ),
    # --- NO side personas ---
    "bot_worker_no": (
        "Delivered three crates across the south bay today. Fuel prices are up again, but at least the weather is holding up.",
        "Family dinner in New Haven tonight. Support your local island diners and family-run shops!",
        "Long day driving the ring road. Sunset over Crown Harbour makes the traffic a little more bearable.",
    ),
    "bot_medic_no": (
        "Another busy clinic day in Crown Harbour. Individual patient wellness always starts with listening to personal concerns.",
        "Reminding patients today: fresh air, proper sleep, and balanced nutrition are the first line of everyday defense.",
        "Enjoying a brief pause between consultations. Looking forward to the medical society symposium next month.",
    ),
    "bot_policy_no": (
        "Reviewing constitutional jurisprudence on emergency regulations at the law library. Proportionality remains the core principle.",
        "Interesting discussion in today's administrative law seminar on statutory limits of executive rulemaking.",
        "Archiving municipal council minutes from the past five years. Institutional memory matters for good governance.",
    ),
    "bot_debater_no": (
        "The beauty of Kingstonia has always been its independent spirit. Let's make sure community voices don't get drowned out.",
        "Reading the morning papers. Vigorous civic scrutiny of every public proposal is what keeps institutions honest.",
        "Enjoying a strong cup of coffee while reviewing recent public policy proposals.",
    ),
}


def get_warmup_seed(account_id: str, index: int = 0) -> str:
    """Return a curated warmup post seed for the specified account."""
    seeds = WARMUP_SEEDS.get(account_id)
    if not seeds:
        return "Enjoying a quiet afternoon in Kingstonia. Beautiful breeze across the island today."
    return seeds[index % len(seeds)]


def list_warmup_seeds(account_id: str) -> list[str]:
    """Return all available warmup post seeds for an account."""
    return list(WARMUP_SEEDS.get(account_id, ()))
