from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class PaymentRequestResult:
    authority: str
    payment_url: str
    gateway_response: dict[str, object]


@dataclass(frozen=True)
class PaymentVerificationResult:
    success: bool
    ref_id: str
    gateway_response: dict[str, object]
    error_message: str = ""
    gateway_status: int | None = None
    amount_rial: int | None = None
    order_id: str = ""


@dataclass(frozen=True)
class PaymentInquiryResult:
    found: bool
    gateway_status: int | None
    ref_id: str
    amount_rial: int | None
    order_id: str
    gateway_response: dict[str, object]
    error_message: str = ""


class PaymentGatewayProvider(Protocol):
    def create_payment_request(
        self,
        *,
        amount: int,
        callback_url: str,
        description: str,
        metadata: dict[str, object],
        order_id: str = "",
        mobile: str = "",
    ) -> PaymentRequestResult: ...

    def verify_payment(
        self,
        *,
        authority: str,
        amount: int,
        gateway_status: str,
        metadata: dict[str, object],
    ) -> PaymentVerificationResult: ...

    def inquire_payment(self, *, authority: str) -> PaymentInquiryResult: ...
