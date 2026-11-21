from __future__ import annotations

from contextlib import contextmanager
import logging
import math
import time
from typing import Any, Iterator
from uuid import uuid4

from django.conf import settings
from redis import Redis
from redis.exceptions import RedisError

from apps.ai_pipeline.exceptions import PipelineError

logger = logging.getLogger("tesanote")

_redis_client: Redis | None = None
_redis_client_failed_at: float | None = None


def _coordination_enabled() -> bool:
    return bool(getattr(settings, "REDIS_URL", "")) and not bool(getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False))


def _get_redis_client() -> Redis | None:
    global _redis_client, _redis_client_failed_at

    if not _coordination_enabled():
        return None
    if _redis_client is not None:
        return _redis_client
    if _redis_client_failed_at is not None and (time.monotonic() - _redis_client_failed_at) < 30:
        return None

    try:
        client = Redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_timeout=1,
            socket_connect_timeout=1,
            health_check_interval=30,
        )
        client.ping()
    except Exception as exc:  # pragma: no cover - depends on local infra availability
        _redis_client_failed_at = time.monotonic()
        logger.warning("redis_coordination_unavailable", extra={"error": str(exc)})
        return None

    _redis_client = client
    _redis_client_failed_at = None
    return _redis_client


def _release_lease(*, client: Redis, key: str, token: str) -> None:
    try:
        current = client.get(key)
        if current == token:
            client.delete(key)
    except RedisError:  # pragma: no cover - defensive
        logger.warning("redis_lease_release_failed", extra={"key": key})


def _minute_bucket() -> int:
    return math.floor(time.time() / 60)


@contextmanager
def lecture_task_lock(*, lecture_id: str, scope: str, ttl_seconds: int) -> Iterator[bool]:
    client = _get_redis_client()
    if client is None:
        yield True
        return

    key = f"tesanote:lecture-lock:{scope}:{lecture_id}"
    token = uuid4().hex
    try:
        acquired = bool(client.set(key, token, nx=True, ex=max(60, ttl_seconds)))
    except RedisError:  # pragma: no cover - defensive
        logger.warning("redis_lecture_lock_failed", extra={"scope": scope, "lecture_id": lecture_id})
        yield True
        return

    try:
        yield acquired
    finally:
        if acquired:
            _release_lease(client=client, key=key, token=token)


@contextmanager
def provider_concurrency_slot(
    *,
    provider_key: str,
    label: str,
    max_concurrency: int,
    wait_timeout_seconds: int,
    poll_interval_seconds: float,
    lease_seconds: int,
    stage: str,
    event_callback: Any | None = None,
) -> Iterator[None]:
    client = _get_redis_client()
    if client is None or max_concurrency <= 0:
        yield
        return

    deadline = time.monotonic() + max(0, wait_timeout_seconds)
    token = uuid4().hex
    wait_notified = False

    while True:
        for slot_index in range(max_concurrency):
            key = f"tesanote:provider-slot:{provider_key}:{slot_index}"
            try:
                acquired = bool(client.set(key, token, nx=True, ex=max(30, lease_seconds)))
            except RedisError:  # pragma: no cover - defensive
                logger.warning("redis_provider_gate_failed", extra={"provider_key": provider_key})
                yield
                return

            if acquired:
                if event_callback is not None:
                    event_callback(
                        {
                            "event": "provider_slot_acquired",
                            "provider_key": provider_key,
                            "slot_index": slot_index + 1,
                            "max_slots": max_concurrency,
                        }
                    )
                try:
                    yield
                finally:
                    _release_lease(client=client, key=key, token=token)
                return

        if not wait_notified and event_callback is not None:
            event_callback(
                {
                    "event": "provider_waiting_for_slot",
                    "provider_key": provider_key,
                    "max_slots": max_concurrency,
                }
            )
            wait_notified = True

        if time.monotonic() >= deadline:
            raise PipelineError(
                stage=stage,
                message=f"System is busy handling many {label} requests right now. Please try again shortly.",
            )
        time.sleep(max(0.05, poll_interval_seconds))


def provider_rate_limit_gate(
    *,
    provider_key: str,
    estimated_tokens: int,
    rpm_limit: int,
    tpm_limit: int,
    wait_timeout_seconds: int,
    poll_interval_seconds: float,
    stage: str,
    event_callback: Any | None = None,
) -> None:
    client = _get_redis_client()
    if client is None or (rpm_limit <= 0 and tpm_limit <= 0):
        return

    deadline = time.monotonic() + max(0, wait_timeout_seconds)
    wait_notified = False

    while True:
        bucket = _minute_bucket()
        rpm_key = f"tesanote:provider-rate:{provider_key}:rpm:{bucket}"
        tpm_key = f"tesanote:provider-rate:{provider_key}:tpm:{bucket}"
        rpm_reserved = False
        tpm_reserved = False
        try:
            if rpm_limit > 0:
                rpm_value = int(client.incr(rpm_key))
                rpm_reserved = True
                if rpm_value == 1:
                    client.expire(rpm_key, 75)
                if rpm_value > rpm_limit:
                    client.decr(rpm_key)
                    rpm_reserved = False
                    raise PipelineError(stage=stage, message="provider_rpm_limit_wait")

            if tpm_limit > 0:
                token_cost = max(1, int(estimated_tokens))
                tpm_value = int(client.incrby(tpm_key, token_cost))
                tpm_reserved = True
                if tpm_value == token_cost:
                    client.expire(tpm_key, 75)
                if tpm_value > tpm_limit:
                    client.decrby(tpm_key, token_cost)
                    tpm_reserved = False
                    raise PipelineError(stage=stage, message="provider_tpm_limit_wait")
            return
        except PipelineError:
            if not wait_notified and event_callback is not None:
                event_callback(
                    {
                        "event": "provider_waiting_for_rate_budget",
                        "provider_key": provider_key,
                        "rpm_limit": rpm_limit,
                        "tpm_limit": tpm_limit,
                        "estimated_tokens": estimated_tokens,
                    }
                )
                wait_notified = True
        except RedisError:  # pragma: no cover - defensive
            logger.warning("redis_provider_rate_gate_failed", extra={"provider_key": provider_key})
            return

        if time.monotonic() >= deadline:
            raise PipelineError(
                stage=stage,
                message="System is busy with many AI requests right now. Please try again shortly.",
            )
        time.sleep(max(0.1, poll_interval_seconds))
