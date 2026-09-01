# Writing a market plugin

A *market* is everything specific to one place: its boundaries, the sources that describe it, the property you are valuing, and any recorded data. The engine knows only the interface.

Most real markets should be **private**. A market plugin holds real addresses, real sale prices and your own property, and the engine is designed so none of that ever has to be published.

## Two ways to supply a market

**A directory** — the normal choice for a private market. Nothing to install, nothing to publish; clone it beside the engine and point at it.

```bash
recomps run --market-path ../my-market --offline
```

**An installed package with an entry point** — the choice for a market you want on `pip install`. The bundled Demoville market registers this way:

```toml
[project.entry-points."recomps.markets"]
mymarket = "my_package:MARKET"
```

```bash
recomps run --market mymarket --offline
```

Both resolve to the same `Market` protocol. Nothing downstream can tell them apart.

## The directory form

One `market.toml` and a fixtures directory:

```
my-market/
  market.toml
  fixtures/
    sold.json
    active.json
  data/            # run snapshots land here; gitignore it
```

```toml
[market]
name = "mymarket"
description = "My Town, ST"
default_profile = "lots"
fixture_dir = "fixtures"
data_dir = "data"

[profiles.lots]
property_type = "vacant_land"   # or single_family, condo_townhome, multi_family
metric = "lot_sqft"             # or living_sqft for improved types
window_days = 90

[profiles.lots.subject]
lot_sqft = 6450
label = "Your lot"
# No address. Size is enough to value a property; an address identifies it.

[profiles.lots.identification]
requires_absent_bed_bath = true   # land: no bed/bath counts on the card
type_labels = ["LOT", "LAND"]
exclude_habitable_structure = true

[profiles.lots.similar_bracket]
attribute = "lot_sqft"
tolerance = 0.25
# explicit_range = [5000.0, 8000.0]   # pin it when the market has a convention

[profiles.lots.exclusions]
max_lot_sqft = 43560.0                # 1 acre
explicit_address_keys = []            # normalized keys, see below
core_min_ppsf = 30.0
core_max_ppsf = 150.0

[geometry]
ns_divider_name = "Main St"
ew_divider_name = "Centre Ave"
ew_longitude = -100.0
ns_centerline = [
  { name = "Main & West", lon = -100.05, lat = 40.018 },
  { name = "Main & Centre", lon = -100.00, lat = 40.014 },
  { name = "Main & East", lon = -99.95, lat = 40.015 },
]

[[sources]]
name = "example-aggregator"
adapter = "example"
role = "Primary sold comps"
rate_limit_seconds = 3.0
quirks = ["Whatever this source does that will bite you."]
```

## Fixtures

JSON lists of comp records. `sold.json` and `active.json` for the default profile; `<profile>.sold.json` and `<profile>.active.json` for a named one.

```json
[
  {
    "address": "123 Example St",
    "sold_date": "2026-07-14",
    "sold_price": 475000,
    "lot_sqft": 5227,
    "brokerage": "Example Realty",
    "agent": "A. Example",
    "final_list_price": 425000,
    "original_list_price": 425000,
    "area": "SW",
    "lat": 40.0121, "lon": -100.017,
    "lot_size_is_rounded": true,
    "sources": ["recorded://2026-08-04"]
  }
]
```

Notes that matter:

- **Omit a field rather than guessing it.** A missing `agent` renders as `—` and is counted as unattributed. A guessed one is a lie that survives into a decision.
- **`lot_size_is_rounded`** marks sizes derived from acreage published to two decimals. That is roughly ±4% of $/sqft on a small parcel, and it raises the caveat automatically.
- **`area` is a fallback.** If a parcel carries `lat`/`lon` the classifier computes its quadrant and overwrites this. If it cannot be geocoded, a supplied `area` is kept rather than discarded.
- **Fixtures may hold more history than one window.** The window is applied at read time, so the same file serves several runs.

## Normalized address keys

`explicit_address_keys` uses the engine's canonical form: lowercase, punctuation dropped, directionals and street types abbreviated, city/state/ZIP tail removed, unit designators kept as `#n`.

```python
>>> from recomps.model.address import normalize_address
>>> normalize_address("9100 Quarry Rd, Demoville, ZZ 00000")
'9100 quarry rd'
```

Generate the key rather than writing it by hand.

## Quadrant geometry

Two rules, both learned expensively.

**Never infer east/west from a street name.** A town's E/W naming grid splits at whichever street the surveyors chose, which is often not the arterial you are using as your divider — so an address labelled "E Something St" can sit west of your divider. Classification reads coordinates only.

**Give the N/S divider real waypoints.** Arterials curve. A single latitude misplaces parcels at the edges of the market. Geocode the divider's intersections (the ArcGIS World Geocoder handles intersections well, scoring 99–100) and list them west to east.

A parcel is compared against the latitude interpolated at its own longitude. Outside the centerline's span the endpoint latitude is held rather than extrapolated.

## Verify against the conformance kit

The engine ships executable checks so the interface cannot drift out from under a plugin it never sees:

```python
from recomps.testing.conformance import check_market, check_market_runs
from datetime import date

def test_conforms():
    assert not check_market(MY_MARKET).problems

def test_runs_offline():
    report = check_market_runs(MY_MARKET, "lots", as_of=date(2026, 8, 4))
    assert not report.problems, report.summary()
```

`check_market_runs` executes the whole pipeline against your fixtures and builds a workbook. It is the check that actually fails when an engine change breaks your data.

## Keeping private data private

The public engine's own tests are structural — they assert that everything shipped *looks* synthetic, because a denylist of real strings would itself be the leak.

The complementary check belongs in your private plugin: build a denylist from your own fixtures and scan the public checkout for it. See `tests/test_no_leaks.py` in a private plugin for the pattern. Run it before every push to the public repository.

What belongs in the private plugin and never in the engine:

- URLs, selectors and site-specific quirks for real listing portals
- recorded datasets and run snapshots
- the subject property, and above all its address
- any named judgement about a specific agent or brokerage — the engine ships the generic pattern checks and no names
