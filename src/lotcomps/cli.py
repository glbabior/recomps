"""The command line.

The shape of it is the argument for the design:

    lotcomps markets                        what markets are available
    lotcomps profiles                       what searches you have saved
    lotcomps profiles new                   set one up
    lotcomps run --offline                  ask the question
    lotcomps history                        what you have asked before
    lotcomps run --compare last             what changed since last time

A *profile* is a saved question -- what kind of property, whose property, how
far back to look, what counts as comparable. Running one produces an answer,
and every answer is archived under the profile that produced it, so the answers
can be compared rather than overwritten.

`run --offline` is the one worth trying first. It exercises collection,
exclusion, statistics, valuation, guidance, quadrant classification, agent
analysis, the workbook, the archive and the methodology document without a
network connection or an API key.
"""

from __future__ import annotations

import shutil
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
from lotcomps.config.store import (
    available_profiles,
    delete_user_profile,
    describe_profile,
    load_user_profiles,
    profile_origin,
    resolve_profile,
    save_user_profile,
)
from lotcomps.model.snapshot import Snapshot, build_snapshot
from lotcomps.pipeline import reopen as reopen_mod
from lotcomps.pipeline.run import run_pipeline
from lotcomps.plugin.loader import discover, load_market
from lotcomps.reporting import compare as compare_mod
from lotcomps.reporting import history as history_mod
from lotcomps.reporting import methodology
from lotcomps.research.fixture import FixtureResearcher
from lotcomps.workbook.builder import build_workbook

MARKET_OPTIONS = [
    click.option("--market", help="Installed market name."),
    click.option(
        "--market-path",
        type=click.Path(exists=True, file_okay=False),
        help="Directory holding market.toml.",
    ),
]


def market_options(fn):
    for option in reversed(MARKET_OPTIONS):
        fn = option(fn)
    return fn


def _fail(message: str) -> None:
    click.secho(f"error: {message}", fg="red", err=True)
    sys.exit(1)


def _open_market(market: str | None, market_path: str | None):
    try:
        return load_market(market, market_path)
    except Exception as exc:
        _fail(str(exc))


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="lotcomps")
def main() -> None:
    """Research real-estate comps and generate a live-formula workbook."""


# ---------------------------------------------------------------------------
# Markets
# ---------------------------------------------------------------------------


@main.command()
def markets() -> None:
    """List installed market plugins."""
    found = discover()
    if not found:
        click.echo(
            "No markets installed. The engine ships 'demoville'; install this package to "
            "register it, or pass --market-path to a market directory."
        )
        return
    for market in found:
        click.echo(f"  {market.name:<16} {market.description}")


# ---------------------------------------------------------------------------
# Profiles -- saved searches
# ---------------------------------------------------------------------------


@main.group(invoke_without_command=True)
@market_options
@click.pass_context
def profiles(ctx, market, market_path) -> None:
    """Manage saved search profiles. With no subcommand, lists them."""
    ctx.ensure_object(dict)
    ctx.obj["market"] = market
    ctx.obj["market_path"] = market_path
    if ctx.invoked_subcommand is None:
        ctx.invoke(profiles_list)


@profiles.command("list")
@click.pass_context
def profiles_list(ctx) -> None:
    """Show every saved search for this market."""
    market_obj = _open_market(ctx.obj["market"], ctx.obj["market_path"])
    found = available_profiles(market_obj)
    if not found:
        click.echo("No profiles yet. Create one with `lotcomps profiles new`.")
        return

    counts = history_mod.profiles_with_runs(market_obj.data_dir())
    default = market_obj.default_profile()
    click.secho(f"Profiles for {market_obj.description or market_obj.name}", bold=True)
    click.echo("")
    for name in sorted(found):
        profile = found[name]
        runs = counts.get(
            (history_mod.safe_name(market_obj.name), history_mod.safe_name(name)), 0
        )
        marker = "*" if name == default else " "
        origin = profile_origin(market_obj, name)
        click.echo(f" {marker} {name}")
        click.echo(f"     {describe_profile(profile)}")
        click.echo(
            f"     {origin}"
            + (f", {runs} saved run{'s' if runs != 1 else ''}" if runs else ", never run")
            + ("  [experimental property type]" if profile.is_experimental else "")
        )
        click.echo("")
    click.echo("  * = used when --profile is omitted")


