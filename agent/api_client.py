"""Async client for the Luma Bistro mock reservation API.

Responsibilities kept here (not in the LLM/tool layer):
  * per-request latency measurement + structured JSONL logging
  * bounded retry (exactly one) for transient failures on safe requests
  * idempotency-key propagation for reservation creation
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

logger = logging.getLogger("luma.api")

# 127.0.0.1 rather than localhost: on Windows, localhost tries IPv6 first and
# the fallback adds ~250 ms to every request.
API_BASE_URL = os.getenv("RESERVATION_API_URL", "http://127.0.0.1:8000")

# One retry, per the assessment spec ("Retry at most once").
MAX_ATTEMPTS = 2
DEFAULT_RETRY_AFTER_MS = 500


class APIUnavailableError(Exception):
    """Raised when the reservation API is still failing after the single retry."""


@dataclass
class APICallRecord:
    method: str
    path: str
    status: int | None
    latency_ms: float
    attempts: int
    ok: bool


@dataclass
class ReservationAPIClient:
    base_url: str = API_BASE_URL
    timeout_s: float = 5.0
    call_log: list[APICallRecord] = field(default_factory=list)
    _client: Optional[httpx.AsyncClient] = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout_s)
        return self._client

    async def aclose(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        retry: bool = True,
    ) -> httpx.Response:
        """Issue a request with latency logging and at most one retry.

        Retries fire only for transport errors, timeouts, and 503 — and only
        when `retry=True`. Creation is safe to retry because every create
        carries a deterministic Idempotency-Key.
        """
        client = await self._ensure_client()
        attempts = 0
        last_exc: Exception | None = None

        while attempts < MAX_ATTEMPTS:
            attempts += 1
            start = time.perf_counter()
            try:
                resp = await client.request(method, path, params=params, json=json_body, headers=headers)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                latency = (time.perf_counter() - start) * 1000
                self._log(method, path, None, latency, attempts, ok=False)
                last_exc = exc
                if retry and attempts < MAX_ATTEMPTS:
                    await asyncio.sleep(DEFAULT_RETRY_AFTER_MS / 1000)
                    continue
                raise APIUnavailableError(f"{method} {path} failed after {attempts} attempt(s): {exc}") from exc

            latency = (time.perf_counter() - start) * 1000
            self._log(method, path, resp.status_code, latency, attempts, ok=resp.status_code < 500)

            if resp.status_code == 503 and retry and attempts < MAX_ATTEMPTS:
                retry_after_ms = DEFAULT_RETRY_AFTER_MS
                try:
                    detail = resp.json().get("detail", {})
                    retry_after_ms = int(detail.get("retry_after_ms", DEFAULT_RETRY_AFTER_MS))
                except Exception:
                    pass
                await asyncio.sleep(retry_after_ms / 1000)
                continue

            if resp.status_code == 503:
                raise APIUnavailableError(f"{method} {path} returned 503 after {attempts} attempt(s)")

            return resp

        raise APIUnavailableError(f"{method} {path} failed: {last_exc}")

    def _log(self, method: str, path: str, status: int | None, latency_ms: float, attempts: int, ok: bool) -> None:
        record = APICallRecord(method, path, status, round(latency_ms, 1), attempts, ok)
        self.call_log.append(record)
        logger.info(
            json.dumps(
                {
                    "event": "api_call",
                    "method": method,
                    "path": path,
                    "status": status,
                    "latency_ms": record.latency_ms,
                    "attempt": attempts,
                    "ok": ok,
                }
            )
        )

    # ---- Typed endpoint helpers -------------------------------------------------

    async def health(self) -> dict:
        resp = await self.request("GET", "/health")
        return resp.json()

    async def check_availability(self, date: str, time_: str, party_size: int) -> httpx.Response:
        return await self.request(
            "GET",
            "/availability",
            params={"date": date, "time": time_, "party_size": party_size},
        )

    async def create_reservation(
        self,
        *,
        name: str,
        phone: str,
        date: str,
        time_: str,
        party_size: int,
        notes: str | None,
        idempotency_key: str,
    ) -> httpx.Response:
        return await self.request(
            "POST",
            "/reservations",
            json_body={
                "name": name,
                "phone": phone,
                "date": date,
                "time": time_,
                "party_size": party_size,
                "notes": notes,
            },
            headers={"Idempotency-Key": idempotency_key},
        )

    async def search_reservations(
        self, phone: str | None = None, confirmation_code: str | None = None
    ) -> httpx.Response:
        params: dict[str, Any] = {}
        if phone:
            params["phone"] = phone
        if confirmation_code:
            params["confirmation_code"] = confirmation_code
        return await self.request("GET", "/reservations/search", params=params)

    async def update_reservation(self, reservation_id: str, patch: dict[str, Any]) -> httpx.Response:
        # PATCH is not blindly retried: without an idempotency key a repeat
        # could double-apply against shifting capacity.
        return await self.request("PATCH", f"/reservations/{reservation_id}", json_body=patch, retry=False)

    async def cancel_reservation(self, reservation_id: str) -> httpx.Response:
        # Cancel is idempotent server-side (second cancel is a no-op), so the
        # single retry is safe.
        return await self.request("POST", f"/reservations/{reservation_id}/cancel")

    async def handoff(self, reason: str, customer_phone: str | None, conversation_summary: str) -> httpx.Response:
        return await self.request(
            "POST",
            "/handoff",
            json_body={
                "reason": reason,
                "customer_phone": customer_phone,
                "conversation_summary": conversation_summary,
            },
        )
