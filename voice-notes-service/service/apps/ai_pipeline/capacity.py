from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import redis
from django.conf import settings
from django.utils import timezone

from infrastructure.observability.scaling import measure_scaling_stage

logger = logging.getLogger("tesanote.capacity")


@dataclass(frozen=True)
class ResourceSnapshot:
    captured_at: datetime
    cpu_count: float
    load_1: float
    cpu_pressure: float
    available_memory_bytes: int
    swap_used_bytes: int
    disk_free_bytes: int
    source: str

    def as_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["captured_at"] = self.captured_at.isoformat()
        return payload

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ResourceSnapshot:
        captured_at = datetime.fromisoformat(str(payload["captured_at"]))
        if captured_at.tzinfo is None:
            captured_at = captured_at.replace(tzinfo=UTC)
        return cls(
            captured_at=captured_at,
            cpu_count=max(1.0, float(payload["cpu_count"])),
            load_1=max(0.0, float(payload["load_1"])),
            cpu_pressure=max(0.0, float(payload["cpu_pressure"])),
            available_memory_bytes=max(0, int(payload["available_memory_bytes"])),
            swap_used_bytes=max(0, int(payload["swap_used_bytes"])),
            disk_free_bytes=max(0, int(payload["disk_free_bytes"])),
            source=str(payload.get("source") or "unknown"),
        )


@dataclass(frozen=True)
class CapacityDecision:
    recommended_capacity: int
    effective_capacity: int
    reason: str
    recommendation_reason: str
    snapshot: ResourceSnapshot | None
    metrics_fresh: bool
    last_capacity_change_at: datetime | None

    def as_admin_payload(self) -> dict[str, Any]:
        return {
            "recommended_capacity": self.recommended_capacity,
            "effective_capacity": self.effective_capacity,
            "reason": self.reason,
            "recommendation_reason": self.recommendation_reason,
            "metrics_fresh": self.metrics_fresh,
            "last_capacity_change_at": self.last_capacity_change_at.isoformat()
            if self.last_capacity_change_at
            else None,
            "snapshot": self.snapshot.as_payload() if self.snapshot else None,
        }