@profiles.command("show")
@click.argument("name")
@click.pass_context
def profiles_show(ctx, name) -> None:
    """Print one profile's settings in full."""
    market_obj = _open_market(ctx.obj["market"], ctx.obj["market_path"])
    try:
        profile = resolve_profile(market_obj, name)
    except KeyError as exc:
        _fail(str(exc))

    size = profile.subject_size()
    bracket_low, bracket_high = (
        profile.similar_bracket.bounds(size) if size else (None, None)
    )
    ex = profile.exclusions

    click.secho(f"{profile.name}", bold=True)
    click.echo(f"  origin              {profile_origin(market_obj, name)}")
    click.echo(f"  property type       {profile.property_type.value.replace('_', ' ')}"
               + ("  (experimental)" if profile.is_experimental else ""))
    click.echo(f"  priced on           {profile.metric.value.replace('_', ' ')}")
    click.echo(f"  look back           {profile.window_days} days")
    click.echo("")
    click.secho("  Your property", bold=True)
    click.echo(f"    label             {profile.subject.label}")
    for field, label in (
        ("lot_sqft", "lot sq ft"),
        ("living_sqft", "living sq ft"),
        ("beds", "bedrooms"),
        ("baths", "bathrooms"),
        ("year_built", "year built"),
    ):
        value = getattr(profile.subject, field)
        if value:
            click.echo(f"    {label:<18}{value:,.0f}" if isinstance(value, (int, float))
                       else f"    {label:<18}{value}")
    click.echo("")
    click.secho("  What counts as comparable", bold=True)
    click.echo(f"    keyed on          {profile.similar_bracket.attribute.value.replace('_', ' ')}")
    if profile.similar_bracket.explicit_range:
        click.echo(f"    size range        {bracket_low:,.0f} to {bracket_high:,.0f} (pinned)")
    elif bracket_low:
        click.echo(f"    size range        {bracket_low:,.0f} to {bracket_high:,.0f} "
                   f"(+/-{profile.similar_bracket.tolerance:.0%})")
    if profile.similar_bracket.match_bed_count:
        click.echo("    bedrooms          must match")
    click.echo("")
    click.secho("  Exclusions", bold=True)
    click.echo("    max lot size      "
               + (f"{ex.max_lot_sqft:,.0f} sq ft" if ex.max_lot_sqft else "no cap"))
    if ex.explicit_address_keys:
        click.echo(f"    named exclusions  {len(ex.explicit_address_keys)}")
    click.echo("    core view         "
               + (f"${ex.core_min_ppsf:,.0f} to ${ex.core_max_ppsf:,.0f} per sq ft"
                  if ex.core_min_ppsf and ex.core_max_ppsf else "not set"))

    runs = history_mod.list_runs(market_obj.data_dir(), market_obj.name, name)
    click.echo("")
    if runs:
        click.echo(f"  {len(runs)} saved run(s), most recent {runs[0].label}")
    else:
        click.echo("  Never run.")


@profiles.command("new")
@click.option("--name", help="Name for the profile. Prompted if omitted.")
@click.option("--from", "base_name", help="Start from an existing profile's settings.")
@click.pass_context
def profiles_new(ctx, name, base_name) -> None:
    """Set up a new search profile, answering a few questions."""
    market_obj = _open_market(ctx.obj["market"], ctx.obj["market_path"])
    base = None
    if base_name:
        try:
            base = resolve_profile(market_obj, base_name)
        except KeyError as exc:
            _fail(str(exc))
        click.echo(f"Starting from {base_name!r}. Press Enter to keep any answer.\n")
    _wizard(market_obj, name, base, replacing=None)


