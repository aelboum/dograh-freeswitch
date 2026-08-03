"""Breakdown of AI-pipeline startup latency into named phases.

Investigates the gap between the AI pipeline worker starting and the first
AI audio reaching the caller (see call_answer_latency for the earlier,
cross-process call-answered -> first-audio metric). Every checkpoint here is
relative to pipeline start, in the same process, so a plain in-memory dict
keyed by workflow_run_id is enough — no Redis needed.

Pure measurement: never influences pipeline behavior. `mark()` is
first-occurrence-wins so it's safe to call repeatedly (e.g. once per turn)
from generic per-frame hooks — only the first turn's timings, the ones that
matter for startup latency, are ever recorded.
"""

import json
import time
from datetime import UTC, datetime
from typing import Optional

from loguru import logger

_trackers: dict[int | str, "_StartupLatencyTracker"] = {}


class _StartupLatencyTracker:
    def __init__(self, workflow_run_id: int | str) -> None:
        self.workflow_run_id = workflow_run_id
        self._started_at = time.time()
        self._t0 = time.monotonic()
        self._checkpoints: dict[str, float] = {}

    def mark(self, name: str) -> None:
        if name in self._checkpoints:
            return
        self._checkpoints[name] = (time.monotonic() - self._t0) * 1000

    def finish(self) -> dict:
        record = {
            "workflow_run_id": self.workflow_run_id,
            "pipeline_start": datetime.fromtimestamp(
                self._started_at, tz=UTC
            ).strftime("%H:%M:%S.%f")[:-3],
            **{
                f"{name}_ms": round(elapsed_ms)
                for name, elapsed_ms in self._checkpoints.items()
            },
        }
        logger.info(f"[pipeline-startup-latency] {json.dumps(record)}")
        return record


def start_tracking(workflow_run_id: int | str) -> None:
    """Begin tracking startup latency for a call. Call once per call, as
    early as possible in the AI pipeline's own setup path."""
    _trackers[workflow_run_id] = _StartupLatencyTracker(workflow_run_id)


def mark(workflow_run_id: int | str, name: str) -> None:
    """Record the elapsed time (ms) since start_tracking() under `name`, the
    first time this checkpoint is reached for this call. No-op if tracking
    was never started (e.g. call ended before setup completed) or the call
    already finished."""
    tracker = _trackers.get(workflow_run_id)
    if tracker is not None:
        tracker.mark(name)


def finish(workflow_run_id: int | str) -> Optional[dict]:
    """Log and return the full checkpoint breakdown, then stop tracking this
    call. Returns None if tracking was never started for this call."""
    tracker = _trackers.pop(workflow_run_id, None)
    if tracker is None:
        return None
    return tracker.finish()
