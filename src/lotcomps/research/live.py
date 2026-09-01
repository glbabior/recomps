"""The live research layer.

The shape of a run, and why it is in this order:

1. **Backbone.** Read each configured index page's embedded data. This is
   deterministic parsing, so it is exact and free, and it yields the complete
   result set rather than the slice a site chose to pre-render. Coordinates and
   per-property URLs usually come with it.

2. **Identify.** Keep the records that match the profile's property type,
   preferring a source's own explicit type label over the absent-bed/bath
   heuristic where one exists.

3. **Fan out.** Whatever the index left blank -- attribution, price history,
   an exact lot size -- is looked up on each property's own page, in parallel
   workers, each with a small call budget and permission to give up.

4. **Verify.** Nothing a page claims enters the dataset until its stated price
   and date match the sale we asked about.

5. **Geocode the remainder.** Only the parcels whose coordinates did not come
   free, which is normally a handful rather than all of them.

Two properties this ordering buys. The expensive, error-prone step runs against
the smallest possible set of items, because everything the cheap deterministic
step could answer has already been answered. And a run that loses a source
degrades rather than fails: the backbone shrinks, the gaps grow, the
diagnostics say so, and the numbers that survive are still checked.
"""

from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass, field
from datetime import date, datetime

from lotcomps.adapters.embedded import EmbeddedSpec, extract_records
from lotcomps.adapters.fetch import PoliteFetcher
from lotcomps.adapters.geocode import VerifyingGeocoder
from lotcomps.adapters.html import focus_text, looks_like_a_block_page, reduce_html
from lotcomps.config.profile import CompProfile
from lotcomps.model.comp import ActiveListing, SoldComp
from lotcomps.plugin.market import Market, SourceSpec
from lotcomps.research.base import Dataset, Diagnostics
from lotcomps.research.llm import (
    DEFAULT_MODEL,
    Budget,
    ExtractionClient,
    PropertyPageFacts,
    build_request,
)
from lotcomps.research.verify import Claim, VerificationLog, apply_claims, flag_anomalous_rows

#: Workers in the fan-out. The per-host lock in the fetcher keeps any one site
#: serial regardless, so this bounds concurrency across sites, not at one.
DEFAULT_WORKERS = 4
#: Characters of page text kept before the reading step. A property page runs
#: to sixty thousand characters of which the facts are a few hundred, and on a
#: measured page 97% of the cost was input tokens. Trimming to the regions that
#: sit beside facts cut pages to under a quarter of their size with identical
#: extraction across every page tested.
DEFAULT_PAGE_CHARS = 14_000
#: The reduction applied before focusing, purely to bound parsing work.
RAW_PAGE_CHARS = 200_000


@dataclass
class SourceOutcome:
    """What one source contributed, or why it did not."""

    name: str
    records: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


@dataclass
class LiveRunLog:
    sources: list[SourceOutcome] = field(default_factory=list)
    verification: VerificationLog = field(default_factory=VerificationLog)
    lookups_attempted: int = 0
    lookups_found: int = 0
    geocoded: int = 0
    anomalies: list[str] = field(default_factory=list)


def _to_sold_comp(row: dict) -> SoldComp:
    return SoldComp(
        address=str(row.get("address") or "").strip(),
        sold_date=row.get("sold_date"),
        sold_price=row.get("sold_price"),
        lot_sqft=row.get("lot_sqft"),
        living_sqft=row.get("living_sqft"),
        beds=row.get("beds"),
        baths=row.get("baths"),
        lat=row.get("lat"),
        lon=row.get("lon"),
        lot_size_is_rounded=bool(row.get("lot_size_is_rounded")),
        sources=[row["source_url"]] if row.get("source_url") else [],
    )


def _matches_profile(row: dict, profile: CompProfile) -> bool:
    """Whether a record is the kind of property being comped.

    A source that states the type outright is believed; the bed/bath heuristic
    is the fallback for sources that do not.
    """
    label = row.get("property_type")
    if label:
        return profile.identification.matches(
            has_bed_bath=row.get("beds") is not None, label=str(label)
        )
    return profile.identification.matches(has_bed_bath=row.get("beds") is not None)


def _in_window(comp: SoldComp, start: date, end: date) -> bool:
    return comp.sold_date is None or start <= comp.sold_date <= end


