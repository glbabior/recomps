"""The command line.

Four commands, and the shape of them is the argument for the design:

    lotcomps markets                     what is installed
    lotcomps configure --market X        build a profile interactively (F0)
    lotcomps run --market X --offline    the whole pipeline, no network
    lotcomps compare A.json B.json       what moved between two runs

`run --offline` is the one worth trying first. It exercises collection,
exclusion, statistics, valuation, guidance, quadrant classification, agent
analysis, the workbook, the snapshot and the methodology document without a
network connection or an API key.
"""

from __future__ import annotations

import sys
from datetime import UTC, date, datetime
from pathlib import Path

import click

from lotcomps import __version__
from lotcomps.config.profile import (
    CompProfile,
    Denominator,
    Exclusions,
    Identification,
    PropertyType,
    SimilarBracket,
    Subject,
)
from lotcomps.config.store import available_profiles, resolve_profile, save_user_profile
from lotcomps.model.snapshot import Snapshot, build_snapshot, snapshot_filename
from lotcomps.pipeline.run import run_pipeline
from lotcomps.plugin.loader import discover, load_market
from lotcomps.reporting import compare as compare_mod
from lotcomps.reporting import methodology
from lotcomps.research.fixture import FixtureResearcher
from lotcomps.workbook.builder import write_workbook


def _fail(message: str) -> None:
    click.secho(f"error: {message}", fg="red", err=True)
    sys.exit(1)


def _open_market(market: str | None, market_path: str | None):
    try:
        return load_market(market, market_path)
    except Exception as exc:
        _fail(str(exc))


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="lotcomps")
def main() -> None:
    """Research real-estate comps and generate a live-formula workbook."""


@main.command()
def markets() -> None:
    """List installed market plugins."""
    found = discover()
    if not found:
        click.echo("No markets installed. The engine ships 'demoville'; install this "
                   "package to register it, or pass --market-path to a market directory.")
        return
    for market in found:
        click.echo(f"  {market.name:<16} {market.description}")


@main.command()
@click.option("--market", help="Installed market name.")
@click.option("--market-path", type=click.Path(exists=True, file_okay=False),
              help="Directory holding market.toml.")
@click.option("--profile", help="Profile to run. Defaults to the market's default.")
@click.option("--window", type=int, help="Trailing days of sales to consider.")
@click.option("--offline", is_flag=True, help="Use recorded fixtures; never touch the network.")
@click.option("--as-of", type=click.DateTime(formats=["%Y-%m-%d"]),
              help="Treat this date as today. Makes a run reproducible.")
@click.option("--out", type=click.Path(file_okay=False), default="out",
              help="Where to write the workbook, snapshot and methodology doc.")
@click.option("--compare", "compare_path", type=click.Path(exists=True, dir_okay=False),
              help="Prior snapshot to diff this run against.")
def run(market, market_path, profile, window, offline, as_of, out, compare_path) -> None:
    """Run the pipeline and write the workbook, snapshot and methodology doc."""
    market_obj = _open_market(market, market_path)
    try:
        comp_profile = resolve_profile(market_obj, profile)
    except KeyError as exc:
        _fail(str(exc))
    if window:
        comp_profile.window_days = window

    if not offline:
        _fail(
            "live research is not wired up in this build. Run with --offline to use the "
            "market's recorded fixtures."
        )

    window_end = as_of.date() if as_of else date.today()
    run_at = (
        datetime.combine(as_of.date(), datetime.min.time(), tzinfo=UTC)
        if as_of
        else datetime.now(UTC)
    )
    try:
        result = run_pipeline(
            market_obj,
            comp_profile,
            FixtureResearcher(),
            window_end=window_end,
            run_at=run_at,
        )
    except Exception as exc:
        _fail(str(exc))

    changes = None
    if compare_path:
        try:
            prior = Snapshot.load(compare_path)
            report = compare_mod.compare(prior, build_snapshot(result))
            changes = report.as_cell_rows()
            click.echo(report.as_text())
            click.echo("")
        except Exception as exc:
            click.secho(f"warning: could not compare against {compare_path}: {exc}",
                        fg="yellow", err=True)

    out_dir = Path(out)
    stem = f"{market_obj.name}_{comp_profile.name}"
    workbook_path = write_workbook(result, out_dir / f"{stem}_comps.xlsx", changes)
    methodology_path = methodology.write(result, out_dir / f"{stem}_methodology.md")
    snapshot = build_snapshot(result)
    snapshot_path = snapshot.write(Path(market_obj.data_dir()) / snapshot_filename(result))

    _report(result, workbook_path, methodology_path, snapshot_path)