@profiles.command("edit")
@click.argument("name")
@click.pass_context
def profiles_edit(ctx, name) -> None:
    """Change a saved profile, keeping its name."""
    market_obj = _open_market(ctx.obj["market"], ctx.obj["market_path"])
    try:
        base = resolve_profile(market_obj, name)
    except KeyError as exc:
        _fail(str(exc))
    if profile_origin(market_obj, name) == "market":
        click.secho(
            f"{name!r} ships with the market. Saving will create your own copy that "
            "takes precedence; the market's version is left alone.\n",
            fg="yellow",
        )
    click.echo("Press Enter to keep any answer.\n")
    _wizard(market_obj, name, base, replacing=name)


@profiles.command("delete")
@click.argument("name")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
@click.pass_context
def profiles_delete(ctx, name, yes) -> None:
    """Delete a saved profile. Saved runs are kept."""
    market_obj = _open_market(ctx.obj["market"], ctx.obj["market_path"])
    if name not in load_user_profiles(market_obj.name):
        if name in market_obj.profiles():
            _fail(f"{name!r} ships with the market; edit it in the market definition instead")
        _fail(f"no saved profile named {name!r}")
    if not yes and not click.confirm(f"Delete profile {name!r}?", default=False):
        click.echo("Left alone.")
        return
    delete_user_profile(market_obj.name, name)
    click.secho(f"Deleted {name!r}. Its saved runs are still in the archive.", fg="green")


# ---------------------------------------------------------------------------
# The wizard, shared by `profiles new` and `profiles edit`
# ---------------------------------------------------------------------------


