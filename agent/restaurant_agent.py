"""Luma Bistro voice agent: LiveKit Agent definition and tool implementations.

Design notes:
  * Tools return plain dicts/strings the LLM relays; they never raise into the
    LLM loop. Validation failures become corrective messages.
  * Write operations are guarded in code, not just in the prompt:
      - create_reservation requires a matching successful availability check
        from this session and uses a deterministic idempotency key.
      - modify/cancel require the reservation to have been located via
        find_reservation in this session.
"""
from __future__ import annotations

import logging
from typing import Optional

from livekit.agents import Agent, RunContext, function_tool

from .api_client import APIUnavailableError, ReservationAPIClient
from .prompts import system_prompt
from .state import CallState, normalize_phone
from .validation import (
    ValidationError,
    parse_date,
    parse_name,
    parse_party_size,
    parse_phone,
    parse_time,
)

logger = logging.getLogger("luma.agent")

TEMP_FAILURE_MSG = (
    "The reservation system is temporarily unavailable even after retrying. "
    "Apologize, and offer to either try once more in a moment or hand the caller to a human. Do not guess."
)


def _detail_code(resp) -> str:
    try:
        detail = resp.json().get("detail", {})
        return detail.get("code", "") if isinstance(detail, dict) else str(detail)
    except Exception:
        return ""


def _alternatives(resp) -> list[dict]:
    try:
        detail = resp.json().get("detail", {})
        if isinstance(detail, dict):
            return detail.get("alternatives", []) or []
    except Exception:
        pass
    return []


