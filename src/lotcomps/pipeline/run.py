"""Pipeline orchestration.

Order matters here in ways that are easy to get wrong:

* Reconciliation and exclusion (F3) run *before* any statistic, so a listing
  that has quietly closed cannot inflate the active side while also sitting in
  the sold side.
* Quadrant classification (F11) runs before the area table but after exclusion,
  so excluded parcels never reach a quadrant row.
* The valuation (F5) reads unrounded rates; nothing in this module rounds.

Every stage is pure given its inputs, which is why the whole pipeline can be
re-run from a snapshot without touching the network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from lotcomps.config.profile import CompProfile
from lotcomps.model.comp import ActiveListing, SoldComp
from lotcomps.pipeline import agents as agents_mod
from lotcomps.pipeline import quadrants as quad_mod
from lotcomps.pipeline import stats as stats_mod
from lotcomps.pipeline.exclusions import ExclusionRecord, apply_filters
from lotcomps.pipeline.guidance import Guidance, build_guidance
from lotcomps.pipeline.valuation import Valuation, value_subject
from lotcomps.plugin.market import Market
from lotcomps.research.base import Dataset, Diagnostics, Researcher


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
    agent_analysis: agents_mod.AgentAnalysis = field(default_factory=agents_mod.AgentAnalysis)
    excluded: list[ExclusionRecord] = field(default_factory=list)
    reconciled: list[str] = field(default_factory=list)
    missing_metric: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    diagnostics: Diagnostics = field(default_factory=Diagnostics)
    researcher: str = ""

    @property
    def pull_date(self) -> date:
        return self.run_at.date()

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
    agent_analysis = agents_mod.analyze(sold)

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
        agent_analysis=agent_analysis,
        excluded=filtered.excluded,
        reconciled=filtered.reconciled,
        missing_metric=filtered.missing_metric,
        caveats=stats_mod.Caveats.build(sold, sold_side).notes,
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
