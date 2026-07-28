"""Per-call session state shared across tools via RunContext.userdata."""
from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional


def normalize_phone(raw: str) -> str:
    """Keep digits and a leading '+', matching the mock API's normalization."""
    return "".join(c for c in raw if c.isdigit() or c == "+")


def valid_phone(raw: str) -> bool:
    digits = re.sub(r"\D", "", raw)
    return 7 <= len(digits) <= 15


@dataclass
class CallState:
    """Everything the agent knows about the current call.

    Preserved across turns and handed to a human verbatim on transfer, which
    satisfies the "context preserved during handoff" requirement.
    """

    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    # Collected customer info
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None

    # Last availability check result, keyed so create_reservation can verify
    # the slot was actually checked before writing.
    last_availability: Optional[dict[str, Any]] = None

    # Reservations created this call: idempotency_key -> reservation record
    created: dict[str, dict[str, Any]] = field(default_factory=dict)

    # Reservation located via find_reservation (modify/cancel target)
    found_reservation: Optional[dict[str, Any]] = None

    # Reservation ids cancelled this call (guards repeated cancel calls)
    cancelled_ids: set[str] = field(default_factory=set)

    # Set once a handoff has been queued
    handoff: Optional[dict[str, Any]] = None

    def idempotency_key(self, name: str, phone: str, date: str, time: str, party_size: int) -> str:
        """Deterministic per-session key: an identical create request (an LLM
        retry, a duplicated tool call, a network retry) always reuses the same
        key, so the API returns the original reservation instead of writing a
        duplicate."""
        raw = f"{self.session_id}|{name.strip().lower()}|{normalize_phone(phone)}|{date}|{time}|{party_size}"
        return "res-" + hashlib.sha256(raw.encode()).hexdigest()[:24]

    def remember_customer(self, name: Optional[str] = None, phone: Optional[str] = None) -> None:
        if name:
            self.customer_name = name
        if phone:
            self.customer_phone = normalize_phone(phone)

    def summary_for_handoff(self) -> str:
        """Structured context appended to every handoff so a human picks up
        with full state even if the model-written summary is thin."""
        parts: list[str] = []
        if self.customer_name:
            parts.append(f"Customer: {self.customer_name}")
        if self.customer_phone:
            parts.append(f"Phone: {self.customer_phone}")
        if self.last_availability:
            a = self.last_availability
            parts.append(
                f"Last availability check: {a.get('date')} {a.get('time')} party {a.get('party_size')} -> "
                f"{'available' if a.get('available') else 'unavailable'}"
            )
        for res in self.created.values():
            parts.append(
                f"Created this call: {res.get('confirmation_code')} {res.get('date')} {res.get('time')} "
                f"party {res.get('party_size')}"
            )
        if self.found_reservation:
            r = self.found_reservation
            parts.append(
                f"Reservation on file: {r.get('confirmation_code')} ({r.get('name')}) "
                f"{r.get('date')} {r.get('time')} party {r.get('party_size')} status {r.get('status')}"
            )
        if self.cancelled_ids:
            parts.append(f"Cancelled this call: {', '.join(sorted(self.cancelled_ids))}")
        return "; ".join(parts) if parts else "No structured state collected."
