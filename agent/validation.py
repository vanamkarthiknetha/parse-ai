"""Explicit validation of LLM-produced tool arguments.

Every tool validates its arguments here before any network call. Invalid
arguments never reach the API; the tool returns a corrective message the LLM
can act on (re-ask the caller) instead of raising.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Optional

from .state import valid_phone

OPEN_DAYS = {1, 2, 3, 4, 5, 6}  # Tue(1)..Sun(6); Monday(0) closed
FIRST_SLOT = (17, 0)
LAST_SLOT = (21, 30)


class ValidationError(Exception):
    """Human-relayable validation failure."""


def parse_date(raw: str, today: Optional[date] = None) -> str:
    raw = (raw or "").strip()
    try:
        d = datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        raise ValidationError(
            f"Invalid date '{raw}'. Dates must be in YYYY-MM-DD format — convert the caller's words first."
        )
    today = today or date.today()
    if d < today:
        raise ValidationError(f"{d.isoformat()} is in the past. Ask the caller for a future date.")
    if d.weekday() == 0:
        raise ValidationError(f"{d.isoformat()} is a Monday and Luma Bistro is closed on Mondays.")
    return d.isoformat()


def parse_time(raw: str) -> str:
    raw = (raw or "").strip()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
    if not m:
        raise ValidationError(
            f"Invalid time '{raw}'. Times must be 24-hour HH:MM — e.g. 6:30 PM is '18:30'."
        )
    hh, mm = int(m.group(1)), int(m.group(2))
    if mm not in (0, 30):
        raise ValidationError(
            f"'{raw}' is not on the half hour. Reservations are in 30-minute slots (e.g. 18:00, 18:30)."
        )
    if not (FIRST_SLOT <= (hh, mm) <= LAST_SLOT):
        raise ValidationError(
            f"'{raw}' is outside dinner service. Luma Bistro seats from 17:00 to 21:30."
        )
    return f"{hh:02d}:{mm:02d}"


def parse_party_size(raw: int) -> int:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        raise ValidationError(f"Invalid party size '{raw}'.")
    if n < 1:
        raise ValidationError("Party size must be at least 1.")
    if n > 8:
        raise ValidationError(
            f"Party size {n} exceeds the standard maximum of 8. Offer to transfer the caller to a human "
            "for large-party bookings (use transfer_to_human)."
        )
    return n


def parse_name(raw: str) -> str:
    name = (raw or "").strip()
    if len(name) < 2:
        raise ValidationError("The name is too short — ask the caller for their full name.")
    if len(name) > 100:
        raise ValidationError("The name is too long — ask the caller to repeat it briefly.")
    return name


def parse_phone(raw: str) -> str:
    if not valid_phone(raw or ""):
        raise ValidationError(
            f"'{raw}' does not look like a valid phone number. Ask the caller to repeat it and read it back."
        )
    return raw.strip()
