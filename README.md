# Luma Bistro Voice Agent — Parse AI Technical Assessment

A real-time voice agent that answers the phone for a restaurant: checks availability, creates / modifies / cancels reservations against the supplied mock API, handles interruptions, corrections and API failures, and hands off to a human with full context when it can't complete a request.

## Architecture

```
 Browser mic/speaker                LiveKit worker (agent/)                    Mock API (app.py)
┌──────────────────┐   WebRTC   ┌──────────────────────────────────┐  HTTP  ┌──────────────────┐
│ frontend/        │◄──────────►│ Deepgram Nova-3 STT (streaming)  │◄──────►│ /availability    │
│  index.html      │  LiveKit   │ Silero VAD + semantic turn model │ retry+ │ /reservations    │
│  token_server.py │   room     │ Gemini 2.5 Flash (tool calling)  │ idem.  │ /reservations/.. │
└──────────────────┘            │ Deepgram Aura-2 TTS (streaming)  │  keys  │ /handoff         │
                                │ 6 guarded function tools         │        │ /admin/reset     │
                                └──────────────────────────────────┘        └──────────────────┘
```

**Stack and why**


| Layer                     | Choice                                      | Rationale                                                                                                                                                                                                                                                                                                  |
| ------------------------- | ------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Transport / orchestration | LiveKit Agents (Python)                     | Production WebRTC with built-in turn taking, barge-in (cancels LLM+TTS mid-utterance and truncates chat history to what was actually spoken), preemptive generation, and per-stage metrics. Swapping the browser for a phone number is config (LiveKit SIP + Twilio trunk), not code.                      |
| STT                       | Deepgram Nova-3 (streaming)                 | Low-latency interim transcripts feed preemptive generation; strong accuracy on names/digits.                                                                                                                                                                                                               |
| Turn taking               | Silero VAD + LiveKit semantic turn detector | The semantic model distinguishes "…for four people" (done) from "…for" (still talking), cutting both false interruptions and dead air. Falls back to VAD-only if model weights aren't downloaded.                                                                                                          |
| LLM                       | Gemini 2.5 Flash (provider-pluggable)       | A voice turn blocks on LLM time-to-first-token, so the default is a fast tool-calling tier — a latency choice, not a cost choice. The provider is auto-detected from whichever key is configured; one env var (`LLM_MODEL` / `LLM_PROVIDER`) switches to `gpt-4o-mini`, `claude-haiku-4-5`, or a top-tier model. |
| TTS                       | Deepgram Aura-2 (streaming)                 | Sub-300 ms first byte; single vendor for both speech directions.                                                                                                                                                                                                                                           |




## Repo layout

```
app.py                    # Supplied mock reservation API (unmodified)
agent/
  main.py                 # Worker entrypoint (console / dev modes), pipeline wiring, metrics
  restaurant_agent.py     # Agent + 6 function tools with code-level write guards
  api_client.py           # httpx client: latency logging, single bounded retry, idempotency keys
  validation.py           # Explicit validation of every LLM-produced tool argument
  state.py                # Per-call state, deterministic idempotency keys, handoff summary
  prompts.py              # System prompt (voice style, confirm-before-write policy)
  metrics_logger.py       # Per-turn end-of-speech -> first-audio latency to logs/metrics.jsonl
frontend/
  token_server.py         # Serves demo page + mints LiveKit tokens
  index.html              # Browser client: mic, agent audio, live transcript
evals/
  test_api_contract.py    # Mock-API contract tests (no LLM key needed) — 9 passing
  test_scenarios.py       # Standard scenarios T1–T7 driving the real agent in text mode
  make_report.py          # Fills EVALUATION_TEMPLATE.md from recorded results
```



## Setup

Requires Python 3.11+ and (for the browser demo) a free LiveKit Cloud project.

```bash
python -m venv .venv
.venv\Scripts\activate                      # Windows   (source .venv/bin/activate elsewhere)
pip install -r requirements.txt             # mock API
pip install -r agent/requirements.txt       # agent + evals
copy .env.example .env                      # then fill in keys (see below)
python -m agent.main download-files         # one-time: turn-detector weights
```

`.env` needs: `DEEPGRAM_API_KEY`, `GOOGLE_API_KEY` (or `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` — the provider is auto-detected), and for the browser demo `LIVEKIT_URL` / `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET`.

## Run

**1. Mock API** (terminal 1):

```bash
python -m uvicorn app:app --port 8000        # or: docker compose up --build
```

**2a. Console voice mode** — no LiveKit account needed (terminal 2):

```bash
python -m agent.main console
```

**2b. Browser demo** (terminals 2 + 3):

