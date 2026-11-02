"""The bot loop: read -> react -> act.

    perceive.py  READ   platform reads -> candidate posts
    decide.py    REACT  post + our side -> Decision (pure, no I/O)
    compose.py          reply text + pre-publish check (placeholder for the LLM)
    loop.py      ACT    executes decisions through the Actuator, one cycle at a time

All platform calls go through `ctn.actuator.Actuator`, never the client directly.
"""

from .brain import BrainVerdict, JevLlmBrain
from .crawl import (
    CommentCrawler,
    CommentReport,
    FrontlineCrawler,
    FrontlineReport,
    audit_suspected_account,
    run_periodic,
)
from .decide import alignment, decide, relevance
from .loop import BotLoop, CommentStepReport, CycleReport
from .news_crawler import NewsSitesCrawler
from .perceive import DEFAULT_KEYWORDS, Observation, perceive

__all__ = [
    "BrainVerdict",
    "JevLlmBrain",
    "CommentCrawler",
    "CommentReport",
    "CommentStepReport",
    "FrontlineCrawler",
    "FrontlineReport",
    "NewsSitesCrawler",
    "DEFAULT_KEYWORDS",
    "BotLoop",
    "CycleReport",
    "Observation",
    "alignment",
    "audit_suspected_account",
    "decide",
    "perceive",
    "relevance",
    "run_periodic",
]


