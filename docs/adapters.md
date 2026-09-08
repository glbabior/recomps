# Source adapters

The engine ships the adapter *interface* and the fixture adapter. It ships no scrapers for real listing portals — those belong to private market plugins, along with the terms-of-service exposure that comes with them.

This document is the contract an adapter has to meet, and the failure modes it has to survive. Every one of the latter was learned by hitting it.

## The contract

There is no `SourceAdapter` class to implement. The seam is one method, in `recomps/research/base.py`:

```python
class Researcher(Protocol):
    def gather(self, market, profile, window_start, window_end) -> Dataset: ...
```

Two implementations ship: `FixtureResearcher` replays recorded JSON, `LiveResearcher` reads the web. A market plugin does not write either. It **describes its sources in `market.toml`**, and `LiveResearcher` reads whatever is described:

```toml
[[sources]]
name = "somewhere-sold"
adapter = "nextdata"
role = "Primary sold backbone"
url = "https://example.invalid/sold/"
rate_limit_seconds = 3.0
side = "sold"                  # or "active"; omitted means sold

[sources.embedded]
script_id = "__NEXT_DATA__"
records_path = "props.searchData.homes"
url_base = "https://example.invalid"
total_path = "props.searchData.totalHomes"

[sources.embedded.fields]
address = { path = "location.streetAddress", transform = "text" }
sold_price = { path = "price.formattedPrice", transform = "money" }
lot_sqft = { path = "lotSize.formattedDimension", transform = "sqft" }
```

Three of those keys carry the weight:

- **`side`** decides which half of the market a source describes. It is declared, not guessed from the name or the role text, so a market's wording never becomes load-bearing. Without it a source is the sold backbone, and the active side stays empty.
- **`total_path`** is where the page states its own result count. Declare it and every run checks what it extracted against what the page claims.
- **`url_base`** is the site a record may link to. A record's URL is chosen by the site being read and is fetched next from this machine, so anything pointing elsewhere is dropped and reported.

Everything a record yields is untrusted until the deterministic layer has filtered it — and it stays untrusted afterwards. It is written to the workbook as text, never as something a spreadsheet will execute.

## Non-negotiables

**Rate limit.** A configurable delay per host, honoured serially per host. Fanning workers out across many addresses is fine; fanning them at one host is not.

**Identify honestly.** A real user agent naming the tool and a contact. No spoofing beyond that. No captcha evasion, ever.

**Respect `robots.txt`.** If it disallows the path, the adapter does not fetch it. A source that blocks you is a source you do not have.

**Fail soft.** A source that errors, times out, or returns something unparseable logs the failure into `Diagnostics.sources_failed` and returns what it has. It never raises through the pipeline. A run that lost one of four sources should say so and continue, not die.

**Never impute.** A field the adapter could not find is `None`. It renders as `—` and is counted as not found. There is no acceptable circumstance for filling it with a plausible value.

## Failure modes every adapter must handle

**The partially-rendered page.** Search pages routinely render only their first seven or eight results server-side and build the rest in JavaScript. A fetcher sees a page that looks complete and is missing most of the data. One index claimed 166 properties and served nine. The first guard is the page against itself: declare `total_path` and the extraction is compared to the site's own count, so a short page is reported as incomplete rather than read as a small market. Cross-checking a second source remains the backstop.

**Pagination that skips.** Some paginated indexes drop entries between pages. Same guard: a second source and a count comparison.

**Rounded acreage.** An index that publishes acreage to two decimals gives you ±4% of $/sqft on a small parcel. Set `lot_size_is_rounded=True` so the caveat travels with the number rather than being silently lost.

**Duplicate and phantom rows.** The same sale listed twice — once bare and once with an MLS lot number appended — or an anomalous interim "sold" row for a property that later sold again.

Do not deduplicate on the address. That was tried and it deleted a real sale: two genuinely different parcels can share one street address, and one observed pair, 3.9 and 5.14 acres, was listed simultaneously two hundred thousand dollars apart. The address selects candidates; **price decides**, with size breaking the tie when a price is missing. Rows that cannot be compared collapse, which preserves the behaviour the rule was written for. Two real sales at one address are both kept and the sharing is reported, because it looks exactly like a duplicate and must not be quietly treated as one.

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
