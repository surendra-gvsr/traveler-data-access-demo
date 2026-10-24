# 001 — Synthetic bitemporal event stream

## Why

Downstream code is only trustworthy if it has met hostile input. Real traveler event
streams are not clean: the same event arrives twice because a producer retried, and an
event that happened on Tuesday shows up after one that happened on Thursday because it
sat in a dead-letter queue. A generator that emits only tidy data lets bugs survive all
the way to production, so this one emits the mess on purpose.

## What

`generate.py` writes two artifacts into `data/`:

- `events.jsonl` — one JSON event per line, **ordered by `ingested_at`** (the order a
  consumer would actually see), not by `occurred_at`.
- `travelers.json` — the identity registry: pseudonymous id, synthetic PII
  (`full_name`, `email`, `phone`), home market. **No consent field** — consent is
  event-sourced (see below).

Generation is seeded and deterministic: same seed, same bytes.

### Event envelope

```json
{
  "event_id":       "evt_000123",
  "event_type":     "search" | "booking" | "cancellation" | "consent_change",
  "traveler_id":    "trv_004",
  "occurred_at":    "2026-09-14T08:12:00+00:00",
  "ingested_at":    "2026-09-14T08:12:31+00:00",
  "source_version": 12,
  "payload":        { ... }
}
```

- `event_id` is the **idempotency key**. A duplicate delivery re-uses it exactly.
- `source_version` is a per-traveler monotonically increasing counter assigned by the
  (fictional) upstream system at the moment the event was produced. It is the ordering
  token for last-write-wins fields. Delivery order does not change it.
- `occurred_at` / `ingested_at` are as defined in [specs/README](README.md).

### Payloads

| `event_type` | Payload fields |
|---|---|
| `search` | `origin`, `destination`, `depart_date`, `return_date`, `adults`, `children`, `cabin`, `contact_email` |
| `booking` | `booking_ref`, `origin`, `destination`, `depart_date`, `return_date`, `adults`, `children`, `cabin`, `nights`, `amount_minor`, `currency`, `contact_email` |
| `cancellation` | `booking_ref`, `reason` |
| `consent_change` | `ai_access` (bool), `purposes` (list), `channel` |

### Consent is an event, not a config file

Every traveler's stream opens with a `consent_change` event, and some travelers change
their mind later. Modelling consent as a static registry would have quietly dodged the
hardest problem in this repo: **consent is a last-write-wins field arriving on an
unordered stream.** A replayed or late-delivered older "opt-in" must not resurrect
access that a newer opt-out revoked. That is a privacy incident, not a data-quality
nit, and it is the same mechanism as [002](002-profile-builder.md)'s stale guard applied
to the one field where being wrong actually matters.

So the generator emits, on purpose:

- a traveler who **opts out late** in the window (consented history, revoked now), and
- a **stale opt-in** for that same traveler: an older-`source_version` `consent_change`
  with `ai_access: true` delivered *after* the opt-out. Correct behaviour is to apply it
  to nothing.

`contact_email` is present in raw payloads **on purpose**. PII genuinely does arrive
inside event payloads, and a redaction layer that is only tested against clean inputs is
decorative. The access layer in [004](004-ai-safe-access-layer.md) must strip it.

### Injected defects

The generator guarantees at least one of each, and records them in
`data/generation_report.json` so tests can assert against known-bad records:

1. **Duplicates** — a byte-identical re-delivery of an earlier event, appearing later in
   the file with a later `ingested_at` but the same `event_id` and `source_version`.
2. **Out-of-order delivery** — an event with a lower `source_version` / earlier
   `occurred_at` appearing after a higher-versioned one for the same traveler.
3. **Late arrival** — an event whose `ingested_at` is hours-to-days after its
   `occurred_at`, straddling at least one feature `as_of` boundary used in tests.

### Population

~12 travelers, ~250 events, spanning ~120 days ending at a fixed `NOW` constant so the
dataset and its tests never rot. Consent trajectories include:

- most travelers: opted in from the start, purposes `["trip_planning"]`
- **one traveler who opted out** and stayed out (the one the demo must skip), with the
  stale opt-in described above arriving afterwards
- **one traveler opted in with a narrower purpose list** (e.g. `["support"]` only), so
  purpose scoping is distinguishable from a blanket allow
- **one traveler with no `consent_change` event at all**, to exercise fail-closed
  defaults for travelers the consent system has never heard of.

## Acceptance criteria

- A1 Same seed produces identical `events.jsonl` bytes across runs.
- A2 The file contains ≥1 duplicate `event_id`, and the duplicate pair is byte-identical
  apart from `ingested_at`.
- A3 The file contains ≥1 traveler whose events are not in `source_version` order when
  read sequentially.
- A4 The file contains ≥1 event where `ingested_at - occurred_at > 12h`.
- A5 Every event validates against the typed `Event` model; `occurred_at <= ingested_at`
  for every event.
- A6 Exactly one traveler's newest `consent_change` has `ai_access: false`, and that
  traveler also has a lower-versioned `ai_access: true` event delivered after it.
- A7 `cancellation` events always reference a `booking_ref` that exists in an earlier
  `booking` event (by `occurred_at`).
- A8 Exactly one traveler has no `consent_change` event in the stream.
