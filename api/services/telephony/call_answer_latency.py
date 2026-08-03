"""AI voice call startup latency: time between call-answer and first AI audio.

Pure instrumentation — never influences call routing or pipeline behavior.
The two timestamps are produced in different processes (the freeswitch-manager
process knows when a call is answered; the api process runs the pipecat
pipeline and knows when the bot's first audio frame goes out), so they're
correlated through a short-lived Redis key keyed by workflow_run_id.
"""

import json
import time
from datetime import UTC, datetime
from typing import Optional

import redis.asyncio as aioredis
from loguru import logger

from api.constants import REDIS_URL

_ANSWER_KEY_PREFIX = "callstartup:answer:"
_ANSWER_KEY_TTL = 300  # seconds — first AI audio should always follow well within this
_SAMPLES_KEY = "callstartup:delays_ms"
_SAMPLES_MAX = 1000  # rolling window used for the aggregate percentile stats

_redis_client: Optional[aioredis.Redis] = None


async def _get_redis() -> aioredis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = await aioredis.from_url(REDIS_URL, decode_responses=True)
    return _redis_client


def _fmt(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%H:%M:%S.%f")[:-3]


async def record_call_answered(workflow_run_id: int | str, call_id: str) -> None:
    """Record the moment a call is answered (e.g. right after uuid_answer)."""
    r = await _get_redis()
    payload = json.dumps({"t": time.time(), "call_id": call_id})
    await r.set(f"{_ANSWER_KEY_PREFIX}{workflow_run_id}", payload, ex=_ANSWER_KEY_TTL)


async def record_first_ai_audio(workflow_run_id: int | str) -> Optional[dict]:
    """Record the first bot-audio frame of a call: logs and stores the sample.

    Returns the computed record, or None if no matching record_call_answered()
    timestamp was found for this workflow_run_id (e.g. a provider that doesn't
    report answer time) — callers should treat that as "metric unavailable",
    not an error.
    """
    r = await _get_redis()
    key = f"{_ANSWER_KEY_PREFIX}{workflow_run_id}"
    raw = await r.get(key)
    if raw is None:
        return None
    await r.delete(key)  # only the first AI audio per call is measured

    answered = json.loads(raw)
    answered_at = float(answered["t"])
    first_audio_at = time.time()
    delay_ms = (first_audio_at - answered_at) * 1000

    record = {
        "call_id": answered.get("call_id") or str(workflow_run_id),
        "answered_at": _fmt(answered_at),
        "first_audio_at": _fmt(first_audio_at),
        "ai_start_delay_ms": round(delay_ms),
    }
    logger.info(f"[call-startup-latency] {json.dumps(record)}")

    await r.lpush(_SAMPLES_KEY, f"{delay_ms:.2f}")
    await r.ltrim(_SAMPLES_KEY, 0, _SAMPLES_MAX - 1)

    stats = await get_latency_stats()
    if stats:
        logger.info(f"[call-startup-latency-stats] {json.dumps(stats)}")

    return record


async def get_latency_stats() -> Optional[dict]:
    """Aggregate stats (ms) over the rolling sample window: avg/min/max/p50/p95/p99."""
    r = await _get_redis()
    raw = await r.lrange(_SAMPLES_KEY, 0, -1)
    if not raw:
        return None
    samples = sorted(float(v) for v in raw)
    n = len(samples)

    def pct(p: float) -> float:
        idx = min(n - 1, max(0, round(p * (n - 1))))
        return samples[idx]

    return {
        "sample_count": n,
        "avg_ms": round(sum(samples) / n, 1),
        "min_ms": round(samples[0], 1),
        "max_ms": round(samples[-1], 1),
        "p50_ms": round(pct(0.50), 1),
        "p95_ms": round(pct(0.95), 1),
        "p99_ms": round(pct(0.99), 1),
    }
