# Recorded demo run

This is a real run against the built database, with a real model choosing the tool
calls. It is kept in the repo so the governance is reviewable without an API key.

```
python -m traveler_access.generate
python -m traveler_access.pipeline load --reset
python -m traveler_access.pipeline build
python demo/ask_claude.py
```

## The run

```
tools offered to the model: get_recent_searches, get_traveler_preferences, list_travelers

==============================================================================
trv_003 (consented) — What kind of trip is this traveler likely planning?
==============================================================================
     [in=4 out=132 cache_read=0 cache_write=1697]

  -> get_traveler_preferences(traveler_id='trv_003')
     OK  top=[BCNx10, ROMx8, LISx3] cabin=premium_economy party=2+0 nights=3.8 lead=37.0d searches=16 bookings=5

  -> get_recent_searches(traveler_id='trv_003')
     OK  5 searches in 30d, features: searches_last_30d=5 bookings_last_90d=3 upcoming_trip=True as_of=2026-10-17T12:05:40Z
     [in=2 out=426 cache_read=1697 cache_write=1389]

  trv_003 is most likely planning a short city break in southern Europe for two adults,
  with Rome first and Barcelona close behind. The latest search (15 Oct) was Stockholm
  Arlanda (ARN) to Rome, 20–24 Nov, for 2 adults with no children in premium economy.
  That fits the profile closely: Barcelona (10 interactions) and Rome (8) are the top
  destinations, trips average 3.8 nights, and bookings are usually made about 37 days
  ahead, which matches this search's 36. I'm fairly confident about the type of trip,
  but less sure Rome is the final pick: the last 30 days include searches for both Rome
  and Barcelona, the profile already shows an upcoming trip and a recent cancellation,
  and the data only runs to 17 Oct, so anything after that isn't included.


==============================================================================
trv_010 (revoked AI access) — What kind of trip is this traveler likely planning?
==============================================================================
     [in=4 out=115 cache_read=0 cache_write=1697]

  -> get_traveler_preferences(traveler_id='trv_010')
     DENIED  AI_ACCESS_REVOKED  This traveler has not consented to their data being used by AI agents.

  -> get_recent_searches(traveler_id='trv_010')
     DENIED  AI_ACCESS_REVOKED  This traveler has not consented to their data being used by AI agents.
     [in=2 out=145 cache_read=1697 cache_write=366]

  I can't analyse traveler trv_010, because they haven't consented to AI agents using
  their data. I asked for both their travel preferences and their recent searches, and
  both requests came back DENIED with reason code AI_ACCESS_REVOKED. That means I have
  no information about this traveler, so I can't say what kind of trip they might be
  planning, even with low confidence. If they give consent again later, I can look at
  their profile then.


==============================================================================
PII check across the whole conversation
==============================================================================
  36 known PII values in the dataset (names, emails, phones)
  4188 characters shown to or written by the model
  no PII field name, no known PII value, no email or phone pattern

==============================================================================
Audit trail written by the server subprocess
==============================================================================
  2026-10-25T00:14:13+00:00  mcp           get_traveler_preferences   trv_003  OK
  2026-10-25T00:14:13+00:00  mcp           get_recent_searches        trv_003  OK
  2026-10-25T00:14:22+00:00  mcp           get_traveler_preferences   trv_010  DENIED  AI_ACCESS_REVOKED
  2026-10-25T00:14:22+00:00  mcp           get_recent_searches        trv_010  DENIED  AI_ACCESS_REVOKED
```

Three things in that output are the design working rather than the model being clever.

**It reasons about freshness unprompted.** "The data only runs to 17 Oct, so anything
after that isn't included" is the `as_of` watermark being used as evidence. Nothing in
the system prompt asks for that caveat; it is available because every response carries
provenance, and an agent that can see staleness can hedge on it.

**It answers entirely from governed fields.** Destinations, cabin, party size, trip
length, lead time. No name, no email, no phone — not because it was told to avoid them
but because they were never in the payload to begin with.

**For `trv_010` it stops.** Both tools deny, and the model says it has no information
and will not guess "even with low confidence". It does not retry, and it does not try
the other tool for the same data by another route. That last part matters more than it
looks: a denial that an agent treats as a transient failure is not really a denial.

## The email that does not cross the boundary

The underlying event carries a contact email. This is the most recent search the
platform holds for `trv_003`, straight out of the `events` table:

```json
{
  "adults": 2,
  "cabin": "premium_economy",
  "children": 0,
  "contact_email": "imani.haddad@mailbox.test",
  "depart_date": "2026-11-20",
  "destination": "ROM",
  "origin": "ARN",
  "return_date": "2026-11-24"
}
```

That email is also stored on the profile row, in a `contact_email` column sitting
directly next to the values the tool does serve. Here is the complete payload the
preferences tool returns — every field, nothing elided:

```json
{
  "activity_level": "high",
  "avg_booking_lead_time_days": 37.0,
  "avg_nightly_spend_minor": 46000,
  "avg_trip_length_nights": 3.8,
  "booking_count": 5,
  "cancellation_count": 1,
  "home_origin": "ARN",
  "last_search_depart_date": "2026-11-20",
  "last_search_destination": "ROM",
  "party_adults": 2,
  "party_children": 0,
  "preferred_cabin": "premium_economy",
  "preferred_currency": "EUR",
  "search_count": 16,
  "top_destinations": [
    {"destination": "BCN", "interactions": 10},
    {"destination": "ROM", "interactions": 8},
    {"destination": "LIS", "interactions": 3}
  ],
  "traveler_id": "trv_003"
}
```

The email is absent because `TravelerPreferences` never names it, not because anything
removed it. The scrubber that runs afterwards found nothing to find — which is the
point: it is there to turn a future careless field addition into a loud failure, not to
do the day-to-day work of keeping PII out.

## Provenance attached to that same response

```json
{
  "source": "profile_store",
  "version": 22,
  "computed_at": "2026-10-25T00:15:40Z",
  "as_of": "2026-10-17T12:05:40Z",
  "event_count": 22,
  "pipeline_version": "0.1.0",
  "freshness_seconds": 139978
}
```

Three numbers worth reading carefully.

`as_of` is not wall-clock now. It is the platform's knowledge watermark — the latest
ingestion timestamp in the store, which is as far forward as any question can honestly
be answered. It sits a week behind `computed_at` because the newest event in the
synthetic log occurred on 2026-10-17; in a live system with events still arriving, the
two would be close.

`freshness_seconds` is 139,978 — about 39 hours between the newest event behind this
answer and the vantage point it was computed from. That is the number an agent actually
needs. "When did the pipeline last run" says nothing about whether the evidence is old.

`version` is 22, the profile's own version counter. Two answers with the same version
were computed from the same facts, which is what makes a disagreement between them
debuggable rather than mysterious.

## Without a model

`python demo/ask_claude.py --dry-run` walks the same tools over the same stdio
transport and prints the same payloads with the model's turn omitted. It needs no API
key, which makes it the fastest way to see the consent and PII behaviour for yourself.
