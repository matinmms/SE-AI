from __future__ import annotations

import logging
import secrets
from datetime import timedelta
from decimal import ROUND_CEILING, Decimal
from urllib.parse import urlparse

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Count, F, Q, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone

from apps.admin_ops.models import AdminAuditAction
from apps.payments.models import (
    DiscountCode,
    DiscountReservation,
    DiscountReservationStatus,
    PaymentPurpose,
    PaymentRecord,
    PaymentStatus,
)
from apps.subscriptions.models import PlanKey, SubscriptionPlan, UserSubscription
from services.admin.audit import AdminAuditService
from services.analytics.product import ProductAnalyticsService
from services.payments.base import PaymentGatewayProvider
from services.payments.first_purchase import FirstPurchaseDiscountService, percent_amount
from services.payments.zarinpal import ZarinpalPaymentProvider
from services.payments.zibal import ZibalGatewayError, ZibalPaymentProvider
from services.subscriptions.lifecycle import SubscriptionLifecycleService

logger = logging.getLogger("tesanote")
User = get_user_model()


class PaymentManagerService:

    @staticmethod
    def _record_audit_safely(**kwargs) -> None:
        try:
            AdminAuditService.record(**kwargs)
        except Exception:
            logger.exception(
                "payment_audit_write_failed",
                extra={
                    "entity_id": str(kwargs.get("entity_id", "")),
                    "action": str(kwargs.get("action", "")),
                },
            )

    @staticmethod
    def _reward_referrer(*, user) -> None:
        """The person who invited this buyer gets the purchase gift (once, never failing the payment)."""
        from apps.referrals.services import reward_referrer_for_purchase

        reward_referrer_for_purchase(user=user)

    @staticmethod
    def _audit_after_commit(**kwargs) -> None:
        transaction.on_commit(
            lambda: PaymentManagerService._record_audit_safely(**kwargs),
            robust=True,
        )

    @staticmethod
    def get_provider() -> PaymentGatewayProvider:
        provider = settings.PAYMENT_PROVIDER.strip().lower()
        if provider == "zibal":
            if not settings.ZIBAL_MERCHANT:
                raise ValueError("شناسه پذیرنده زیبال تنظیم نشده است.")
            if not settings.DEBUG and not settings.PAYMENT_MOCK_ENABLED and settings.ZIBAL_MERCHANT == "zibal":
                raise ValueError("Test Zibal merchant is not allowed in production.")
            return ZibalPaymentProvider()
        if provider == "zarinpal":
            return ZarinpalPaymentProvider()
        raise ValueError(f"Unsupported payment provider: {provider}")

    @staticmethod
    def build_callback_url() -> str:
        return f"{settings.BACKEND_PUBLIC_BASE_URL.rstrip('/')}{settings.PAYMENT_CALLBACK_PATH}"

    @staticmethod
    def build_frontend_result_url() -> str:
        return f"{settings.PAYMENT_FRONTEND_BASE_URL.rstrip('/')}/payment/result"

    @staticmethod
    def _validate_production_payment_urls() -> None:
        if settings.DEBUG or settings.PAYMENT_MOCK_ENABLED:
            return
        for url in (
            PaymentManagerService.build_callback_url(),
            PaymentManagerService.build_frontend_result_url(),
        ):
            parsed = urlparse(url)
            if parsed.scheme != "https" or parsed.hostname != "tesanote.ir":
                raise ValueError("Production payment URLs must use https://tesanote.ir.")

    @staticmethod
    def list_user_payments(*, user):
        return PaymentRecord.objects.filter(user=user).select_related("subscription_plan").order_by("-created_at")[:50]

    @staticmethod
    def build_user_payment_summary(*, user) -> dict[str, int]:
        aggregates = PaymentRecord.objects.filter(user=user, status=PaymentStatus.SUCCESS).aggregate(
            total_paid=Sum("amount"),
            successful_payments=Count("id"),
        )
        return {
            "total_paid": int(aggregates["total_paid"] or 0),
            "successful_payments": int(aggregates["successful_payments"] or 0),
        }

    @staticmethod
    def get_user_payment_by_authority(*, user, authority: str) -> PaymentRecord:
        return get_object_or_404(
            PaymentRecord.objects.select_related("subscription_plan"),
            user=user,
            authority=authority,
        )

    @staticmethod
    def get_user_payment_by_id(*, user, payment_id: str) -> PaymentRecord:
        return get_object_or_404(
            PaymentRecord.objects.select_related("subscription_plan"),
            user=user,
            id=payment_id,
        )

    @staticmethod
    def find_callback_payment(*, authority: str, order_id: str = "") -> PaymentRecord | None:
        queryset = PaymentRecord.objects.select_related("subscription_plan", "user")
        if order_id:
            return queryset.filter(
                Q(gateway_track_id=authority) | Q(authority=authority),
                order_id=order_id,
            ).first()
        return queryset.filter(Q(gateway_track_id=authority) | Q(authority=authority)).first()

    @staticmethod
    def defer_verification(
        *,
        payment_id: str,
        failure_code: str,
        message: str,
        gateway_payload: dict[str, object] | None = None,
        gateway_status: int | None = None,
    ) -> PaymentRecord:
        with transaction.atomic():
            payment = PaymentRecord.objects.select_for_update().get(id=payment_id)
            if payment.status != PaymentStatus.SUCCESS:
                payment.status = PaymentStatus.VERIFYING
                payment.failure_code = failure_code[:64]
                payment.failure_message = message[:500]
                update_fields = ["status", "failure_code", "failure_message", "updated_at"]
                if gateway_payload is not None:
                    payment.gateway_response = {
                        **(payment.gateway_response or {}),
                        "last_verification": gateway_payload,
                    }
                    update_fields.append("gateway_response")
                if gateway_status is not None:
                    payment.gateway_status = gateway_status
                    update_fields.append("gateway_status")
                payment.save(
                    update_fields=update_fields
                )
            return payment

    @staticmethod
    def normalize_discount_code(code: str) -> str:
        return code.strip().upper()

    @staticmethod
    def calculate_real_processing_cost(*, monthly_minutes: int) -> int:
        return int(round((monthly_minutes / 60) * settings.REAL_COST_PER_60_MINUTES_TOMAN))

    @staticmethod
    def calculate_plan_pricing(
        *,
        plan: SubscriptionPlan,
        discount: DiscountCode | None,
        now=None,
        first_purchase_percent: int = 0,
    ) -> dict[str, int | str | bool]:
        """Calculate every order price component on the server in a stable order.

        The first-purchase window and a discount code never stack: whichever
        takes more off the post-public-discount price wins, ties go to the code.
        """
        original_amount = plan.price_toman
        public_discount_percent = plan.active_public_discount_percent(now=now)
        public_discount_amount = min(original_amount, int(round((original_amount * public_discount_percent) / 100)))
        after_public_discount = max(0, original_amount - public_discount_amount)
        discount_percent = discount.percent if discount else 0
        discount_amount = min(after_public_discount, int(round((after_public_discount * discount_percent) / 100)))
        first_purchase_percent = first_purchase_percent if plan.key != PlanKey.FREE else 0
        first_purchase_amount = percent_amount(after_public_discount, first_purchase_percent)
        code_applied = discount is not None
        if first_purchase_amount > discount_amount:
            discount_amount = 0
            discount_percent = 0
            code_applied = False
        else:
            first_purchase_amount = 0
            first_purchase_percent = 0
        return {
            "amount": max(0, after_public_discount - discount_amount - first_purchase_amount),
            "original_amount": original_amount,
            "public_discount_percent": public_discount_percent,
            "public_discount_amount": public_discount_amount,
            "discount_amount": discount_amount,
            "discount_percent": discount_percent,
            "discount_code_text": discount.code if code_applied and discount else "",
            "code_applied": code_applied,
            "first_purchase_discount_percent": first_purchase_percent,
            "first_purchase_discount_amount": first_purchase_amount,
        }

    MIN_TOPUP_MINUTES = 1
    MAX_TOPUP_MINUTES = 100_000

    @staticmethod
    def calculate_topup_pricing(*, plan: SubscriptionPlan, minutes: int) -> dict[str, int]:
        """Extra-minute pricing is based on the plan's original (pre-discount)
        price, not the discounted price the plan itself sells for -- top-ups
        are not part of the plan's own promotional pricing. Decimal-safe
        throughout; the payable amount is always rounded up to the next
        whole toman so a fractional remainder is never undercharged.
        """
        if minutes <= 0:
            raise ValueError("تعداد دقیقه باید بیشتر از صفر باشد.")
        if plan.key == PlanKey.FREE:
            raise ValueError("خرید دقیقه اضافه فقط برای پلن‌های پولی امکان‌پذیر است.")
        basis_price = plan.original_price_toman or plan.price_toman
        if plan.monthly_minutes <= 0 or basis_price <= 0:
            raise ValueError("نرخ این پلن برای محاسبه دقیقه اضافه در دسترس نیست.")
        unit_price = Decimal(basis_price) / Decimal(plan.monthly_minutes)
        exact_amount = unit_price * Decimal(minutes)
        amount = int(exact_amount.quantize(Decimal("1"), rounding=ROUND_CEILING))
        return {
            "minutes": minutes,
            "unit_price_toman": unit_price,
            "amount": amount,
        }

    @staticmethod
    def get_valid_discount(*, code: str, plan: SubscriptionPlan) -> DiscountCode | None:
        normalized_code = PaymentManagerService.normalize_discount_code(code)
        if not normalized_code:
            return None

        now = timezone.now()
        discount = (
            DiscountCode.objects.filter(code=normalized_code, is_active=True)
            .filter(Q(starts_at__isnull=True) | Q(starts_at__lte=now))
            .filter(Q(expires_at__isnull=True) | Q(expires_at__gte=now))
            .first()
        )
        if discount is None:
            raise ValueError("کد تخفیف معتبر نیست یا منقضی شده است.")
        if discount.max_redemptions is not None and discount.redeemed_count >= discount.max_redemptions:
            raise ValueError("ظرفیت استفاده از این کد تخفیف تکمیل شده است.")
        if plan.price_toman <= 0:
            raise ValueError("کد تخفیف برای این پلن قابل استفاده نیست.")
        applicable_plans = list(discount.applicable_plans.all())
        if applicable_plans and plan not in applicable_plans:
            allowed_names = "، ".join(sorted(p.name or p.key for p in applicable_plans))
            raise ValueError(f"این کد تخفیف فقط برای پلن‌های {allowed_names} قابل استفاده است.")
        return discount

    @staticmethod
    def build_discount_preview(*, plan_key: str, discount_code: str, user=None) -> dict[str, object]:
        plan = get_object_or_404(SubscriptionPlan, key=plan_key, is_active=True)
        if plan.key == "FREE":
            raise ValueError("کد تخفیف برای پلن رایگان قابل استفاده نیست.")
        discount = PaymentManagerService.get_valid_discount(code=discount_code, plan=plan)
        pricing = PaymentManagerService.calculate_plan_pricing(
            plan=plan,
            discount=discount,
            first_purchase_percent=FirstPurchaseDiscountService.active_percent(user=user) if user else 0,
        )
        return {
            "plan_key": plan.key,
            "code": pricing["discount_code_text"],
            "percent": pricing["discount_percent"],
            "first_purchase_discount_percent": pricing["first_purchase_discount_percent"],
            "first_purchase_discount_amount": pricing["first_purchase_discount_amount"],
            "code_applied": pricing["code_applied"],
            "original_amount": pricing["original_amount"],
            "public_discount_percent": pricing["public_discount_percent"],
            "public_discount_amount": pricing["public_discount_amount"],
            "discount_amount": pricing["discount_amount"],
            "final_amount": pricing["amount"],
        }

    @staticmethod
    def _release_discount_reservation(payment: PaymentRecord) -> None:
        DiscountReservation.objects.filter(
            payment=payment,
            status=DiscountReservationStatus.RESERVED,
        ).update(status=DiscountReservationStatus.RELEASED, released_at=timezone.now(), updated_at=timezone.now())

    @staticmethod
    @transaction.atomic
    def _prepare_payment(*, user, plan: SubscriptionPlan, discount_code: str) -> tuple[PaymentRecord, bool]:
        now = timezone.now()
        active_statuses = [PaymentStatus.CREATED, PaymentStatus.REQUESTING, PaymentStatus.PENDING, PaymentStatus.VERIFYING]
        existing = (
            PaymentRecord.objects.select_for_update()
            .filter(user=user, subscription_plan=plan, provider="zibal", status__in=active_statuses)
            .order_by("-created_at")
            .first()
        )
        normalized_code = PaymentManagerService.normalize_discount_code(discount_code)
        first_purchase_percent = FirstPurchaseDiscountService.active_percent(user=user, now=now)
        if (
            existing
            and existing.expires_at
            and existing.expires_at > now
            and existing.discount_code_text == normalized_code
            and existing.first_purchase_discount_percent == first_purchase_percent
        ):
            return existing, False
        if existing:
            existing.status = PaymentStatus.EXPIRED
            existing.failure_code = "session_replaced"
            existing.save(update_fields=["status", "failure_code", "updated_at"])
            PaymentManagerService._release_discount_reservation(existing)

        discount = PaymentManagerService.get_valid_discount(code=normalized_code, plan=plan)
        if discount is not None:
            discount = DiscountCode.objects.select_for_update().get(id=discount.id)
            reserved_count = DiscountReservation.objects.filter(
                discount_code=discount,
                status=DiscountReservationStatus.RESERVED,
                expires_at__gt=now,
            ).count()
            if discount.max_redemptions is not None and discount.redeemed_count + reserved_count >= discount.max_redemptions:
                raise ValueError("ظرفیت استفاده از این کد تخفیف تکمیل شده است.")

        pricing = PaymentManagerService.calculate_plan_pricing(
            plan=plan, discount=discount, now=now, first_purchase_percent=first_purchase_percent
        )
        if discount is not None and not pricing["code_applied"]:
            discount = None
        expires_at = now + timedelta(minutes=settings.PAYMENT_SESSION_TTL_MINUTES)
        try:
            payment = PaymentRecord.objects.create(
                user=user,
                subscription_plan=plan,
                amount=int(pricing["amount"]),
                original_amount=int(pricing["original_amount"]),
                public_discount_percent=int(pricing["public_discount_percent"]),
                public_discount_amount=int(pricing["public_discount_amount"]),
                discount_code=discount,
                discount_code_text=str(pricing["discount_code_text"]),
                discount_percent=int(pricing["discount_percent"]),
                discount_amount=int(pricing["discount_amount"]),
                first_purchase_discount_percent=int(pricing["first_purchase_discount_percent"]),
                first_purchase_discount_amount=int(pricing["first_purchase_discount_amount"]),
                status=PaymentStatus.REQUESTING,
                provider="zibal",
                gateway_amount_rial=int(pricing["amount"]) * 10,
                initiated_at=now,
                expires_at=expires_at,
            )
        except IntegrityError:
            payment = PaymentRecord.objects.get(
                user=user,
                subscription_plan=plan,
                provider="zibal",
                status__in=active_statuses,
            )
            return payment, False
        payment.order_id = str(payment.id)
        payment.save(update_fields=["order_id", "updated_at"])
        if discount is not None:
            DiscountReservation.objects.create(
                discount_code=discount,
                payment=payment,
                expires_at=expires_at,
            )
        return payment, True

    @staticmethod
    def _start_gateway_checkout(*, payment: PaymentRecord, description: str) -> PaymentRecord:
        """Shared by plan-purchase and minute-top-up checkout: request the
        gateway session and persist the resulting PENDING state. Callers
        have already created the PaymentRecord with its final `amount`.
        """
        provider = PaymentManagerService.get_provider()
        try:
            result = provider.create_payment_request(
                amount=payment.amount,
                callback_url=PaymentManagerService.build_callback_url(),
                description=description,
                metadata={
                    "user_id": str(payment.user_id),
                    "plan_key": payment.subscription_plan.key,
                },
                order_id=payment.order_id or str(payment.id),
                mobile=payment.user.phone_number or "",
            )
        except ZibalGatewayError as exc:
            with transaction.atomic():
                locked = PaymentRecord.objects.select_for_update().get(id=payment.id)
                locked.status = PaymentStatus.FAILED
                locked.failure_code = exc.code
                locked.failure_message = str(exc)[:500]
                locked.save(update_fields=["status", "failure_code", "failure_message", "updated_at"])
                PaymentManagerService._release_discount_reservation(locked)
            raise ValueError(str(exc)) from exc

        with transaction.atomic():
            payment = PaymentRecord.objects.select_for_update().get(id=payment.id)
            payment.status = PaymentStatus.PENDING
            payment.authority = result.authority
            payment.gateway_track_id = result.authority
            payment.payment_url = result.payment_url
            payment.gateway_response = result.gateway_response
            payment.failure_code = ""
            payment.failure_message = ""
            payment.save(
                update_fields=[
                    "status", "authority", "gateway_track_id", "payment_url", "gateway_response",
                    "failure_code", "failure_message", "updated_at",
                ]
            )
        logger.info(
            "payment_created",
            extra={
                "user_id": str(payment.user_id),
                "payment_id": str(payment.id),
                "purpose": payment.purpose,
                "plan_key": payment.subscription_plan.key,
                "amount": payment.amount,
                "authority": payment.authority,
            },
        )
        return payment

    @staticmethod
    def create_payment_session(*, user, plan_key: str, discount_code: str = "") -> PaymentRecord:
        PaymentManagerService._validate_production_payment_urls()
        plan = get_object_or_404(SubscriptionPlan, key=plan_key, is_active=True)
        if plan.key == "FREE":
            raise ValueError("Free plan cannot be purchased.")
        payment, created = PaymentManagerService._prepare_payment(
            user=user,
            plan=plan,
            discount_code=discount_code,
        )
        if not created:
            if payment.status != PaymentStatus.SUCCESS and not payment.payment_url:
                raise ValueError(
                    "درخواست پرداخت دیگری در حال ایجاد است. چند لحظه دیگر دوباره تلاش کنید."
                )
            return payment
        if payment.amount == 0:
            return PaymentManagerService._fulfill_payment(
                payment_id=str(payment.id),
                ref_id=f"PROMO-{str(payment.id)[:8].upper()}",
                gateway_status=1,
                gateway_payload={
                    "provider": "promotion",
                    "result": 100,
                    "orderId": payment.order_id,
                },
            )

        payment = PaymentManagerService._start_gateway_checkout(
            payment=payment,
            description=f"Tesanote subscription payment for {plan.name}",
        )
        ProductAnalyticsService.record(
            name="PAYMENT_STARTED",
            user=user,
            metadata={"plan_key": plan.key, "amount": payment.amount},
        )
        AdminAuditService.record(
            action=AdminAuditAction.PAYMENT_EVENT,
            actor=None,
            target_user=user,
            entity_type="payment",
            entity_id=str(payment.id),
            metadata={
                "event": "created",
                "plan_key": plan.key,
                "amount": payment.amount,
                "original_amount": payment.original_amount,
                "public_discount_percent": payment.public_discount_percent,
                "public_discount_amount": payment.public_discount_amount,
                "discount_code": payment.discount_code_text,
                "discount_amount": payment.discount_amount,
                "first_purchase_discount_percent": payment.first_purchase_discount_percent,
                "first_purchase_discount_amount": payment.first_purchase_discount_amount,
                "authority": payment.authority,
            },
        )
        return payment

    @staticmethod
    @transaction.atomic
    def _prepare_topup_payment(*, user, minutes: int) -> tuple[PaymentRecord, bool]:
        subscription = SubscriptionLifecycleService.get_active_subscription(user=user)
        plan = subscription.plan
        if plan.key == PlanKey.FREE:
            raise ValueError("خرید دقیقه اضافه فقط برای کاربران دارای پلن پولی فعال امکان‌پذیر است.")

        now = timezone.now()
        active_statuses = [
            PaymentStatus.CREATED,
            PaymentStatus.REQUESTING,
            PaymentStatus.PENDING,
            PaymentStatus.VERIFYING,
        ]
        existing = (
            PaymentRecord.objects.select_for_update()
            .filter(
                user=user,
                subscription_plan=plan,
                purpose=PaymentPurpose.MINUTE_TOPUP,
                provider="zibal",
                status__in=active_statuses,
            )
            .order_by("-created_at")
            .first()
        )
        if (
            existing
            and existing.expires_at
            and existing.expires_at > now
            and existing.topup_minutes == minutes
        ):
            return existing, False
        if existing:
            existing.status = PaymentStatus.EXPIRED
            existing.failure_code = "session_replaced"
            existing.save(update_fields=["status", "failure_code", "updated_at"])

        pricing = PaymentManagerService.calculate_topup_pricing(plan=plan, minutes=minutes)
        expires_at = now + timedelta(minutes=settings.PAYMENT_SESSION_TTL_MINUTES)
        try:
            payment = PaymentRecord.objects.create(
                user=user,
                subscription_plan=plan,
                purpose=PaymentPurpose.MINUTE_TOPUP,
                topup_minutes=minutes,
                amount=pricing["amount"],
                original_amount=pricing["amount"],
                status=PaymentStatus.REQUESTING,
                provider="zibal",
                gateway_amount_rial=pricing["amount"] * 10,
                initiated_at=now,
                expires_at=expires_at,
            )
        except IntegrityError:
            payment = PaymentRecord.objects.get(
                user=user,
                subscription_plan=plan,
                purpose=PaymentPurpose.MINUTE_TOPUP,
                provider="zibal",
                status__in=active_statuses,
            )
            return payment, False
        payment.order_id = str(payment.id)
        payment.save(update_fields=["order_id", "updated_at"])
        return payment, True

    @staticmethod
    @transaction.atomic
    def _prepare_wallet_topup_payment(*, user, amount_toman: int, gift_toman: int) -> tuple[PaymentRecord, bool]:
        # The wallet is not tied to a plan; the row only needs one to satisfy its foreign key.
        plan = SubscriptionLifecycleService.get_active_subscription(user=user).plan
        now = timezone.now()
        active_statuses = [
            PaymentStatus.CREATED,
            PaymentStatus.REQUESTING,
            PaymentStatus.PENDING,
            PaymentStatus.VERIFYING,
        ]
        existing = (
            PaymentRecord.objects.select_for_update()
            .filter(user=user, subscription_plan=plan, purpose=PaymentPurpose.WALLET_TOPUP, provider="zibal", status__in=active_statuses)
            .order_by("-created_at")
            .first()
        )
        if (
            existing
            and existing.expires_at
            and existing.expires_at > now
            and existing.wallet_amount_toman == amount_toman
            and existing.wallet_gift_toman == gift_toman
        ):
            return existing, False
        if existing:
            existing.status = PaymentStatus.EXPIRED
            existing.failure_code = "session_replaced"
            existing.save(update_fields=["status", "failure_code", "updated_at"])
        expires_at = now + timedelta(minutes=settings.PAYMENT_SESSION_TTL_MINUTES)
        payment = PaymentRecord.objects.create(
            user=user,
            subscription_plan=plan,
            purpose=PaymentPurpose.WALLET_TOPUP,
            amount=amount_toman,
            original_amount=amount_toman,
            wallet_amount_toman=amount_toman,
            wallet_gift_toman=gift_toman,
            status=PaymentStatus.REQUESTING,
            provider="zibal",
            gateway_amount_rial=amount_toman * 10,
            initiated_at=now,
            expires_at=expires_at,
        )
        payment.order_id = str(payment.id)
        payment.save(update_fields=["order_id", "updated_at"])
        return payment, True

    @staticmethod
    def create_wallet_topup_session(*, user, amount_toman: int) -> PaymentRecord:
        """Charge the prepaid wallet. The first-top-up gift is fixed now, from the offer window that is
        open at this moment, so a slow bank page can never lose a gift the user had earned."""
        from apps.wallet import services as wallet_services

        PaymentManagerService._validate_production_payment_urls()
        amount_toman = int(amount_toman)
        if amount_toman < settings.WALLET_MIN_TOPUP_TOMAN or amount_toman > settings.WALLET_MAX_TOPUP_TOMAN:
            raise ValueError(
                f"مبلغ شارژ باید بین {settings.WALLET_MIN_TOPUP_TOMAN:,} تا {settings.WALLET_MAX_TOPUP_TOMAN:,} تومان باشد."
            )
        gift = wallet_services.gift_for_amount(amount_toman) if wallet_services.offer_is_active(user) else 0
        payment, created = PaymentManagerService._prepare_wallet_topup_payment(
            user=user, amount_toman=amount_toman, gift_toman=gift
        )
        if not created:
            if payment.status != PaymentStatus.SUCCESS and not payment.payment_url:
                raise ValueError("درخواست پرداخت دیگری در حال ایجاد است. چند لحظه دیگر دوباره تلاش کنید.")
            return payment
        payment = PaymentManagerService._start_gateway_checkout(
            payment=payment, description=f"Tesanote wallet top-up ({amount_toman} toman)"
        )
        ProductAnalyticsService.record(
            name="PAYMENT_STARTED",
            user=user,
            metadata={"purpose": "wallet_topup", "amount": amount_toman, "gift": gift},
        )
        return payment

    @staticmethod
    def create_topup_session(*, user, minutes: int) -> PaymentRecord:
        PaymentManagerService._validate_production_payment_urls()
        if (
            minutes < PaymentManagerService.MIN_TOPUP_MINUTES
            or minutes > PaymentManagerService.MAX_TOPUP_MINUTES
        ):
            raise ValueError(
                f"تعداد دقیقه باید بین {PaymentManagerService.MIN_TOPUP_MINUTES} تا "
                f"{PaymentManagerService.MAX_TOPUP_MINUTES} باشد."
            )
        payment, created = PaymentManagerService._prepare_topup_payment(user=user, minutes=minutes)
        if not created:
            if payment.status != PaymentStatus.SUCCESS and not payment.payment_url:
                raise ValueError(
                    "درخواست پرداخت دیگری در حال ایجاد است. چند لحظه دیگر دوباره تلاش کنید."
                )
            return payment

        payment = PaymentManagerService._start_gateway_checkout(
            payment=payment,
            description=(
                f"Tesanote minute top-up ({minutes} minutes) for {payment.subscription_plan.name}"
            ),
        )
        ProductAnalyticsService.record(
            name="PAYMENT_STARTED",
            user=user,
            metadata={
                "purpose": "topup",
                "plan_key": payment.subscription_plan.key,
                "minutes": minutes,
                "amount": payment.amount,
            },
        )
        AdminAuditService.record(
            action=AdminAuditAction.PAYMENT_EVENT,
            actor=None,
            target_user=user,
            entity_type="payment",
            entity_id=str(payment.id),
            metadata={
                "event": "topup_created",
                "plan_key": payment.subscription_plan.key,
                "minutes": minutes,
                "amount": payment.amount,
                "authority": payment.authority,
            },
        )
        return payment

    @staticmethod
    def _verification_matches(payment: PaymentRecord, *, amount_rial: int | None, order_id: str, ref_id: str) -> bool:
        return bool(
            ref_id.strip()
            and amount_rial == payment.gateway_amount_rial
            and order_id.strip() == (payment.order_id or "").strip()
        )

    @staticmethod
    def _fulfill_verified_payment(
        *,
        payment: PaymentRecord,
        ref_id: str,
        gateway_status: int,
        gateway_payload: dict[str, object],
    ) -> PaymentRecord:
        try:
            return PaymentManagerService._fulfill_payment(
                payment_id=str(payment.id),
                ref_id=ref_id.strip(),
                gateway_status=gateway_status,
                gateway_payload=gateway_payload,
            )
        except Exception:
            logger.exception(
                "zibal_fulfillment_failed",
                extra={
                    "payment_id": str(payment.id),
                    "track_id": payment.gateway_track_id,
                    "ref_id": ref_id,
                },
            )
            return PaymentManagerService.defer_verification(
                payment_id=str(payment.id),
                failure_code="fulfillment_deferred",
                message="پرداخت تایید شده است و فعال‌سازی اشتراک در حال تکمیل است.",
            )

    @staticmethod
    @transaction.atomic
    def _fulfill_payment(
        *,
        payment_id: str,
        ref_id: str,
        gateway_status: int,
        gateway_payload: dict[str, object],
    ) -> PaymentRecord:
        payment = (
            PaymentRecord.objects.select_for_update()
            # PostgreSQL cannot lock the nullable side of the LEFT JOIN created
            # by select_related("fulfilled_subscription"). The FK id is enough
            # for the idempotency check below, so keep the locking query on
            # non-nullable relations only.
            .select_related("subscription_plan", "user")
            .get(id=payment_id)
        )
        User.objects.select_for_update().get(id=payment.user_id)
        if payment.fulfilled_at and payment.fulfilled_subscription_id:
            return payment

        now = timezone.now()
        if payment.purpose == PaymentPurpose.WALLET_TOPUP:
            from apps.wallet import services as wallet_services
            from apps.wallet.models import OfferState, TopupOffer

            subscription = SubscriptionLifecycleService.get_active_subscription(user=payment.user)
            wallet_services.credit_paid(
                user=payment.user, amount=payment.wallet_amount_toman, key=f"topup:{payment.id}",
                ref_type="payment", ref_id=str(payment.id), metadata={"ref_id": ref_id},
            )
            if payment.wallet_gift_toman > 0:
                wallet_services.credit_gift(
                    user=payment.user, amount=payment.wallet_gift_toman, key=f"topup-gift:{payment.id}",
                    expires_at=now + timedelta(days=settings.WALLET_GIFT_EXPIRY_DAYS),
                    ref_type="payment", ref_id=str(payment.id),
                )
                TopupOffer.objects.filter(user=payment.user).update(
                    state=OfferState.CLAIMED, claimed_payment_id=str(payment.id), updated_at=now
                )
        elif payment.purpose == PaymentPurpose.MINUTE_TOPUP:
            subscription = (
                UserSubscription.objects.select_for_update()
                .select_related("plan")
                .filter(user=payment.user, is_active=True)
                .first()
            )
            if subscription is None or subscription.plan.key == PlanKey.FREE:
                # Real money was already captured by the gateway; never
                # silently discard it. Leave the payment SUCCESS but
                # unfulfilled so it surfaces for admin follow-up instead of
                # crediting minutes onto a Free plan that never bought them.
                payment.status = PaymentStatus.SUCCESS
                payment.ref_id = ref_id
                payment.gateway_status = gateway_status
                payment.gateway_response = {
                    **(payment.gateway_response or {}),
                    "verification": gateway_payload,
                }
                payment.paid_at = payment.paid_at or now
                payment.verified_at = now
                payment.failure_code = "topup_target_plan_expired"
                payment.failure_message = (
                    "پرداخت موفق بود اما پلن پولی کاربر پیش از اعمال دقیقه اضافه منقضی شد."
                )
                payment.save(
                    update_fields=[
                        "status", "ref_id", "gateway_status", "gateway_response", "paid_at",
                        "verified_at", "failure_code", "failure_message", "updated_at",
                    ]
                )
                PaymentManagerService._audit_after_commit(
                    action=AdminAuditAction.PAYMENT_EVENT,
                    actor=None,
                    target_user=payment.user,
                    entity_type="payment",
                    entity_id=str(payment.id),
                    metadata={
                        "event": "topup_fulfillment_blocked_plan_expired",
                        "amount": payment.amount,
                    },
                )
                return payment
            subscription.bonus_minutes += payment.topup_minutes
            subscription.save(update_fields=["bonus_minutes", "updated_at"])
            logger.info(
                "bonus_minutes_credited",
                extra={
                    "user_id": str(payment.user_id),
                    "subscription_id": str(subscription.id),
                    "payment_id": str(payment.id),
                    "minutes": payment.topup_minutes,
                    "bonus_minutes": subscription.bonus_minutes,
                },
            )
        else:
            subscription = SubscriptionLifecycleService.create_subscription(
                user=payment.user,
                plan_key=payment.subscription_plan.key,
                carry_over_paid_minutes=True,
                carry_over_remaining_time=True,
            )
            PaymentManagerService._reward_referrer(user=payment.user)
        reservation = DiscountReservation.objects.select_for_update().filter(payment=payment).first()
        if reservation and reservation.status == DiscountReservationStatus.RESERVED:
            reservation.status = DiscountReservationStatus.CONSUMED
            reservation.consumed_at = now
            reservation.save(update_fields=["status", "consumed_at", "updated_at"])
            DiscountCode.objects.filter(id=reservation.discount_code_id).update(redeemed_count=F("redeemed_count") + 1)
        payment.status = PaymentStatus.SUCCESS
        payment.ref_id = ref_id
        payment.gateway_status = gateway_status
        payment.gateway_response = {**(payment.gateway_response or {}), "verification": gateway_payload}
        payment.paid_at = payment.paid_at or now
        payment.verified_at = now
        payment.fulfilled_at = now
        payment.fulfilled_subscription = subscription
        payment.failure_code = ""
        payment.failure_message = ""
        payment.save(
            update_fields=[
                "status", "ref_id", "gateway_status", "gateway_response", "paid_at", "verified_at",
                "fulfilled_at", "fulfilled_subscription", "failure_code", "failure_message", "updated_at",
            ]
        )
        is_topup = payment.purpose == PaymentPurpose.MINUTE_TOPUP
        PaymentManagerService._audit_after_commit(
            action=AdminAuditAction.SUBSCRIPTION_ACTIVATION,
            actor=None,
            target_user=payment.user,
            entity_type="payment",
            entity_id=str(payment.id),
            metadata={
                "event": (
                    "topup_fulfilled"
                    if is_topup
                    else (
                        "zibal_payment_fulfilled"
                        if payment.provider == "zibal"
                        else "promotion_fulfilled"
                    )
                ),
                "plan_key": payment.subscription_plan.key,
                "topup_minutes": payment.topup_minutes if is_topup else None,
                "amount": payment.amount,
                "order_id": payment.order_id,
                "track_id": payment.gateway_track_id,
                "ref_id": payment.ref_id,
            },
        )
        ProductAnalyticsService.record(
            name="PAYMENT_COMPLETED",
            user=payment.user,
            metadata={
                "plan_key": payment.subscription_plan.key,
                "amount": payment.amount,
                "purpose": payment.purpose,
            },
        )
        return payment

    @staticmethod
    def _generate_manual_tracking_code() -> str:
        for _ in range(10):
            code = f"TN-{timezone.now():%Y%m%d}-{secrets.token_hex(3).upper()}"
            if not PaymentRecord.objects.filter(manual_tracking_code=code).exists():
                return code
        raise ValueError("Unable to generate a unique tracking code.")

    @staticmethod
    @transaction.atomic
    def submit_manual_receipt(*, user, plan_key: str, receipt_file, payer_note: str = "", discount_code: str = "") -> PaymentRecord:
        plan = get_object_or_404(SubscriptionPlan, key=plan_key, is_active=True)
        if plan.key == "FREE":
            raise ValueError("Free plan cannot be purchased.")
        discount = PaymentManagerService.get_valid_discount(code=discount_code, plan=plan)
        pricing = PaymentManagerService.calculate_plan_pricing(
            plan=plan,
            discount=discount,
            first_purchase_percent=FirstPurchaseDiscountService.active_percent(user=user),
        )
        if discount is not None and not pricing["code_applied"]:
            discount = None

        payment = PaymentRecord.objects.create(
            user=user,
            subscription_plan=plan,
            amount=int(pricing["amount"]),
            original_amount=int(pricing["original_amount"]),
            public_discount_percent=int(pricing["public_discount_percent"]),
            public_discount_amount=int(pricing["public_discount_amount"]),
            discount_code=discount,
            discount_code_text=str(pricing["discount_code_text"]),
            discount_percent=int(pricing["discount_percent"]),
            discount_amount=int(pricing["discount_amount"]),
            first_purchase_discount_percent=int(pricing["first_purchase_discount_percent"]),
            first_purchase_discount_amount=int(pricing["first_purchase_discount_amount"]),
            status=PaymentStatus.PENDING,
            provider="manual",
            gateway_amount_rial=int(pricing["amount"]) * 10,
            manual_tracking_code=PaymentManagerService._generate_manual_tracking_code(),
            receipt_file=receipt_file,
            submitted_at=timezone.now(),
            gateway_response={
                "temporary_manual_payment": True,
                "method": "card_transfer",
                "payer_note": payer_note.strip(),
                "first_purchase_discount_percent": int(pricing["first_purchase_discount_percent"]),
                "discount_code": str(pricing["discount_code_text"]),
                "discount_percent": int(pricing["discount_percent"]),
                "discount_amount": int(pricing["discount_amount"]),
            },
        )
        payment.order_id = str(payment.id)
        payment.save(update_fields=["order_id", "updated_at"])
        logger.info(
            "manual_payment_submitted",
            extra={
                "user_id": str(user.id),
                "payment_id": str(payment.id),
                "plan_key": plan.key,
                "amount": payment.amount,
                "original_amount": payment.original_amount,
                "discount_code": payment.discount_code_text,
                "discount_amount": payment.discount_amount,
                "manual_tracking_code": payment.manual_tracking_code,
            },
        )
        AdminAuditService.record(
            action=AdminAuditAction.PAYMENT_EVENT,
            actor=None,
            target_user=user,
            entity_type="payment",
            entity_id=str(payment.id),
            metadata={
                "event": "manual_receipt_submitted",
                "plan_key": plan.key,
                "amount": payment.amount,
                "original_amount": payment.original_amount,
                "discount_code": payment.discount_code_text,
                "discount_amount": payment.discount_amount,
                "manual_tracking_code": payment.manual_tracking_code,
            },
        )
        return payment

    @staticmethod
    def handle_callback(
        *,
        authority: str,
        gateway_status: str,
        raw_payload: dict[str, object],
        order_id: str = "",
        callback_success: str = "1",
    ) -> PaymentRecord:
        with transaction.atomic():
            payment = (
                PaymentRecord.objects.select_for_update()
                .select_related("subscription_plan", "user")
                .filter(gateway_track_id=authority)
                .first()
                or PaymentRecord.objects.select_for_update()
                .select_related("subscription_plan", "user")
                .filter(authority=authority)
                .first()
            )
            if payment is None:
                raise ValueError("Unknown payment track ID.")
            if payment.provider == "zibal" and not settings.PAYMENT_MOCK_ENABLED:
                if not authority.isdigit():
                    raise ValueError("Payment track ID is invalid.")
                if order_id != payment.order_id:
                    raise ValueError("Payment order ID does not match.")
            elif order_id and order_id != payment.order_id:
                raise ValueError("Payment order ID does not match.")
            if payment.status == PaymentStatus.SUCCESS:
                return payment

            callback_payload = {
                key: str(raw_payload.get(key, ""))[:128]
                for key in ("trackId", "orderId", "success", "status", "Authority", "Status")
                if key in raw_payload
            }
            payment.gateway_response = {**(payment.gateway_response or {}), "callback": callback_payload}
            if callback_success != "1" or gateway_status.strip() in {"3", "NOK"}:
                payment.status = PaymentStatus.CANCELED
                payment.gateway_status = int(gateway_status) if gateway_status.isdigit() else 3
                payment.failure_code = "user_canceled"
                payment.failure_message = "پرداخت توسط کاربر لغو شد."
                payment.save(
                    update_fields=[
                        "gateway_response", "status", "gateway_status", "failure_code", "failure_message", "updated_at",
                    ]
                )
                PaymentManagerService._audit_after_commit(
                    action=AdminAuditAction.PAYMENT_EVENT,
                    actor=None,
                    target_user=payment.user,
                    entity_type="payment",
                    entity_id=str(payment.id),
                    metadata={
                        "event": "zibal_callback_canceled",
                        "order_id": payment.order_id,
                        "track_id": payment.gateway_track_id,
                        "gateway_status": payment.gateway_status,
                    },
                )
                return payment
            payment.status = PaymentStatus.VERIFYING
            payment.save(update_fields=["gateway_response", "status", "updated_at"])
            PaymentManagerService._audit_after_commit(
                action=AdminAuditAction.PAYMENT_EVENT,
                actor=None,
                target_user=payment.user,
                entity_type="payment",
                entity_id=str(payment.id),
                metadata={
                    "event": "zibal_callback_received",
                    "order_id": payment.order_id,
                    "track_id": payment.gateway_track_id,
                },
            )

        provider = PaymentManagerService.get_provider()
        metadata = {**(payment.gateway_response or {}), "order_id": payment.order_id or ""}
        try:
            verification = provider.verify_payment(
                authority=authority,
                amount=payment.amount,
                gateway_status=gateway_status,
                metadata=metadata,
            )
            result_code = int(verification.gateway_response.get("result", 0) or 0)
            if result_code == 201 or (not verification.success and gateway_status.strip() == "2"):
                inquiry = provider.inquire_payment(authority=authority)
                if (
                    inquiry.found
                    and inquiry.gateway_status == 1
                    and PaymentManagerService._verification_matches(
                        payment,
                        amount_rial=inquiry.amount_rial,
                        order_id=inquiry.order_id,
                        ref_id=inquiry.ref_id,
                    )
                ):
                    return PaymentManagerService._fulfill_verified_payment(
                        payment=payment,
                        ref_id=inquiry.ref_id,
                        gateway_status=1,
                        gateway_payload=inquiry.gateway_response,
                    )
            if verification.success and PaymentManagerService._verification_matches(
                payment,
                amount_rial=verification.amount_rial,
                order_id=verification.order_id,
                ref_id=verification.ref_id,
            ):
                return PaymentManagerService._fulfill_verified_payment(
                    payment=payment,
                    ref_id=verification.ref_id,
                    gateway_status=verification.gateway_status or 1,
                    gateway_payload=verification.gateway_response,
                )
        except ZibalGatewayError as exc:
            with transaction.atomic():
                payment = PaymentRecord.objects.select_for_update().get(id=payment.id)
                payment.status = PaymentStatus.VERIFYING if exc.transient else PaymentStatus.FAILED
                payment.failure_code = exc.code
                payment.failure_message = str(exc)[:500]
                payment.save(update_fields=["status", "failure_code", "failure_message", "updated_at"])
                PaymentManagerService._audit_after_commit(
                    action=AdminAuditAction.PAYMENT_EVENT,
                    actor=None,
                    target_user=payment.user,
                    entity_type="payment",
                    entity_id=str(payment.id),
                    metadata={
                        "event": "zibal_verification_deferred" if exc.transient else "zibal_verification_failed",
                        "failure_code": exc.code,
                        "track_id": payment.gateway_track_id,
                    },
                )
            return payment
        except Exception:
            logger.exception(
                "zibal_verification_unexpected_error",
                extra={"payment_id": str(payment.id), "track_id": authority},
            )
            return PaymentManagerService.defer_verification(
                payment_id=str(payment.id),
                failure_code="verification_deferred",
                message="پرداخت دریافت شد و نتیجه آن در حال بررسی امن است.",
            )

        if not verification.success and gateway_status.strip() == "2":
            return PaymentManagerService.defer_verification(
                payment_id=str(payment.id),
                failure_code=f"zibal_verify_{result_code or 'pending'}",
                message="پرداخت دریافت شد و تایید نهایی آن در حال انجام است.",
                gateway_payload=verification.gateway_response,
                gateway_status=verification.gateway_status,
            )

        with transaction.atomic():
            payment = PaymentRecord.objects.select_for_update().get(id=payment.id)
            if payment.status != PaymentStatus.SUCCESS:
                payment.status = PaymentStatus.FAILED
                payment.gateway_status = verification.gateway_status
                payment.gateway_response = {**(payment.gateway_response or {}), "verification": verification.gateway_response}
                payment.failure_code = "verification_mismatch"
                payment.failure_message = "اطلاعات تاییدشده پرداخت با سفارش مطابقت ندارد."
                payment.save(
                    update_fields=[
                        "status", "gateway_status", "gateway_response", "failure_code", "failure_message", "updated_at",
                    ]
                )
                PaymentManagerService._audit_after_commit(
                    action=AdminAuditAction.PAYMENT_EVENT,
                    actor=None,
                    target_user=payment.user,
                    entity_type="payment",
                    entity_id=str(payment.id),
                    metadata={
                        "event": "zibal_verification_mismatch",
                        "failure_code": payment.failure_code,
                        "track_id": payment.gateway_track_id,
                    },
                )
        return payment

    @staticmethod
    def reconcile_zibal_payments(*, limit: int = 50) -> dict[str, int]:
        now = timezone.now()
        release_before = now - timedelta(minutes=5)
        expired = list(
            PaymentRecord.objects.filter(
                provider="zibal",
                status__in=[PaymentStatus.CREATED, PaymentStatus.REQUESTING, PaymentStatus.PENDING, PaymentStatus.CANCELED],
                expires_at__lte=release_before,
            )[:limit]
        )
        for payment in expired:
            with transaction.atomic():
                locked = PaymentRecord.objects.select_for_update().get(id=payment.id)
                if locked.status != PaymentStatus.SUCCESS:
                    locked.status = PaymentStatus.EXPIRED
                    locked.save(update_fields=["status", "updated_at"])
                    PaymentManagerService._release_discount_reservation(locked)

        stale_before = now - timedelta(seconds=settings.PAYMENT_RECONCILE_STALE_SECONDS)
        recovery_window = now - timedelta(days=7)
        candidates = list(
            PaymentRecord.objects.filter(
                provider="zibal",
                gateway_track_id__isnull=False,
                updated_at__lte=stale_before,
            )
            .filter(
                Q(status=PaymentStatus.PENDING, expires_at__gt=now)
                | Q(
                    status__in=[PaymentStatus.VERIFYING, PaymentStatus.FAILED],
                    created_at__gte=recovery_window,
                )
            )
            .order_by("updated_at")[:limit]
        )
        recovered = 0
        provider = PaymentManagerService.get_provider()
        for payment in candidates:
            try:
                inquiry = provider.inquire_payment(authority=payment.gateway_track_id or "")
                if not inquiry.found:
                    continue
                if inquiry.gateway_status == 2:
                    result = PaymentManagerService.handle_callback(
                        authority=payment.gateway_track_id or "",
                        gateway_status="2",
                        raw_payload={"source": "reconciliation"},
                        order_id=payment.order_id or "",
                    )
                    recovered += int(result.status == PaymentStatus.SUCCESS)
                elif inquiry.gateway_status == 1 and PaymentManagerService._verification_matches(
                    payment,
                    amount_rial=inquiry.amount_rial,
                    order_id=inquiry.order_id,
                    ref_id=inquiry.ref_id,
                ):
                    result = PaymentManagerService._fulfill_verified_payment(
                        payment=payment,
                        ref_id=inquiry.ref_id,
                        gateway_status=1,
                        gateway_payload=inquiry.gateway_response,
                    )
                    recovered += int(result.status == PaymentStatus.SUCCESS)
            except ZibalGatewayError as exc:
                logger.warning(
                    "zibal_reconciliation_gateway_error",
                    extra={
                        "payment_id": str(payment.id),
                        "track_id": payment.gateway_track_id,
                        "failure_code": exc.code,
                    },
                )
                PaymentManagerService.defer_verification(
                    payment_id=str(payment.id),
                    failure_code=f"reconcile_{exc.code}"[:64],
                    message=str(exc),
                )
            except ValueError as exc:
                logger.warning(
                    "zibal_reconciliation_validation_error",
                    extra={"payment_id": str(payment.id), "track_id": payment.gateway_track_id},
                )
                PaymentManagerService.defer_verification(
                    payment_id=str(payment.id),
                    failure_code="reconciliation_validation_error",
                    message=str(exc),
                )
            except Exception:
                logger.exception(
                    "zibal_reconciliation_unexpected_error",
                    extra={"payment_id": str(payment.id), "track_id": payment.gateway_track_id},
                )
                PaymentManagerService.defer_verification(
                    payment_id=str(payment.id),
                    failure_code="reconciliation_deferred",
                    message="پرداخت دریافت شد و نتیجه آن در حال بررسی امن است.",
                )
        return {"expired": len(expired), "checked": len(candidates), "recovered": recovered}

    @staticmethod
    @transaction.atomic
    def approve_manual_payment(*, payment_id: str, actor, admin_note: str = "") -> PaymentRecord:
        payment = (
            PaymentRecord.objects.select_for_update()
            .select_related("subscription_plan", "user")
            .filter(id=payment_id)
            .first()
        )
        if payment is None:
            raise ValueError("Payment was not found.")
        if payment.status != PaymentStatus.PENDING:
            raise ValueError("Only pending payments can be approved.")
        if not payment.receipt_file:
            raise ValueError("Manual payment does not have a receipt file.")

        payment.status = PaymentStatus.SUCCESS
        payment.ref_id = payment.manual_tracking_code
        payment.paid_at = timezone.now()
        payment.reviewed_by = actor
        payment.reviewed_at = timezone.now()
        payment.admin_note = admin_note.strip()
        payment.gateway_response = {
            **(payment.gateway_response or {}),
            "manual_review": "approved",
            "reviewed_by": str(actor.id),
        }
        payment.save(
            update_fields=[
                "status",
                "ref_id",
                "paid_at",
                "reviewed_by",
                "reviewed_at",
                "admin_note",
                "gateway_response",
                "updated_at",
            ]
        )
        if payment.discount_code_id:
            DiscountCode.objects.filter(id=payment.discount_code_id).update(redeemed_count=F("redeemed_count") + 1)
        subscription = SubscriptionLifecycleService.create_subscription(
            user=payment.user,
            plan_key=payment.subscription_plan.key,
            carry_over_paid_minutes=True,
            carry_over_remaining_time=True,
        )
        payment.fulfilled_at = timezone.now()
        payment.fulfilled_subscription = subscription
        payment.save(update_fields=["fulfilled_at", "fulfilled_subscription", "updated_at"])
        PaymentManagerService._reward_referrer(user=payment.user)
        AdminAuditService.record(
            action=AdminAuditAction.SUBSCRIPTION_ACTIVATION,
            actor=actor,
            target_user=payment.user,
            entity_type="payment",
            entity_id=str(payment.id),
            metadata={
                "event": "manual_payment_approved",
                "plan_key": payment.subscription_plan.key,
                "amount": payment.amount,
                "original_amount": payment.original_amount,
                "discount_code": payment.discount_code_text,
                "discount_amount": payment.discount_amount,
                "manual_tracking_code": payment.manual_tracking_code,
            },
        )
        return payment

    @staticmethod
    @transaction.atomic
    def reject_manual_payment(*, payment_id: str, actor, admin_note: str = "") -> PaymentRecord:
        payment = (
            PaymentRecord.objects.select_for_update()
            .select_related("subscription_plan", "user")
            .filter(id=payment_id)
            .first()
        )
        if payment is None:
            raise ValueError("Payment was not found.")
        if payment.status != PaymentStatus.PENDING:
            raise ValueError("Only pending payments can be rejected.")

        payment.status = PaymentStatus.CANCELED
        payment.reviewed_by = actor
        payment.reviewed_at = timezone.now()
        payment.admin_note = admin_note.strip()
        payment.gateway_response = {
            **(payment.gateway_response or {}),
            "manual_review": "rejected",
            "reviewed_by": str(actor.id),
        }
        payment.save(update_fields=["status", "reviewed_by", "reviewed_at", "admin_note", "gateway_response", "updated_at"])
        AdminAuditService.record(
            action=AdminAuditAction.PAYMENT_EVENT,
            actor=actor,
            target_user=payment.user,
            entity_type="payment",
            entity_id=str(payment.id),
            metadata={
                "event": "manual_payment_rejected",
                "plan_key": payment.subscription_plan.key,
                "amount": payment.amount,
                "original_amount": payment.original_amount,
                "discount_code": payment.discount_code_text,
                "discount_amount": payment.discount_amount,
                "manual_tracking_code": payment.manual_tracking_code,
            },
        )
        return payment

    @staticmethod
    def list_admin_payments(*, status_filter: str, search: str):
        queryset = PaymentRecord.objects.select_related("user", "subscription_plan").order_by("-created_at")
        if status_filter:
            queryset = queryset.filter(status=status_filter.upper())
        if search:
            queryset = queryset.filter(
                Q(user__phone_number__icontains=search)
                | Q(user__full_name__icontains=search)
                | Q(user__email__icontains=search)
                | Q(authority__icontains=search)
                | Q(ref_id__icontains=search)
            )
        return queryset

    @staticmethod
    def serialize_admin_payments(payments) -> list[dict[str, object]]:
        return [PaymentManagerService.serialize_admin_payment(payment) for payment in payments]

    @staticmethod
    def serialize_admin_payment(payment: PaymentRecord) -> dict[str, object]:
        gateway_response = payment.gateway_response or {}
        return {
            "id": str(payment.id),
            "user": {
                "id": str(payment.user_id),
                "phone_number": payment.user.phone_number,
                "email": payment.user.email,
                "full_name": payment.user.full_name,
            },
            "plan_key": payment.subscription_plan.key,
            "plan_name": payment.subscription_plan.name,
            "amount": payment.amount,
            "original_amount": payment.original_amount or payment.subscription_plan.price_toman,
            "public_discount_percent": payment.public_discount_percent,
            "public_discount_amount": payment.public_discount_amount,
            "discount_code": payment.discount_code_text,
            "discount_percent": payment.discount_percent,
            "discount_amount": payment.discount_amount,
            "first_purchase_discount_percent": payment.first_purchase_discount_percent,
            "first_purchase_discount_amount": payment.first_purchase_discount_amount,
            "real_cost_amount": PaymentManagerService.calculate_real_processing_cost(
                monthly_minutes=payment.subscription_plan.monthly_minutes
            ),
            "estimated_profit": payment.amount
            - PaymentManagerService.calculate_real_processing_cost(monthly_minutes=payment.subscription_plan.monthly_minutes),
            "status": payment.status,
            "provider": payment.provider,
            "order_id": payment.order_id,
            "gateway_track_id": payment.gateway_track_id,
            "gateway_status": payment.gateway_status,
            "failure_code": payment.failure_code,
            "failure_message": payment.failure_message,
            "expires_at": payment.expires_at,
            "verified_at": payment.verified_at,
            "fulfilled_at": payment.fulfilled_at,
            "authority": payment.authority,
            "ref_id": payment.ref_id,
            "manual_tracking_code": payment.manual_tracking_code,
            "receipt_url": payment.receipt_file.url if payment.receipt_file else "",
            "payer_note": str(gateway_response.get("payer_note", "")),
            "submitted_at": payment.submitted_at,
            "reviewed_at": payment.reviewed_at,
            "admin_note": payment.admin_note,
            "paid_at": payment.paid_at,
            "created_at": payment.created_at,
        }

    @staticmethod
    def build_admin_payment_summary() -> dict[str, int]:
        return {
            "total_revenue": int(PaymentRecord.objects.filter(status=PaymentStatus.SUCCESS).aggregate(value=Sum("amount"))["value"] or 0),
            "total_discount": int(
                (PaymentRecord.objects.filter(status=PaymentStatus.SUCCESS).aggregate(value=Sum("discount_amount"))["value"] or 0)
                + (PaymentRecord.objects.filter(status=PaymentStatus.SUCCESS).aggregate(value=Sum("public_discount_amount"))["value"] or 0)
            ),
            "total_real_cost": int(
                sum(
                    PaymentManagerService.calculate_real_processing_cost(monthly_minutes=payment.subscription_plan.monthly_minutes)
                    for payment in PaymentRecord.objects.filter(status=PaymentStatus.SUCCESS).select_related("subscription_plan")
                )
            ),
            "pending_payments": PaymentRecord.objects.filter(status=PaymentStatus.PENDING).count(),
            "failed_payments": PaymentRecord.objects.filter(status=PaymentStatus.FAILED).count(),
            "canceled_payments": PaymentRecord.objects.filter(status=PaymentStatus.CANCELED).count(),
            "successful_payments": PaymentRecord.objects.filter(status=PaymentStatus.SUCCESS).count(),
        }
