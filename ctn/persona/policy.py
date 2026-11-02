"""Versioned fixed routing/expansion policy, shared by all production callers."""
import hashlib
import json
from pathlib import Path

REPLY_POLICY=json.loads((Path(__file__).resolve().parents[2]/'policies/reply-expansion-v1.json').read_text())
REPLY_POLICY_HASH=hashlib.sha256(json.dumps(REPLY_POLICY,sort_keys=True,separators=(',',':')).encode()).hexdigest()
