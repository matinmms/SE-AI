from __future__ import annotations

import json
import random
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from django.conf import settings

from services.payments.base import (
    PaymentInquiryResult,
    PaymentRequestResult,
    PaymentVerificationResult,
)

try:
    import requests
except ImportError:  # pragma: no cover - production requirements include requests
    requests = None  # type: ignore[assignment]


class ZibalGatewayError(RuntimeError):
    def __init__(self, message: str, *, code: str = "gateway_error", transient: bool = False):
        super().__init__(message)
        self.code = code
        self.transient = transient


class ZibalPaymentProvider:
    def __init__(self) -> None:
        self._session = requests.Session() if requests is not None else None
        if self._session is not None:
            self._session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})

    @staticmethod
    def _safe_payload(payload: dict[str, Any]) -> dict[str, object]:
        allowed = {
            "result",
            "message",
            "trackId",
            "status",
            "amount",
            "refNumber",
            "orderId",
            "paidAt",
            "verifiedAt",
            "createdAt",
        }
        return {key: payload[key] for key in allowed if key in payload}

    @staticmethod
    def _optional_int(value: object) -> int | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _text(value: object) -> str:
        return "" if value is None else str(value).strip()

    @staticmethod
    def _require_gateway_url(url: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != "gateway.zibal.ir":
            raise ZibalGatewayError("نشانی درگاه پرداخت معتبر نیست.", code="invalid_gateway_url")
        return url

    def _post(self, url: str, body: dict[str, object], *, retries: int) -> dict[str, Any]:
        self._require_gateway_url(url)
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                if self._session is not None:
                    response = self._session.post(
                        url,
                        json=body,
                        timeout=(settings.ZIBAL_CONNECT_TIMEOUT_SECONDS, settings.ZIBAL_READ_TIMEOUT_SECONDS),
                    )
                    response.raise_for_status()
                    payload = response.json()
                else:
                    request = Request(
                        url,
                        data=json.dumps(body).encode("utf-8"),
                        headers={"Accept": "application/json", "Content-Type": "application/json"},
                        method="POST",
                    )
                    with urlopen(request, timeout=settings.ZIBAL_READ_TIMEOUT_SECONDS) as response:  # noqa: S310
                        payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("Response is not a JSON object.")
                return payload
            except tuple(
                [URLError]
                + ([requests.Timeout, requests.ConnectionError] if requests is not None else [])
            ) as exc:
                last_error = exc
                if attempt < retries:
                    time.sleep(min(0.75, 0.2 * (2**attempt)) + random.uniform(0, 0.08))
                    continue
                raise ZibalGatewayError(
                    "ارتباط با درگاه پرداخت موقتاً برقرار نشد.",
                    code="gateway_unavailable",
                    transient=True,
                ) from exc
            except tuple(
                [HTTPError, ValueError, json.JSONDecodeError]
                + ([requests.RequestException] if requests is not None else [])
            ) as exc:
                last_error = exc
                raise ZibalGatewayError("پاسخ نامعتبر از درگاه پرداخت دریافت شد.", code="invalid_gateway_response") from exc
        raise ZibalGatewayError(str(last_error or "Gateway request failed."), transient=True)

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
        amount_rial = amount * 10
        if settings.PAYMENT_MOCK_ENABLED:
            track_id = str(random.SystemRandom().randint(10_000_000_000, 99_999_999_999))
            return PaymentRequestResult(
                authority=track_id,
                payment_url=f"{settings.ZIBAL_START_URL.rstrip('/')}/{track_id}",
                gateway_response={"provider": "zibal", "mode": "mock", "result": 100, "trackId": track_id},
            )

        body: dict[str, object] = {
            "merchant": settings.ZIBAL_MERCHANT,
            "amount": amount_rial,
            "callbackUrl": callback_url,
            "description": description[:255],
            "orderId": order_id,
        }
        if mobile:
            body["mobile"] = mobile
        payload = self._post(settings.ZIBAL_REQUEST_URL, body, retries=0)
        if payload.get("result") != 100 or not str(payload.get("trackId", "")).isdigit():
            raise ZibalGatewayError(
                str(payload.get("message") or "ایجاد پرداخت در زیبال ناموفق بود."),
                code=f"zibal_request_{payload.get('result', 'invalid')}",
            )
        track_id = str(payload["trackId"])
        return PaymentRequestResult(
            authority=track_id,
            payment_url=f"{settings.ZIBAL_START_URL.rstrip('/')}/{track_id}",
            gateway_response={"provider": "zibal", **self._safe_payload(payload)},
        )

    def verify_payment(
        self,
        *,
        authority: str,
        amount: int,
        gateway_status: str,
        metadata: dict[str, object],
    ) -> PaymentVerificationResult:
        if settings.PAYMENT_MOCK_ENABLED:
            success = gateway_status in {"1", "2", "OK"} and not authority.startswith("FAIL")
            order_id = str(metadata.get("order_id", ""))
            amount_rial = amount * 10
            return PaymentVerificationResult(
                success=success,
                ref_id=f"ZBL-{authority[-8:]}" if success else "",
                gateway_response={
                    "provider": "zibal",
                    "mode": "mock",
                    "result": 100 if success else 202,
                    "status": 1 if success else 3,
                    "amount": amount_rial,
                    "orderId": order_id,
                },
                error_message="" if success else "Mock payment verification failed.",
                gateway_status=1 if success else 3,
                amount_rial=amount_rial,
                order_id=order_id,
            )

        payload = self._post(
            settings.ZIBAL_VERIFY_URL,
            {"merchant": settings.ZIBAL_MERCHANT, "trackId": int(authority)},
            retries=settings.ZIBAL_VERIFY_MAX_RETRIES,
        )
        result = self._optional_int(payload.get("result")) or 0
        status = self._optional_int(payload.get("status"))
        return PaymentVerificationResult(
            success=result == 100 and status == 1,
            ref_id=self._text(payload.get("refNumber")),
            gateway_response={"provider": "zibal", **self._safe_payload(payload)},
            error_message="" if result == 100 else str(payload.get("message") or "تایید پرداخت ناموفق بود."),
            gateway_status=status,
            amount_rial=self._optional_int(payload.get("amount")),
            order_id=self._text(payload.get("orderId")),
        )

    def inquire_payment(self, *, authority: str) -> PaymentInquiryResult:
        if settings.PAYMENT_MOCK_ENABLED:
            return PaymentInquiryResult(
                found=True,
                gateway_status=1,
                ref_id=f"ZBL-{authority[-8:]}",
                amount_rial=None,
                order_id="",
                gateway_response={"provider": "zibal", "mode": "mock", "result": 100, "status": 1},
            )
        payload = self._post(
            settings.ZIBAL_INQUIRY_URL,
            {"merchant": settings.ZIBAL_MERCHANT, "trackId": int(authority)},
            retries=settings.ZIBAL_VERIFY_MAX_RETRIES,
        )
        result = self._optional_int(payload.get("result")) or 0
        status = self._optional_int(payload.get("status"))
        return PaymentInquiryResult(
            found=result == 100,
            gateway_status=status,
            ref_id=self._text(payload.get("refNumber")),
            amount_rial=self._optional_int(payload.get("amount")),
            order_id=self._text(payload.get("orderId")),
            gateway_response={"provider": "zibal", **self._safe_payload(payload)},
            error_message="" if result == 100 else str(payload.get("message") or "استعلام پرداخت ناموفق بود."),
        )
