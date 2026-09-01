"""A conformance kit for market plugins.

The interesting market plugins are private, which means they cannot be tested in
this repository's CI and this repository cannot be tested against them. Without
something to hold the contract still, the interface drifts: the engine changes a
field, the private plugin keeps working against a stale assumption, and nobody
finds out until a run produces quietly wrong numbers.

So the contract ships as executable checks. A private plugin imports this module
and asserts against its own market:

    from lotcomps.testing.conformance import check_market

    def test_conforms():
        report = check_market(MY_MARKET)
        assert not report.problems, report.summary()

`check_market` is structural and offline. `check_market_runs` goes further and
executes the whole pipeline against the plugin's fixtures, which is the check
that actually catches a schema change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from lotcomps.config.profile import CompProfile, Denominator, PropertyType
from lotcomps.plugin.market import Market


@dataclass
class ConformanceReport:
    market: str = ""
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"Conformance report for {self.market or '<unnamed>'}"]
        for item in self.checked:
            lines.append(f"  ok       {item}")
        for item in self.warnings:
            lines.append(f"  warning  {item}")
        for item in self.problems:
            lines.append(f"  PROBLEM  {item}")
        return "\n".join(lines)


def _check_profile(name: str, profile: CompProfile, report: ConformanceReport) -> None:
    if profile.name != name:
        report.warnings.append(
            f"profile registered as {name!r} but names itself {profile.name!r}"
        )
    for problem in profile.validate():
        report.problems.append(f"profile {name!r}: {problem}")
    if profile.property_type is PropertyType.VACANT_LAND:
        if profile.metric is not Denominator.LOT_SQFT:
            report.problems.append(f"profile {name!r}: land must price on lot_sqft")
        if not profile.identification.requires_absent_bed_bath:
            report.warnings.append(
                f"profile {name!r}: land normally identifies on absent bed/bath counts"
            )
    elif profile.metric is not Denominator.LIVING_SQFT:
        report.warnings.append(
            f"profile {name!r}: improved types normally price on living area"
        )


def check_market(market: Market) -> ConformanceReport:
    """Structural checks. No network, no fixtures required."""
    report = ConformanceReport(market=getattr(market, "name", ""))

    if not getattr(market, "name", ""):
        report.problems.append("market has no name")
    else:
        report.checked.append("has a name")

    for method in ("profiles", "default_profile", "geometry", "sources", "fixture_dir",
                   "data_dir", "metadata"):
        if not callable(getattr(market, method, None)):
            report.problems.append(f"missing method {method}()")
    if report.problems:
        return report
    report.checked.append("implements the Market protocol")

    profiles = market.profiles()
    if not profiles:
        report.problems.append("ships no profiles")
    else:
        report.checked.append(f"ships {len(profiles)} profile(s)")
        for name, profile in profiles.items():
            _check_profile(name, profile, report)

    default = market.default_profile()
    if default and default not in profiles:
        report.problems.append(f"default profile {default!r} is not among its profiles")
    elif default:
        report.checked.append(f"default profile {default!r} resolves")

    geometry = market.geometry()
    if geometry.is_configured():
        longitudes = [w.lon for w in geometry.ns_centerline]
        if longitudes != sorted(longitudes) and longitudes != sorted(longitudes, reverse=True):
            report.warnings.append(
                "N/S centerline waypoints are not in longitude order; they are sorted "
                "before use, but out-of-order data usually means a transcription slip"
            )
        if len(geometry.ns_centerline) < 2:
            report.warnings.append(
                "N/S divider has a single waypoint, so it is effectively a straight "
                "latitude -- fine only if the divider really is straight"
            )
        low, high = min(longitudes), max(longitudes)
        if not low <= (geometry.ew_longitude or 0) <= high:
            report.warnings.append(
                "the E/W divider longitude falls outside the N/S centerline's span, so "
                "parcels near it are classified against a held endpoint latitude"
            )
        report.checked.append("quadrant geometry is configured")
    else:
        report.warnings.append("no quadrant geometry; by-area analysis will be empty")

    if not market.sources():
        report.warnings.append("declares no sources")
    else:
        report.checked.append(f"declares {len(market.sources())} source(s)")

    if not market.fixture_dir():
        report.warnings.append("has no fixture directory, so it cannot run offline")
    else:
        report.checked.append("has a fixture directory")

    if not market.data_dir():
        report.problems.append("has no data directory for snapshots")
    return report


def check_market_runs(
    market: Market, profile_name: str | None = None, as_of: date | None = None
) -> ConformanceReport:
    """Run the full offline pipeline against the plugin's fixtures.

    This is the check worth having in a private plugin's CI: it is what fails
    when the engine changes a field name the plugin's fixtures still use.
    """
    from lotcomps.model.snapshot import build_snapshot
    from lotcomps.pipeline.run import run_pipeline
    from lotcomps.research.fixture import FixtureResearcher
    from lotcomps.workbook.builder import build_workbook

    report = check_market(market)
    if report.problems:
        return report

    name = profile_name or market.default_profile()
    profile = market.profiles()[name]
    try:
        result = run_pipeline(
            market, profile, FixtureResearcher(), window_end=as_of or date.today()
        )
    except Exception as exc:
        report.problems.append(f"offline run failed: {type(exc).__name__}: {exc}")
        return report
    report.checked.append(f"offline run completed with {result.sold_stats.count} sold comps")

    if result.sold_stats.count == 0:
        report.problems.append(
            "offline run found no sold comps in the window; check the fixture dates "
            "against the profile's window_days and the as-of date"
        )
    try:
        build_workbook(result)
        report.checked.append("workbook builds")
    except Exception as exc:
        report.problems.append(f"workbook build failed: {type(exc).__name__}: {exc}")
    try:
        build_snapshot(result).to_json()
        report.checked.append("snapshot serializes")
    except Exception as exc:
        report.problems.append(f"snapshot serialization failed: {type(exc).__name__}: {exc}")
    return report