def _wizard(
    market_obj, name: str | None, base: CompProfile | None, replacing: str | None
) -> None:
    """Ask for a profile's settings. `base` supplies the defaults when editing."""
    existing = available_profiles(market_obj)
    click.secho(
        f"Setting up a search for {market_obj.description or market_obj.name}", bold=True
    )
    if existing and not base:
        click.echo(f"Existing profiles: {', '.join(sorted(existing))}")
    click.echo("")

    name = name or replacing or click.prompt(
        "Profile name", default=base.name if base else "my-property"
    )

    click.echo("")
    click.echo("What kind of property are you comping?")
    types = list(PropertyType)
    for i, property_type in enumerate(types, start=1):
        label = property_type.value.replace("_", " ")
        suffix = "" if property_type is PropertyType.VACANT_LAND else "  (experimental)"
        click.echo(f"  {i}. {label}{suffix}")
    default_choice = types.index(base.property_type) + 1 if base else 1
    choice = click.prompt(
        "Choice", type=click.IntRange(1, len(types)), default=default_choice, show_default=True
    )
    property_type = types[choice - 1]
    improved = property_type.is_improved

    if improved:
        click.secho(
            "\nHeads up: profiles for built property are wired through the whole pipeline "
            "but have not been checked against a real run. Treat the output as provisional.",
            fg="yellow",
        )

    metric = Denominator.LIVING_SQFT if improved else Denominator.LOT_SQFT
    click.echo("")
    click.echo(
        f"Comparing on {'living area' if improved else 'lot size'}, per square foot."
    )

    prior = base.subject if base and base.property_type is property_type else Subject()
    subject = Subject(
        label=click.prompt("\nWhat should we call your property?", default=prior.label)
    )
    if improved:
        subject.living_sqft = click.prompt(
            "Living area (sq ft)", type=float, default=prior.living_sqft
        ) if prior.living_sqft else click.prompt("Living area (sq ft)", type=float)
        subject.lot_sqft = (
            click.prompt("Lot size (sq ft), optional", type=float, default=prior.lot_sqft or 0.0)
            or None
        )
        subject.beds = click.prompt("Bedrooms", type=float, default=prior.beds or 3.0)
        subject.baths = click.prompt("Bathrooms", type=float, default=prior.baths or 2.0)
        subject.year_built = (
            click.prompt("Year built, optional", type=int, default=prior.year_built or 0) or None
        )
    else:
        subject.lot_sqft = (
            click.prompt("Lot size (sq ft)", type=float, default=prior.lot_sqft)
            if prior.lot_sqft
            else click.prompt("Lot size (sq ft)", type=float)
        )

    size = subject.size_for(metric)
    base_bracket = base.similar_bracket if base else SimilarBracket()
    click.echo("")
    click.echo("Comparable sales are the ones close to your property in size.")
    tolerance = (
        click.prompt(
            "  How close, as a plus-or-minus percentage",
            type=float,
            default=base_bracket.tolerance * 100,
        )
        / 100.0
    )
    low, high = size * (1 - tolerance), size * (1 + tolerance)
    click.echo(f"  That is {low:,.0f} to {high:,.0f} sq ft.")
    pinned = bool(base_bracket.explicit_range)
    if click.confirm("  Use exact size limits instead?", default=pinned):
        default_low, default_high = base_bracket.explicit_range or (
            round(low, -2), round(high, -2)
        )
        low = click.prompt("    Smallest", type=float, default=default_low)
        high = click.prompt("    Largest", type=float, default=default_high)
        bracket = SimilarBracket(
            attribute=metric, tolerance=tolerance, explicit_range=(low, high)
        )
    else:
        bracket = SimilarBracket(attribute=metric, tolerance=tolerance)
    if improved:
        bracket.match_bed_count = click.confirm(
            "  Only compare against the same bedroom count?",
            default=base_bracket.match_bed_count,
        )

    click.echo("")
    window = click.prompt(
        "How many days of sales to look back over",
        type=int,
        default=base.window_days if base else 90,
    )

    base_ex = base.exclusions if base else Exclusions()
    exclusions = Exclusions(explicit_address_keys=list(base_ex.explicit_address_keys))
    click.echo("")
    if not improved:
        default_acres = (base_ex.max_lot_sqft or 0) / 43_560 if base_ex.max_lot_sqft else 1.0
        acres = click.prompt(
            "Ignore parcels bigger than how many acres? (0 for no limit)",
            type=float,
            default=default_acres,
        )
        exclusions.max_lot_sqft = acres * 43_560 if acres else None
    else:
        exclusions.max_lot_sqft = None

    click.echo("")
    click.echo(
        "A few sales are always weirdly cheap or weirdly expensive. The 'core view' "
        "reports a second set of figures with those set aside. Nothing is deleted."
    )
    low_default = base_ex.core_min_ppsf or (30.0 if not improved else 0.0)
    high_default = base_ex.core_max_ppsf or (150.0 if not improved else 0.0)
    low_bound = click.prompt(
        "  Ignore below what price per sq ft? (0 to skip)", type=float, default=low_default
    )
    high_bound = click.prompt(
        "  Ignore above what price per sq ft? (0 to skip)", type=float, default=high_default
    )
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

    overwriting = name in existing and replacing != name
    if overwriting and not click.confirm(
        f"\n{name!r} already exists. Replace it?", default=False
    ):
        click.echo("Nothing saved.")
        return

    click.echo("")
    click.secho("  " + describe_profile(profile), fg="cyan")
    path = save_user_profile(market_obj.name, profile)
    click.secho(f"\nSaved {name!r} to {path}", fg="green")
    click.echo("\nRun it with:")
    target = (
        f"--market {market_obj.name}"
        if market_obj.name in {m.name for m in discover()}
        else "--market-path <this market's directory>"
    )
    click.echo(f"  lotcomps run {target} --profile {name} --offline")


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


