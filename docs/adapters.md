# Source adapters

The engine ships the adapter *interface* and the fixture adapter. It ships no scrapers for real listing portals — those belong to private market plugins, along with the terms-of-service exposure that comes with them.

This document is the contract an adapter has to meet, and the failure modes it has to survive. Every one of the latter was learned by hitting it.

## The contract

```python
class SourceAdapter(Protocol):
    name: str

    def fetch_sold(self, market, profile, window_start, window_end) -> list[SoldComp]: ...
    def fetch_active(self, market, profile) -> list[ActiveListing]: ...
    def fetch_property(self, address: str) -> dict: ...   # attribution, history, coords
```

Anything an adapter returns is untrusted until the deterministic layer has filtered it.

## Non-negotiables

**Rate limit.** A configurable delay per host, honoured serially per host. Fanning workers out across many addresses is fine; fanning them at one host is not.

**Identify honestly.** A real user agent naming the tool and a contact. No spoofing beyond that. No captcha evasion, ever.

**Respect `robots.txt`.** If it disallows the path, the adapter does not fetch it. A source that blocks you is a source you do not have.

**Fail soft.** A source that errors, times out, or returns something unparseable logs the failure into `Diagnostics.sources_failed` and returns what it has. It never raises through the pipeline. A run that lost one of four sources should say so and continue, not die.

**Never impute.** A field the adapter could not find is `None`. It renders as `—` and is counted as not found. There is no acceptable circumstance for filling it with a plausible value.

## Failure modes every adapter must handle

**The partially-rendered page.** Search pages routinely render only their first seven or eight results server-side and build the rest in JavaScript. A fetcher sees a page that looks complete and is missing most of the data. An adapter must never treat a fetched page as the whole result set — cross-check the count against a second source and record the disagreement as a diagnostic.

**Pagination that skips.** Some paginated indexes drop entries between pages. Same guard: a second source and a count comparison.

**Rounded acreage.** An index that publishes acreage to two decimals gives you ±4% of $/sqft on a small parcel. Set `lot_size_is_rounded=True` so the caveat travels with the number rather than being silently lost.

**Duplicate and phantom rows.** The same sale listed twice, or an anomalous interim "sold" row for a property that later sold again. Deduplicate on the normalized address and verify against price and date.

**Stale attribution.** Property pages happily attribute a transaction from decades ago. Every attribution must be checked against the sale price and date already known for that parcel before it is accepted.

**Fuzzy geocodes.** Geocoders will match a street that does not exist onto one that does, and return it with a confident score — a real case resolved a "Vista Rd" onto a "St" half a mile away. Verify the returned street name against the query, and re-query the fallback geocoder on a mismatch.

**Listings that have already closed.** Active indexes lag closings by days. The pipeline reconciles this (F3), but an adapter that can see a status should report it.

**Hard blocks.** Some sources return 405 to any fetcher, or show "Public Record" with no attribution at all. The correct outcome is "not found", recorded as such. A human-driven browser session is a fallback of last resort and is not something this pipeline automates.

## Geocoding

Two adapters, in order:

**US Census** (`geocoding.geo.census.gov`, onelineaddress). Street-interpolated to roughly 30 m, which is well inside what quadrant classification needs. Batches fine. Free, no key. Watch for the fuzzy-match failure above.

**ArcGIS World Geocoder** as fallback, and for street *intersections* — which is how divider centerline waypoints are obtained, scoring 99–100.

**Nominatim** is robots-blocked from some environments. Treat it as an optional adapter, disabled by default.

Un-numbered parcels ("0 Something Rd") often will not geocode at all. Pull coordinates from the listing page instead.

## Fixture mode

Every adapter needs one. It is what makes the test suite deterministic and what lets anyone run the pipeline without credentials. Record real responses once, and replay them.

The engine's `FixtureResearcher` covers the whole research layer at once; a per-adapter fixture mode is for testing an adapter's own parsing.
