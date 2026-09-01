# REComps

**An LLM-orchestrated research pipeline that prices a property against its comparable sales, and writes the answer as a spreadsheet you can argue with.**

```bash
pip install -e ".[ui]"
recomps
```

Run in a terminal with no arguments, it opens the interface. Every operation is
also a subcommand, and in a script or CI a bare `recomps` prints its help rather
than silently starting a server that never exits:

```bash
recomps run --market demoville --offline
```

That command runs the entire pipeline — collection, exclusion, statistics, valuation, pricing strategy, geographic classification, agent analysis — against a synthetic market bundled with the engine, and writes a five-sheet Excel workbook, a JSON snapshot, and a methodology document. No API key. No network. No scraping.

---

## The problem

In January 2026 the Eaton Fire burned through Altadena, California. What is left on thousands of parcels is a foundation and a view, and their owners have to decide whether to rebuild or sell into a market that did not exist eighteen months ago.

Pricing a burned lot is genuinely hard. There is no established price-per-square-foot for a vacant parcel in a neighborhood that was fully built out a year ago. The comparable sales are recent, thin, and scattered across aggregator sites that disagree with each other. And the usual shortcut — take the market's median $/sqft and multiply — is wrong in a specific, expensive way: **smaller parcels sell at a higher rate per square foot than larger ones**, so a market-wide figure systematically understates a small lot.

This project began as a research process run by hand, three times, with an AI assistant. It worked. It also took hours each time and lived entirely in one person's head. REComps is that process turned into software.

## What it produces

A workbook of five sheets: **Summary**, **Pricing Guidance**, **Sold Comps**, **Active Listings**, **Agents**.

Everything in it is a live formula. The Summary's median $/sqft is `=MEDIAN('Sold Comps'!E2:E47)`, not a number that was true when the file was written. Delete a comp you don't believe, correct a lot size the aggregator rounded, type a different size for your own property into the yellow cell — and the valuation, the pricing strategies, and the walk-away floor all move. The workbook is a model, not a report.

Alongside it: a JSON snapshot of every row and statistic, and a methodology document describing what the run actually did, so a future run — by this tool or by a person with an AI assistant — can reproduce or challenge it.

## The architecture

```
                     ┌──────────────────────────────────────────┐
   market plugin ───▶ │  PLANNER                                 │
   (geometry,         │  what to collect, from which sources     │
    sources,          └───────────────────┬──────────────────────┘
    subject)                              │  fan out, ~7–8 items per worker
                          ┌───────────────┼───────────────┐
                          ▼               ▼               ▼
                     ┌─────────┐    ┌─────────┐    ┌─────────┐
                     │ worker  │    │ worker  │    │ worker  │   tool-using LLM calls
                     │ sold    │    │ price   │    │ geocode │   with a per-item
                     │ comps   │    │ history │    │         │   call budget
                     └────┬────┘    └────┬────┘    └────┬────┘
                          └───────────────┼───────────────┘
                                          ▼
                     ┌──────────────────────────────────────────┐
                     │  VERIFIER                                │
                     │  every fact matched against the known    │
                     │  sale price and date, or rejected        │
                     └───────────────────┬──────────────────────┘
                                         ▼
                     ┌──────────────────────────────────────────┐
                     │  DATASET  ──▶  deterministic analysis     │
                     │  exclusions ▸ statistics ▸ valuation      │
                     │  ▸ guidance ▸ quadrants ▸ agents          │
                     └───────────────────┬──────────────────────┘
                                         ▼
                        workbook  ·  snapshot  ·  methodology
```

Four ideas hold this up.

**The research layer is an interface, not a hard dependency.** `FixtureResearcher` reads recorded JSON; `LiveResearcher` researches the web. They are interchangeable, which is why the entire test suite — including every number the analysis produces — runs offline and deterministically.

**Read the page's own data before asking a model to read the page.** Most listing sites are single-page apps that ship their full result set as JSON inside the HTML and render it with JavaScript afterwards. A fetcher that reads only rendered text sees whatever the server pre-rendered — often a handful of cards — and reports that as everything. Parsing the embedded payload instead is exact, free, and complete: on one real index page it returned 40 records rather than 7, with coordinates and canonical URLs that never appear in the visible page at all. The model is the fallback for pages that genuinely have no structured data, which is where it belongs.

