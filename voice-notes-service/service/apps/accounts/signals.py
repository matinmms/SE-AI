from __future__ import annotations

from django.db.models.signals import pre_delete
from django.dispatch import receiver

from apps.accounts.deleted_numbers import remember_deleted_phone
from apps.accounts.models import User


@receiver(pre_delete, sender=User, dispatch_uid="remember_deleted_phone_number")
def remember_phone_of_deleted_user(sender, instance: User, **kwargs) -> None:
    """Every deletion path (the user's own, the admin, a queryset delete) records the number."""
    remember_deleted_phone(instance.phone_number)
