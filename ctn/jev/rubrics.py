"""Explicit access to versioned competition rubrics; no active routing policy.

Post rubrics evaluate one JevInput-shaped record. Comparison rubrics evaluate
a record containing ``current`` and ``historical`` JevInput-shaped records.
T04 owns validated state packing. T06/T07 own answer availability and routing.
The JSON descriptions are a first-version baseline, not measured outcomes.
"""

from pathlib import Path
from typing import Literal

from .questions import QuestionSet, QuestionValidationError


def load_rubric(name: Literal["post", "comparison"]) -> QuestionSet:
    """Load a named baseline explicitly; never enables service calls or actions."""
    if type(name) is not str or name not in {"post", "comparison"}:
        raise QuestionValidationError("name", "enum", "unsupported rubric")
    return QuestionSet.load(Path(__file__).with_name("rubrics") / f"{name}.v1.json")


def relative_stance(
    policy_stance: Literal["yes", "no", "mixed", "neutral", "unclear"],
    assigned_side: Literal["yes", "no", "unknown"] | None,
) -> Literal["aligned", "opposing", "mixed", "neutral", "unknown"]:
    """Map an expressed referendum position to the supplied team assignment.

    Does not infer the author's affiliation. Never guesses our assigned side.
    """
    if type(policy_stance) is not str or policy_stance not in {"yes", "no", "mixed", "neutral", "unclear"}:
        raise QuestionValidationError("policy_stance", "enum", "unsupported referendum position")
    if assigned_side is not None and (type(assigned_side) is not str or assigned_side not in {"yes", "no", "unknown"}):
        raise QuestionValidationError("assigned_side", "enum", "unsupported team assignment")
    if assigned_side in {None, "unknown"} or policy_stance == "unclear":
        return "unknown"
    if policy_stance in {"mixed", "neutral"}:
        return policy_stance
    return "aligned" if policy_stance == assigned_side else "opposing"