class RestaurantAgent(Agent):
    def __init__(self, api: ReservationAPIClient | None = None) -> None:
        super().__init__(instructions=system_prompt())
        self.api = api or ReservationAPIClient()

    @staticmethod
    def _known_reservation(state: CallState, reservation_id: str) -> Optional[dict]:
        """A reservation is a valid modify/cancel target if it was located via
        find_reservation OR created earlier in this same call (so a correction
        arriving after the write becomes a clean modify, never a duplicate)."""
        found = state.found_reservation
        if found and found.get("reservation_id") == reservation_id:
            return found
        for res in state.created.values():
            if res.get("reservation_id") == reservation_id:
                return res
        return None

    # ------------------------------------------------------------------ tools

    @function_tool
    async def check_availability(
        self,
        context: RunContext[CallState],
        date: str,
        time: str,
        party_size: int,
    ) -> dict | str:
        """Check whether a table is available. Always call this before promising any
        time or creating a reservation.

        Args:
            date: Reservation date in YYYY-MM-DD.
            time: Reservation time in 24-hour HH:MM (30-minute slots).
            party_size: Number of guests (1-8).
        """
        state = context.userdata
        try:
            date = parse_date(date)
            time = parse_time(time)
            party_size = parse_party_size(party_size)
        except ValidationError as e:
            return {"error": str(e)}

        try:
            resp = await self.api.check_availability(date, time, party_size)
        except APIUnavailableError:
            return {"error": TEMP_FAILURE_MSG}

        if resp.status_code == 422:
            return {
                "available": False,
                "error": (
                    f"{date} at {time} is not on the booking calendar. "
                    "Offer to check a different date or time instead."
                ),
            }
        if resp.status_code != 200:
            return {"error": f"Unexpected availability error (HTTP {resp.status_code}). {TEMP_FAILURE_MSG}"}

        data = resp.json()
        state.last_availability = {
            "date": date,
            "time": time,
            "party_size": party_size,
            "available": data["available"],
        }
        result: dict = {
            "available": data["available"],
            "date": date,
            "time": time,
            "party_size": party_size,
        }
        if not data["available"]:
            alts = data.get("alternatives", [])
            result["alternatives"] = alts
            result["instruction"] = (
                "That slot is unavailable. Offer ONLY these alternative times to the caller."
                if alts
                else "That slot is unavailable and no alternatives exist that day. Offer to try another date."
            )
        return result

    @function_tool
    async def create_reservation(
        self,
        context: RunContext[CallState],
        name: str,
        phone: str,
        date: str,
        time: str,
        party_size: int,
        caller_confirmed: bool,
        notes: Optional[str] = None,
    ) -> dict | str:
        """Create the reservation. Call ONLY after (1) check_availability confirmed
        this exact slot and (2) you read ALL details back and the caller clearly
        said yes.

        Args:
            name: Customer's full name.
            phone: Customer's phone number.
            date: Reservation date in YYYY-MM-DD.
            time: Reservation time in 24-hour HH:MM.
            party_size: Number of guests (1-8).
            caller_confirmed: True ONLY if you already read back name, phone,
                date, time and party size and the caller explicitly confirmed.
            notes: Optional special requests.
        """
        state = context.userdata
        if not caller_confirmed:
            return {
                "error": (
                    "Do not book yet: read all details back to the caller and get an explicit yes, "
                    "then call again with caller_confirmed=true."
                )
            }
        try:
            name = parse_name(name)
            phone = parse_phone(phone)
            date = parse_date(date)
            time = parse_time(time)
            party_size = parse_party_size(party_size)
        except ValidationError as e:
            return {"error": str(e)}

        state.remember_customer(name=name, phone=phone)
        key = state.idempotency_key(name, phone, date, time, party_size)

        # Duplicate guard: identical reservation already created this call.
        if key in state.created:
            existing = state.created[key]
            return {
                "status": "already_booked",
                "reservation": existing,
                "instruction": (
                    "This exact reservation already exists from this call — do NOT announce a new booking. "
                    f"Remind the caller of confirmation code {existing.get('confirmation_code')}."
                ),
            }

        # Guard: require a successful availability check for this exact slot.
        last = state.last_availability
        if not last or (last["date"], last["time"]) != (date, time) or last["party_size"] != party_size:
            return {
                "error": (
                    "Availability has not been checked for this exact date/time/party size. "
                    "Call check_availability first, then confirm with the caller, then create."
                )
            }
        if not last["available"]:
            return {
                "error": "The last availability check said this slot is NOT available. Offer the alternatives instead."
            }

        try:
            resp = await self.api.create_reservation(
                name=name,
                phone=phone,
                date=date,
                time_=time,
                party_size=party_size,
                notes=notes,
                idempotency_key=key,
            )
        except APIUnavailableError:
            return {"error": TEMP_FAILURE_MSG}

        if resp.status_code == 409:
            return {
                "error": "The slot filled up before booking completed.",
                "alternatives": _alternatives(resp),
                "instruction": "Apologize and offer only these alternatives.",
            }
        if resp.status_code == 422:
            return {"error": f"The system rejected the details ({_detail_code(resp)}). Re-check them with the caller."}
        if resp.status_code != 200:
            return {"error": f"Unexpected booking error (HTTP {resp.status_code}). {TEMP_FAILURE_MSG}"}

        reservation = resp.json()
        state.created[key] = reservation
        logger.info("reservation created: %s", reservation.get("confirmation_code"))
        return {
            "status": "confirmed",
            "reservation": reservation,
            "instruction": (
                f"Tell the caller the booking is confirmed and read the confirmation code "
                f"{reservation.get('confirmation_code')} clearly."
            ),
        }

    @function_tool
    async def find_reservation(
        self,
        context: RunContext[CallState],
        phone: Optional[str] = None,
        confirmation_code: Optional[str] = None,
    ) -> dict | str:
        """Look up an existing reservation by phone number or confirmation code.
        Required before any modification or cancellation.

        Args:
            phone: Phone number on the reservation.
            confirmation_code: Confirmation code like LUMA-4821.
        """
        state = context.userdata
        if not phone and not confirmation_code:
            return {"error": "Ask the caller for either their confirmation code or the phone number on the booking."}
        if phone:
            try:
                phone = parse_phone(phone)
            except ValidationError as e:
                return {"error": str(e)}
            state.remember_customer(phone=phone)

        try:
            resp = await self.api.search_reservations(
                phone=normalize_phone(phone) if phone else None,
                confirmation_code=confirmation_code,
            )
        except APIUnavailableError:
            return {"error": TEMP_FAILURE_MSG}

        if resp.status_code != 200:
            return {"error": f"Lookup failed (HTTP {resp.status_code}). {TEMP_FAILURE_MSG}"}

        results = resp.json().get("results", [])
        active = [r for r in results if r.get("status") != "cancelled"]
        if not active:
            return {
                "found": False,
                "instruction": (
                    "No active reservation found. Re-confirm the code or phone number with the caller; "
                    "after two failures, offer a human."
                ),
            }
        if len(active) == 1:
            state.found_reservation = active[0]
            state.remember_customer(name=active[0].get("name"), phone=active[0].get("phone"))
            return {
                "found": True,
                "reservation": active[0],
                "instruction": "Read the reservation back to the caller and confirm it is the right one before changing anything.",
            }
        return {
            "found": True,
            "multiple": True,
            "reservations": active,
            "instruction": "Multiple reservations found — ask the caller which one they mean.",
        }

    @function_tool
    async def modify_reservation(
        self,
        context: RunContext[CallState],
        reservation_id: str,
        new_date: Optional[str] = None,
        new_time: Optional[str] = None,
        new_party_size: Optional[int] = None,
        new_notes: Optional[str] = None,
    ) -> dict | str:
        """Modify an existing reservation. Call ONLY after find_reservation located it
        and the caller confirmed the exact change.

        Args:
            reservation_id: The reservation_id from find_reservation.
            new_date: New date YYYY-MM-DD, if changing.
            new_time: New time HH:MM, if changing.
            new_party_size: New party size (1-8), if changing.
            new_notes: Replacement notes, if changing.
        """
        state = context.userdata
        target = self._known_reservation(state, reservation_id)
        if target is None:
            return {"error": "Locate the reservation with find_reservation and confirm it with the caller first."}
        if not any([new_date, new_time, new_party_size, new_notes is not None]):
            return {"error": "No changes given. Ask the caller what they want to change."}

        patch: dict = {}
        try:
            if new_date:
                patch["date"] = parse_date(new_date)
            if new_time:
                patch["time"] = parse_time(new_time)
            if new_party_size is not None:
                patch["party_size"] = parse_party_size(new_party_size)
            if new_notes is not None:
                patch["notes"] = new_notes
        except ValidationError as e:
            return {"error": str(e)}

        try:
            resp = await self.api.update_reservation(reservation_id, patch)
        except APIUnavailableError:
            return {"error": TEMP_FAILURE_MSG}

        if resp.status_code == 404:
            return {"error": "That reservation no longer exists. Re-check with find_reservation."}
        if resp.status_code == 409:
            code = _detail_code(resp)
            if code == "ALREADY_CANCELLED":
                return {"error": "That reservation was already cancelled and cannot be modified. Offer a new booking."}
            return {
                "error": "The requested new slot is not available.",
                "alternatives": _alternatives(resp),
                "instruction": "Offer ONLY these alternatives; the original reservation is unchanged.",
            }
        if resp.status_code == 422:
            return {"error": f"The new details were rejected ({_detail_code(resp)}). Re-check them with the caller."}
        if resp.status_code != 200:
            return {"error": f"Unexpected modify error (HTTP {resp.status_code}). {TEMP_FAILURE_MSG}"}

        updated = resp.json()
        state.found_reservation = updated
        return {
            "status": "updated",
            "reservation": updated,
            "instruction": "Confirm the updated details back to the caller.",
        }

    @function_tool
    async def cancel_reservation(self, context: RunContext[CallState], reservation_id: str) -> dict | str:
        """Cancel a reservation. Call ONLY after find_reservation located it and the
        caller explicitly confirmed they want it cancelled.

        Args:
            reservation_id: The reservation_id from find_reservation.
        """
        state = context.userdata
        if self._known_reservation(state, reservation_id) is None:
            return {"error": "Locate the reservation with find_reservation and confirm the cancellation first."}
        if reservation_id in state.cancelled_ids:
            return {
                "status": "already_cancelled",
                "instruction": "It was already cancelled during this call — reassure the caller; do not cancel again.",
            }

        try:
            resp = await self.api.cancel_reservation(reservation_id)
        except APIUnavailableError:
            return {"error": TEMP_FAILURE_MSG}

        if resp.status_code == 404:
            return {"error": "That reservation no longer exists."}
        if resp.status_code != 200:
            return {"error": f"Unexpected cancel error (HTTP {resp.status_code}). {TEMP_FAILURE_MSG}"}

        state.cancelled_ids.add(reservation_id)
        cancelled = resp.json()
        state.found_reservation = cancelled
        return {
            "status": "cancelled",
            "reservation": cancelled,
            "instruction": "Confirm the cancellation to the caller and offer to rebook any time.",
        }

    @function_tool
    async def transfer_to_human(self, context: RunContext[CallState], reason: str, summary: str) -> dict | str:
        """Hand the call to a human host. Use for parties over 8, repeated system
        failures, requests you cannot complete, or when the caller asks for a person.

        Args:
            reason: Short machine-readable reason (e.g. 'large_party', 'api_failure', 'caller_request').
            summary: Complete summary of the conversation and the caller's request so far.
        """
        state = context.userdata
        if state.handoff:
            return {
                "status": "already_queued",
                "handoff": state.handoff,
                "instruction": "A human is already on the way — reassure the caller.",
            }

        full_summary = f"{summary.strip()} | State: {state.summary_for_handoff()}"
        try:
            resp = await self.api.handoff(reason, state.customer_phone, full_summary)
        except APIUnavailableError:
            # Even the handoff endpoint failing must not strand the caller.
            return {
                "status": "handoff_endpoint_down",
                "instruction": (
                    "Tell the caller you could not reach a host right now either; ask them to call back shortly. "
                    "Apologize sincerely."
                ),
            }

        if resp.status_code != 200:
            return {"status": "handoff_failed", "instruction": "Apologize and ask the caller to call back shortly."}

        state.handoff = resp.json()
        return {
            "status": "queued",
            "handoff": state.handoff,
            "instruction": "Tell the caller a human host will pick up shortly and that their details have been passed along.",
        }
