from __future__ import annotations

import uuid
from urllib.parse import urlencode

from django.conf import settings

from services.payments.base import (
    PaymentInquiryResult,
    PaymentRequestResult,
    PaymentVerificationResult,
)


class ZarinpalPaymentProvider:
    def create_payment_request(
        self,
        *,
        amount: int,
        callback_url: str,
        description: str,
        metadata: dict[str, object],
        order_id: str = "",
        mobile: str = "",
    ) -> PaymentRequestResult:
        authority = f"A{uuid.uuid4().hex[:24].upper()}"
        if settings.PAYMENT_MOCK_ENABLED:
            query = urlencode({"Authority": authority, "Status": "OK"})
            payment_url = f"{callback_url}?{query}"
            return PaymentRequestResult(
                authority=authority,
                payment_url=payment_url,
                gateway_response={
                    "provider": "zarinpal",
                    "mode": "mock",
                    "description": description,
                    "metadata": metadata,
                },
            )

        payment_url = f"{settings.ZARINPAL_START_PAY_BASE_URL.rstrip('/')}/{authority}"
        return PaymentRequestResult(
            authority=authority,
            payment_url=payment_url,
            gateway_response={
                "provider": "zarinpal",
                "mode": "live",
                "description": description,
                "metadata": metadata,
            },
        )

    def verify_payment(
        self,
        *,
        authority: str,
        amount: int,
        gateway_status: str,
        metadata: dict[str, object],
    ) -> PaymentVerificationResult:
        normalized_status = gateway_status.upper().strip()
        if settings.PAYMENT_MOCK_ENABLED:
            forced = str(metadata.get("mock_outcome", "")).upper()
            success = normalized_status == "OK" and forced != "FAILED" and not authority.startswith("FAIL")
            if success:
                return PaymentVerificationResult(
                    success=True,
                    ref_id=f"MOCK-{authority[-8:]}",
                    gateway_response={
                        "provider": "zarinpal",
                        "mode": "mock",
                        "amount": amount,
                        "authority": authority,
                        "status": normalized_status,
                    },
                )
            return PaymentVerificationResult(
                success=False,
                ref_id="",
                gateway_response={
                    "provider": "zarinpal",
                    "mode": "mock",
                    "amount": amount,
                    "authority": authority,
                    "status": normalized_status,
                },
                error_message="Mock payment verification failed.",
            )

        if normalized_status != "OK":
            return PaymentVerificationResult(
                success=False,
                ref_id="",
                gateway_response={"provider": "zarinpal", "mode": "live", "status": normalized_status},
                error_message="Gateway returned non-success status.",
            )

        return PaymentVerificationResult(
            success=True,
            ref_id=f"LIVE-{authority[-8:]}",
            gateway_response={"provider": "zarinpal", "mode": "live", "status": normalized_status},
        )

    def inquire_payment(self, *, authority: str) -> PaymentInquiryResult:
        return PaymentInquiryResult(
            found=False,
            gateway_status=None,
            ref_id="",
            amount_rial=None,
            order_id="",
            gateway_response={"provider": "zarinpal", "authority": authority},
            error_message="Inquiry is not supported by the legacy provider.",
        )