class LiveResearcher:
    """Collects a dataset from the web, politely and with its spending visible."""

    name = "live"

    def __init__(
        self,
        fetcher: PoliteFetcher,
        extractor: ExtractionClient,
        geocoder: VerifyingGeocoder | None = None,
        workers: int = DEFAULT_WORKERS,
        page_chars: int = DEFAULT_PAGE_CHARS,
        enrich: bool = True,
        max_lookups: int | None = None,
    ) -> None:
        self.fetcher = fetcher
        self.extractor = extractor
        self.geocoder = geocoder
        self.workers = workers
        self.page_chars = page_chars
        self.enrich = enrich
        #: A hard ceiling on property-page lookups, mostly for trying the layer
        #: out without spending a full run's budget. The comps beyond it keep
        #: whatever the index gave them and are reported as unenriched.
        self.max_lookups = max_lookups
        self.log = LiveRunLog()

    # -- step 1: the backbone ---------------------------------------------

    def _read_index(
        self, source: SourceSpec, profile: CompProfile, raw_config: dict
    ) -> tuple[list[SoldComp], SourceOutcome]:
        outcome = SourceOutcome(name=source.name)
        embedded = raw_config.get("embedded")
        if not embedded or not source.url:
            outcome.error = "no index URL or embedded-data mapping configured"
            return [], outcome

        response = self.fetcher.fetch(source.url, adapter_version=f"{source.adapter}-1")
        if not response.ok:
            outcome.error = response.describe()
            return [], outcome
        if looks_like_a_block_page(response.text):
            outcome.error = "served a challenge page rather than results"
            return [], outcome

        extracted = extract_records(response.text, EmbeddedSpec.from_config(embedded))
        if extracted.problems and not extracted.rows:
            outcome.error = "; ".join(extracted.problems[:2])
            return [], outcome

        comps = [
            _to_sold_comp(row)
            for row in extracted.rows
            if _matches_profile(row, profile) and row.get("address")
        ]
        outcome.records = len(comps)
        return comps, outcome

    # -- step 3: the fan-out ----------------------------------------------

    def _needs_lookup(self, comp: SoldComp, profile: CompProfile) -> bool:
        if not comp.sources:
            return False  # nowhere to look
        return (
            comp.agent is None
            or comp.brokerage is None
            or comp.final_list_price is None
            or comp.metric_sqft(profile.metric.value) is None
        )

    def _look_up(self, comp: SoldComp) -> tuple[SoldComp, list[Claim], str]:
        """Read one property's own page. Returns (comp, claims, problem)."""
        url = comp.sources[0]
        response = self.fetcher.fetch(url, adapter_version="property-page-1")
        if not response.ok:
            return comp, [], f"{comp.address}: {response.describe()}"
        if looks_like_a_block_page(response.text):
            return comp, [], f"{comp.address}: {url} served a challenge page"

        text = focus_text(
            reduce_html(response.text, max_chars=RAW_PAGE_CHARS),
            max_chars=self.page_chars,
        )
        result = self.extractor.extract(
            text,
            PropertyPageFacts,
            build_request(
                comp.address,
                comp.sold_price,
                comp.sold_date,
                "Find the listing agent and brokerage, any buyer-side agent, the "
                "final and original list prices from the price history, and the "
                "lot size.",
            ),
        )
        if not result.ok:
            return comp, [], f"{comp.address}: extraction failed -- {result.error}"

        facts = result.parsed
        if facts.page_is_about_a_different_property:
            return comp, [], f"{comp.address}: the page found is about another property"

        stated_date = None
        if facts.stated_sold_date:
            try:
                stated_date = datetime.strptime(facts.stated_sold_date[:10], "%Y-%m-%d").date()
            except ValueError:
                stated_date = None

        def claim(field_name: str, value: object) -> Claim:
            return Claim(
                address=comp.address,
                field_name=field_name,
                value=value,
                source_url=url,
                claimed_price=facts.stated_sold_price,
                claimed_date=stated_date,
            )

        claims = [
            claim("agent", facts.listing_agent),
            claim("brokerage", facts.listing_brokerage),
            claim("buyer_agent", facts.buyer_agent),
            claim("final_list_price", facts.final_list_price),
            claim("original_list_price", facts.original_list_price),
            claim("lot_sqft", facts.lot_sqft),
            claim("living_sqft", facts.living_sqft),
        ]
        if facts.dual_agency_stated and facts.listing_agent:
            # The page said it outright, which is the only trustworthy signal.
            claims.append(claim("buyer_agent", facts.listing_agent))
        return comp, [c for c in claims if c.value not in (None, "")], ""

    def _fan_out(self, comps: list[SoldComp], profile: CompProfile) -> list[Claim]:
        targets = [c for c in comps if self._needs_lookup(c, profile)]
        if self.max_lookups is not None and len(targets) > self.max_lookups:
            skipped = len(targets) - self.max_lookups
            targets = targets[: self.max_lookups]
            self.log.verification.unverifiable.append(
                f"{skipped} parcel(s) were not looked up because the run was capped "
                f"at {self.max_lookups} lookups. Their attribution is missing rather "
                "than absent from the source."
            )
        self.log.lookups_attempted = len(targets)
        if not targets:
            return []

        claims: list[Claim] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as pool:
            for _comp, found, problem in pool.map(self._look_up, targets):
                if problem:
                    self.log.verification.unverifiable.append(problem)
                    continue
                if found:
                    self.log.lookups_found += 1
                    claims.extend(found)
        return claims

    # -- step 5: geocode what is left -------------------------------------

    def _geocode_gaps(self, comps: list[SoldComp], market: Market) -> None:
        if self.geocoder is None:
            return
        city = str(market.metadata().get("city") or market.description or "")
        missing = [c for c in comps if c.lat is None or c.lon is None]
        for comp in missing:
            query = f"{comp.address}, {city}" if city else comp.address
            result = self.geocoder.locate(query)
            if result.ok:
                comp.lat, comp.lon = result.lat, result.lon
                self.log.geocoded += 1

    # -- the run ------------------------------------------------------------

    def gather(
        self,
        market: Market,
        profile: CompProfile,
        window_start: date,
        window_end: date,
    ) -> Dataset:
        comps: list[SoldComp] = []

        for source in market.sources():
            if not source.enabled or "embedded" not in source.config:
                continue
            found, outcome = self._read_index(source, profile, source.config)
            self.log.sources.append(outcome)
            comps.extend(found)

        comps = [c for c in comps if _in_window(c, window_start, window_end)]
        self.log.anomalies = flag_anomalous_rows(comps)

        if self.enrich and comps:
            claims = self._fan_out(comps, profile)
            self.log.verification = apply_claims(
                comps, claims, identification=profile.identification
            )
            # A listing never reduced has one price, and the reference data
            # records it in both columns rather than leaving one blank.
            for comp in comps:
                if comp.final_list_price and comp.original_list_price is None:
                    comp.original_list_price = comp.final_list_price

        self._geocode_gaps(comps, market)

        diagnostics = self._diagnostics()
        # Active listings need their own index source. Saying "0 active" without
        # saying why reads as "nothing is for sale", which is a different and
        # much more interesting claim than "we could not look".
        active: list[ActiveListing] = []
        if not active:
            reachable = [s.name for s in market.sources() if s.enabled and s.config.get("embedded")]
            configured = [
                s.name for s in market.sources()
                if "active" in (s.role or "").lower() or "active" in s.name.lower()
            ]
            if configured:
                diagnostics.not_found.append(
                    "No active listings were collected. The configured source(s) for them "
                    f"({', '.join(configured)}) provided none this run -- see sources_failed. "
                    "Asking-price context and the active-median valuation basis are "
                    "unavailable, not zero."
                )
            else:
                diagnostics.not_found.append(
                    "No active-listing source is configured for this market, so the "
                    "active side is empty by design rather than because nothing is "
                    f"for sale. Sources read this run: {', '.join(reachable) or 'none'}."
                )
        return Dataset(sold=comps, active=active, diagnostics=diagnostics)

    def _diagnostics(self) -> Diagnostics:
        spent = self.extractor.budget.spent
        diagnostics = Diagnostics(
            sources_used=[f"{s.name} ({s.records} records)" for s in self.log.sources if s.ok],
            sources_failed=[f"{s.name}: {s.error}" for s in self.log.sources if not s.ok],
            rejected=list(self.log.verification.rejected),
            not_found=list(self.log.verification.unverifiable),
            llm_calls=spent.calls,
            input_tokens=spent.input_tokens,
            output_tokens=spent.output_tokens,
            estimated_cost_usd=spent.cost_usd(self.extractor.model),
            cache_hits=self.fetcher.cache.hits,
        )
        if self.log.lookups_attempted:
            diagnostics.sources_used.append(
                f"property pages ({self.log.lookups_found} of "
                f"{self.log.lookups_attempted} lookups found something)"
            )
        if self.log.geocoded:
            diagnostics.sources_used.append(f"geocoder ({self.log.geocoded} parcels)")
        diagnostics.not_found.extend(self.log.anomalies)
        return diagnostics


