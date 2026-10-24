# 004 — The AI-safe access layer

## Why

A language model is an unusually leaky consumer of data. Anything that reaches its
context window may be echoed into a reply, summarised into a log, or carried into a
follow-up turn — so "we only passed the email so the model could match records" is not a
containment story. The boundary has to hold *before* the bytes reach the model, not
after.

Four things must be true of every single read, with no exceptions and no code path that
can skip them:

1. The traveler consented, for this purpose, as of now.
2. No PII crosses the boundary.
3. The caller can tell where the answer came from and how old it is.
4. There is a record that the read happened, whether it succeeded or not.

The design goal is that these are **structurally impossible to forget** rather than
things a careful developer remembers. Tools do not get to query the store directly:
`access.py` owns the only read path, and it does all four or returns nothing.

## What

```python
serve(request: AccessRequest) -> AccessResponse
```

One function, one choke point. Every MCP tool in [005](005-mcp-tool-contract.md) is a
thin typed wrapper over it.

```python
AccessRequest(tool, traveler_id, purpose, caller, as_of=None, params={})
AccessResponse(status, data|None, provenance|None, reason_code|None, request_id)
```

### 1. Consent, enforced at read time

The check order is: traveler exists → consent row exists → `ai_access` is true →
`purpose` is in `purposes`. Failing any of them returns `DENIED` with a machine-readable
`reason_code` (`UNKNOWN_TRAVELER`, `NO_CONSENT_RECORD`, `AI_ACCESS_REVOKED`,
`PURPOSE_NOT_PERMITTED`) and **no data and no partial data** — not a trimmed profile,
not a count, not an existence hint beyond the reason code itself.

Two decisions worth defending out loud:

- **Fail closed.** No consent row means deny. The absence of a recorded "yes" is not a
  "yes", and a system that defaults to allow will leak every traveler the consent
  pipeline hasn't caught up on yet.
- **Consent is evaluated at read time, never as-of.** `as_of` rewinds *features*
  ([003](003-point-in-time-features.md)) and must never rewind *permission*. If a
  traveler revoked access yesterday, a request with `as_of` set to last week still gets
  `AI_ACCESS_REVOKED`. Otherwise `as_of` becomes a trivial bypass for withdrawal of
  consent — the historical data was collected under consent, but the *right to use it
  now* was withdrawn now. Point-in-time correctness applies to facts, not to rights.

### 2. PII removal, by construction and then by assertion

Defence in depth, because either layer alone fails in a predictable way:

- **Allowlist (prevents).** Output models are Pydantic classes containing only
  non-PII fields. The layer builds responses by naming the fields it wants, never by
  spreading a profile dict into a response. There is no `**profile` anywhere in
  `access.py`; a field reaches the model only because someone typed its name into a
  schema.
- **Scrubber (detects).** Before returning, the serialised response is scanned for PII
  patterns — email, phone, and the known PII values for that traveler pulled from the
  identity registry (the strongest check available, since we know exactly what their
  email is and can assert that exact string is absent). A hit raises `PIILeakError`,
  the response becomes a `DENIED` / `PII_LEAK_BLOCKED`, and the incident is audited.

The scrubber is not belt-and-braces theatre. The allowlist is only correct as long as
every future contributor understands it; the scrubber is what turns a careless addition
into a loud test failure instead of a silent leak. It fails the request rather than
silently masking, because a masked leak is a bug you never find out about.

### 3. Provenance on every result

```json
"provenance": {
  "source":         "profile_store",
  "version":        17,
  "computed_at":    "2026-10-24T09:00:00+00:00",
  "as_of":          "2026-10-24T09:00:00+00:00",
  "event_count":    9,
  "pipeline_version": "0.1.0",
  "freshness_seconds": 3600
}
```

`source` and `version` answer "which store, which profile revision"; `as_of` and
`computed_at` answer "which vantage point, computed when"; `freshness_seconds` is the
gap between the newest event behind the answer and `as_of`, which is the number an agent
actually needs to decide whether to trust a profile. It is attached by `serve`, not by
individual tools, so no tool can return an unattributed number.

### 4. Audit log

Append-only JSONL at `data/audit.jsonl`, one record per call, written for allows **and**
denials — a denial record is the evidence that the control worked.

```json
{"ts":"...","request_id":"...","caller":"claude-demo","tool":"get_traveler_preferences",
 "traveler_id":"trv_004","purpose":"trip_planning","decision":"DENIED",
 "reason_code":"AI_ACCESS_REVOKED","fields_returned":[],"provenance":null,"duration_ms":2}
```

`fields_returned` lists **field names, never values**. An audit log that records
payloads becomes a second copy of the data it is supposed to be policing — the most
common way a privacy control turns into a privacy liability. The audit record is written
in a `finally` block so a crash mid-read still leaves a trace.

## Acceptance criteria

- D1 A traveler whose newest consent is `ai_access: false` gets `DENIED` /
  `AI_ACCESS_REVOKED` from every tool, with `data is None`.
- D2 The stale opt-in from [001](001-event-stream.md) does not restore access.
- D3 A traveler with no consent record gets `DENIED` / `NO_CONSENT_RECORD` (fail closed).
- D4 A purpose outside the traveler's `purposes` gets `DENIED` /
  `PURPOSE_NOT_PERMITTED`, even with `ai_access: true`.
- D5 **`as_of` cannot bypass revocation.** A request with `as_of` before the opt-out is
  still denied.
- D6 For every tool and every traveler, the serialised response contains none of that
  traveler's PII values, and no key matching the PII field names.
- D7 Injecting a PII field into an output model raises `PIILeakError` and produces a
  `PII_LEAK_BLOCKED` denial rather than a leaked response. (Test wires a deliberately
  bad response through the layer.)
- D8 Every call appends exactly one audit record; denials are recorded with their reason
  code; `fields_returned` never contains a PII field name.
- D9 Every successful response carries complete provenance — `source`, `version`,
  `computed_at` non-null.
- D10 The audit log is append-only: existing lines are byte-identical after further
  calls.