**The fetching happens here, not on someone else's servers.** Rate limiting, `robots.txt`, honest identification: these can only be *enforced* where the requests are made. A remote fetching service can only be hoped to behave.

**Fan-out with a budget.** Attribution, price history and geocoding are per-property lookups across dozens of addresses. They run as parallel workers of roughly seven or eight items each, with a per-item tool-call budget and a graceful give-up, and the run reports its own token cost at the end.

**Structured extraction, then verification.** Workers return strictly shaped rows and the orchestrator rejects malformed output. Then nothing enters the dataset without being matched against the sale price and date already known for that property. This is not ceremony — it is what catches a geocoder confidently resolving `Larkspur Vista Rd` to `Larkspur St` half a mile away, and a property page cheerfully reporting a sale from 1998.

**Spending is bounded, reported, and mostly avoidable.** Every run prints its token count and cost and stops at a ceiling. More usefully: the step that produces the most data — the complete result set, with coordinates and canonical URLs — is plain parsing and costs nothing at all. Only the per-property reading is metered, and pages are trimmed to the regions that carry facts before being sent. On real pages that cut them to under a quarter of their size with byte-identical extraction across every page tested.

**"Not found" is a value.** An unattributable sale renders as `—` and says so in the methodology doc. It is never imputed, never quietly dropped, and never filled with a plausible guess. A dataset that admits its gaps is worth more than one that hides them.

## What the verification pass actually catches

These are real failures from real runs, and the reason each guard exists.

| Failure | What it looks like | Guard |
|---|---|---|
| **The naming-grid trap** | An address labelled `88 E Chandler St` sits *west* of the north–south divider. The town's E/W street-naming grid splits at a different avenue than the one used as the analytical divider. | Quadrants are assigned from geocoded coordinates. Street-name directionals are never read. |
| **The straight-line trap** | The divider arterial curves. Classifying against a single latitude misplaces parcels at the edges of the market. | The divider is a piecewise-linear centerline of geocoded intersections; a parcel is compared against the latitude interpolated at *its own* longitude. |
| **The phantom page** | A fetched search page renders only its first seven or eight results server-side. The rest is JavaScript. A fetcher sees a complete-looking page that is missing most of the data. | Cross-check against a second source; a count that disagrees is a diagnostic, not a rounding error. |
| **The stale attribution** | A property page attributes the listing agent of a transaction from decades ago. | Every attribution is verified against the known sale price and date before it is accepted. |
| **The rounded acre** | An aggregator publishes acreage to two decimals. On a 5,000 sqft parcel that is ±4% of the $/sqft. | Rounded sizes are flagged and the caveat travels with the statistics. |
| **The closed "active" listing** | An index still lists a property as for sale days after it closed, inflating both sides of the market. | Reconciliation moves it to the sold side and notes the move. |

## The analysis, and why it is shaped this way

**Medians lead.** Parcel quality skews means badly. A single view lot or a single unbuildable slope moves the average and not the median, so the headline figures are medians and the means are shown beside them for contrast.

**The similar-size bracket is the primary anchor.** Because smaller parcels carry a higher rate, the valuation that matters is the average $/sqft of *sales near your own size*, not the market's. Four bases are reported side by side rather than blended — similar-size average (primary), all-sold median, all-sold average, and active-listing median — because the spread between them is information.

**Nothing is rounded before it is used.** Every valuation multiplies an unrounded rate by a size. Rounding $/sqft to the two decimals that get displayed shifts the resulting valuation by tens of dollars, and quietly breaks any regression test built on real numbers.

**List price is bait, not a ceiling.** In this market the sold-to-ask distribution has two tails: under-priced listings get bid up well above ask, while over-priced ones take serial cuts and still close below the reduced ask. That is why the recommended strategy prices just under a search-band edge — buyers filter by price band, and reaching the band below costs less than it appears to.