def build_live_researcher(
    cache_dir: str,
    *,
    via: str = "subscription",
    budget: Budget | None = None,
    model: str | None = None,
    workers: int = DEFAULT_WORKERS,
    delay_seconds: float = 3.0,
    cache_ttl: float = 6 * 60 * 60,
    enrich: bool = True,
    max_lookups: int | None = None,
) -> LiveResearcher:
    """Assemble the layer with sensible defaults."""
    from lotcomps.adapters.cache import PageCache
    from lotcomps.adapters.geocode import ArcGISGeocoder, CensusGeocoder

    cache = PageCache(cache_dir, ttl_seconds=cache_ttl)
    fetcher = PoliteFetcher(cache, delay_seconds=delay_seconds)

    # Two ways to do the reading, and the difference is who pays. Through a
    # Claude subscription the usage counts against that plan; through the API
    # it is billed per token. The fetching is local either way, so the
    # politeness rules hold in both.
    if via == "api":
        extractor = ExtractionClient(model=model or DEFAULT_MODEL, budget=budget or Budget())
    elif via == "subscription":
        from lotcomps.research.claude_code import DEFAULT_CLI_MODEL, ClaudeCodeExtractor

        extractor = ClaudeCodeExtractor(model=model or DEFAULT_CLI_MODEL, budget=budget)
    else:
        raise ValueError(f"unknown reading route {via!r}; use 'subscription' or 'api'")
    geocoder = VerifyingGeocoder(CensusGeocoder(fetcher), ArcGISGeocoder(fetcher))
    return LiveResearcher(
        fetcher, extractor, geocoder, workers=workers, enrich=enrich,
        max_lookups=max_lookups,
    )
