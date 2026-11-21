from __future__ import annotations

from apps.admin_ops.models import AdminAuditLog


class AdminAuditService:
    @staticmethod
    def record(*, action: str, actor, target_user, entity_type: str, entity_id: str = "", metadata: dict[str, object] | None = None) -> AdminAuditLog:
        return AdminAuditLog.objects.create(
            actor=actor,
            target_user=target_user,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            metadata=metadata or {},
        )

