# Parse AI Voice AI Engineer Assessment Starter

This package contains the standardized Luma Bistro mock reservation API, fixed data, and required test scenarios.

## Run

```bash
docker compose up --build
```

API: http://localhost:8000  
Swagger: http://localhost:8000/docs

## Fixed facts
- Time zone: America/Los_Angeles
- Open Tuesday-Sunday, 5:00 PM-10:00 PM; closed Monday
- 30-minute slots
- Maximum standard party size: 8; larger parties require handoff
- Existing reservation: LUMA-4821, Alex Morgan, +1 310 555 0147, 2026-08-14 at 18:00, party of 2

## Important behavior
- POST /reservations requires an Idempotency-Key header.
- Reusing the same key must return the same reservation, not create a duplicate.
- The first availability request for 2026-08-16 returns HTTP 503, then succeeds on retry.
- Invalid inputs return 422; unavailable booking attempts return 409.
- POST /admin/reset resets the assessment data before each test.

Use the exact scenarios in standard_test_cases.json and complete EVALUATION_TEMPLATE.md.
