"""Lightweight latency tracking.

Recording is one dict update under a lock (microseconds, no I/O). A request
that notices the flush interval has passed hands the aggregate to a short
background thread, which upserts hourly rows into `PerfMetricBucket`. Only
counts, sums, maxima and a fixed histogram are stored -- never raw samples --
so storage and query cost stay tiny and p50/p95 are bucket estimates.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from contextlib import contextmanager
from datetime import timedelta

from django.conf import settings
from django.db import close_old_connections, transaction
from django.utils import timezone

logger = logging.getLogger("tesanote")

BUCKET_BOUNDS_MS = (50, 100, 200, 400, 800, 1500, 3000, 6000, 12000, 30000, 60000)
MAX_VALUE_MS = 10 * 60 * 1000
MAX_LABELS_PER_FLUSH = 400
LABEL_PATTERN = re.compile(r"^[ -~]{1,120}$")

# Client-reportable metrics. Anything else is rejected, so the label space
# can't be flooded by arbitrary payloads.
CLIENT_METRICS = {
    "page": {"ttfb", "fcp", "lcp", "load", "route"},
    "action": {"otp_send", "otp_verify", "upload_init", "upload_total", "note_open"},
}

_lock = threading.Lock()
_pending: dict[tuple[str, str, str, int], list] = {}
_last_flush = time.monotonic()
_flushing = False
_last_sweep = 0.0


def _bucket_index(value_ms: int) -> int:
    for index, bound in enumerate(BUCKET_BOUNDS_MS):
        if value_ms <= bound:
            return index
    return len(BUCKET_BOUNDS_MS)


def _hour_of(moment) -> int:
    return int(moment.timestamp()) // 3600 * 3600


def record(kind: str, metric: str, label: str, value_ms: float) -> None:
    if not getattr(settings, "PERF_METRICS_ENABLED", True) or not LABEL_PATTERN.match(label):
        return
    value = int(min(max(value_ms, 0), MAX_VALUE_MS))
    key = (kind, metric, label, _hour_of(timezone.now()))
    due = False
    with _lock:
        entry = _pending.get(key)
        if entry is None:
            if len(_pending) >= MAX_LABELS_PER_FLUSH:
                return
            entry = _pending[key] = [0, 0, 0, [0] * (len(BUCKET_BOUNDS_MS) + 1)]
        entry[0] += 1
        entry[1] += value
        entry[2] = max(entry[2], value)
        entry[3][_bucket_index(value)] += 1
        due = time.monotonic() - _last_flush >= settings.PERF_FLUSH_SECONDS
    if due:
        _flush_in_background()


@contextmanager
def timed(metric: str, step: str):
    """Time one step of a slow endpoint, e.g. `with timed("upload_complete", "probe"):`."""
    started = time.perf_counter()
    try:
        yield
    finally:
        record("stage", metric, step, (time.perf_counter() - started) * 1000)


def _flush_in_background() -> None:
    global _flushing, _last_flush
    with _lock:
        if _flushing:
            return
        _flushing = True
        _last_flush = time.monotonic()
    threading.Thread(target=_flush_guarded, name="perf-flush", daemon=True).start()


def _flush_guarded() -> None:
    global _flushing
    try:
        flush()
    except Exception:  # never let metrics take anything down
        logger.warning("perf_flush_failed", exc_info=True)
    finally:
        close_old_connections()
        with _lock:
            _flushing = False


def flush() -> int:
    """Write pending aggregates to the database. Returns rows touched."""
    from datetime import datetime, timezone as dt_timezone

    from apps.admin_ops.models import PerfMetricBucket

    with _lock:
        batch = dict(_pending)
        _pending.clear()
    if not batch:
        return 0
    with transaction.atomic():
        for (kind, metric, label, hour), (count, total, peak, histogram) in batch.items():
            hour_dt = datetime.fromtimestamp(hour, tz=dt_timezone.utc)
            row, _ = PerfMetricBucket.objects.select_for_update().get_or_create(
                kind=kind, metric=metric, label=label, hour=hour_dt, defaults={"buckets": [0] * len(histogram)}
            )
            merged = list(row.buckets or [0] * len(histogram))
            merged = (merged + [0] * len(histogram))[: len(histogram)]
            row.buckets = [a + b for a, b in zip(merged, histogram)]
            row.count += count
            row.sum_ms += total
            row.max_ms = max(row.max_ms, peak)
            row.save(update_fields=["buckets", "count", "sum_ms", "max_ms", "updated_at"])
    global _last_sweep
    if time.monotonic() - _last_sweep > 6 * 3600:  # occasional, cheap retention sweep
        _last_sweep = time.monotonic()
        PerfMetricBucket.objects.filter(
            hour__lt=timezone.now() - timedelta(days=settings.PERF_RETENTION_DAYS)
        ).delete()
    return len(batch)


def percentile(histogram: list[int], fraction: float, peak: int) -> int | None:
    total = sum(histogram)
    if not total:
        return None
    target = total * fraction
    seen = 0
    for index, bucket_count in enumerate(histogram):
        seen += bucket_count
        if seen >= target:
            if index < len(BUCKET_BOUNDS_MS):
                return min(BUCKET_BOUNDS_MS[index], peak) if peak else BUCKET_BOUNDS_MS[index]
            return peak
    return peak


def summarize(rows) -> list[dict[str, object]]:
    """Merge hourly rows into one entry per (kind, metric, label)."""
    merged: dict[tuple[str, str, str], dict] = {}
    for row in rows:
        entry = merged.setdefault(
            (row.kind, row.metric, row.label),
            {"count": 0, "sum": 0, "max": 0, "hist": [0] * (len(BUCKET_BOUNDS_MS) + 1)},
        )
        entry["count"] += row.count
        entry["sum"] += row.sum_ms
        entry["max"] = max(entry["max"], row.max_ms)
        for index, value in enumerate((row.buckets or [])[: len(entry["hist"])]):
            entry["hist"][index] += value
    results = []
    for (kind, metric, label), entry in merged.items():
        results.append(
            {
                "kind": kind,
                "metric": metric,
                "label": label,
                "count": entry["count"],
                "avg_ms": round(entry["sum"] / entry["count"]) if entry["count"] else 0,
                "p50_ms": percentile(entry["hist"], 0.5, entry["max"]),
                "p95_ms": percentile(entry["hist"], 0.95, entry["max"]),
                "max_ms": entry["max"],
                # Share of requests slower than 1.5s: a cheap "how often is it painful" figure.
                "slow_share": round(sum(entry["hist"][BUCKET_BOUNDS_MS.index(1500) + 1 :]) / entry["count"] * 100, 1)
                if entry["count"]
                else 0,
            }
        )
    return sorted(results, key=lambda item: (item["p95_ms"] or 0) * item["count"], reverse=True)
