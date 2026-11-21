from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from django.conf import settings
from django.db import DatabaseError, OperationalError, close_old_connections, connection
from django.db.utils import InterfaceError
from django.utils import timezone

logger = logging.getLogger("tesanote.processing_heartbeat")


class HeartbeatService:
    """Phase 12: durable liveness evidence for an actively-PROCESSING job.

    A tick is a single cheap, conditional UPDATE keyed by (lecture_id,
    status=PROCESSING) — the same shape of query already indexed by
    ai_job_lecture_status_idx, so this adds no new index and never touches
    business/processing state. It naturally becomes a no-op once the job
    leaves PROCESSING (terminal, or already reset by recovery), which is a
    convenient side effect: a superseded execution's ticks simply stop
    mattering once recovery has moved the row on, without needing the ticker
    itself to know about execution generations.
    """

    @staticmethod
    def tick(*, lecture_id: str) -> None:
        if not settings.PROCESSING_HEARTBEAT_ENABLED:
            return
        from apps.ai_pipeline.models import LectureProcessingJob, LectureProcessingJobStatus

        try:
            LectureProcessingJob.objects.filter(
                lecture_id=lecture_id, status=LectureProcessingJobStatus.PROCESSING
            ).update(last_heartbeat_at=timezone.now())
        except (OperationalError, DatabaseError, InterfaceError) as exc:  # pragma: no cover
            # A missed heartbeat is tolerated by design (stale detection requires
            # several consecutive misses) — never let this fail processing.
            logger.warning(
                "processing_heartbeat_tick_failed",
                extra={"lecture_id": lecture_id, "error": str(exc)},
            )

    @staticmethod
    @contextmanager
    def keepalive(*, lecture_id: str, interval_seconds: int | None = None) -> Iterator[None]:
        """Keep the heartbeat fresh for the duration of a long blocking call.

        Between-stage progress writes already double as heartbeats (see
        `tick`'s call sites), but a single long FFmpeg subprocess, provider
        HTTP call, or transcription submission loop has no natural checkpoint
        in between. This spawns a daemon thread that ticks on its own cadence
        for exactly the lifetime of the wrapped block, using its own DB
        connection (closed on exit) so it never shares a connection with the
        calling thread.
        """
        if not settings.PROCESSING_HEARTBEAT_ENABLED:
            yield
            return

        interval = interval_seconds or settings.PROCESSING_HEARTBEAT_INTERVAL_SECONDS
        stop_event = threading.Event()

        def _loop() -> None:
            close_old_connections()
            try:
                while not stop_event.wait(interval):
                    try:
                        HeartbeatService.tick(lecture_id=lecture_id)
                    except Exception:  # pragma: no cover - defensive, never crash the thread
                        logger.exception(
                            "processing_heartbeat_loop_tick_failed",
                            extra={"lecture_id": lecture_id},
                        )
            finally:
                connection.close()

        thread = threading.Thread(
            target=_loop, name=f"processing-heartbeat-{lecture_id[:8]}", daemon=True
        )
        HeartbeatService.tick(lecture_id=lecture_id)
        thread.start()
        try:
            yield
        finally:
            stop_event.set()
            thread.join(timeout=2)
