"""Standard test scenarios T1-T7 from standard_test_cases.json.

Each test drives the REAL agent (same tools, same prompt, same API client)
through LiveKit's text-mode AgentSession, then asserts against the mock API's
state — the ground truth — rather than against model wording. Tool-call
accuracy is asserted from the API client's own call log.

Requires an LLM key (ANTHROPIC_API_KEY or OPENAI_API_KEY + LLM_PROVIDER=openai).
Voice-path latency (end-of-speech -> first audio) is measured in live sessions
by agent/metrics_logger.py; here we record LLM wall-time and API latency.
"""
from __future__ import annotations

import os
import time

import httpx
import pytest

from agent.main import build_llm, resolve_llm_provider
from agent.restaurant_agent import RestaurantAgent
from agent.state import CallState

from livekit.agents import AgentSession

HAS_LLM_KEY = any(
    os.getenv(k) for k in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")
)

pytestmark = pytest.mark.skipif(
    not HAS_LLM_KEY,
    reason="no LLM API key configured (set GOOGLE_API_KEY/GEMINI_API_KEY, OPENAI_API_KEY, or ANTHROPIC_API_KEY)",
)


class ScenarioRun:
    """Drives one scripted conversation and captures timing + tool calls."""

    def __init__(self) -> None:
        self.agent = RestaurantAgent()
        self.state = CallState()
        self.turn_seconds: list[float] = []
        self.replies: list[str] = []

    async def run(self, script: list[str]) -> None:
        llm = build_llm()
        session: AgentSession[CallState] = AgentSession(llm=llm, userdata=self.state)
        try:
            await session.start(self.agent)
            for line in script:
                t0 = time.perf_counter()
                result = await session.run(user_input=line)
                self.turn_seconds.append(time.perf_counter() - t0)
                for ev in result.events:
                    item = getattr(ev, "item", None)
                    if getattr(item, "type", "") == "message" and getattr(item, "role", "") == "assistant":
                        content = getattr(item, "content", None)
                        if content:
                            self.replies.append(" ".join(str(c) for c in content))
        finally:
            await session.aclose()
            await self.agent.api.aclose()

    # -- helpers over the HTTP-level call log (ground truth for tool accuracy)
    def calls(self, method: str, path_prefix: str) -> list:
        return [c for c in self.agent.api.call_log if c.method == method and c.path.startswith(path_prefix)]

    def api_latencies_ms(self) -> list[float]:
        return [c.latency_ms for c in self.agent.api.call_log]

    def record(self, recorder: list, test_id: str, name: str, outcome: str, notes: str = "") -> None:
        lat = self.api_latencies_ms()
        recorder.append(
            {
                "test": test_id,
                "name": name,
                "outcome": outcome,
                "tool_http_calls": [f"{c.method} {c.path} -> {c.status} (attempt {c.attempts})" for c in self.agent.api.call_log],
                "creates": len(self.calls("POST", "/reservations") ) - len(self.calls("POST", "/reservations/")),
                "turn_wall_seconds": [round(s, 2) for s in self.turn_seconds],
                "api_latency_ms": {"max": max(lat) if lat else None, "all": lat},
                "notes": notes,
            }
        )


def _search(base: str, **params) -> list[dict]:
    return httpx.get(f"{base}/reservations/search", params=params, timeout=5.0).json()["results"]


def _remaining(base: str, date: str, time_: str) -> int:
    return httpx.get(
        f"{base}/availability", params={"date": date, "time": time_, "party_size": 1}, timeout=5.0
    ).json()["remaining_capacity"]


# ---------------------------------------------------------------------- T1


async def test_t1_create_available_reservation(reset_api, recorder):
    run = ScenarioRun()
    await run.run(
        [
            "Hi, I'd like to reserve a table for four on Friday, August 14 at 6 PM.",
            "The name is Jordan Lee, phone number 310-555-0199.",
            "No notes.",
            "Yes, that's all correct — please confirm.",
        ]
    )
    results = _search(reset_api, phone="3105550199")
    assert len(results) == 1, f"expected exactly one reservation, got {results}"
    r = results[0]
    assert (r["date"], r["time"], r["party_size"]) == ("2026-08-14", "18:00", 4)
    assert len(run.calls("GET", "/availability")) >= 1, "must check availability before booking"
    creates = [c for c in run.calls("POST", "/reservations") if "/cancel" not in c.path]
    assert len(creates) == 1, "exactly one create call"
    run.record(recorder, "T1", "Create available reservation", "booked 2026-08-14 18:00 x4")


# ---------------------------------------------------------------------- T2


async def test_t2_unavailable_time_offers_alternatives(reset_api, recorder):
    run = ScenarioRun()
    await run.run(
        [
            "I want to book a table for four people on Friday, August 14 at 6:30 PM.",
            "Hmm, okay — I can do 7:30 PM instead.",
            "Taylor Kim, 424-555-0188.",
            "Yes, confirm it.",
        ]
    )
    results = _search(reset_api, phone="4245550188")
    assert len(results) == 1
    assert (results[0]["date"], results[0]["time"], results[0]["party_size"]) == ("2026-08-14", "19:30", 4)
    availability_calls = run.calls("GET", "/availability")
    assert len(availability_calls) >= 2, "should have checked 18:30 (full) and then 19:30"
    creates = [c for c in run.calls("POST", "/reservations") if "/cancel" not in c.path]
    assert len(creates) == 1
    run.record(recorder, "T2", "Unavailable time", "offered API alternatives; booked 19:30")