def _choose_profile(market_obj, requested: str | None) -> CompProfile:
    """Resolve --profile, offering a menu when run interactively without one."""
    found = available_profiles(market_obj)
    if requested is None and _interactive() and len(found) > 1:
        default = market_obj.default_profile()
        names = sorted(found)
        click.echo("Which search would you like to run?\n")
        for i, name in enumerate(names, start=1):
            marker = " (default)" if name == default else ""
            click.echo(f"  {i}. {name}{marker}")
            click.echo(f"     {describe_profile(found[name])}")
        click.echo("")
        index = click.prompt(
            "Choice",
            type=click.IntRange(1, len(names)),
            default=names.index(default) + 1 if default in names else 1,
        )
        requested = names[index - 1]
        click.echo("")
    try:
        return resolve_profile(market_obj, requested)
    except KeyError as exc:
        _fail(str(exc))


@main.command()
@market_options
@click.option("--profile", help="Which saved search to run. Prompted if omitted.")
@click.option("--window", type=int, help="Override the profile's look-back period, in days.")
@click.option("--offline", is_flag=True, help="Use recorded data; never touch the network.")
@click.option("--live", "live_mode", is_flag=True,
              help="Research the web now. Costs money; the run reports how much.")
@click.option("--max-lookups", type=int, default=None,
              help="Cap per-property lookups. Useful for a cheap trial run.")
@click.option("--max-cost", type=float, default=25.0,
              help="Stop the run if the estimated spend passes this, in dollars.")
@click.option("--model", default=None, help="Model to use for reading pages.")
@click.option("--cache-dir", type=click.Path(file_okay=False), default=None,
              help="Where fetched pages are cached. Defaults to the market's data dir.")
@click.option("--as-of", type=click.DateTime(formats=["%Y-%m-%d"]),
              help="Treat this date as today. Makes a run reproducible.")
@click.option("--out", type=click.Path(file_okay=False), default="out",
              help="Where to put the current workbook. Every run is also archived.")
@click.option("--compare", "compare_target", default=None,
              help="Compare against a saved run: a snapshot path, or 'last'.")
@click.option("--no-archive", is_flag=True, help="Do not keep this run in the archive.")
def run(market, market_path, profile, window, offline, live_mode, max_lookups, max_cost,
        model, cache_dir, as_of, out, compare_target, no_archive) -> None:
    """Run a search and write the workbook, the archive entry and the write-up."""
    market_obj = _open_market(market, market_path)
    comp_profile = _choose_profile(market_obj, profile)
    if window:
        comp_profile.window_days = window

    if offline and live_mode:
        _fail("--offline and --live ask for opposite things; pick one")
    if not offline and not live_mode:
        _fail(
            "say which: --offline reads the market's recorded data, --live researches "
            "the web now (and costs money)"
        )

    window_end = as_of.date() if as_of else date.today()
    run_at = (
        datetime.combine(as_of.date(), datetime.min.time(), tzinfo=UTC)
        if as_of
        else datetime.now(UTC)
    )
    if live_mode:
        from lotcomps.research.live import build_live_researcher
        from lotcomps.research.llm import Budget

        researcher = build_live_researcher(
            cache_dir or str(Path(market_obj.data_dir()) / "http-cache"),
            budget=Budget(max_cost_usd=max_cost),
            model=model,
            max_lookups=max_lookups,
        )
        if not researcher.extractor.available:
            _fail(
                "live research needs Anthropic API credentials. Set ANTHROPIC_API_KEY, "
                "or run with --offline."
            )
        click.echo(
            "Researching. Pages are fetched one at a time per site, so this takes "
            "a few minutes.\n"
        )
    else:
        researcher = FixtureResearcher()

    try:
        result = run_pipeline(
            market_obj, comp_profile, researcher, window_end=window_end, run_at=run_at,
        )
    except Exception as exc:
        _fail(str(exc))
    finally:
        closer = getattr(getattr(researcher, "fetcher", None), "close", None)
        if closer:
            closer()

    snapshot = build_snapshot(result)
    changes = None
    if compare_target:
        changes = _do_compare(market_obj, comp_profile, snapshot, compare_target, run_at)

    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{market_obj.name}_{comp_profile.name}"
    workbook_path = out_dir / f"{stem}_comps.xlsx"
    methodology_path = out_dir / f"{stem}_methodology.md"

    build_workbook(result, changes).save(workbook_path)
    methodology.write(result, methodology_path)

    archive_dir = None
    if not no_archive:
        archive_dir = history_mod.run_dir(
            market_obj.data_dir(), market_obj.name, comp_profile.name, run_at
        )
        archive_dir.mkdir(parents=True, exist_ok=True)
        snapshot.write(archive_dir / history_mod.SNAPSHOT_NAME)
        shutil.copy2(workbook_path, archive_dir / history_mod.WORKBOOK_NAME)
        shutil.copy2(methodology_path, archive_dir / history_mod.METHODOLOGY_NAME)

    _report(result, workbook_path, methodology_path, archive_dir)


