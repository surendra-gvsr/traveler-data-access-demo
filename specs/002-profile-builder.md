# 002 — Version-aware, idempotent profile builder

## Why

The builder runs incrementally over a stream that repeats itself and arrives out of
order. Two failure modes follow directly, and both are silent — they corrupt numbers
rather than raising:

1. **Double counting.** A retried event inflates `search_count`, which inflates every
   derived preference built on top of it.
2. **Stale overwrite.** A late-arriving older event overwrites a newer value, so
   "last destination searched" points at a trip the traveler has already taken.

The fix is to classify every profile field by how it behaves under replay and
reordering, and to handle each class differently. There is no single update rule that is
correct for all of them.

## What

`apply_event(store, event) -> ApplyResult` is the only write path. It is wrapped in one
SQLite transaction per event, so a crash mid-event leaves no partial profile.

### Field classes

| Class | Examples | Update rule | Safe under replay? | Safe under reorder? |
|---|---|---|---|---|
| **Counter** (commutative) | `search_count`, `booking_count`, `cancellation_count`, `destination_counts`, `cabin_counts`, `nights_total`, `lead_time_days_total` | add the delta | only via `event_id` dedupe | yes — addition commutes |
| **Last-write-wins** (order-sensitive) | `last_search_destination`, `last_search_depart_date`, `home_origin`, `party_adults`, `party_children`, `preferred_currency`, PII (`full_name`, `email`, `phone`) | overwrite **only if** `event.source_version > profile.last_source_version` | yes — idempotent overwrite | yes — guarded by version |
| **Watermark** | `last_source_version`, `last_occurred_at`, `profile_version`, `updated_at` | monotonic max / increment | yes | yes |
| **Consent** | `ai_access`, `purposes` | last-write-wins, **versioned independently** | yes | yes |

Consent gets its own row in `consent_state` rather than living on the profile, and its
own version watermark (`consent_source_version`). Two reasons, both practical:

- A traveler can have consent and no profile (they opted out before ever searching), or
  a profile and no consent record at all. Separate tables make "never heard of them"
  representable, which is what fail-closed denial needs.
- Consent must be queryable without materialising a profile, because the access layer
  checks it *before* deciding whether it is allowed to read anything else.

The update rule is the same stale guard as any other LWW field — which is the point. A
late-arriving older opt-in loses to a newer opt-out because the version comparison says
so, not because of a special case bolted on for privacy.

### Dedupe

A `processed_events(event_id PRIMARY KEY, traveler_id, applied_at)` table is the
idempotency ledger. `apply_event` inserts into it first; a primary-key conflict short
circuits the whole event as `DUPLICATE` and nothing else is touched.

This is deliberately *not* "check then write" — the insert **is** the check, inside the
transaction, so a concurrent second delivery cannot slip between the two steps.

### Possible outcomes

`ApplyResult.status` is one of:

- `APPLIED` — new event, counters added, LWW fields taken.
- `DUPLICATE` — `event_id` already in the ledger. No mutation at all.
- `APPLIED_STALE` — new event, but `source_version <= last_source_version`. Counters are
  still added (the event really happened and addition commutes), LWW fields are
  **skipped**. This is the interesting case and the one most pipelines get wrong: a late
  event is not a nothing, it is a partial something.

`profile_version` increments on `APPLIED` and `APPLIED_STALE`, never on `DUPLICATE`, and
is what the access layer reports as provenance.

### Derived preference view

`derive_preferences(profile)` is a pure function over a stored profile. It computes
`top_destinations` (top 3), `preferred_cabin` (modal), `typical_party_size`,
`avg_trip_length_nights`, `avg_booking_lead_time_days`, and an `activity_level` band.
It returns **no PII** — see [004](004-ai-safe-access-layer.md). Keeping it pure means
the governance layer never has to reason about storage.

## Acceptance criteria

- B1 **Replay idempotency.** Applying the full event file twice yields profiles
  byte-identical to applying it once, including `profile_version`.
- B2 **Duplicate detection.** Applying a known duplicate returns `DUPLICATE` and leaves
  every counter unchanged.
- B3 **Stale guard.** Given v5 then v3 for the same traveler, `last_search_destination`
  reflects v5, and the result for v3 is `APPLIED_STALE`.
- B4 **Counters still accrue when stale.** In B3, `search_count` is 2, not 1.
- B5 **Order independence.** Building from a shuffled event file produces identical
  counters and identical LWW fields to building from `ingested_at` order.
- B6 **Crash safety.** An exception raised mid-apply leaves the profile and the
  `processed_events` ledger unchanged (no half-applied event).
- B7 `derive_preferences` output contains no key or value matching the PII patterns in
  [004](004-ai-safe-access-layer.md).
- B8 **Consent stale guard.** Given opt-in at v2, opt-out at v9, then a replayed opt-in
  at v2 delivered last, the stored consent state is `ai_access: false`.
- B9 A traveler with no `consent_change` event has no `consent_state` row — absent, not
  defaulted to either value at the storage layer.
