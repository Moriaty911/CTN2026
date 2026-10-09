"""Explicit live transport check: python3 -m ctn.jev.smoke.

Uses locally configured credentials, one synthetic triage request, zero retries.
Prints only call metadata; does not validate business answers or save raw JSON.
"""

from dataclasses import replace
from datetime import datetime, timezone
import json

from ..config import JevConfig
from .client import JevClient
from .packing import JevPacker


def main() -> int:
    try:
        config = replace(JevConfig.load(), max_retries=0)
    except (ValueError, OSError, UnicodeError):
        print(json.dumps({"status": "error", "error_code": "invalid_local_configuration"}))
        return 1
    packed = JevPacker(model=config.model, forbidden_values=(config.api_key,) if config.api_key else ()).pack_post({
        "schema_version": "jev-input.v1",
        "post": {"post_id": "synthetic-transport-smoke", "text": "Can someone explain what this proposal would change?"},
        "source": {"kind": "synthetic", "observed_at": datetime.now(timezone.utc).isoformat()},
    })
    result = JevClient(config).evaluate(packed)
    print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))
    return 0 if result.status == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
