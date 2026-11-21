from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Count, Exists, OuterRef, Q, Sum
from django.utils import timezone

from apps.admin_ops.models import (
    AdminAuditAction,
    ProductEvent,
    ProductEventName,
    RevenueTimelineSettings,
)
from apps.lectures.models import Lecture, LectureProcessingState, LectureStatus
from apps.notes.models import NotePdf
from apps.payments.models import PaymentRecord, PaymentStatus
from apps.subscriptions.models import PlanKey, UserSubscription
from apps.uploads.models import UploadAttempt
from apps.usage.models import UsageLog, UsageLogStatus
from services.admin.audit import AdminAuditService
from services.subscriptions.lifecycle import SubscriptionLifecycleService

User = get_user_model()


class AnalyticsService:
    REVENUE_TIME_ZONE = ZoneInfo("Asia/Tehran")

    @staticmethod
    def calculate_real_processing_cost(*, minutes: int | float) -> int:
        return int(round((float(minutes or 0) / 60) * settings.REAL_COST_PER_60_MINUTES_TOMAN))

    @staticmethod
    def _growth_payload(*, queryset, date_field: str, value_field: str | None = None) -> dict[str, float | int | None]:
        now = timezone.now()
        current_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        previous_end = current_start
        previous_start = (current_start - timedelta(days=1)).replace(day=1)
        current_filter = {f"{date_field}__gte": current_start}
        previous_filter = {f"{date_field}__gte": previous_start, f"{date_field}__lt": previous_end}
        if value_field:
            current_value = queryset.filter(**current_filter).aggregate(v=Sum(value_field))["v"] or 0
            previous_value = queryset.filter(**previous_filter).aggregate(v=Sum(value_field))["v"] or 0
        else:
            current_value = queryset.filter(**current_filter).count()
            previous_value = queryset.filter(**previous_filter).count()
        growth = None if previous_value == 0 else round(((current_value - previous_value) / previous_value) * 100, 2)
        return {"current": int(current_value), "previous": int(previous_value), "growth_percent": growth}

    @staticmethod
    def build_system_analytics(
        *, revenue_start_date: date | None = None, revenue_end_date: date | None = None
    ) -> dict[str, object]:
        user_queryset = User.objects.all()
        payment_queryset = PaymentRecord.objects.filter(status=PaymentStatus.SUCCESS)
        usage_queryset = UsageLog.objects.filter(status=UsageLogStatus.COMPLETED)
        today = timezone.localdate(timezone=AnalyticsService.REVENUE_TIME_ZONE)
        month_start = date(today.year, today.month, 1)
        revenue_start_date = revenue_start_date or month_start
        revenue_end_date = revenue_end_date or today
        start_at = AnalyticsService._revenue_day_start(revenue_start_date)
        end_at = AnalyticsService._revenue_day_start(revenue_end_date + timedelta(days=1))
        range_revenue = payment_queryset.filter(paid_at__gte=start_at, paid_at__lt=end_at)
        range_total = int(range_revenue.aggregate(v=Sum("amount"))["v"] or 0)
        range_days = (revenue_end_date - revenue_start_date).days + 1
        previous_start_at = AnalyticsService._revenue_day_start(
            revenue_start_date - timedelta(days=range_days)
        )
        previous_total = int(
            payment_queryset.filter(paid_at__gte=previous_start_at, paid_at__lt=start_at)
            .aggregate(v=Sum("amount"))["v"]
            or 0
        )
        current_month_start_at = AnalyticsService._revenue_day_start(month_start)
        next_month_start = (
            date(today.year + 1, 1, 1)
            if today.month == 12
            else date(today.year, today.month + 1, 1)
        )
        next_month_start_at = AnalyticsService._revenue_day_start(next_month_start)
        current_month_total = int(
            payment_queryset.filter(paid_at__gte=current_month_start_at, paid_at__lt=next_month_start_at)
            .aggregate(v=Sum("amount"))["v"]
            or 0
        )
        timeline_settings = RevenueTimelineSettings.objects.filter(key="primary").first()
        lifetime_queryset = payment_queryset
        lifetime_start_date = timeline_settings.lifetime_start_date if timeline_settings else None
        if lifetime_start_date:
            lifetime_queryset = lifetime_queryset.filter(
                paid_at__gte=AnalyticsService._revenue_day_start(lifetime_start_date)
            )
        total_revenue = int(lifetime_queryset.aggregate(v=Sum("amount"))["v"] or 0)
        total_processing_minutes = int(usage_queryset.aggregate(v=Sum("audio_minutes"))["v"] or 0)
        estimated_ai_cost = AnalyticsService.calculate_real_processing_cost(minutes=total_processing_minutes)
        subscription_distribution = list(
            UserSubscription.objects.filter(is_active=True)
            .values("plan__key")
            .annotate(count=Count("id"))
            .order_by("plan__key")
        )
        from apps.wallet.models import WalletAccount, WalletTransaction, WalletTxKind

        topups = WalletTransaction.objects.filter(kind=WalletTxKind.TOPUP, user__is_staff=False)
        wallet_stats = {
            # people who really paid money into the wallet (staff excluded), and how many are paying repeatedly
            "topped_up_users": topups.values("user_id").distinct().count(),
            "repeat_topup_users": topups.values("user_id").annotate(n=Count("id")).filter(n__gte=2).count(),
            "topup_total_toman": int(topups.aggregate(v=Sum("amount_toman"))["v"] or 0),
            "users_with_balance": WalletAccount.objects.filter(user__is_staff=False)
            .filter(Q(paid_toman__gt=0) | Q(gift_toman__gt=0))
            .count(),
        }
        return {
            "wallet_stats": wallet_stats,
            "total_users": user_queryset.count(),
            "active_users_30d": user_queryset.filter(last_login__gte=timezone.now() - timedelta(days=30)).count(),
            "total_revenue": total_revenue,
            "revenue_timeline": {
                "today": today.isoformat(),
                "range_start": revenue_start_date.isoformat(),
                "range_end": revenue_end_date.isoformat(),
                "range_total": range_total,
                "previous_range_total": previous_total,
                "current_month_start": month_start.isoformat(),
                "current_month_end": today.isoformat(),
                "current_month_total": current_month_total,
                "lifetime_start_date": (
                    lifetime_start_date.isoformat()
                    if lifetime_start_date
                    else None
                ),
            },
            "total_processing_minutes": total_processing_minutes,
            "estimated_ai_cost": estimated_ai_cost,
            "estimated_profit": total_revenue - estimated_ai_cost,
            "monthly_user_growth": AnalyticsService._growth_payload(queryset=user_queryset, date_field="created_at"),
            "monthly_revenue_growth": AnalyticsService._growth_payload(queryset=payment_queryset, date_field="paid_at", value_field="amount"),
            "total_generated_pdfs": NotePdf.objects.count(),
            "active_processing_jobs": Lecture.objects.filter(status=LectureStatus.PROCESSING).count(),
            "failed_jobs": Lecture.objects.filter(status=LectureStatus.FAILED).count(),
            "subscription_distribution": [
                {"plan_key": row["plan__key"], "count": row["count"]}
                for row in subscription_distribution
            ],
        }

    @staticmethod
    def _revenue_day_start(value: date) -> datetime:
        return datetime.combine(value, time.min, tzinfo=AnalyticsService.REVENUE_TIME_ZONE)

    @staticmethod
    def _person_key_resolver(events):
        """Return `key(user_id, visitor_id)` that merges a visitor with the
        account they later signed up as, so a person who browsed anonymously
        and then registered is counted once -- not as a visitor *and* a user."""
        visitor_to_user: dict[str, str] = {}
        for user_id, visitor_id in (
            events.exclude(user_id__isnull=True)
            .exclude(visitor_id="")
            .values_list("user_id", "visitor_id")
            .distinct()
        ):
            visitor_to_user[visitor_id] = str(user_id)

        def key(user_id, visitor_id) -> str | None:
            if user_id:
                return str(user_id)
            if visitor_id:
                return visitor_to_user.get(visitor_id, f"visitor:{visitor_id}")
            return None

        return key

    @staticmethod
    def excluded_account_ids() -> set:
        """Staff plus the accounts named in ANALYTICS_EXCLUDED_ACCOUNTS (phone, email or user id)."""
        ids = set(User.objects.filter(is_staff=True).values_list("id", flat=True))
        for item in settings.ANALYTICS_EXCLUDED_ACCOUNTS:
            query = Q(phone_number=item) | Q(email__iexact=item)
            try:
                query |= Q(id=uuid.UUID(item))
            except ValueError:
                pass
            ids.update(User.objects.filter(query).values_list("id", flat=True))
        return ids

    @staticmethod
    def build_product_funnel(
        *, days: int = 30, start_date: date | None = None, end_date: date | None = None
    ) -> dict[str, object]:
        """Two plain, strictly-chained funnels.

        Every step counts *people* (a visitor merged with their account once
        they sign up) who reached that step **and** every step before it, so
        a step can never exceed the one above it and "conversion from
        previous" is always between 0 and 100. Each step carries a
        plain-language `description` of exactly what is counted.

        - `upload_funnel`: a new visitor opening the upload page, choosing a
          file as a guest, pressing "process", being sent to signup,
          registering, and the real upload / finished note (the last two are
          read from `Lecture` rows, never from client events that an ad
          blocker could drop).
        - `plan_funnel`: pricing page -> signup -> checkout -> payment.
        """
        until = None
        if start_date or end_date:
            today = timezone.localdate(timezone=AnalyticsService.REVENUE_TIME_ZONE)
            end_date = min(end_date or today, today)
            start_date = min(start_date or end_date, end_date)
            since = AnalyticsService._revenue_day_start(start_date)
            until = AnalyticsService._revenue_day_start(end_date + timedelta(days=1))
            days = (end_date - start_date).days + 1
        else:
            days = min(max(int(days), 1), 365)
            since = timezone.now() - timedelta(days=days)
        events = ProductEvent.objects.filter(created_at__gte=since)
        if until:
            events = events.filter(created_at__lt=until)
        excluded_user_ids = AnalyticsService.excluded_account_ids()
        if excluded_user_ids:
            excluded_visitors = set(
                ProductEvent.objects.filter(user_id__in=excluded_user_ids)
                .exclude(visitor_id="")
                .values_list("visitor_id", flat=True)
            )
            events = events.exclude(user_id__in=excluded_user_ids).exclude(visitor_id__in=excluded_visitors)
        person_key = AnalyticsService._person_key_resolver(events)

        # Accounts that existed before the window are returning learners, not
        # guests: they never fire the guest/signup steps, so keeping them in the
        # guest-to-first-note chain would show them as fake "drop-offs". They are
        # reported separately in `registered_summary` instead.
        returning_ids = {
            str(pk)
            for pk in User.objects.filter(created_at__lt=since)
            .exclude(email="system@tesanote.local")
            .exclude(id__in=excluded_user_ids)
            .values_list("id", flat=True)
        }

        def people(qs) -> set[str]:
            found = {person_key(u, v) for u, v in qs.values_list("user_id", "visitor_id")}
            found.discard(None)
            return found

        def newcomers(found: set[str]) -> set[str]:
            return found - returning_ids

        def event_people(name: str, *, path: str | None = None) -> set[str]:
            qs = events.filter(name=name)
            if path is not None:
                qs = qs.filter(path=path)
            return people(qs)

        def lecture_people(*, completed_only: bool, include_returning: bool = False) -> set[str]:
            qs = (
                Lecture.objects.filter(created_at__gte=since)
                .exclude(user_id__isnull=True)
                .exclude(processing_metadata__has_key="shared_copy")
            )
            if until:
                qs = qs.filter(created_at__lt=until)
            if excluded_user_ids:
                qs = qs.exclude(user_id__in=excluded_user_ids)
            if completed_only:
                qs = qs.filter(status=LectureStatus.COMPLETED)
            found = {person_key(user_id, None) for user_id in qs.values_list("user_id", flat=True).distinct()}
            return found if include_returning else newcomers(found)

        def chain(definitions: list[tuple[str, str, str, set[str]]]) -> list[dict[str, object]]:
            steps: list[dict[str, object]] = []
            reached: set[str] | None = None
            first_count = 0
            previous_count: int | None = None
            for event, label, description, raw in definitions:
                reached = set(raw) if reached is None else reached & raw
                count = len(reached)
                if previous_count is None:
                    first_count = count
                steps.append(
                    {
                        "event": event,
                        "label": label,
                        "description": description,
                        "count": count,
                        "conversion_from_previous": (
                            None
                            if previous_count is None
                            else (round(count / previous_count * 100, 1) if previous_count else 0)
                        ),
                        "percent_of_start": (
                            round(count / first_count * 100, 1) if first_count else 0
                        ),
                        "drop_off": 0 if previous_count is None else previous_count - count,
                    }
                )
                previous_count = count
            return steps

        def biggest_drop(steps: list[dict[str, object]]) -> dict[str, object] | None:
            candidates = [step for step in steps[1:] if step["drop_off"]]
            return max(candidates, key=lambda step: step["drop_off"], default=None)

        upload_funnel = chain(
            [
                (
                    "PAGE_VIEW",
                    "وارد سایت شدند",
                    "هر کسی که حداقل یک صفحه از تسانت را دیده است.",
                    newcomers(event_people(ProductEventName.PAGE_VIEW)),
                ),
                (
                    ProductEventName.UPLOAD_OPENED,
                    "صفحه آپلود را باز کردند",
                    "صفحه «آپلود» را دیده‌اند (برای مهمان‌ها هم باز می‌شود).",
                    newcomers(event_people(ProductEventName.UPLOAD_OPENED)),
                ),
                (
                    ProductEventName.GUEST_FILE_SELECTED,
                    "فایل صدا انتخاب کردند",
                    "بدون ثبت‌نام، فایل صوتی جلسه را در صفحه آپلود انتخاب کرده‌اند.",
                    newcomers(event_people(ProductEventName.GUEST_FILE_SELECTED)),
                ),
                (
                    ProductEventName.GUEST_PROCESS_CLICKED,
                    "روی «پردازش» زدند",
                    "دکمه پردازش را زده‌اند و به صفحه ثبت‌نام فرستاده شده‌اند.",
                    newcomers(event_people(ProductEventName.GUEST_PROCESS_CLICKED)),
                ),
                (
                    ProductEventName.AUTH_STARTED,
                    "صفحه ثبت‌نام را دیدند",
                    "صفحه ثبت‌نام/ورود برایشان باز شده است.",
                    newcomers(event_people(ProductEventName.AUTH_STARTED)),
                ),
                (
                    ProductEventName.OTP_SENT,
                    "کد تأیید گرفتند",
                    "شماره موبایل وارد کرده‌اند و پیامک کد تأیید برایشان ارسال شده است.",
                    newcomers(event_people(ProductEventName.OTP_SENT)),
                ),
                (
                    ProductEventName.AUTHENTICATED,
                    "ثبت‌نام / ورود کامل شد",
                    "کد را درست وارد کرده‌اند و حساب کاربری دارند.",
                    newcomers(event_people(ProductEventName.AUTHENTICATED)),
                ),
                (
                    "LECTURE_CREATED",
                    "آپلود واقعی شروع شد",
                    "بعد از ثبت‌نام، فایل برایشان آپلود و جلسه ساخته شده است.",
                    lecture_people(completed_only=False),
                ),
                (
                    "LECTURE_COMPLETED",
                    "جزوه ساخته شد",
                    "اولین جزوه‌شان با موفقیت آماده شده است.",
                    lecture_people(completed_only=True),
                ),
            ]
        )

        plan_funnel = chain(
            [
                (
                    ProductEventName.PRICING_VIEWED,
                    "صفحه قیمت‌ها را دیدند",
                    "صفحه «پلن‌ها و قیمت» را باز کرده‌اند.",
                    event_people(ProductEventName.PRICING_VIEWED),
                ),
                (
                    ProductEventName.AUTH_STARTED,
                    "صفحه ثبت‌نام/ورود را دیدند",
                    "برای خرید به صفحه ثبت‌نام/ورود رسیده‌اند.",
                    event_people(ProductEventName.AUTH_STARTED),
                ),
                (
                    ProductEventName.AUTHENTICATED,
                    "وارد حساب شدند",
                    "ثبت‌نام یا ورودشان کامل شده است.",
                    event_people(ProductEventName.AUTHENTICATED),
                ),
                (
                    ProductEventName.PAYMENT_CHECKOUT_VIEWED,
                    "صفحه پرداخت را دیدند",
                    "صفحه خلاصه سفارش/پرداخت را دیده‌اند.",
                    event_people(ProductEventName.PAYMENT_CHECKOUT_VIEWED),
                ),
                (
                    ProductEventName.PAYMENT_STARTED,
                    "به درگاه رفتند",
                    "روی پرداخت زده‌اند و به درگاه بانکی منتقل شده‌اند.",
                    event_people(ProductEventName.PAYMENT_STARTED),
                ),
                (
                    ProductEventName.PAYMENT_COMPLETED,
                    "پرداخت موفق",
                    "پرداخت را کامل کرده‌اند.",
                    event_people(ProductEventName.PAYMENT_COMPLETED),
                ),
            ]
        )

        landing_funnel = chain(
            [
                (
                    ProductEventName.LANDING_VIEW,
                    "صفحه‌ی اصلی را دیدند",
                    "هر نفرِ یکتا که صفحه‌ی اصلی را باز کرده است.",
                    event_people(ProductEventName.LANDING_VIEW),
                ),
                (
                    ProductEventName.CTA_UPLOAD_CLICK,
                    "روی آپلود زدند",
                    "یکی از دکمه‌های «آپلود ویس کلاس» را زده‌اند.",
                    event_people(ProductEventName.CTA_UPLOAD_CLICK),
                ),
                (
                    ProductEventName.FILE_SELECTED,
                    "فایل انتخاب کردند",
                    "بدون ثبت‌نام، فایلی را از دستگاه انتخاب کرده‌اند.",
                    event_people(ProductEventName.FILE_SELECTED),
                ),
                (
                    ProductEventName.ANON_UPLOAD_COMPLETED,
                    "آپلود موقت کامل شد",
                    "فایل کامل و بدون خطا در آپلود موقت ذخیره شده است.",
                    event_people(ProductEventName.ANON_UPLOAD_COMPLETED),
                ),
                (
                    ProductEventName.BUILD_CLICK,
                    "روی «بساز جزوه‌م» زدند",
                    "بعد از آپلود، دکمه‌ی ساخت جزوه را زده‌اند.",
                    event_people(ProductEventName.BUILD_CLICK),
                ),
                (
                    ProductEventName.OTP_SENT,
                    "کد تأیید گرفتند",
                    "شماره‌ی موبایل داده‌اند و پیامک کد برایشان رفته است.",
                    event_people(ProductEventName.OTP_SENT),
                ),
                (
                    ProductEventName.AUTHENTICATED,
                    "وارد شدند",
                    "کد را درست وارد کرده‌اند و حساب دارند.",
                    event_people(ProductEventName.AUTHENTICATED),
                ),
                (
                    ProductEventName.PROCESSING_STARTED,
                    "پردازش شروع شد",
                    "پردازش فایلشان واقعاً شروع شده است.",
                    event_people(ProductEventName.PROCESSING_STARTED),
                ),
                (
                    ProductEventName.FIRST_NOTE_READY,
                    "اولین جزوه آماده شد",
                    "اولین جزوه‌شان با موفقیت ساخته شده است.",
                    event_people(ProductEventName.FIRST_NOTE_READY),
                ),
                (
                    ProductEventName.FIRST_NOTE_OPENED,
                    "جزوه را باز کردند",
                    "جزوه‌ی آماده را باز کرده‌اند.",
                    event_people(ProductEventName.FIRST_NOTE_OPENED),
                ),
                (
                    ProductEventName.PAYWALL_SHOWN,
                    "پنجره‌ی پلن‌ها را دیدند",
                    "هنگام آپلود بعدی یا با دکمه‌ی ارتقا، پلن‌ها برایشان باز شده است.",
                    event_people(ProductEventName.PAYWALL_SHOWN),
                ),
                (
                    ProductEventName.PLAN_SELECTED,
                    "پلن انتخاب کردند",
                    "یکی از پلن‌ها را در پنجره‌ی پلن‌ها انتخاب کرده‌اند.",
                    event_people(ProductEventName.PLAN_SELECTED),
                ),
                (
                    ProductEventName.CHECKOUT_STARTED,
                    "به درگاه رفتند",
                    "در صفحه‌ی پرداخت، دکمه‌ی پرداخت را زده‌اند.",
                    event_people(ProductEventName.CHECKOUT_STARTED),
                ),
                (
                    ProductEventName.PAYMENT_COMPLETED,
                    "پرداخت موفق",
                    "پرداخت را کامل کرده‌اند.",
                    event_people(ProductEventName.PAYMENT_COMPLETED),
                ),
            ]
        )

        all_uploaders = lecture_people(completed_only=False, include_returning=True)
        all_completers = lecture_people(completed_only=True, include_returning=True)
        new_accounts = {
            str(pk)
            for pk in User.objects.filter(created_at__gte=since, **({"created_at__lt": until} if until else {}))
            .exclude(email="system@tesanote.local")
            .exclude(id__in=excluded_user_ids)
            .values_list("id", flat=True)
        }
        registered_summary = {
            "new_accounts": len(new_accounts),
            "new_accounts_uploaded": len(new_accounts & all_uploaders),
            "new_accounts_completed": len(new_accounts & all_completers),
            "returning_uploaded": len(all_uploaders & returning_ids),
            "returning_completed": len(all_completers & returning_ids),
            "total_uploaded": len(all_uploaders),
            "total_completed": len(all_completers),
        }

        top_exit_paths = list(
            events.filter(name=ProductEventName.PAGE_EXIT)
            .exclude(path="")
            .values("path")
            .annotate(exits=Count("id"), visitors=Count("visitor_id", distinct=True))
            .order_by("-exits")[:8]
        )
        return {
            "days": days,
            "start_date": (start_date or timezone.localdate(since, AnalyticsService.REVENUE_TIME_ZONE)).isoformat(),
            "end_date": (end_date or timezone.localdate(timezone=AnalyticsService.REVENUE_TIME_ZONE)).isoformat(),
            "upload_funnel": upload_funnel,
            "registered_summary": registered_summary,
            "plan_funnel": plan_funnel,
            "landing_funnel": landing_funnel,
            "biggest_drop_landing": biggest_drop(landing_funnel),
            "excluded_accounts": len(excluded_user_ids),
            "biggest_drop_upload": biggest_drop(upload_funnel),
            "biggest_drop_plan": biggest_drop(plan_funnel),
            "top_exit_paths": top_exit_paths,
        }

    @staticmethod
    def build_idle_users(*, days: int = 30, limit: int = 100) -> dict[str, object]:
        """Registered learners (signed up in the last `days`) who have never
        created a lecture, with the last pages they were seen on / left.

        A person's pre-signup browsing is merged in through the visitor id of
        their authenticated events, and `PAGE_EXIT` beacons (which carry no
        account) are attributed the same way.
        """
        days = min(max(int(days), 1), 365)
        limit = min(max(int(limit), 1), 300)
        since = timezone.now() - timedelta(days=days)
        users = list(
            User.objects.filter(is_staff=False, is_active=True, created_at__gte=since)
            .exclude(email="system@tesanote.local")
            .exclude(lectures__isnull=False)
            .order_by("-created_at")[:limit]
        )
        user_ids = [user.id for user in users]
        visitor_to_user: dict[str, str] = {}
        for user_id, visitor_id in (
            ProductEvent.objects.filter(user_id__in=user_ids)
            .exclude(visitor_id="")
            .values_list("user_id", "visitor_id")
            .distinct()
        ):
            visitor_to_user[visitor_id] = str(user_id)

        attempts = {
            str(row["user_id"]): row["total"]
            for row in UploadAttempt.objects.filter(user_id__in=user_ids)
            .values("user_id")
            .annotate(total=Count("id"))
        }
        by_user: dict[str, dict[str, object]] = {
            str(user.id): {"views": [], "exit": None, "upload_opened": False} for user in users
        }
        relevant = ProductEvent.objects.filter(
            Q(user_id__in=user_ids) | Q(visitor_id__in=list(visitor_to_user))
        ).order_by("-created_at")
        for user_id, visitor_id, name, path, created_at in relevant.values_list(
            "user_id", "visitor_id", "name", "path", "created_at"
        )[:30000]:
            owner = str(user_id) if user_id else visitor_to_user.get(visitor_id)
            bucket = by_user.get(owner or "")
            if bucket is None:
                continue
            if name == ProductEventName.PAGE_VIEW and path:
                bucket["views"].append((path, created_at))
            elif name == ProductEventName.PAGE_EXIT and path and bucket["exit"] is None:
                bucket["exit"] = (path, created_at)
            elif name == ProductEventName.UPLOAD_OPENED:
                bucket["upload_opened"] = True

        rows = []
        for user in users:
            bucket = by_user[str(user.id)]
            views = bucket["views"]
            trail: list[str] = []
            for path, _ in views:
                if path not in trail:
                    trail.append(path)
                if len(trail) == 5:
                    break
            last_view = views[0] if views else None
            last_exit = bucket["exit"]
            rows.append(
                {
                    "id": str(user.id),
                    "full_name": user.full_name,
                    "phone_number": user.phone_number,
                    "email": user.email,
                    "signed_up_at": user.created_at,
                    "last_login": user.last_login,
                    "has_tracking_data": bool(views or last_exit),
                    "opened_upload_page": bool(bucket["upload_opened"]),
                    "upload_attempts": attempts.get(str(user.id), 0),
                    "last_page": last_view[0] if last_view else None,
                    "last_page_at": last_view[1] if last_view else None,
                    "last_exit_page": last_exit[0] if last_exit else None,
                    "last_exit_at": last_exit[1] if last_exit else None,
                    "recent_pages": trail,
                }
            )
        return {"days": days, "count": len(rows), "results": rows}

    @staticmethod
    def list_admin_users(*, search: str, plan_key: str):
        queryset = User.objects.all().order_by("-created_at")
        if search:
            queryset = queryset.filter(
                Q(phone_number__icontains=search) | Q(full_name__icontains=search) | Q(email__icontains=search)
            )
        if plan_key:
            on_plan = Exists(UserSubscription.objects.filter(user=OuterRef("pk"), is_active=True, plan__key=plan_key))
            if plan_key == PlanKey.FREE:
                # A user with no active subscription is shown (and treated) as Free.
                has_active = Exists(UserSubscription.objects.filter(user=OuterRef("pk"), is_active=True))
                queryset = queryset.filter(on_plan | ~has_active)
            else:
                queryset = queryset.filter(on_plan)
        return queryset.select_related()

    @staticmethod
    def serialize_admin_user_rows(users) -> list[dict[str, object]]:
        from apps.wallet import services as wallet_services

        rows: list[dict[str, object]] = []
        for user in users:
            subscription = SubscriptionLifecycleService.get_active_subscription(user=user)
            profitability = AnalyticsService.build_user_profitability(user=user)
            rows.append(
                {
                    "id": str(user.id),
                    "phone_number": user.phone_number,
                    "email": user.email,
                    "full_name": user.full_name,
                    "is_staff": user.is_staff,
                    "subscription_plan": subscription.plan.key,
                    "remaining_minutes": subscription.remaining_minutes,
                    "reserved_minutes": subscription.reserved_minutes,
                    "used_minutes": subscription.used_minutes,
                    "wallet": wallet_services.admin_wallet_snapshot(user),
                    "usage_minutes": profitability["usage_minutes"],
                    "estimated_ai_cost": profitability["estimated_ai_cost"],
                    "revenue": profitability["revenue"],
                    "estimated_profit": profitability["estimated_profit"],
                    "created_at": user.created_at,
                    "last_login": user.last_login,
                }
            )
        return rows

    @staticmethod
    def build_user_profitability(*, user) -> dict[str, int]:
        usage = UsageLog.objects.filter(user=user, status=UsageLogStatus.COMPLETED).aggregate(total_minutes=Sum("audio_minutes"))
        revenue = PaymentRecord.objects.filter(user=user, status=PaymentStatus.SUCCESS).aggregate(v=Sum("amount"))["v"] or 0
        usage_minutes = int(usage["total_minutes"] or 0)
        estimated_ai_cost = AnalyticsService.calculate_real_processing_cost(minutes=usage_minutes)
        return {
            "revenue": int(revenue),
            "estimated_ai_cost": estimated_ai_cost,
            "estimated_profit": int(revenue) - estimated_ai_cost,
            "usage_minutes": usage_minutes,
        }

    @staticmethod
    @transaction.atomic
    def add_bonus_minutes(*, actor, target_user, minutes: int, reason: str):
        subscription = (
            UserSubscription.objects.select_for_update()
            .select_related("plan", "user")
            .get(user=target_user, is_active=True)
        )
        subscription.remaining_minutes += minutes
        subscription.save(update_fields=["remaining_minutes", "updated_at"])
        SubscriptionLifecycleService.sync_legacy_user_fields(user=target_user, subscription=subscription)
        AdminAuditService.record(
            action=AdminAuditAction.QUOTA_ADJUSTMENT,
            actor=actor,
            target_user=target_user,
            entity_type="subscription",
            entity_id=str(subscription.id),
            metadata={"minutes": minutes, "reason": reason},
        )
        return subscription

    @staticmethod
    def build_job_monitoring_summary(*, cutoff) -> dict[str, int]:
        return {
            "queued_jobs": Lecture.objects.filter(
                status=LectureStatus.PROCESSING,
                processing_state=LectureProcessingState.QUEUED,
            ).count(),
            "active_jobs": Lecture.objects.filter(status=LectureStatus.PROCESSING).count(),
            "completed_jobs": Lecture.objects.filter(
                status=LectureStatus.COMPLETED,
                processing_completed_at__gte=cutoff,
            ).count(),
            "failed_jobs": Lecture.objects.filter(
                status=LectureStatus.FAILED,
                processing_completed_at__gte=cutoff,
            ).count(),
        }