# ---------------------------------------------------------------------- T3


async def test_t3_correction_before_booking(reset_api, recorder):
    run = ScenarioRun()
    await run.run(
        [
            "Table on Saturday, August 15 at 6:30 PM for two people, please.",
            "Casey Brown, 213-555-0114.",
            # Correction arrives while the agent is confirming (text analog of barge-in)
            "Sorry, wait — make that four people, not two.",
            "Yes, that's right, confirm it.",
        ]
    )
    results = _search(reset_api, phone="2135550114")
    assert len(results) == 1, f"expected exactly one reservation, got {results}"
    assert results[0]["party_size"] == 4, "final party size must reflect the correction"
    assert (results[0]["date"], results[0]["time"]) == ("2026-08-15", "18:30")
    creates = [c for c in run.calls("POST", "/reservations") if "/cancel" not in c.path]
    assert len(creates) == 1, "correction must not produce a second reservation"
    run.record(recorder, "T3", "Correction / barge-in", "single booking with corrected party size 4")


# ---------------------------------------------------------------------- T4


async def test_t4_modify_existing(reset_api, recorder):
    run = ScenarioRun()
    await run.run(
        [
            "I need to change my reservation, the confirmation code is LUMA-4821.",
            "Move it to 7:30 PM on the same date, and make it four people.",
            "Yes, please confirm the change.",
        ]
    )
    r = _search(reset_api, confirmation_code="LUMA-4821")[0]
    assert (r["date"], r["time"], r["party_size"]) == ("2026-08-14", "19:30", 4)
    assert r["status"] == "confirmed"
    assert len(run.calls("GET", "/reservations/search")) >= 1, "must search before modifying"
    assert len(run.calls("PATCH", "/reservations/res_existing_4821")) == 1
    assert _remaining(reset_api, "2026-08-14", "18:00") == 6  # 4 + returned 2
    assert _remaining(reset_api, "2026-08-14", "19:30") == 4  # 8 - 4
    run.record(recorder, "T4", "Modify existing reservation", "PATCH applied once; capacity moved")


# ---------------------------------------------------------------------- T5


async def test_t5_cancel_existing(reset_api, recorder):
    run = ScenarioRun()
    await run.run(
        [
            "I'd like to cancel my reservation. The code is LUMA-4821.",
            "Yes, cancel it.",
        ]
    )
    r = _search(reset_api, confirmation_code="LUMA-4821")[0]
    assert r["status"] == "cancelled"
    assert len(run.calls("GET", "/reservations/search")) >= 1, "must search before cancelling"
    cancels = run.calls("POST", "/reservations/res_existing_4821/cancel")
    assert len(cancels) == 1, "cancel exactly once"
    assert _remaining(reset_api, "2026-08-14", "18:00") == 6  # capacity restored
    run.record(recorder, "T5", "Cancel existing reservation", "cancelled once after confirmation")


# ---------------------------------------------------------------------- T6


async def test_t6_temporary_api_failure_retry_once(reset_api, recorder):
    run = ScenarioRun()
    await run.run(
        [
            "Could you check Sunday, August 16 at 6 PM for two people?",
        ]
    )
    availability = run.calls("GET", "/availability")
    assert availability, "availability must be checked"
    statuses = [c.status for c in availability]
    assert 503 in statuses, "first call should have hit the seeded 503"
    assert statuses.count(503) == 1, "retry at most once"
    assert 200 in statuses, "retry should have succeeded"
    # Nothing invented, nothing written
    assert not [c for c in run.calls("POST", "/reservations") if "/cancel" not in c.path]
    run.record(recorder, "T6", "Temporary API failure", "503 absorbed by single retry; real result relayed")


# ---------------------------------------------------------------------- T7


async def test_t7_duplicate_protection(reset_api, recorder):
    run = ScenarioRun()
    await run.run(
        [
            "Book a table on Friday, August 14 at 8 PM for two.",
            "Morgan Reed, 310-555-0166.",
            "Yes, confirm.",
            # Caller repeats the request verbatim after booking
            "Sorry, one more time — book Friday August 14 at 8 PM for two, Morgan Reed, 310-555-0166.",
        ]
    )
    results = _search(reset_api, phone="3105550166")
    assert len(results) == 1, f"duplicate reservation created: {results}"
    reservation = results[0]

    # Replay the create at the HTTP layer with the SAME idempotency key the
    # agent used — the literal T7 instruction.
    r = httpx.post(
        f"{reset_api}/reservations",
        json={
            "name": reservation["name"],
            "phone": reservation["phone"],
            "date": reservation["date"],
            "time": reservation["time"],
            "party_size": reservation["party_size"],
            "notes": reservation.get("notes"),
        },
        headers={"Idempotency-Key": _extract_idempotency_key(run)},
        timeout=5.0,
    )
    assert r.status_code == 200
    assert r.json()["reservation_id"] == reservation["reservation_id"], "same key must return same record"
    assert len(_search(reset_api, phone="3105550166")) == 1, "still exactly one reservation"
    run.record(recorder, "T7", "Duplicate protection", "same idempotency key returned original record")


def _extract_idempotency_key(run: ScenarioRun) -> str:
    """The exact Idempotency-Key the agent sent, taken from session state."""
    assert run.state.created, "agent recorded no created reservation"
    return next(iter(run.state.created.keys()))
