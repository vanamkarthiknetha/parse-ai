"""Direct contract tests against the mock reservation API.

These validate the behaviors the agent depends on — idempotency, the seeded
503, 409 alternatives, capacity accounting — and run without any LLM/STT keys.
Also covers the mechanical half of T7 (same idempotency key => same record).
"""
from __future__ import annotations

import uuid

import httpx


def _client(base: str) -> httpx.Client:
    return httpx.Client(base_url=base, timeout=5.0)


def test_health_and_restaurant(reset_api):
    with _client(reset_api) as c:
        assert c.get("/health").json()["status"] == "ok"
        info = c.get("/restaurant").json()
        assert info["name"] == "Luma Bistro"
        assert info["max_standard_party_size"] == 8


def test_availability_and_alternatives(reset_api):
    with _client(reset_api) as c:
        # 18:30 on Aug 14 is fully booked (capacity 0) -> alternatives offered
        r = c.get("/availability", params={"date": "2026-08-14", "time": "18:30", "party_size": 4}).json()
        assert r["available"] is False
        alt_times = {a["time"] for a in r["alternatives"]}
        assert alt_times and "18:30" not in alt_times
        for a in r["alternatives"]:
            assert a["remaining_capacity"] >= 4


def test_invalid_slot_returns_422(reset_api):
    with _client(reset_api) as c:
        r = c.get("/availability", params={"date": "2026-09-01", "time": "18:00", "party_size": 2})
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "INVALID_SLOT"


def test_seeded_503_then_success(reset_api):
    with _client(reset_api) as c:
        first = c.get("/availability", params={"date": "2026-08-16", "time": "18:00", "party_size": 2})
        assert first.status_code == 503
        assert first.json()["detail"]["code"] == "TEMPORARY_UPSTREAM_FAILURE"
        second = c.get("/availability", params={"date": "2026-08-16", "time": "18:00", "party_size": 2})
        assert second.status_code == 200
        assert second.json()["available"] is True


def test_idempotent_create_no_duplicate(reset_api):
    """T7 mechanics: replaying the create with the same key returns the same
    reservation and consumes capacity exactly once."""
    with _client(reset_api) as c:
        key = f"test-{uuid.uuid4().hex[:12]}"
        body = {
            "name": "Morgan Reed",
            "phone": "310-555-0166",
            "date": "2026-08-14",
            "time": "20:00",
            "party_size": 2,
            "notes": None,
        }
        r1 = c.post("/reservations", json=body, headers={"Idempotency-Key": key})
        assert r1.status_code == 200
        r2 = c.post("/reservations", json=body, headers={"Idempotency-Key": key})
        assert r2.status_code == 200
        assert r1.json()["reservation_id"] == r2.json()["reservation_id"]

        # capacity 20:00 started at 6; exactly one decrement of 2
        avail = c.get("/availability", params={"date": "2026-08-14", "time": "20:00", "party_size": 1}).json()
        assert avail["remaining_capacity"] == 4

        results = c.get("/reservations/search", params={"phone": "310-555-0166"}).json()["results"]
        assert len(results) == 1


def test_create_conflict_offers_alternatives(reset_api):
    with _client(reset_api) as c:
        body = {
            "name": "Test Full",
            "phone": "555-000-1111",
            "date": "2026-08-14",
            "time": "18:30",  # capacity 0
            "party_size": 2,
        }
        r = c.post("/reservations", json=body, headers={"Idempotency-Key": uuid.uuid4().hex})
        assert r.status_code == 409
        detail = r.json()["detail"]
        assert detail["code"] == "SLOT_UNAVAILABLE"
        assert detail["alternatives"]


def test_modify_and_cancel_capacity_accounting(reset_api):
    with _client(reset_api) as c:
        # Move seeded LUMA-4821 (Aug 14 18:00, party 2) to 19:30 party 4
        r = c.patch("/reservations/res_existing_4821", json={"time": "19:30", "party_size": 4})
        assert r.status_code == 200
        assert (r.json()["time"], r.json()["party_size"]) == ("19:30", 4)
        assert c.get("/availability", params={"date": "2026-08-14", "time": "18:00", "party_size": 1}).json()[
            "remaining_capacity"
        ] == 6  # 4 + returned 2
        assert c.get("/availability", params={"date": "2026-08-14", "time": "19:30", "party_size": 1}).json()[
            "remaining_capacity"
        ] == 4  # 8 - 4

        # Cancel restores the (new) slot; second cancel is a no-op
        r = c.post("/reservations/res_existing_4821/cancel")
        assert r.json()["status"] == "cancelled"
        assert c.get("/availability", params={"date": "2026-08-14", "time": "19:30", "party_size": 1}).json()[
            "remaining_capacity"
        ] == 8
        r2 = c.post("/reservations/res_existing_4821/cancel")
        assert r2.json()["status"] == "cancelled"
        assert c.get("/availability", params={"date": "2026-08-14", "time": "19:30", "party_size": 1}).json()[
            "remaining_capacity"
        ] == 8


def test_search_requires_criteria(reset_api):
    with _client(reset_api) as c:
        assert c.get("/reservations/search").status_code == 422
        by_code = c.get("/reservations/search", params={"confirmation_code": "luma-4821"}).json()["results"]
        assert by_code and by_code[0]["reservation_id"] == "res_existing_4821"


def test_handoff_records_summary(reset_api):
    with _client(reset_api) as c:
        r = c.post(
            "/handoff",
            json={
                "reason": "large_party",
                "customer_phone": "+13105550147",
                "conversation_summary": "Party of 12 on Aug 15; needs human.",
            },
        )
        assert r.status_code == 200
        assert r.json()["status"] == "queued"
