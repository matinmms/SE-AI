from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator

import redis
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger("tesanote.stage_metrics")

# Phase 18: compact, bounded visibility into REAL per-stage execution
# concurrency (as opposed to logical PROCESSING admission, which
# `PROCESSING_MAX_CONCURRENT_JOBS` governs). Each stage task brackets its own
# execution with `StageConcurrencyStore.track(...)`, which records itself in
# a Redis sorted set scored by start time. Reads prune any entry older than
# a generous max-task-duration bound before counting, so a worker that dies
# mid-task (SIGKILL/OOM, `finally` never runs) cannot leak the counter
# forever — this is the self-healing mechanism the operations dashboard
# relies on instead of a hard TTL key, which cannot represent "N concurrently
# active items" on its own.
#
# This is an operational signal only. PostgreSQL (`LectureProcessingJob`)
# remains the sole authority for job state; nothing here is read by the
# scheduler, capacity controller, or heartbeat/recovery logic.
_ACTIVE_KEY_PREFIX = "tesanote:stage-active:"
_PROVIDER_KEY_PREFIX = "tesanote:provider-active:"
_QUEUED_KEY_PREFIX = "tesanote:stage-queued:"
_DURATION_KEY_PREFIX = "tesanote:stage-durations:"
_DEFAULT_STALE_AFTER_SECONDS = 600
# Phase 18 (queue-age + service-time model): a queued-but-never-started entry
# is almost certainly a lost/never-consumed message, not real queue age, past
# this bound — pruned on read for the same self-healing reason as the active
# set. Recent-duration samples are capped to a small bounded list per queue
# (not an unbounded/permanent time series) purely to compute p50/p95.
_QUEUED_STALE_AFTER_SECONDS = 1800
_MAX_DURATION_SAMPLES = 200