**Size is shown next to rate, always.** A quadrant can carry higher absolute prices *and* a lower average $/sqft purely because its parcels are larger. Without the size column beside it, a naive rate comparison inverts the real premium.

**The agent analysis is a shortlist to interview, not a ranking.** Samples are one to three sales per agent. A high sold-to-ask ratio can reflect a deliberately low list price as much as skill. The recommended interview test is in the workbook: ask each candidate for a list price *and* an expected close, before showing them your numbers.

## Comp profiles

The kind of property being comped is configuration, not an assumption. In the original hand-run process, "this is vacant land" was baked into four separate places — the identification heuristic, the $/sqft denominator, the exclusion rules, and the similar-size bracket. A `CompProfile` makes each one explicit:

```python
CompProfile(
    name="empty-lots",
    property_type=PropertyType.VACANT_LAND,
    metric=Denominator.LOT_SQFT,               # the $/sqft denominator
    identification=Identification(              # no bed/bath counts ⇒ it's land
        requires_absent_bed_bath=True),
    similar_bracket=SimilarBracket(             # ±25% of the subject's lot size
        attribute=Denominator.LOT_SQFT, tolerance=0.25),
    exclusions=Exclusions(max_lot_sqft=43_560,  # 1 acre
                          core_min_ppsf=30, core_max_ppsf=150),
)
```

Switch `property_type` to single-family and the pipeline prices on living area, inserts Beds / Baths / Year Built columns, keeps lot size as a secondary measure, and keys the bracket on living area — and the workbook's formulas follow, because **no formula in this codebase names a column letter**. Columns are declared with semantic keys and resolved at render time:

```python
Column(key="ppsf", formula="={price}{row}/{metric}{row}")
```

That indirection is the difference between a profile layer that parameterizes the pipeline and one that merely relabels it. There is a test for exactly that.

Build one interactively:

```bash
recomps profiles new --market demoville
```

## Saved searches and saved runs

A profile is a saved question. Running it produces an answer, and every answer is archived under the profile that produced it rather than overwriting the last one:

```
data/runs/<market>__<profile>/<timestamp>/
    snapshot.json     every row and every statistic
    comps.xlsx        the workbook, as it was that day
    methodology.md    what the run actually did
```

```bash
recomps profiles                    # what searches are saved, and how often each has run
recomps profiles show empty-lots    # what exactly this one asks for
recomps profiles new --from empty-lots   # a variation on an existing search
recomps history                     # every run, newest first
recomps run --compare last          # what moved since the previous run of this profile
```

A saved run can be **reopened**, which is more than reading its numbers back. The archive keeps the rows the run collected, so the analysis can be run over them again — to reprint the workbook months later, or to ask a different question of the same sales without going back to any website:

```bash
recomps render --run 2026-08-04                     # reprint that day's workbook
recomps render --run last --subject-size 7500       # what if my lot were bigger
recomps render --run last --exclude "12 Example St" # drop a comp you disagree with
```

Reopening deliberately re-runs the analysis rather than restoring the saved figures. Restoring them would let a saved run and a fresh one disagree about what the same numbers mean; recomputing cannot. It also uses the profile *as it was at the time of the run*, so editing a profile later does not change what a past run meant.

> **Scope honesty.** Vacant land is the validated reference path; every golden number in the regression suite comes from a real land dataset. Improved-property profiles are wired end to end and tested structurally, but their identification heuristics have not been validated against a live run. They ship marked experimental, and say so in their own output.

## Markets are plugins

A *market* is everything specific to one place: its boundaries, its sources, the property being valued, and its recorded data. The engine knows only the interface.

```python
class Market(Protocol):
    name: str
    def profiles(self) -> dict[str, CompProfile]: ...
    def geometry(self) -> MarketGeometry: ...   # quadrant dividers
    def sources(self) -> list[SourceSpec]: ...  # adapters + their quirks
    def fixture_dir(self) -> str | None: ...    # offline data
    def data_dir(self) -> str: ...              # where snapshots go
```

Two ways to supply one, both resolving to the same protocol:

```bash
recomps run --market demoville --offline          # installed, via entry point
recomps run --market-path ../my-market --offline   # a directory with market.toml
```

The directory route exists because the interesting markets are private. A real market plugin holds real addresses, real sale prices and a real subject property; it lives in its own repository, is never published, and is used straight from a sibling clone with nothing installed.

Verify a plugin against the interface with the conformance kit the engine ships:

```python
from recomps.testing.conformance import check_market

def test_my_market():
    assert not check_market(MY_MARKET).problems
```

## Demoville

Demoville does not exist. Its streets, firms and people are invented and its coordinates sit in empty farmland. Its fixtures are generated, not scraped.

They are shaped to break things rather than to look realistic: a parcel over the exclusion cap, a sale with no published size, an unattributed sale, a listing cut twice that still closed below its reduced ask, a heavy overbid, a stale "active" listing that had already closed, a dual-agency sale, a parcel sitting on the divider, and an address whose street name says East while its coordinates say west.

## Compliance and scope

- **This repository ships no scraped data and no scraping code targeting real listing sites.** The engine documents the adapter *interface*; live adapters and their site-specific quirks belong to private market plugins. Users are responsible for complying with the terms of service of any source they configure.
- Live adapters must rate-limit, identify themselves honestly, and respect `robots.txt`. No captcha evasion, no header spoofing. An adapter that cannot fetch a page logs it and continues; it does not pretend.
- **Output is market research, not an appraisal.** The author is not a licensed appraiser and this tool does not present itself as one. Every artifact it writes says so.
- Real addresses, subject properties and run snapshots stay in private plugins. CI enforces it.

## Interfaces

`recomps ui` opens a browser interface; everything else is `recomps <command>`. Neither is a subset of the other: every operation — listing and editing saved searches, running, browsing past runs, reopening one, regenerating its workbook — is a plain Python call that both merely wrap.

The interface is careful about one distinction the engine makes and a screen easily hides. Opening a past run shows the figures **that run reported**, read off disk with nothing recalculated. Untick a comp you disagree with, or type a different size for your property, and it **recomputes from that run's own saved rows** — no re-fetching, the same sales. Those two states look identical on a screen and mean different things, so every view says in words which one you are looking at.

It is also explicit about who pays before a run starts, since the reading is charged either to a Claude subscription or to a metered API account.

## Development

```bash
uv venv --python 3.13 && uv pip install -e ".[dev]"
pytest
recomps run --market demoville --offline --as-of 2026-08-31
```

Live research needs the optional extra:

```bash
uv pip install -e ".[dev,live]"
recomps run --market-path ../my-market --live
```

**Who pays for the page reading is a choice.** `--via subscription` (the
default) drives the `claude` CLI, so usage counts against a Claude subscription
and nothing is billed per page. `--via api` calls the Messages API and bills an
API account per token, with `--max-cost` as a ceiling.

One trap worth knowing: an `ANTHROPIC_API_KEY` in the environment **shadows** a
signed-in subscription, so a tool that inherits it gets billed even though the
user has a plan. The subscription route removes it from the child process for
exactly that reason.

`--max-lookups` caps the per-property fan-out, which is where nearly all the
cost sits; a capped run is the cheap way to check a market's sources still work
before committing to a full one.

Tests cover the quadrant classifier (including a divider parcel and the E-street-name trap), bracket selection, sold-to-ask arithmetic, brokerage-family grouping, address normalization, profile validation, snapshot round-trips, and a structural check that a non-land profile genuinely reshapes the workbook. Everything runs offline.

## Built with Claude

The research process this implements was developed interactively with Claude over three end-to-end runs against a real market: the source quirks, the verification rules, the naming-grid trap, and the analytical choices were all found by doing the work, not by designing up front. That transcript became a specification, and the specification became this repository — reviewed, argued with, and built in Claude Code.

The provenance is part of the point. The interesting artifact is not that an AI wrote some Python; it is that a messy, source-hostile, judgment-heavy research process was run by hand until it was understood, and only then turned into an architecture.

## License

MIT.