class ResourceSnapshotStore:
    """Small Redis-backed state; Redis loss deliberately degrades to capacity one.

    Phase 16: every key is scoped by worker-host id, so multiple processing
    hosts never overwrite each other's telemetry. Every call site that omits
    `host_id` defaults to this process's own identity
    (`settings.PROCESSING_WORKER_HOST_ID`), which is exactly what every
    existing single-host call site already wants — this is a pure rename of
    the underlying Redis key, not a behavior change, when only one host is
    registered.
    """

    _SNAPSHOT_PREFIX = "tesanote:capacity:snapshot:v1"
    _STATE_PREFIX = "tesanote:capacity:state:v1"
    _HISTORY_PREFIX = "tesanote:capacity:history:v1"

    @classmethod
    def _client(cls) -> redis.Redis:
        return redis.Redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
        )

    @staticmethod
    def _host(host_id: str | None) -> str:
        return host_id or str(getattr(settings, "PROCESSING_WORKER_HOST_ID", "web-local"))

    @classmethod
    def _snapshot_key(cls, host_id: str | None) -> str:
        return f"{cls._SNAPSHOT_PREFIX}:{cls._host(host_id)}"

    @classmethod
    def _state_key(cls, host_id: str | None) -> str:
        return f"{cls._STATE_PREFIX}:{cls._host(host_id)}"

    @classmethod
    def _history_key(cls, host_id: str | None) -> str:
        return f"{cls._HISTORY_PREFIX}:{cls._host(host_id)}"

    @classmethod
    def latest_snapshot(cls, *, host_id: str | None = None) -> ResourceSnapshot | None:
        try:
            raw = cls._client().get(cls._snapshot_key(host_id))
            return ResourceSnapshot.from_payload(json.loads(raw)) if raw else None
        except Exception:
            logger.warning("capacity_snapshot_read_failed", exc_info=True)
            return None

    @classmethod
    def write_snapshot(cls, snapshot: ResourceSnapshot, *, host_id: str | None = None) -> None:
        ttl = max(60, int(getattr(settings, "RESOURCE_SAMPLE_MAX_AGE_SECONDS", 90)) * 3)
        cls._client().set(cls._snapshot_key(host_id), json.dumps(snapshot.as_payload()), ex=ttl)

    @classmethod
    def read_state(cls, *, host_id: str | None = None) -> dict[str, Any]:
        try:
            raw = cls._client().get(cls._state_key(host_id))
            return json.loads(raw) if raw else {}
        except Exception:
            logger.warning("capacity_state_read_failed", exc_info=True)
            return {}

    @classmethod
    def write_state(cls, state: dict[str, Any], *, host_id: str | None = None) -> None:
        cls._client().set(cls._state_key(host_id), json.dumps(state), ex=60 * 60 * 24 * 30)

    @classmethod
    def record_history(cls, payload: dict[str, Any], *, host_id: str | None = None) -> None:
        client = cls._client()
        key = cls._history_key(host_id)
        client.lpush(key, json.dumps(payload))
        client.ltrim(key, 0, 19)
        client.expire(key, 60 * 60 * 24 * 30)

    @classmethod
    def recent_history(cls, *, host_id: str | None = None) -> list[dict[str, Any]]:
        try:
            raw_items = cls._client().lrange(cls._history_key(host_id), 0, 9)
            return [json.loads(item) for item in raw_items]
        except Exception:
            logger.warning("capacity_history_read_failed", exc_info=True)
            return []

    @classmethod
    def registered_host_ids(cls) -> list[str]:
        """De-duplicated, order-preserving registry from settings."""
        seen: dict[str, None] = {}
        for host_id in getattr(settings, "PROCESSING_WORKER_HOST_IDS", []) or []:
            if host_id:
                seen[str(host_id)] = None
        return list(seen) or [cls._host(None)]

    @classmethod
    def all_snapshots(cls) -> dict[str, ResourceSnapshot | None]:
        """One Redis round-trip (MGET) for every registered host's snapshot."""
        host_ids = cls.registered_host_ids()
        try:
            keys = [cls._snapshot_key(host_id) for host_id in host_ids]
            raw_values = cls._client().mget(keys)
        except Exception:
            logger.warning("capacity_all_snapshots_read_failed", exc_info=True)
            return dict.fromkeys(host_ids)
        result: dict[str, ResourceSnapshot | None] = {}
        for host_id, raw in zip(host_ids, raw_values, strict=True):
            if not raw:
                result[host_id] = None
                continue
            try:
                result[host_id] = ResourceSnapshot.from_payload(json.loads(raw))
            except (ValueError, KeyError, TypeError):
                result[host_id] = None
        return result


class HostResourceProbe:
    """Cheap Linux probes that work inside the deployed containers without Docker CLI calls."""

    @staticmethod
    def _read_int(path: str) -> int | None:
        try:
            raw = Path(path).read_text(encoding="utf-8").strip()
            return None if raw == "max" else int(raw)
        except (OSError, ValueError):
            return None

    @classmethod
    def _cpu_count(cls) -> float:
        try:
            quota, period = Path("/sys/fs/cgroup/cpu.max").read_text(encoding="utf-8").split()
            if quota != "max" and int(period) > 0:
                return max(1.0, int(quota) / int(period))
        except (OSError, ValueError):
            pass
        return float(max(1, os.cpu_count() or 1))

    @staticmethod
    def _proc_meminfo() -> dict[str, int]:
        """Return byte values from procfs; an unavailable host remains a safe fallback."""
        try:
            return {
                key.rstrip(":"): int(value) * 1024
                for key, value, *_ in (
                    line.split() for line in Path("/proc/meminfo").read_text().splitlines()
                )
            }
        except (OSError, ValueError):
            return {}

    @classmethod
    def _memory_available(cls) -> tuple[int, str]:
        current = cls._read_int("/sys/fs/cgroup/memory.current")
        maximum = cls._read_int("/sys/fs/cgroup/memory.max")
        candidates: list[int] = []
        if current is not None and maximum is not None and maximum > 0:
            candidates.append(max(0, maximum - current))

        host_available = cls._proc_meminfo().get("MemAvailable")
        if host_available is not None:
            candidates.append(max(0, host_available))

        if not candidates:
            return 0, "unavailable"
        # The smaller headroom protects against either a container limit or host contention.
        return min(candidates), "cgroup-v2+procfs" if len(candidates) == 2 else "procfs"

    @classmethod
    def _swap_used(cls) -> int:
        cgroup_swap = cls._read_int("/sys/fs/cgroup/memory.swap.current")
        values = cls._proc_meminfo()
        host_swap = max(0, values.get("SwapTotal", 0) - values.get("SwapFree", 0))
        # A busy host is unsafe even when this particular container has not swapped yet.
        return max(0, cgroup_swap or 0, host_swap)

    @classmethod
    def collect(cls, *, now: datetime | None = None) -> ResourceSnapshot:
        available_memory, source = cls._memory_available()
        disk = os.statvfs("/")
        cpu_count = cls._cpu_count()
        load_1 = os.getloadavg()[0]
        return ResourceSnapshot(
            captured_at=now or timezone.now(),
            cpu_count=cpu_count,
            load_1=load_1,
            cpu_pressure=round(load_1 / cpu_count, 3),
            available_memory_bytes=available_memory,
            swap_used_bytes=cls._swap_used(),
            disk_free_bytes=disk.f_bavail * disk.f_frsize,
            source=source,
        )