class StageConcurrencyStore:
    """Tracks real concurrently-executing task/provider-call counts per stage."""

    @staticmethod
    def _client() -> redis.Redis:
        return redis.Redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
        )

    @staticmethod
    def _broker_client() -> redis.Redis:
        return redis.Redis.from_url(
            settings.CELERY_BROKER_URL,
            decode_responses=True,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
        )

    @classmethod
    def _stale_after(cls) -> int:
        configured = getattr(
            settings, "STAGE_CONCURRENCY_STALE_SECONDS", _DEFAULT_STALE_AFTER_SECONDS
        )
        return max(30, int(configured))

    @classmethod
    def _record_start(cls, *, key_prefix: str, name: str, token: str) -> None:
        try:
            cls._client().zadd(f"{key_prefix}{name}", {token: timezone.now().timestamp()})
        except Exception:  # pragma: no cover - defensive, never fail the caller
            logger.warning("stage_metrics_start_failed", extra={"stage_or_provider_name": name})

    @classmethod
    def _record_end(cls, *, key_prefix: str, name: str, token: str) -> None:
        try:
            cls._client().zrem(f"{key_prefix}{name}", token)
        except Exception:  # pragma: no cover - defensive
            logger.warning("stage_metrics_end_failed", extra={"stage_or_provider_name": name})

    @classmethod
    def _active_count(cls, *, key_prefix: str, name: str) -> int:
        key = f"{key_prefix}{name}"
        cutoff = timezone.now().timestamp() - cls._stale_after()
        try:
            client = cls._client()
            client.zremrangebyscore(key, "-inf", cutoff)
            return int(client.zcard(key))
        except Exception:  # pragma: no cover - defensive, fail closed to 0
            logger.warning("stage_metrics_read_failed", extra={"stage_or_provider_name": name})
            return 0

    @classmethod
    def record_enqueued(cls, *, queue: str, task_id: str) -> None:
        """Called at dispatch time (before `.apply_async`/`.delay`/`send_task`)
        so `oldest_queued_age` can report real wait time, not just LLEN depth.
        """
        cls._record_start(key_prefix=_QUEUED_KEY_PREFIX, name=queue, token=task_id)

    @classmethod
    def _clear_queued(cls, *, queue: str, task_id: str) -> None:
        cls._record_end(key_prefix=_QUEUED_KEY_PREFIX, name=queue, token=task_id)

    @classmethod
    def oldest_queued_age_seconds(cls, *, queue: str) -> float | None:
        """Age in seconds of the longest-waiting still-queued entry, or None
        if nothing is queued (or the entry was enqueued before this metric
        existed / Redis is unavailable) — never a false 0.
        """
        key = f"{_QUEUED_KEY_PREFIX}{queue}"
        now = timezone.now().timestamp()
        cutoff = now - _QUEUED_STALE_AFTER_SECONDS
        try:
            client = cls._client()
            client.zremrangebyscore(key, "-inf", cutoff)
            oldest = client.zrange(key, 0, 0, withscores=True)
        except Exception:  # pragma: no cover - defensive
            logger.warning(
                "stage_metrics_queue_age_read_failed", extra={"stage_or_provider_name": queue}
            )
            return None
        if not oldest:
            return None
        return max(0.0, now - oldest[0][1])

    @classmethod
    def record_service_duration(cls, *, queue: str, duration_seconds: float) -> None:
        """Append one execution-duration sample (bounded list, newest first)
        for p50/p95 service-time reporting. Not a permanent time series —
        trimmed to `_MAX_DURATION_SAMPLES` on every write.
        """
        key = f"{_DURATION_KEY_PREFIX}{queue}"
        try:
            client = cls._client()
            client.lpush(key, duration_seconds)
            client.ltrim(key, 0, _MAX_DURATION_SAMPLES - 1)
            client.expire(key, cls._stale_after() * 4)
        except Exception:  # pragma: no cover - defensive
            logger.warning(
                "stage_metrics_duration_record_failed", extra={"stage_or_provider_name": queue}
            )

    @classmethod
    def service_duration_percentiles(cls, *, queue: str) -> dict[str, float | None]:
        key = f"{_DURATION_KEY_PREFIX}{queue}"
        try:
            raw = cls._client().lrange(key, 0, _MAX_DURATION_SAMPLES - 1)
        except Exception:  # pragma: no cover - defensive
            logger.warning(
                "stage_metrics_duration_read_failed", extra={"stage_or_provider_name": queue}
            )
            return {"p50": None, "p95": None, "sample_size": 0}
        values = sorted(float(v) for v in raw)
        if not values:
            return {"p50": None, "p95": None, "sample_size": 0}

        def _pct(fraction: float) -> float:
            index = min(len(values) - 1, int(round(fraction * (len(values) - 1))))
            return round(values[index], 2)

        return {"p50": _pct(0.5), "p95": _pct(0.95), "sample_size": len(values)}

    @classmethod
    @contextlib.contextmanager
    def track_stage(cls, *, queue: str, task_id: str) -> Iterator[None]:
        cls._clear_queued(queue=queue, task_id=task_id)
        cls._record_start(key_prefix=_ACTIVE_KEY_PREFIX, name=queue, token=task_id)
        started_at = timezone.now().timestamp()
        try:
            yield
        finally:
            cls._record_end(key_prefix=_ACTIVE_KEY_PREFIX, name=queue, token=task_id)
            cls.record_service_duration(
                queue=queue, duration_seconds=timezone.now().timestamp() - started_at
            )

    @classmethod
    def stage_active_count(cls, *, queue: str) -> int:
        return cls._active_count(key_prefix=_ACTIVE_KEY_PREFIX, name=queue)

    @classmethod
    def stage_active_counts(cls, *, queues: list[str]) -> dict[str, int]:
        return {queue: cls.stage_active_count(queue=queue) for queue in queues}

    @classmethod
    @contextlib.contextmanager
    def track_provider_call(cls, *, provider: str, call_id: str) -> Iterator[None]:
        cls._record_start(key_prefix=_PROVIDER_KEY_PREFIX, name=provider, token=call_id)
        try:
            yield
        finally:
            cls._record_end(key_prefix=_PROVIDER_KEY_PREFIX, name=provider, token=call_id)

    @classmethod
    def provider_active_count(cls, *, provider: str) -> int:
        return cls._active_count(key_prefix=_PROVIDER_KEY_PREFIX, name=provider)

    @classmethod
    def queue_depth(cls, *, queue: str) -> int | None:
        """Waiting-message count for a Celery/Redis-broker queue (LLEN).

        Returns None (not 0) on any broker read failure, so callers can
        distinguish "confirmed empty" from "could not be read" rather than
        silently reporting a false healthy zero.
        """
        try:
            return int(cls._broker_client().llen(queue))
        except Exception:  # pragma: no cover - defensive
            logger.warning("stage_metrics_queue_depth_failed", extra={"queue": queue})
            return None

    @classmethod
    def queue_depths(cls, *, queues: list[str]) -> dict[str, int | None]:
        return {queue: cls.queue_depth(queue=queue) for queue in queues}
