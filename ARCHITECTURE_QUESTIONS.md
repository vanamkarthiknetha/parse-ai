# Architecture Questions

## 1. Why this voice framework, STT, LLM, TTS, and transport?

- **LiveKit Agents + WebRTC transport.** The hard real-time problems — echo cancellation, jitter, turn detection, barge-in that actually cancels in-flight generation, per-stage metrics — are solved in the framework rather than hand-rolled. The same worker takes a phone number later via LiveKit SIP + Twilio without code changes, and the text-mode `AgentSession` lets the eval suite exercise the identical agent without audio.
- **Deepgram Nova-3 STT.** Streaming interim results (needed for preemptive generation), strong digit/name accuracy, ~sub-300 ms finalization.
- **Claude Haiku 4.5 LLM (configurable).** The voice turn blocks on LLM time-to-first-token, so the default is the fastest current Claude model with reliable tool calling. This is a latency decision: for non-realtime workloads I would default to `claude-opus-5`, and `LLM_MODEL` makes that a one-line switch.
- **Deepgram Aura-2 TTS.** Streaming synthesis with low first-byte latency; one speech vendor simplifies keys, billing and failure modes.

## 2. How is session and reservation state stored?

Per-call state lives in a `CallState` dataclass attached to the LiveKit session (`session.userdata`) and passed to every tool via `RunContext`: customer name/phone, the last availability check, reservations created this call (keyed by idempotency key), the reservation located for modify/cancel, cancelled ids, and handoff status. Reservation *records* live only in the reservation API — the agent never caches them as truth, it re-reads via search. Nothing is shared between calls; a worker restart loses only in-progress calls. In production, `CallState` would be checkpointed to Redis keyed by call id so a reconnect resumes the conversation.

## 3. How do you cancel generation during barge-in?

LiveKit handles this natively and it's why I chose it. VAD (plus the semantic turn model) detects caller speech above `min_interruption_duration`; the framework then aborts the in-flight LLM stream and TTS synthesis, flushes unplayed audio, and truncates the assistant turn in chat history to the words actually spoken — so the model's next turn knows what the caller heard, not what it intended to say. `preemptive_generation=True` starts LLM/TTS on interim transcripts and those speculative runs are cancelled the same way if the transcript changes. Tool side-effects are not cancelled mid-flight; writes happen only after explicit confirmation, so an interruption during a confirmation read-back arrives *before* any write and simply becomes a correction.

## 4. How are tool arguments validated?

Three layers. (1) Schema: LiveKit generates typed JSON schemas from the tool signatures, so the LLM must produce the right shape. (2) Semantic validation in `agent/validation.py`: every argument is parsed before any network call — date format/past/Monday-closed, 30-minute slot within service hours, party size 1–8 (>8 routes to handoff), phone digit-count, name length. Failures return corrective messages the LLM uses to re-ask the caller; invalid arguments never reach the API. (3) The API's own Pydantic validation as the final backstop (422s are mapped back to friendly guidance).

## 5. How are duplicate writes prevented?

Layered, cheapest first: (1) the prompt requires one explicit confirmation and forbids re-creating a booking already made; (2) `create_reservation` computes a **deterministic** idempotency key = hash(session id + name + phone + date + time + party size), so a repeated tool call, LLM retry, or network retry sends the *same* key and the API returns the original record instead of writing a new one; (3) an in-session guard short-circuits identical creates and answers with the existing confirmation code without any HTTP call; (4) modify/cancel are guarded by requiring a prior in-session lookup, and cancels are tracked so a second "cancel it" is a no-op. T3/T7 assert exactly one record end-to-end.

## 6. Which failures are retried?