def _do_compare(market_obj, comp_profile, snapshot, target, run_at):
    """Resolve --compare and print the differences. Returns rows for the workbook."""
    if target == "last":
        record = history_mod.latest_run(
            market_obj.data_dir(), market_obj.name, comp_profile.name, before=run_at
        )
        if record is None:
            click.secho(
                f"  no earlier run of {comp_profile.name!r} to compare against; "
                "this one becomes the baseline\n",
                fg="yellow",
            )
            return None
        path = record.snapshot
        click.echo(f"Comparing against the run of {record.label}\n")
    else:
        path = Path(target)
        if not path.is_file():
            click.secho(f"warning: no snapshot at {target}", fg="yellow", err=True)
            return None
    try:
        report = compare_mod.compare(Snapshot.load(path), snapshot)
    except Exception as exc:
        click.secho(f"warning: could not compare against {path}: {exc}", fg="yellow", err=True)
        return None
    click.echo(report.as_text())
    click.echo("")
    return report.as_cell_rows()


def _report(result, workbook_path, methodology_path, archive_dir) -> None:
    sold, active = result.sold_stats, result.active_stats
    click.secho(f"{result.market_description or result.market_name}", bold=True)
    click.echo(f"  profile        {result.profile.name} ({result.profile.property_type.value})")
    click.echo(f"  window         {result.window_start} to {result.window_end}")
    click.echo(f"  sold           {sold.count}"
               + (f"   median ${sold.median_ppsf:,.2f}/sqft" if sold.median_ppsf else ""))
    click.echo(f"  active         {active.count}"
               + (f"   median ${active.median_ppsf:,.2f}/sqft" if active.median_ppsf else ""))
    if result.valuation and result.valuation.primary and result.valuation.primary.value:
        primary = result.valuation.primary
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
    click.echo(f"  write-up       {methodology_path}")
    if archive_dir:
        click.echo(f"  archived       {archive_dir}")
    click.echo("")
    click.secho("  Market research, not an appraisal.", fg="yellow")


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


@main.command()
@market_options
@click.option("--profile", help="Only show runs of this profile.")
@click.option("--limit", type=int, default=20, help="How many runs to show.")
def history(market, market_path, profile, limit) -> None:
    """List saved runs, newest first."""
    market_obj = _open_market(market, market_path)
    records = history_mod.list_runs(market_obj.data_dir(), market_obj.name, profile)
    if not records:
        scope = f" for profile {profile!r}" if profile else ""
        click.echo(f"No saved runs{scope} yet. Run one with `lotcomps run --offline`.")
        return
    click.secho(f"Saved runs for {market_obj.description or market_obj.name}", bold=True)
    click.echo("")
    current = None
    for record in records[:limit]:
        if record.profile != current:
            current = record.profile
            click.secho(f"  {record.profile}", bold=True)
        click.echo(f"     {record.label}   {record.directory}")
    if len(records) > limit:
        click.echo(f"\n  ... and {len(records) - limit} more")
    click.echo("")
    click.echo("  Compare the two most recent with:")
    click.echo("    lotcomps run --profile <name> --offline --compare last")