def _report(result, workbook_path, methodology_path, snapshot_path) -> None:
    sold, active = result.sold_stats, result.active_stats
    click.secho(f"{result.market_description or result.market_name}", bold=True)
    click.echo(f"  profile        {result.profile.name} ({result.profile.property_type.value})")
    click.echo(f"  window         {result.window_start} to {result.window_end}")
    click.echo(f"  sold           {sold.count}"
               + (f"   median ${sold.median_ppsf:,.2f}/sqft" if sold.median_ppsf else ""))
    click.echo(f"  active         {active.count}"
               + (f"   median ${active.median_ppsf:,.2f}/sqft" if active.median_ppsf else ""))
    if result.valuation and result.valuation.primary:
        primary = result.valuation.primary
        if primary.value:
            click.secho(
                f"  valuation      ${primary.value:,.0f}  "
                f"({primary.sample_size} similar-size comps at ${primary.ppsf:,.2f}/sqft)",
                fg="cyan",
            )
    if result.guidance:
        for strategy in result.guidance.strategies:
            if strategy.recommended and strategy.list_price:
                click.secho(f"  suggested list ${strategy.list_price:,.0f}", fg="cyan")
        if result.guidance.floor:
            click.echo(f"  floor          ${result.guidance.floor:,.0f}")
    if result.excluded:
        click.echo(f"  excluded       {len(result.excluded)}")
    if result.reconciled:
        click.echo(f"  reconciled     {len(result.reconciled)} listing(s) had already closed")
    diagnostics = result.diagnostics
    if diagnostics.llm_calls:
        click.echo(f"  llm            {diagnostics.llm_calls} calls, "
                   f"{diagnostics.input_tokens:,} in / {diagnostics.output_tokens:,} out"
                   + (f", ~${diagnostics.estimated_cost_usd:,.2f}"
                      if diagnostics.estimated_cost_usd else ""))
    for warning in result.warnings:
        click.secho(f"  ! {warning}", fg="yellow")
    click.echo("")
    click.echo(f"  workbook       {workbook_path}")
    click.echo(f"  methodology    {methodology_path}")
    click.echo(f"  snapshot       {snapshot_path}")
    click.echo("")
    click.secho("  Market research, not an appraisal.", fg="yellow")


@main.command()
@click.argument("prior", type=click.Path(exists=True, dir_okay=False))
@click.argument("current", type=click.Path(exists=True, dir_okay=False))
def compare(prior, current) -> None:
    """Diff two run snapshots."""
    try:
        report = compare_mod.compare(Snapshot.load(prior), Snapshot.load(current))
    except Exception as exc:
        _fail(str(exc))
    click.echo(report.as_text())


@main.command()
@click.option("--market", help="Installed market name.")
@click.option("--market-path", type=click.Path(exists=True, file_okay=False),
              help="Directory holding market.toml.")
