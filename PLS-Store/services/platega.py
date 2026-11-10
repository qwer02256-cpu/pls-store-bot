"""
Platega.io payment client.
Official docs: https://docs.platega.io/
Base URL: https://app.platega.io/
Auth: headers X-MerchantId + X-Secret
Create: POST /transaction/process  (with paymentMethod) or POST /v2/transaction/process (method chosen on page)
Status: GET /transaction/{id}
Callback: POST to your URL with same X-MerchantId / X-Secret headers, body status CONFIRMED | CANCELED | ...
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

log = logging.getLogger("pls-store.platega")

BASE_URL = "https://app.platega.io"


class PlategaError(Exception):
    def __init__(self, message: str, status_code: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class PlategaClient:
    def __init__(self, merchant_id: str, secret: str, timeout: float = 30.0):
        if not merchant_id or not secret:
            raise ValueError("PLATEGA_MERCHANT_ID and PLATEGA_SECRET must be set")
        self.merchant_id = merchant_id
        self.secret = secret
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={
                "X-MerchantId": merchant_id,
                "X-Secret": secret,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def create_payment(
        self,
        amount: float,
        currency: str = "RUB",
        description: str = "Пополнение баланса PLS Store",
        payload: str = "",
        return_url: str = "https://t.me/",
        failed_url: str = "https://t.me/",
        payment_method: int | None = 2,  # 2 = SBP QR (most common for RU)
        metadata: dict | None = None,
    ) -> dict[str, Any]:
        """
        Create payment.
        payment_method=2 → SBP/QR (POST /transaction/process)
        payment_method=None → payer chooses on Platega page (POST /v2/transaction/process)
        """
        body: dict[str, Any] = {
            "paymentDetails": {
                "amount": float(amount),
                "currency": currency,
            },
            "description": description[:200],
            "return": return_url,
            "failedUrl": failed_url,
            "payload": payload or "",
            "metadata": metadata or {},
        }
        if payment_method is not None:
            body["paymentMethod"] = payment_method
            endpoint = "/transaction/process"
        else:
            endpoint = "/v2/transaction/process"

        try:
            resp = await self._client.post(endpoint, json=body)
            data = resp.json() if resp.content else {}
            if resp.status_code >= 400:
                log.error("Platega create error %s: %s", resp.status_code, data)
                raise PlategaError(
                    f"Platega error {resp.status_code}: {data}",
                    status_code=resp.status_code,
                    payload=data,
                )
            return data
        except httpx.HTTPError as e:
            log.exception("Platega HTTP error on create")
            raise PlategaError(str(e)) from e

    async def get_status(self, transaction_id: str) -> dict[str, Any]:
        try:
            resp = await self._client.get(f"/transaction/{transaction_id}")
            data = resp.json() if resp.content else {}
            if resp.status_code >= 400:
                raise PlategaError(
                    f"Status error {resp.status_code}: {data}",
                    status_code=resp.status_code,
                    payload=data,
                )
            return data
        except httpx.HTTPError as e:
            raise PlategaError(str(e)) from e

    def verify_callback_headers(self, headers: dict) -> bool:
        """Verify that callback comes from Platega (same X-MerchantId + X-Secret)."""
        mid = headers.get("X-MerchantId") or headers.get("x-merchantid") or ""
        sec = headers.get("X-Secret") or headers.get("x-secret") or ""
        return mid == self.merchant_id and sec == self.secret


# Payment method codes from official docs
PAYMENT_METHODS = {
    2: "СБП / QR",
    3: "ERIP",
    11: "Карты (acquiring)",
    12: "Международный",
    13: "Криптовалюта",
    14: "Sberpay",
}