@main.command()
@market_options
@click.option("--profile", help="Which profile's history to open. Prompted if omitted.")
@click.option("--run", "run_label", default="last",
              help="Which saved run: 'last', a date like 2026-08-04, or a snapshot path.")
@click.option("--out", type=click.Path(file_okay=False), default="out",
              help="Where to write the regenerated workbook.")
@click.option("--exclude", multiple=True,
              help="Address to drop as not comparable. Repeatable.")
@click.option("--subject-size", type=float,
              help="Re-value against a different size for your own property.")
def render(market, market_path, profile, run_label, out, exclude, subject_size) -> None:
    """Regenerate a workbook from a saved run, without re-fetching anything.

    Use this to reprint a past run months later, or to ask a different question
    of the same sales -- drop a comp you disagree with, or try a different size
    for your own property -- and see the valuation move.
    """
    market_obj = _open_market(market, market_path)
    record = None
    if Path(run_label).is_file():
        snapshot_path = Path(run_label)
        profile_name = profile
    else:
        comp_profile = _choose_profile(market_obj, profile)
        profile_name = comp_profile.name
        records = history_mod.list_runs(market_obj.data_dir(), market_obj.name, profile_name)
        if not records:
            _fail(f"no saved runs for profile {profile_name!r}; run one first")
        if run_label != "last":
            records = [r for r in records if r.run_at.strftime("%Y-%m-%d") == run_label]
            if not records:
                _fail(f"no saved run of {profile_name!r} dated {run_label}")
        record = records[0]
        if record.snapshot is None:
            _fail(f"the run of {record.label} has no saved data to reopen")
        snapshot_path = record.snapshot

    try:
        snapshot = Snapshot.load(snapshot_path)
        saved_profile = reopen_mod.profile_from_snapshot(snapshot)
    except Exception as exc:
        _fail(str(exc))

    changed = None
    if subject_size:
        changed = CompProfile.from_dict(saved_profile.to_dict())
        setattr(changed.subject, changed.metric.value, subject_size)

    try:
        result = reopen_mod.reopen(
            snapshot, market_obj, profile=changed, exclude_addresses=list(exclude)
        )
    except Exception as exc:
        _fail(str(exc))

    if record:
        click.echo(f"Reopened the run of {record.label} ({record.profile})\n")
    if exclude:
        click.echo(f"  dropped        {len(exclude)} sale(s) as not comparable")
    if subject_size:
        click.echo(f"  re-valued at   {subject_size:,.0f} sq ft")

    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = "_adjusted" if (exclude or subject_size) else ""
    stem = f"{market_obj.name}_{result.profile.name}{suffix}"
    workbook_path = out_dir / f"{stem}_comps.xlsx"
    methodology_path = out_dir / f"{stem}_methodology.md"
    build_workbook(result).save(workbook_path)
    methodology.write(result, methodology_path)
    _report(result, workbook_path, methodology_path, None)


@main.command()
@click.argument("prior", type=click.Path(exists=True, dir_okay=False))
@click.argument("current", type=click.Path(exists=True, dir_okay=False))
def compare(prior, current) -> None:
    """Compare two saved runs, given their snapshot files."""
    try:
        report = compare_mod.compare(Snapshot.load(prior), Snapshot.load(current))
    except Exception as exc:
        _fail(str(exc))
    click.echo(report.as_text())


@main.command(hidden=True)
@market_options
@click.option("--name", help="Name for the profile.")
@click.pass_context
def configure(ctx, market, market_path, name) -> None:
    """Deprecated alias for `lotcomps profiles new`."""
    click.secho("note: `configure` is now `lotcomps profiles new`\n", fg="yellow")
    market_obj = _open_market(market, market_path)
    _wizard(market_obj, name, None, replacing=None)


if __name__ == "__main__":  # pragma: no cover
    main()
