"""Structured logging.

Two sinks, deliberately:

* JSONL to disk -- machine-readable, one record per event, safe to grep and to
  post-process. This is the raw material for the closed calibration loop.
* A compact human console stream -- so that at 3am during the run we can see
  what the fleet is doing without parsing JSON.

We never log secrets, and platform content is logged as data with its origin
marked, because competition rule 6.4 says prompts and outputs may be collected
for review and we would rather our logs be legible than voluminous.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

from .models import to_jsonable


class JsonlWriter:
    """Append-only JSONL sink. Flushes every record so a crash never loses the
    last decision -- those last decisions are exactly what we need after a
    restart."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8")

    def write(self, record: dict[str, Any]) -> None:
        record.setdefault("ts", time.time())
        self._fh.write(json.dumps(to_jsonable(record), ensure_ascii=False) + "\n")
        self._fh.flush()

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass


class Telemetry:
    """Facade over the sinks, with one writer per event class so log rotation
    and post-hoc analysis stay simple."""

    def __init__(self, log_dir: Path, level: str = "INFO") -> None:
        log_dir.mkdir(parents=True, exist_ok=True)
        self.decisions = JsonlWriter(log_dir / "decisions.jsonl")
        self.actions = JsonlWriter(log_dir / "actions.jsonl")
        self.observations = JsonlWriter(log_dir / "observations.jsonl")
        self.guard = JsonlWriter(log_dir / "guard.jsonl")
        self.system = JsonlWriter(log_dir / "system.jsonl")

        self._log = logging.getLogger("ctn")
        self._log.setLevel(getattr(logging, level, logging.INFO))
        if not self._log.handlers:
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s",
                                  datefmt="%H:%M:%S")
            )
            self._log.addHandler(handler)
            self._log.propagate = False

    # ---- convenience ----------------------------------------------------- #
    def info(self, msg: str, **fields: Any) -> None:
        self._log.info(msg)
        self.system.write({"level": "info", "msg": msg, **fields})

    def warn(self, msg: str, **fields: Any) -> None:
        self._log.warning(msg)
        self.system.write({"level": "warn", "msg": msg, **fields})

    def error(self, msg: str, **fields: Any) -> None:
        self._log.error(msg)
        self.system.write({"level": "error", "msg": msg, **fields})

    def metric(self, name: str, **fields: Any) -> None:
        self.system.write({"level": "metric", "name": name, **fields})

    def close(self) -> None:
        for writer in (self.decisions, self.actions, self.observations,
                       self.guard, self.system):
            writer.close()
