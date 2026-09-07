"""Pipeline orchestration.

Order matters here in ways that are easy to get wrong:

* Reconciliation and exclusion (F3) run *before* any statistic, so a listing
  that has quietly closed cannot inflate the active side while also sitting in
  the sold side.
* Quadrant classification (F11) runs before the area table but after exclusion,
  so excluded parcels never reach a quadrant row.
* The valuation (F5) reads unrounded rates; nothing in this module rounds.
* The size/rate table (F12) runs after exclusion for the same reason as the
  area table, and reports on the same sold set the statistics describe.
* The bracket ladder (F13) runs over that same set, so its widest rung is
  the all-sold basis the valuation reports, to the cent.

Every stage is pure given its inputs, which is why the whole pipeline can be
re-run from a snapshot without touching the network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from recomps.clock import local_date
from recomps.config.profile import CompProfile
from recomps.model.comp import ActiveListing, SoldComp
from recomps.pipeline import agents as agents_mod
from recomps.pipeline import quadrants as quad_mod
from recomps.pipeline import size_bands as size_mod
from recomps.pipeline import stats as stats_mod
from recomps.pipeline.exclusions import ExclusionRecord, apply_filters
from recomps.pipeline.guidance import Guidance, build_guidance
from recomps.pipeline.ladder import BracketLadder, build_bracket_ladder
from recomps.pipeline.valuation import Valuation, value_subject
from recomps.plugin.market import Market
from recomps.research.base import Dataset, Diagnostics, Researcher


@dataclass
class RunResult:
    """Everything one run produced, and enough context to explain it."""

    market_name: str
    market_description: str
    profile: CompProfile
    run_at: datetime
    window_start: date
    window_end: date
    sold: list[SoldComp] = field(default_factory=list)
    active: list[ActiveListing] = field(default_factory=list)
    sold_stats: stats_mod.SideStats = field(default_factory=stats_mod.SideStats)
    active_stats: stats_mod.SideStats = field(default_factory=stats_mod.SideStats)
    sold_to_ask: stats_mod.SoldToAskStats = field(default_factory=stats_mod.SoldToAskStats)
    valuation: Valuation | None = None
    guidance: Guidance | None = None
    area_table: quad_mod.AreaTable = field(default_factory=quad_mod.AreaTable)
    size_bands: size_mod.SizeBandTable = field(default_factory=size_mod.SizeBandTable)
    ladder: BracketLadder = field(default_factory=BracketLadder)
    agent_analysis: agents_mod.AgentAnalysis = field(default_factory=agents_mod.AgentAnalysis)
    excluded: list[ExclusionRecord] = field(default_factory=list)
    reconciled: list[str] = field(default_factory=list)
    missing_metric: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    diagnostics: Diagnostics = field(default_factory=Diagnostics)
    researcher: str = ""

    @property
    def pull_date(self) -> date:
        """The day this run happened, locally.

        Not `run_at.date()`: an evening run west of Greenwich is already
        tomorrow in UTC, and every artifact would date itself a day ahead.
        """
        return local_date(self.run_at)

    @property
    def warnings(self) -> list[str]:
        out = list(self.valuation.warnings) if self.valuation else []
        if self.guidance:
            out.extend(self.guidance.warnings)
        if self.profile.is_experimental:
            out.insert(
                0,
                f"The {self.profile.property_type.value} profile is experimental: it is wired "
                "end to end but its identification heuristics have not been validated against "
                "a live run. Treat its numbers as provisional.",
            )
        if self.active_stats.count == 0 and self.sold_stats.count:
            out.append(
                "No active listings were collected, so the asking-price basis is "
                "unavailable rather than zero. See the write-up for why."
            )
        if self.sold_stats.count == 0:
            out.append("No sold comps in the window; every valuation basis is unavailable.")
        elif self.sold_stats.count < 10:
            out.append(
                f"Only {self.sold_stats.count} sold comps in the window. Widen --window before "
                "relying on the medians."
            )
        return out


def window_for(profile: CompProfile, end: date | None = None) -> tuple[date, date]:
    end = end or date.today()
    return end - timedelta(days=profile.window_days), end


def analyze(
    dataset: Dataset,
    market: Market,
    profile: CompProfile,
    window_start: date,
    window_end: date,
    *,
    run_at: datetime | None = None,
    researcher_name: str = "",
) -> RunResult:
    """Run the deterministic half of the pipeline over a collected dataset."""
    filtered = apply_filters(dataset.sold, dataset.active, profile)
    sold, active = filtered.sold, filtered.active

    geometry = market.geometry()
    if geometry.is_configured():
        quad_mod.classify_all(list(sold) + list(active), geometry)

    sold_side = stats_mod.sold_stats(sold, profile)
    active_side = stats_mod.active_stats(active, profile)
    ratio_stats = stats_mod.sold_to_ask(sold)
    ratios = [r for c in sold if (r := c.sold_to_ask()) is not None]

    valuation = value_subject(sold, active, profile)
    guidance = build_guidance(valuation, ratio_stats, ratios)
    area_table = quad_mod.build_area_table(sold, active, profile, geometry)
    size_bands = size_mod.build_size_band_table(sold, profile)
    ladder = build_bracket_ladder(sold, profile)
    agent_analysis = agents_mod.analyze(
        sold, denominator=profile.metric.value, active=active
    )

    return RunResult(
        market_name=market.name,
        market_description=getattr(market, "description", ""),
        profile=profile,
        run_at=run_at or datetime.now(UTC),
        window_start=window_start,
        window_end=window_end,
        sold=sold,
        active=active,
        sold_stats=sold_side,
        active_stats=active_side,
        sold_to_ask=ratio_stats,
        valuation=valuation,
        guidance=guidance,
        area_table=area_table,
        size_bands=size_bands,
        ladder=ladder,
        agent_analysis=agent_analysis,
        excluded=filtered.excluded,
        reconciled=filtered.reconciled,
        missing_metric=filtered.missing_metric,
        caveats=(
            stats_mod.Caveats.build(sold, sold_side).notes
            + [
                f"{note}. Kept as separate sales -- one street address can carry more "
                "than one parcel, and merging them would delete a real sale."
                for note in filtered.distinct_at_one_address
            ]
        ),
        diagnostics=dataset.diagnostics,
        researcher=researcher_name,
    )


def run_pipeline(
    market: Market,
    profile: CompProfile,
    researcher: Researcher,
    *,
    window_end: date | None = None,
    run_at: datetime | None = None,
) -> RunResult:
    """Collect and analyze end to end."""
    problems = profile.validate()
    if problems:
        raise ValueError(
            "profile is not usable:\n  - " + "\n  - ".join(problems)
        )
    start, end = window_for(profile, window_end)
    dataset = researcher.gather(market, profile, start, end)
    return analyze(
        dataset,
        market,
        profile,
        start,
        end,
        run_at=run_at,
        researcher_name=getattr(researcher, "name", ""),
    )
