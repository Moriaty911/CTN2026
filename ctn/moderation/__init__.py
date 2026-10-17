"""Stage 3 Blue-Team moderation: bot detection, virtual team clustering, and high-ROI reporting."""

from .detector import (
    BotDetector,
    DetectionResult,
    infer_post_side,
    shingle_jaccard,
    word_shingles,
)
from .reporter import BlueTeamReporter, ReportAttempt, ReportBatchResult

__all__ = [
    "BlueTeamReporter",
    "BotDetector",
    "DetectionResult",
    "ReportAttempt",
    "ReportBatchResult",
    "infer_post_side",
    "shingle_jaccard",
    "word_shingles",
]