class DynamicCapacityController:
    """Slow to grow, fast to retreat. Any missing signal returns capacity one."""

    @staticmethod
    def minimum() -> int:
        return max(1, int(getattr(settings, "PROCESSING_CONCURRENCY_MIN", 1)))

    @staticmethod
    def _absolute_ceiling() -> int:
        """Phase 17: an architectural sanity bound no configuration value can
        exceed, independent of PROCESSING_CONCURRENCY_HARD_MAX. Prevents a
        typo (e.g. "200") from ever becoming live capacity. This is not the
        production target — it exists so raising the *configured* hard max
        (the actual operator-facing lever) is itself always still bounded by
        something. Default (8) is a conservative single-node ceiling, not a
        claim that 8 is safe; each step above the historically-validated
        value must still be measured before being configured."""
        try:
            return max(1, int(getattr(settings, "PROCESSING_CONCURRENCY_ABSOLUTE_MAX", 8)))
        except (TypeError, ValueError):
            return 8

    @classmethod
    def hard_maximum(cls) -> int:
        """The real operator-facing ceiling. Defaults to 2 (unchanged
        production behavior on deploy). Raising it requires an explicit
        PROCESSING_CONCURRENCY_HARD_MAX config change; malformed or
        non-positive values fail safe to `minimum()`, and no value can ever
        exceed `_absolute_ceiling()` regardless of configuration."""
        try:
            configured = int(getattr(settings, "PROCESSING_CONCURRENCY_HARD_MAX", 2))
        except (TypeError, ValueError):
            configured = cls.minimum()
        if configured < cls.minimum():
            configured = cls.minimum()
        return max(cls.minimum(), min(configured, cls._absolute_ceiling()))

    @classmethod
    def maximum(cls) -> int:
        return max(cls.minimum(), min(cls.hard_maximum(), int(getattr(settings, "PROCESSING_CONCURRENCY_MAX", 2))))

    @classmethod
    def _legacy_static_capacity(cls) -> int:
        return max(cls.minimum(), min(cls.hard_maximum(), int(getattr(settings, "PROCESSING_MAX_CONCURRENT_JOBS", 1))))

    @staticmethod
    def _parse_time(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            return None

    @classmethod
    def _is_fresh(cls, snapshot: ResourceSnapshot, now: datetime) -> bool:
        max_age = max(15, int(getattr(settings, "RESOURCE_SAMPLE_MAX_AGE_SECONDS", 90)))
        return (now - snapshot.captured_at).total_seconds() <= max_age

    @classmethod
    def _pressure_reason(cls, snapshot: ResourceSnapshot) -> str | None:
        if snapshot.cpu_pressure >= float(getattr(settings, "CPU_SCALE_UP_THRESHOLD", 0.60)):
            return "CPU_PRESSURE"
        if snapshot.available_memory_bytes < int(
            getattr(settings, "MEMORY_SCALE_UP_MIN_AVAILABLE_BYTES", 1_000_000_000)
        ):
            return "MEMORY_PRESSURE"
        if snapshot.swap_used_bytes > int(
            getattr(settings, "SWAP_SCALE_UP_MAX_USED_BYTES", 256_000_000)
        ):
            return "SWAP_PRESSURE"
        if snapshot.disk_free_bytes < int(getattr(settings, "DISK_SAFETY_MIN_FREE_BYTES", 2_000_000_000)):
            return "DISK_PRESSURE"
        return None

    @classmethod
    def decide(
        cls,
        *,
        snapshot: ResourceSnapshot | None = None,
        now: datetime | None = None,
        host_id: str | None = None,
    ) -> CapacityDecision:
        now = now or timezone.now()
        snapshot = (
            snapshot
            if snapshot is not None
            else ResourceSnapshotStore.latest_snapshot(host_id=host_id)
        )
        state = ResourceSnapshotStore.read_state(host_id=host_id)
        last_change = cls._parse_time(state.get("last_capacity_change_at"))
        recommendation = cls.minimum()
        recommendation_reason = "METRICS_STALE"

        if snapshot is None:
            recommendation_reason = "METRICS_STALE"
            state["healthy_since"] = None
        elif not cls._is_fresh(snapshot, now):
            recommendation_reason = "METRICS_STALE"
            state["healthy_since"] = None
        else:
            pressure = cls._pressure_reason(snapshot)
            if pressure:
                recommendation_reason = pressure
                state["healthy_since"] = None
            elif cls.maximum() == cls.minimum():
                recommendation_reason = "STATIC_MAXIMUM"
                recommendation = cls.minimum()
            else:
                healthy_since = cls._parse_time(state.get("healthy_since"))
                if healthy_since is None:
                    healthy_since = now
                    state["healthy_since"] = now.isoformat()
                stable_seconds = max(
                    30, int(getattr(settings, "CONCURRENCY_SCALE_UP_STABLE_SECONDS", 120))
                )
                if (now - healthy_since).total_seconds() >= stable_seconds:
                    recommendation = cls.maximum()
                    recommendation_reason = "HEALTHY_CAPACITY_2"
                else:
                    recommendation_reason = "SCALE_UP_STABILIZING"

        dynamic_enabled = bool(getattr(settings, "DYNAMIC_CONCURRENCY_ENABLED", False))
        effective = recommendation if dynamic_enabled else cls._legacy_static_capacity()
        reason = recommendation_reason if dynamic_enabled else "DYNAMIC_DISABLED"
        previous_effective = int(state.get("effective_capacity", cls.minimum()))
        changed = previous_effective != effective
        if changed:
            last_change = now
        state.update(
            {
                "effective_capacity": effective,
                "recommended_capacity": recommendation,
                "reason": reason,
                "recommendation_reason": recommendation_reason,
                "last_capacity_change_at": last_change.isoformat() if last_change else None,
            }
        )
        try:
            ResourceSnapshotStore.write_state(state, host_id=host_id)
            if changed or state.get("last_history_reason") != reason:
                ResourceSnapshotStore.record_history(
                    {
                        "at": now.isoformat(),
                        "recommended_capacity": recommendation,
                        "effective_capacity": effective,
                        "reason": reason,
                        "recommendation_reason": recommendation_reason,
                    },
                    host_id=host_id,
                )
                state["last_history_reason"] = reason
                ResourceSnapshotStore.write_state(state, host_id=host_id)
        except Exception:
            # A controller that cannot persist state must remain conservative.
            logger.warning("capacity_decision_storage_failed", exc_info=True)
            recommendation = cls.minimum()
            effective = cls.minimum() if dynamic_enabled else cls._legacy_static_capacity()
            reason = "METRICS_STALE" if dynamic_enabled else "DYNAMIC_DISABLED"
            recommendation_reason = "METRICS_STALE"

        metric_metadata = {
            "recommended_capacity": recommendation,
            "effective_capacity": effective,
            "cpu_pressure": snapshot.cpu_pressure if snapshot else None,
            "available_memory_bytes": snapshot.available_memory_bytes if snapshot else None,
            "swap_used_bytes": snapshot.swap_used_bytes if snapshot else None,
            "disk_free_bytes": snapshot.disk_free_bytes if snapshot else None,
        }
        with measure_scaling_stage(stage="dynamic_capacity_recommendation", **metric_metadata):
            pass
        if changed:
            with measure_scaling_stage(stage="dynamic_capacity_changed", **metric_metadata):
                pass
        if recommendation_reason == "METRICS_STALE":
            with measure_scaling_stage(stage="dynamic_capacity_metrics_stale", **metric_metadata):
                pass

        return CapacityDecision(
            recommended_capacity=recommendation,
            effective_capacity=max(cls.minimum(), min(cls.hard_maximum(), effective)),
            reason=reason,
            recommendation_reason=recommendation_reason,
            snapshot=snapshot,
            metrics_fresh=snapshot is not None and cls._is_fresh(snapshot, now),
            last_capacity_change_at=last_change,
        )


class ResourceSampler:
    @classmethod
    def sample_and_store(cls, *, host_id: str | None = None) -> ResourceSnapshot | None:
        """Sample and persist THIS process's own resources under `host_id`
        (defaults to `settings.PROCESSING_WORKER_HOST_ID`). A worker host
        always reports on itself — there is no remote-sampling path — so
        whichever host actually executes this call is, by construction, the
        host being measured."""
        try:
            snapshot = HostResourceProbe.collect()
            ResourceSnapshotStore.write_snapshot(snapshot, host_id=host_id)
            DynamicCapacityController.decide(snapshot=snapshot, host_id=host_id)
            with measure_scaling_stage(stage="resource_sample", lightweight=True):
                pass
            return snapshot
        except Exception as exc:
            logger.exception("resource_sampler_failed")
            with measure_scaling_stage(
                stage="resource_sampler_failure", error_class=exc.__class__.__name__, lightweight=True
            ):
                pass
            return None


# ---------------------------------------------------------------------------
# Phase 16: distributed capacity model.
#
# DynamicCapacityController answers a single-host question ("should THIS
# host's capacity rise from its static minimum toward its maximum"), tuned
# for a shared web+processing host where scale-up should be slow and
# conservative (see the stabilization hysteresis above). DistributedCapacityController
# answers a different question: "how many hosts are currently healthy enough
# to trust their pre-validated, human-configured slot count at all". A
# dedicated worker host's safe slot count is established once via the
# Phase 16 concurrency-validation procedure (see docs/scaling) and set as
# PROCESSING_HOST_MAX_SLOTS/env per host; this controller's only job at
# request time is the fail-closed freshness/pressure gate, not re-deriving
# the ceiling from scratch on every tick.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HostCapacityStatus:
    host_id: str
    safe_slots: int
    reason: str
    snapshot: ResourceSnapshot | None

    def as_payload(self) -> dict[str, Any]:
        return {
            "host_id": self.host_id,
            "safe_slots": self.safe_slots,
            "reason": self.reason,
            "snapshot": self.snapshot.as_payload() if self.snapshot else None,
        }


@dataclass(frozen=True)
class DistributedCapacityDecision:
    per_host: tuple[HostCapacityStatus, ...]
    total_safe_slots: int
    global_max_slots: int
    effective_capacity: int

    def as_admin_payload(self) -> dict[str, Any]:
        return {
            "effective_capacity": self.effective_capacity,
            "total_safe_slots": self.total_safe_slots,
            "global_max_slots": self.global_max_slots,
            "healthy_host_count": sum(1 for h in self.per_host if h.safe_slots > 0),
            "registered_host_count": len(self.per_host),
            "hosts": [h.as_payload() for h in self.per_host],
        }


class DistributedCapacityController:
    """Aggregates fail-closed per-host slot counts into one global figure.

    A host with missing or stale telemetry, or under CPU/memory/swap/disk
    pressure right now, contributes exactly zero slots — never partial
    credit, and never based on a stale "it used to be healthy" assumption.
    """

    @classmethod
    def host_status(cls, *, host_id: str, now: datetime | None = None) -> HostCapacityStatus:
        now = now or timezone.now()
        snapshot = ResourceSnapshotStore.latest_snapshot(host_id=host_id)
        if snapshot is None:
            return HostCapacityStatus(host_id, 0, "TELEMETRY_MISSING", None)
        if not DynamicCapacityController._is_fresh(snapshot, now):
            return HostCapacityStatus(host_id, 0, "TELEMETRY_STALE", snapshot)
        pressure = DynamicCapacityController._pressure_reason(snapshot)
        if pressure:
            return HostCapacityStatus(host_id, 0, pressure, snapshot)
        configured = max(
            1,
            min(
                int(getattr(settings, "PROCESSING_HOST_MAX_SLOTS", 2)),
                DynamicCapacityController.hard_maximum(),
            ),
        )
        return HostCapacityStatus(host_id, configured, "HEALTHY", snapshot)

    @classmethod
    def aggregate(cls, *, now: datetime | None = None) -> DistributedCapacityDecision:
        now = now or timezone.now()
        host_ids = ResourceSnapshotStore.registered_host_ids()
        per_host = tuple(cls.host_status(host_id=host_id, now=now) for host_id in host_ids)
        total = sum(h.safe_slots for h in per_host)
        global_max = max(0, int(getattr(settings, "PROCESSING_GLOBAL_MAX_SLOTS", 2)))
        effective = min(total, global_max)
        return DistributedCapacityDecision(
            per_host=per_host,
            total_safe_slots=total,
            global_max_slots=global_max,
            effective_capacity=effective,
        )