```bash
python -m agent.main dev
python -m uvicorn frontend.token_server:app --port 8080   # then open http://localhost:8080
```

Click "Start call" and talk. Live transcripts render below the buttons; latency per turn is logged to `logs/metrics.jsonl`.

## Tests and evaluation

```bash
python -m pytest evals/test_api_contract.py     # contract tests, no keys needed
python -m pytest evals/test_scenarios.py -v     # T1–T7 against the real agent (needs LLM key)
python -m evals.make_report                     # renders EVALUATION_TEMPLATE.md
```

The scenario suite drives the exact scripts from `standard_test_cases.json` through the real agent (same prompt, tools, API client) using LiveKit's text-mode `AgentSession`, then asserts against **API state** (reservation records, capacity) and the **HTTP call log** (exactly one write, retry-at-most-once) rather than model wording. The API is reset before every test.

## Latency measurement

- Voice path: `agent/metrics_logger.py` correlates LiveKit's per-turn metrics into a single end-of-speech → first-audio number (`EOU delay + LLM TTFT + TTS TTFB`) written to `logs/metrics.jsonl`; a p50/p95 summary is logged at session end.
- Tool path: every mock-API call is logged as JSON with latency and attempt count.



## Major decisions

- **Writes are guarded in code, not just prompt.** `create_reservation` refuses to run unless the caller explicitly confirmed (`caller_confirmed`) and a successful availability check for that exact slot happened in-session; `modify`/`cancel` require the reservation to have been located via `find_reservation` (or created earlier in the same call) first. Prompt-only policies fail sometimes; code guards don't.
- **Duplicate prevention is layered**: deterministic idempotency key (hash of session + booking details) so any retry/repeat reuses the same key and the API returns the original record; an in-session duplicate short-circuit that answers with the existing confirmation code; and the API's own idempotency store.
- **Retry policy is deliberately narrow**: exactly one retry, only for 503/transport errors, only on requests that are safe (GETs, keyed create, idempotent cancel). PATCH is never blindly retried.
- **Handoff preserves context by construction**: the model writes a summary, and the code appends a structured dump of everything collected (name, phone, pending details, bookings made/cancelled) so a thin model summary can't lose state.
- **Tool errors are messages, not exceptions**: every failure returns a corrective instruction the LLM relays or acts on ("offer ONLY these alternatives"), keeping the conversation recoverable.



## Known limitations

- Text-mode evals verify reasoning, tool accuracy and state, but not the audio path itself; voice latency/barge-in are measured in live sessions and shown in the demo video.
- The mock API is single-process/in-memory; concurrent capacity races aren't exercised.
- Date parsing trusts the LLM's YYYY-MM-DD conversion (validated for format/range, cross-checked against the spoken confirmation read-back, but "next Friday" ambiguity ultimately resolves in the model).
- Handoff is simulated (queued via `/handoff`) — no real SIP transfer.
- English-only STT/turn-detection configuration.



## Scaling approach

Short version (detail in [ARCHITECTURE_QUESTIONS.md](ARCHITECTURE_QUESTIONS.md)): LiveKit workers are stateless and horizontally scalable — 10 concurrent calls fit one worker; 100 needs a small autoscaled pool (each call ≈ one CPU-light async pipeline, VAD/turn-detector being the only local compute); 1,000 adds regional worker pools, a real reservation DB with row-level locking + idempotency table instead of the in-memory mock, provider quota management and circuit breakers, and SIP ingress via Twilio Elastic SIP trunking into LiveKit SIP.

## AI tooling disclosure

Built with **Claude Code (Claude Fable 5)** driving the implementation end-to-end: scaffolding, tool/guard design, eval harness, and docs. All architectural decisions (stack choice, guard placement, idempotency scheme, retry policy, eval assertions) are documented above and in `ARCHITECTURE_QUESTIONS.md`, and I can explain or modify any part live.

---



## Appendix: supplied mock API (unmodified)

API: [http://localhost:8000](http://localhost:8000) · Swagger: [http://localhost:8000/docs](http://localhost:8000/docs)

- Timezone America/Los_Angeles; open Tue–Sun 17:00–22:00; 30-min slots; max standard party 8 (larger ⇒ handoff).
- Seeded reservation: LUMA-4821, Alex Morgan, +1 310 555 0147, 2026-08-14 18:00, party 2.
- `POST /reservations` requires an `Idempotency-Key` header; same key ⇒ same reservation.
- First availability request for 2026-08-16 returns 503, then succeeds (retry test).
- Invalid input ⇒ 422; unavailable slot ⇒ 409 with alternatives; `POST /admin/reset` restores seed data.

