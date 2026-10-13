"""The bot loop: read -> react -> act.

    perceive.py  READ   platform reads -> candidate posts
    decide.py    REACT  post + our side -> Decision (pure, no I/O)
    compose.py          reply text + pre-publish check (placeholder for the LLM)
    loop.py      ACT    executes decisions through the Actuator, one cycle at a time

All platform calls go through `ctn.actuator.Actuator`, never the client directly.
"""

from .decide import alignment, decide, relevance
from .loop import BotLoop, CycleReport
from .perceive import DEFAULT_KEYWORDS, Observation, perceive

__all__ = [
    "DEFAULT_KEYWORDS",
    "BotLoop",
    "CycleReport",
    "Observation",
    "alignment",
    "decide",
    "perceive",
    "relevance",
]