Exactly one retry, and only where a retry cannot double-write: transport errors/timeouts and 503 (using the server's `retry_after_ms` hint) on GET availability/search, on create (safe because of the idempotency key), and on cancel (idempotent server-side). PATCH is never blindly retried — without a key, a repeat could double-apply against moving capacity. 4xx responses are never retried; they're mapped to corrective messages (409 → relay the API's alternatives, 422 → re-collect the bad field). If the single retry also fails, the agent says so honestly and offers to try again or hand off — it never invents a result.

## 7. How is context preserved during handoff?

Two channels, so a weak model summary can't lose state: the LLM writes a conversation summary as a tool argument, and the code appends a structured dump built from `CallState` (name, phone, last availability check, bookings created/cancelled this call, located reservation). Both go to `POST /handoff` with the customer phone; the returned handoff id is kept in state so a second handoff request reassures rather than re-queues. In production the same payload would ride SIP REFER headers / a screen-pop to the human agent.

## 8. Which production metrics and logs matter?

Per turn: end-of-speech → first-audio (the number callers feel), decomposed into EOU delay, LLM TTFT, TTS TTFB (all logged to `logs/metrics.jsonl` with p50/p95 at session end); interruption count; STT finalization delay. Per tool call: latency, status, attempt count (JSONL). Per call: task outcome (booked/modified/cancelled/handoff/abandoned), duplicate-write count (must be 0), token usage per provider. Business: task success rate, handoff rate and reasons, containment. Logs: full transcript + tool I/O with PII redaction, correlated by call id. Alerts: p95 turn latency, tool error rate, 5xx from the reservation backend, provider quota exhaustion.

## 9. How would the system change at 10, 100, and 1,000 concurrent calls?

- **10:** nothing structural — one worker host handles this; each call is an async pipeline whose heavy lifting (STT/TTS/LLM) is provider-side. Mock API → real DB.
- **100:** a small autoscaled worker pool (LiveKit dispatches jobs to available workers; CPU per call is dominated by VAD/turn-detector inference), provider rate-limit budgeting and connection pooling, Redis for call-state checkpointing, centralized structured logging/tracing.
- **1,000:** regional worker pools near callers; reservation service becomes a proper API with a transactional DB (row-level locks on slot capacity, idempotency-key table with TTL), read replicas for availability; circuit breakers + secondary STT/TTS/LLM providers for failover; SIP ingress at scale (Twilio Elastic SIP → LiveKit SIP); dedicated capacity/quota contracts with the model providers; canaried prompt/model rollouts gated by the eval suite.

## 10. What would you improve in the supplied API?

- Idempotency keys on **modify/cancel** (and scoping keys per resource), not just create; currently a retried PATCH can double-move capacity.
- Optimistic concurrency (ETag/version) on reservations so two agents can't clobber each other's updates.
- A hold/lock primitive ("hold this slot for 60 s") so confirmation read-back can't lose the slot to a race (the current 409-on-create is handled, but a hold gives a better caller experience).
- Cursor pagination + date-range search; searching by phone returns all history unbounded.
- Structured error envelope everywhere (some errors are `{code}`, FastAPI validation errors have a different shape), machine-readable `retry_after` on every 5xx, and 201/204 status semantics.
- Auth (API keys/JWT), rate limiting, and an events/webhook feed for downstream systems (confirmation SMS, host stand).

## 11. How would you protect PII, recordings, transcripts, and secrets?

Minimize what exists: no recordings by default; transcripts and tool logs redact phone numbers (keep last-4) and names at the logging layer before storage. Encrypt in transit (WebRTC is SRTP; API calls TLS) and at rest with short retention (e.g. 30 days transcripts, 90 days aggregate metrics), with deletion honoring user requests. Secrets only via environment/secret manager (never in the repo — `.env` is git-ignored, `.env.example` documents the shape), scoped per environment and rotated. Access to logs behind role-based access with audit trails. Provider selection with no-training/zero-retention API options where offered. The handoff payload carries only what the human needs.

## 12. Estimate cost per five-minute call.

Assuming ~15 agent turns, agent speaks ~2,000 characters, defaults as configured:

| Component | Basis | Cost |
|---|---|---|
| Deepgram Nova-3 STT | ~$0.0077/min × 5 min streamed | ~$0.04 |
| Deepgram Aura-2 TTS | ~$0.030/1k chars × ~2k chars | ~$0.06 |
| Claude Haiku 4.5 | ~50k input ($1/MTok) + ~2k output ($5/MTok), history resent per turn | ~$0.06 |
| LiveKit Cloud | 2 participants × 5 min | ~$0.01–0.03 |
| **Total (browser)** | | **≈ $0.17–0.19** |
| + Twilio PSTN inbound (if phone) | ~$0.0085/min × 5 | +~$0.04 |

Levers: prompt caching on the LLM (the system prompt + tool schemas dominate input tokens and are cache-hits after turn one, cutting LLM cost roughly in half), trimming spoken verbosity (TTS is per-character), and history compaction on long calls.
