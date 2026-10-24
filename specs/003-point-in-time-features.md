# 003 — Point-in-time features without leakage

## Why

"Searches in the last 30 days" sounds like one number. It is actually a family of
numbers, one per vantage point in time, and the only way to get it wrong is to be vague
about which vantage point you meant.

Two distinct leaks are possible, and the second one is the expensive one because the
naive fix for the first does not catch it:

1. **Future leakage.** Counting events with `occurred_at > as_of`. Obvious once stated,
   and caught by any `WHERE occurred_at <= as_of`.
2. **Knowledge leakage.** Counting an event that *occurred* before `as_of` but was not
   *ingested* until after it. The timestamp filter passes. The number is still a lie,
   because no decision made at `as_of` could have used that event — the platform did not
   know it yet. A model trained on these features learns from information that will not
   exist at inference time, scores beautifully offline, and underperforms in production.

This spec exists so that the second filter is not optional.

## What

```python
compute_features(store, traveler_id, as_of, windows=...) -> FeatureSet
```

Features are computed **from the event log**, not from the current profile. The profile
is a "latest state" object with no memory of what it looked like last Tuesday;
reconstructing history from it is not possible. The event log is the only honest source
for an as-of question.

### The filter

Every feature applies all three conditions:

```sql
WHERE occurred_at <= :as_of      -- no future events          (leak 1)
  AND ingested_at <= :as_of      -- nothing we did not know   (leak 2)
  AND occurred_at >  :window_start
```

plus **`SELECT DISTINCT event_id`** semantics, because the duplicates from
[001](001-event-stream.md) are still in the log and would otherwise double count inside
the window.

### Feature set

| Feature | Window |
|---|---|
| `searches_last_30d` | 30d |
| `distinct_destinations_last_30d` | 30d |
| `bookings_last_90d` | 90d |
| `cancellations_last_90d` | 90d |
| `days_since_last_search` | unbounded, as-of aware |
| `has_upcoming_trip` | bookings with `depart_date > as_of` |

`FeatureSet` carries its own `as_of`, `computed_at`, and the number of events that
cleared the filter, so a feature value is never passed around naked — provenance travels
with the number.

### The honesty property, stated precisely

> For any `as_of` T and any event e with `e.ingested_at > T`, the feature values at T are
> identical whether or not e exists in the log.

That is the property the tests assert, and it is strictly stronger than "filter on
`occurred_at`".

## Acceptance criteria

- C1 **No future leakage.** An event occurring after `as_of` does not change any feature
  value at `as_of`.
- C2 **No knowledge leakage.** The known late-arriving event from
  [001](001-event-stream.md) is excluded at an `as_of` between its `occurred_at` and its
  `ingested_at` — and included at an `as_of` after its `ingested_at`. Same event, two
  vantage points, two correct answers.
- C3 **Dedupe inside the window.** A duplicated event counts once.
- C4 **Window edges.** An event exactly at `as_of` is included; one exactly at
  `window_start` is excluded (half-open interval `(window_start, as_of]`).
- C5 **Replayability.** Recomputing the same `(traveler_id, as_of)` after appending
  newer events returns the identical earlier value.
- C6 **The strong property.** Deleting all events with `ingested_at > T` from the log
  leaves every feature value at T unchanged. This is C2 generalised, and it is the test
  that would have caught the mistake in the first place.
