#!/usr/bin/env bash
set -euo pipefail
exec python - <<'PY_HEALTH'
import json,os,time
from pathlib import Path
p=Path(os.getenv('CTN_STATE_DIR','/state'))/'heartbeat'
try:
    data=json.loads(p.read_text())
    healthy=data['alive'] and not data['error'] and time.time()-data['timestamp']<float(os.getenv('CTN_HEARTBEAT_MAX_AGE_S','300'))
except (OSError,ValueError,KeyError): healthy=False
raise SystemExit(0 if healthy else 1)
PY_HEALTH