@click.option("--name", help="Name for the profile. Prompted if omitted.")
def configure(market, market_path, name) -> None:
    """Build a named comp profile interactively (F0)."""
    market_obj = _open_market(market, market_path)
    existing = available_profiles(market_obj)

    click.secho(f"Configuring a profile for {market_obj.description or market_obj.name}", bold=True)
    if existing:
        click.echo(f"Existing profiles: {', '.join(sorted(existing))}")
    click.echo("")

    name = name or click.prompt("Profile name", default="my-property")

    click.echo("")
    click.echo("What kind of property are you comping?")
    types = list(PropertyType)
    for i, property_type in enumerate(types, start=1):
        label = property_type.value.replace("_", " ")
        suffix = "" if property_type in (PropertyType.VACANT_LAND,) else "  (experimental)"
        click.echo(f"  {i}. {label}{suffix}")
    choice = click.prompt(
        "Choice", type=click.IntRange(1, len(types)), default=1, show_default=True
    )
    property_type = types[choice - 1]
    improved = property_type.is_improved

    if improved:
        click.secho(
            "\nHeads up: improved-property profiles are wired end to end but their "
            "identification heuristics have not been validated against a live run. "
            "Treat the output as provisional.",
            fg="yellow",
        )

    metric = Denominator.LIVING_SQFT if improved else Denominator.LOT_SQFT
    click.echo("")
    click.echo(f"Pricing on {metric.value.replace('_', ' ')} "
               f"({'living area' if improved else 'lot size'}).")

    subject = Subject(label=click.prompt("\nLabel for your property", default="Your property"))
    if improved:
        subject.living_sqft = click.prompt("Living area (sq ft)", type=float)
        subject.lot_sqft = (
            click.prompt("Lot size (sq ft), optional", type=float, default=0.0) or None
        )
        subject.beds = click.prompt("Bedrooms", type=float, default=3.0)
        subject.baths = click.prompt("Bathrooms", type=float, default=2.0)
        subject.year_built = click.prompt("Year built, optional", type=int, default=0) or None
    else:
        subject.lot_sqft = click.prompt("Lot size (sq ft)", type=float)

    size = subject.size_for(metric)
    click.echo("")
    tolerance = click.prompt(
        "Similar-size bracket, as +/- percent of your property", type=float, default=25.0
    ) / 100.0
    low, high = size * (1 - tolerance), size * (1 + tolerance)
    click.echo(f"  That is {low:,.0f} to {high:,.0f} sq ft.")
    if click.confirm("  Pin the bracket to specific bounds instead?", default=False):
        low = click.prompt("  Low", type=float, default=round(low, -2))
        high = click.prompt("  High", type=float, default=round(high, -2))
        bracket = SimilarBracket(attribute=metric, tolerance=tolerance,
                                 explicit_range=(low, high))
    else:
        bracket = SimilarBracket(attribute=metric, tolerance=tolerance)
    if improved:
        bracket.match_bed_count = click.confirm(
            "  Require comps to match your bedroom count?", default=False
        )

    click.echo("")
    window = click.prompt("Trailing window, in days", type=int, default=90)

    exclusions = Exclusions()
    if not improved:
        acres = click.prompt(
            "Exclude parcels larger than how many acres? (0 for no cap)",
            type=float, default=1.0,
        )
        exclusions.max_lot_sqft = acres * 43_560 if acres else None
    else:
        exclusions.max_lot_sqft = None
    click.echo("")
    click.echo("The 'core view' reports statistics with extreme $/sqft filtered out. "
               "Values stay in the data either way.")
    low_bound = click.prompt("  Core view lower $/sqft (0 to skip)", type=float,
                             default=30.0 if not improved else 0.0)
    high_bound = click.prompt("  Core view upper $/sqft (0 to skip)", type=float,
                              default=150.0 if not improved else 0.0)
    exclusions.core_min_ppsf = low_bound or None
    exclusions.core_max_ppsf = high_bound or None

    profile = CompProfile(
        name=name,
        property_type=property_type,
        metric=metric,
        identification=Identification(
            requires_absent_bed_bath=not improved,
            type_labels=["LOT", "LAND"] if not improved else ["SINGLE_FAMILY", "HOUSE", "SFR"],
            exclude_habitable_structure=not improved,
        ),
        similar_bracket=bracket,
        exclusions=exclusions,
        subject=subject,
        attributes=["beds", "baths", "year_built"] if improved else [],
        window_days=window,
    )

    problems = profile.validate()
    if problems:
        click.secho("\nThat profile is not usable yet:", fg="red")
        for problem in problems:
            click.echo(f"  - {problem}")
        sys.exit(1)

    path = save_user_profile(market_obj.name, profile)
    click.echo("")
    click.secho(f"Saved profile {name!r} to {path}", fg="green")
    click.echo("")
    click.echo("Run it with:")
    click.echo(f"  lotcomps run --market {market_obj.name} --profile {name} --offline")


if __name__ == "__main__":  # pragma: no cover
    main()
