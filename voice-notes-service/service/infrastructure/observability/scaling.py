from __future__ import annotations

import json
import logging
import random
import resource
import sys
import time
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Any

from django.conf import settings

SCALING_BASELINE_MARKER = "SCALING_BASELINE_METRIC"
logger = logging.getLogger("tesanote.scaling")

_SAFE_METADATA_KEYS = frozenset(
    {
        "audio_duration_seconds",
        "active_slots",
        "available_slots",
        "available_memory_bytes",
        "cpu_pressure",
        "disk_free_bytes",
        "effective_capacity",
        "broker_wait_ms",
        "chunk_count",
        "error_class",
        "input_size_bytes",
        "job_id",
        "lightweight",
        "model",
        "processing_mode",
        "provider",
        "provider_latency_ms",
        "queue_wait_ms",
        "recommended_capacity",
        "retry_count",
        "recovered_count",
        "scheduler_wait_ms",
        "swap_used_bytes",
        "slide_count",
        "task_name",
        "execution_time_ms",
        "pending_wait_ms",
        # Phase 12: processing heartbeat/recovery.
        "outcome",
        "candidates",
        "recovered",
        "failed_permanently",
        "skipped",
        "call",
    }
)


def _cpu_time_ms(who: int) -> int:
    usage = resource.getrusage(who)
    return round((usage.ru_utime + usage.ru_stime) * 1000)


def _rss_mb() -> float:
    """Return the process high-water RSS without adding a production dependency."""
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports KiB; macOS reports bytes. Production runs on Linux.
    bytes_used = rss if sys.platform == "darwin" else rss * 1024
    return round(bytes_used / (1024 * 1024), 2)


def _sampling_enabled() -> bool:
    if not bool(getattr(settings, "SCALING_BASELINE_METRICS_ENABLED", False)):
        return False
    sample_rate = float(getattr(settings, "SCALING_BASELINE_METRICS_SAMPLE_RATE", 1.0) or 0)
    return sample_rate >= 1 or (sample_rate > 0 and random.random() < sample_rate)


class ScalingStageMeasurement(AbstractContextManager["ScalingStageMeasurement"]):
    """Emit one privacy-safe timing record while preserving the wrapped operation."""

    def __init__(
        self,
        *,
        stage: str,
        lecture_id: str | None = None,
        task_id: str | None = None,
        **metadata: Any,
    ) -> None:
        invalid_keys = set(metadata).difference(_SAFE_METADATA_KEYS)
        if invalid_keys:
            raise ValueError(f"Unsafe scaling metric metadata keys: {sorted(invalid_keys)}")
        self.stage = stage
        self.lecture_id = str(lecture_id or "")
        self.task_id = str(task_id or "")
        self.metadata = {key: value for key, value in metadata.items() if value is not None}
        self.enabled = _sampling_enabled()
        self._started_at: datetime | None = None
        self._started_perf: float | None = None
        self._cpu_before_ms = 0
        self._child_cpu_before_ms = 0
        self._rss_before_mb = 0.0
        self._failed = False
        self._error_type = ""

    def __enter__(self) -> ScalingStageMeasurement:
        if self.enabled:
            self._started_at = datetime.now(UTC)
            self._started_perf = time.perf_counter()
            self._cpu_before_ms = _cpu_time_ms(resource.RUSAGE_SELF)
            self._child_cpu_before_ms = _cpu_time_ms(resource.RUSAGE_CHILDREN)
            self._rss_before_mb = _rss_mb()
        return self

    def add(self, **metadata: Any) -> None:
        invalid_keys = set(metadata).difference(_SAFE_METADATA_KEYS)
        if invalid_keys:
            raise ValueError(f"Unsafe scaling metric metadata keys: {sorted(invalid_keys)}")
        self.metadata.update({key: value for key, value in metadata.items() if value is not None})

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        if not self.enabled or self._started_at is None or self._started_perf is None:
            return False

        self._failed = exc_type is not None
        self._error_type = exc_type.__name__ if exc_type is not None else ""
        finished_at = datetime.now(UTC)
        rss_after_mb = _rss_mb()
        payload: dict[str, Any] = {
            "event": "scaling_baseline_stage",
            "lecture_id": self.lecture_id,
            "task_id": self.task_id,
            "stage": self.stage,
            "started_at": self._started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "wall_time_ms": round((time.perf_counter() - self._started_perf) * 1000),
            "process_cpu_time_ms": max(0, _cpu_time_ms(resource.RUSAGE_SELF) - self._cpu_before_ms),
            "child_cpu_time_ms": max(
                0, _cpu_time_ms(resource.RUSAGE_CHILDREN) - self._child_cpu_before_ms
            ),
            "rss_before_mb": self._rss_before_mb,
            "rss_after_mb": rss_after_mb,
            "rss_high_water_delta_mb": round(rss_after_mb - self._rss_before_mb, 2),
            "outcome": "failed" if self._failed else "success",
        }
        if self._error_type:
            payload["error_type"] = self._error_type
        payload.update(self.metadata)
        logger.info(
            "%s %s", SCALING_BASELINE_MARKER, json.dumps(payload, sort_keys=True, default=str)
        )
        return False


def measure_scaling_stage(
    *,
    stage: str,
    lecture_id: str | None = None,
    task_id: str | None = None,
    **metadata: Any,
) -> ScalingStageMeasurement:
    return ScalingStageMeasurement(
        stage=stage,
        lecture_id=lecture_id,
        task_id=task_id,
        **metadata,
    )
