from __future__ import annotations

import traceback as traceback_lib
from typing import Any

from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone

from apps.admin_ops.models import AdminAuditAction, ProcessingError
from services.admin.audit import AdminAuditService


class ErrorRecordingService:
    @staticmethod
    @transaction.atomic
    def record_processing_error(
        *,
        user=None,
        lecture=None,
        stage: str,
        error_type: str,
        error_message: str,
        exception: Exception | None = None,
        traceback_text: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> ProcessingError:
        serialized_traceback = traceback_text
        if not serialized_traceback and exception is not None:
            serialized_traceback = "".join(
                traceback_lib.format_exception(type(exception), exception, exception.__traceback__)
            )
        error = ProcessingError.objects.filter(
            lecture=lecture,
            stage=stage,
            error_type=error_type,
            error_message=error_message,
            resolved=False,
        ).order_by("-created_at").first()
        if error is None:
            error = ProcessingError.objects.create(
                user=user,
                lecture=lecture,
                stage=stage,
                error_type=error_type,
                error_message=error_message,
                traceback=serialized_traceback,
                metadata=metadata or {},
            )
        else:
            error.user = user
            error.traceback = serialized_traceback or error.traceback
            error.metadata = {**(error.metadata or {}), **(metadata or {})}
            error.save(update_fields=["user", "traceback", "metadata", "updated_at"])
        return error

    @staticmethod
    def list_errors(*, resolved, stage: str, search: str):
        queryset = ProcessingError.objects.select_related("user", "resolved_by").order_by("resolved", "-created_at")
        if resolved is not None and str(resolved) != "":
            queryset = queryset.filter(resolved=str(resolved).lower() == "true")
        if stage:
            queryset = queryset.filter(stage=stage)
        if search:
            queryset = queryset.filter(Q(error_message__icontains=search) | Q(error_type__icontains=search))
        return queryset

    @staticmethod
    def get_error(*, error_id):
        return get_object_or_404(ProcessingError.objects.select_related("user", "resolved_by"), id=error_id)

    @staticmethod
    @transaction.atomic
    def resolve_error(*, error_id, actor):
        error = ErrorRecordingService.get_error(error_id=error_id)
        error.resolved = True
        error.resolved_by = actor
        error.resolved_at = timezone.now()
        error.save(update_fields=["resolved", "resolved_by", "resolved_at", "updated_at"])
        AdminAuditService.record(
            action=AdminAuditAction.PROCESSING_ERROR_RESOLVED,
            actor=actor,
            target_user=error.user,
            entity_type="processing_error",
            entity_id=str(error.id),
            metadata={"stage": error.stage, "error_type": error.error_type},
        )
        return error

    @staticmethod
    @transaction.atomic
    def request_retry(*, error_id, actor):
        error = ErrorRecordingService.get_error(error_id=error_id)
        error.retry_count += 1
        error.save(update_fields=["retry_count", "updated_at"])
        AdminAuditService.record(
            action=AdminAuditAction.PROCESSING_RETRY_REQUESTED,
            actor=actor,
            target_user=error.user,
            entity_type="processing_error",
            entity_id=str(error.id),
            metadata={"stage": error.stage, "error_type": error.error_type, "retry_count": error.retry_count},
        )
        return error
