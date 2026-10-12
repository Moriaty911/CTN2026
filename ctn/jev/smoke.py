"""Explicit live transport check: python3 -m ctn.jev.smoke.

Uses locally configured credentials, one synthetic v2 deep request, zero retries.
Checks wire parsing, prints metadata/issue codes only; no accuracy measurement.
"""

from dataclasses import replace
from collections import Counter
from datetime import datetime, timezone
import json

from ..config import JevConfig
from .client import JevClient
from .packing import JevPacker
from .parsing import JevResponseParser
from .rubrics import load_rubric


def main() -> int:
    try:
        config = replace(JevConfig.load(), max_retries=0)
    except (ValueError, OSError, UnicodeError):
        print(json.dumps({"status": "error", "error_code": "invalid_local_configuration"}))
        return 1
    rubric = load_rubric("post", version="v2")
    packed = JevPacker(model=config.model, forbidden_values=(config.api_key,) if config.api_key else ()).pack_post({
        "schema_version": "jev-input.v1",
        "post": {"post_id": "synthetic-transport-smoke", "text": "I support voluntary H2D2 vaccination, but oppose requiring vaccination proof to enter public spaces. I am willing to reconsider if someone explains the evidence for this access requirement."},
        "source": {"kind": "synthetic", "observed_at": datetime.now(timezone.utc).isoformat()},
        "task_context": {"assigned_side": "yes"},
    }, stage="deep", question_set=rubric)
    result = JevClient(config).evaluate(packed)
    parsed = JevResponseParser().parse(result, question_set=rubric)
    print(json.dumps({"schema_version": "jev-live-smoke.v1", "synthetic_only": True,
        "recorded_at": datetime.now(timezone.utc).isoformat(), "max_retries": 0,
        "call": result.to_dict(), "assessment_status": parsed.status,
        "answer_status_counts": dict(Counter(a.status for a in parsed.output.answers.values())) if parsed.output else {},
        "answer_issue_codes": dict(Counter(a.issue.code for a in parsed.output.answers.values() if a.issue)) if parsed.output else {},
        "parsing_issue_codes": [issue.code for issue in parsed.issues],
        "business_accuracy_validated": False}, ensure_ascii=False, sort_keys=True))
    return 0 if parsed.status == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
