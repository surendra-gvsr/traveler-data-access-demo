# 005 — MCP tool contract and demo

## Why

MCP is the boundary where governance stops being internal design and becomes an API
promise. The tools are the only thing the model can see, so their schemas *are* the
privacy policy as far as the model is concerned: a field that is not in an output schema
cannot be requested, inferred, or coaxed out by a cleverly worded prompt. Prompting is
not a control. Schemas are.

The second thing this spec pins down is how a refusal reaches the model. A denial that
arrives as a transport-level exception produces a confused agent that retries, or
hallucinates an answer, or tells the user the system is broken. A denial that arrives as
a *well-formed result saying "denied, because consent was revoked"* produces an agent
that says "I can't help with that traveler" and moves on. Same control, completely
different user experience, and it is decided here.

## What

`mcp_server.py` runs over stdio using the official `mcp` Python SDK. Three tools, each a
thin typed wrapper that builds an `AccessRequest`, calls `serve`, and returns the
response. **No tool touches SQLite directly** — that invariant is what makes
[004](004-ai-safe-access-layer.md) unskippable, and it is cheap to verify by grepping
this file for store access.

### `get_traveler_preferences(traveler_id, purpose="trip_planning")`

Returns the derived preference view: `top_destinations`, `preferred_cabin`,
`typical_party_size`, `avg_trip_length_nights`, `avg_booking_lead_time_days`,
`activity_level`, plus counts — and `provenance`. No PII fields exist in the schema.

### `get_recent_searches(traveler_id, window_days=30, as_of=None, limit=10)`

Returns point-in-time search summaries (`origin`, `destination`, `depart_date`,
`return_date`, `adults`, `children`, `cabin`, `occurred_at`) and the matching
[003](003-point-in-time-features.md) feature values, plus `provenance`. `contact_email`
is present in the underlying raw payloads and must not appear here — this is the tool
where redaction is load-bearing rather than theoretical.

### `list_travelers()`

Ids and consent status only, no PII, no profile data. Exists so the demo can discover
subjects without hardcoding ids. Documented as a demo affordance: a real deployment
would not hand an agent an enumeration of its customer base.

### Response shape

Every tool returns the same envelope, so the model learns one pattern:

```json
{ "status": "OK",     "data": {...}, "provenance": {...}, "request_id": "..." }
{ "status": "DENIED", "data": null,  "reason_code": "AI_ACCESS_REVOKED",
  "message": "This traveler has not consented to AI access.", "request_id": "..." }
```

`message` is human-readable and written for the model to relay. `reason_code` is for
machines. Tool *descriptions* state plainly that denials are normal and must not be
worked around, because the model's behaviour on refusal is part of the product.

## Demo

`demo/ask_claude.py` connects Claude to the server over stdio — a real MCP client
session, `list_tools` into the Anthropic tool schema, tool calls bridged back to
`session.call_tool` — and asks, for each of two travelers:

> "What kind of trip is this traveler likely planning?"

- **Consented traveler:** Claude calls both tools, reasons from destinations, cabin,
  party size and trip length, and answers. The transcript shows the tool results, so the
  absence of the email is visible rather than claimed.
- **Opted-out traveler:** the first tool call returns `DENIED` / `AI_ACCESS_REVOKED`.
  Claude reports that it cannot analyse this traveler and stops. No retry, no guess.

The script prints every tool call and raw result, then the audit log tail, so a viewer
sees the control and its evidence in one screen. System prompt and tool schemas are
cached (`cache_control`) since both runs share them.

Requires `ANTHROPIC_API_KEY`. `demo/transcript.md` holds a recorded run so the repo is
reviewable without a key.

## Acceptance criteria

- E1 `list_tools` exposes exactly the three tools with complete JSON schemas.
- E2 Each tool's output schema contains no PII field names (`full_name`, `email`,
  `phone`), asserted against the schema, not a sample response.
- E3 Calling any tool for the opted-out traveler returns a well-formed `DENIED`
  envelope, not an exception or a transport error.
- E4 `get_recent_searches` output contains no `contact_email` key and none of the
  traveler's PII values, for every traveler in the dataset.
- E5 Every tool call produces an audit record (asserted by diffing the audit log before
  and after a call).
- E6 `mcp_server.py` contains no direct store or SQL access — all reads go through
  `serve`.
- E7 Tools are callable over a real stdio MCP session, not just as Python functions.
