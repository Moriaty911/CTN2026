"""Campaign talking points and framing guidance for Kingstonia H2D2 Referendum.

Provides side-specific, purpose-aligned framing guidance to ground P01 (support),
P02 (direct answer), P03 (argument/media framing), P07 (concern empathy), and
P08 (clarification) replies without fabricating external citations.
"""

from __future__ import annotations

SIDE_CORE_FRAMING: dict[str, str] = {
    "yes": (
        "Campaign Stance (YES): Support sensible public health protections and H2D2 vaccination "
        "readiness to keep Kingstonia's transit, hospitals, schools, and local economy safe from "
        "Dolphin Flu disruptions."
    ),
    "no": (
        "Campaign Stance (NO): Defend voluntary healthcare decisions, informed consent, and "
        "proportionality—opposing coercive mandates that exclude healthy residents from transit, "
        "work, or daily life in Kingstonia."
    ),
}

PURPOSE_SIDE_GUIDANCE: dict[str, dict[str, str]] = {
    "yes": {
        "P01": (
            "Reinforce the author's point by connecting preventative H2D2 measures to everyday "
            "community resilience—keeping clinics uncrowded, buses safe, and businesses open."
        ),
        "P02": (
            "Answer the question directly and calmly from a pro-public-health perspective, "
            "using only verified supplied policy details; do not invent medical exemptions or statistics."
        ),
        "P03": (
            "Acknowledge the point raised, then gently pivot or counter by showing why proactive "
            "prevention beats reactive hospital crises and lock-downs."
        ),
        "P07": (
            "Validate the author's practical concern with genuine empathy first, then reassure them "
            "about how orderly preventative measures protect vulnerable families and commuters."
        ),
        "P08": (
            "Ask a sharp, civil clarifying question that highlights the real-world public health or "
            "community safety stakes of the issue."
        ),
    },
    "no": {
        "P01": (
            "Reinforce the author's point by connecting personal medical autonomy with practical "
            "fairness for workers, commuters, and small businesses."
        ),
        "P02": (
            "Answer the question directly and calmly from a pro-choice perspective, explaining why "
            "voluntary guidance and doctor-patient trust work better than blanket restrictions."
        ),
        "P03": (
            "Acknowledge the point raised, then gently pivot or counter by showing why top-down "
            "exclusion rules hurt everyday residents without proportionate justification."
        ),
        "P07": (
            "Validate the author's practical concern with genuine empathy first, then emphasize that "
            "people deserve transparent information and personal choice rather than coercion."
        ),
        "P08": (
            "Ask a sharp, civil clarifying question that probes proportionality, individual rights, "
            "or practical enforcement burdens."
        ),
    },
}


def get_campaign_guidance(side: str, purpose: str) -> str:
    """Return concise campaign stance and purpose-specific framing guidance."""
    norm_side = (side or "").strip().lower()
    if norm_side not in SIDE_CORE_FRAMING:
        return ""
    core = SIDE_CORE_FRAMING[norm_side]
    specific = PURPOSE_SIDE_GUIDANCE.get(norm_side, {}).get(purpose, "")
    if specific:
        return f"{core} {specific}"
    return core


__all__ = ["SIDE_CORE_FRAMING", "PURPOSE_SIDE_GUIDANCE", "get_campaign_guidance"]
