# Evaluation Results

| Test | Pass/Fail | Final outcome | Tool calls | Duplicate/wrong write? | End-of-speech to first audio | API latency | Notes |
|---|---|---|---|---|---:|---:|---|
| T1 | Pass | booked 2026-08-14 18:00 x4 | 2 calls | No | n/a (run a live voice session) | 3.6 ms max |  |
| T2 | Pass | offered API alternatives; booked 19:30 | 3 calls | No | n/a (run a live voice session) | 4.0 ms max |  |
| T3 | Pass | single booking with corrected party size 4 | 3 calls | No | n/a (run a live voice session) | 4.4 ms max |  |
| T4 | Pass | PATCH applied once; capacity moved | 3 calls | No | n/a (run a live voice session) | 3.2 ms max |  |
| T5 | Pass | cancelled once after confirmation | 2 calls | No | n/a (run a live voice session) | 3.5 ms max |  |
| T6 | Pass | 503 absorbed by single retry; real result relayed | 2 calls | No | n/a (run a live voice session) | 4.6 ms max |  |
| T7 | Pass | same idempotency key returned original record | 2 calls | No | n/a (run a live voice session) | 4.2 ms max |  |

- Task success rate: 7/7 scenarios passing (assertion-verified against API state)
- Tool-call accuracy: every write asserted to happen exactly once; see per-test call logs in evals/results/scenario_results.json
- Duplicate-write rate: 0 (T3/T7 assert exactly one record; idempotency replay returns the original)
- Reservation API latency: p50 3 ms / p95 4 ms over 17 calls (local mock; includes seeded 503 retry waits)
- Voice response latency: populate by running a live session (console/browser); logged to logs/metrics.jsonl
- Known limitations: single-process in-memory mock API; text-mode evals exercise reasoning/tools but not audio; see README.
