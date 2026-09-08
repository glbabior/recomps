"""The command line.

The shape of it is the argument for the design:

    recomps markets                        what markets are available
    recomps profiles                       what searches you have saved
    recomps profiles new                   set one up
    recomps run --offline                  ask the question
    recomps history                        what you have asked before
    recomps run --compare last             what changed since last time

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
import time
from datetime import UTC, date, datetime
from pathlib import Path

import click

from recomps import __version__
from recomps.config.profile import (
    CompProfile,
    Denominator,
    Exclusions,
    Identification,
    PropertyType,
    SimilarBracket,
    Subject,
)
from recomps.config.store import (
    available_profiles,
    delete_user_profile,
    describe_profile,
    load_user_profiles,
    profile_origin,
    resolve_profile,
    save_user_profile,
)
from recomps.model.snapshot import Snapshot, build_snapshot
from recomps.pipeline import reopen as reopen_mod
from recomps.pipeline.run import run_pipeline
from recomps.plugin.loader import discover, load_market
from recomps.reporting import compare as compare_mod
from recomps.reporting import history as history_mod
from recomps.reporting import methodology
from recomps.research.fixture import FixtureResearcher
from recomps.workbook.builder import build_workbook

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


@click.group(
    context_settings={"help_option_names": ["-h", "--help"]},
    invoke_without_command=True,
)
@click.version_option(__version__, prog_name="recomps")
@click.pass_context
def main(ctx: click.Context) -> None:
    """Research real-estate comps and generate a live-formula workbook.

    Run with no arguments in a terminal and the graphical interface opens.
    Everything it does is also available as a subcommand below.
    """
    if ctx.invoked_subcommand is not None:
        return
    # Bare `recomps` opens the interface, because this is a tool people use by
    # looking at it. But only when a person is actually watching: in a pipe, a
    # script or CI, silently starting a web server that never exits would be a
    # trap, so those get the help text they expected.
    if _interactive():
        ctx.invoke(ui, market=None, market_path=None, port=8501)
    else:
        click.echo(ctx.get_help())


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
        click.echo("No profiles yet. Create one with `recomps profiles new`.")
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
    click.echo(f"  recomps run {target} --profile {name} --offline")


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
@click.option("--via", type=click.Choice(["subscription", "api"]), default="subscription",
              show_default=True,
              help="Who pays for the page reading: your Claude subscription, or a "
                   "metered API account.")
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
@click.option("--yes", "assume_yes", is_flag=True,
              help="Do not stop to confirm the per-property lookups. For scripts.")
def run(market, market_path, profile, window, offline, live_mode, max_lookups, max_cost,
        via, model, cache_dir, as_of, out, compare_target, no_archive, assume_yes,
) -> None:
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
        from recomps.research.live import build_live_researcher
        from recomps.research.llm import Budget

        def _ask(plan) -> bool:
            # The index is already read and free; this is the first moment the
            # run knows what it wants to spend, and the count came from the
            # site rather than from the user.
            click.echo("")
            click.secho(f"  {plan.describe()}", fg="yellow")
            if not _interactive():
                click.echo("  Not a terminal, so nothing is assumed. Pass --yes "
                           "to allow the lookups.")
                return False
            return click.confirm("  Go ahead?", default=True)

        researcher = build_live_researcher(
            cache_dir or str(Path(market_obj.data_dir()) / "http-cache"),
            via=via,
            budget=Budget(max_cost_usd=max_cost) if via == "api" else None,
            model=model,
            max_lookups=max_lookups,
            confirm=True if assume_yes else _ask,
        )
        if not researcher.extractor.available:
            _fail(
                "live research needs the `claude` CLI signed in to a Claude "
                "subscription. Run `claude auth login`, or use --via api with "
                "ANTHROPIC_API_KEY set, or run with --offline."
                if via == "subscription"
                else "live research via the API needs ANTHROPIC_API_KEY set."
            )
        payer = getattr(researcher.extractor, "pays_from", None)
        if payer:
            click.secho(f"Reading pages via your {payer}. Usage counts against that "
                        "plan; you are not billed per page.", fg="cyan")
        else:
            click.secho("Reading pages via the metered API. This run will be billed "
                        f"to your API account, up to ${max_cost:,.2f}.", fg="yellow")
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
    # What the run could not see changes every figure above it, so it prints
    # with them rather than only in the write-up.
    for note in diagnostics.not_found:
        click.secho(f"  ! {note}", fg="yellow")
    for failure in diagnostics.sources_failed:
        click.secho(f"  ! source unavailable: {failure}", fg="yellow")
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
        click.echo(f"No saved runs{scope} yet. Run one with `recomps run --offline`.")
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
    click.echo("    recomps run --profile <name> --offline --compare last")


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
            # Matched against the date the interface *shows*, or "--run
            # 2026-08-04" would miss the run listed under that date.
            records = [r for r in records if r.local_date == run_label]
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


def _open_when_ready(url: str, timeout: float = 30.0) -> None:
    """Open a browser once the server answers, not before.

    Opening immediately shows an error page, because the server takes a second
    or two to bind. Waiting for it to answer means the first thing the user
    sees is the app.
    """
    import threading
    import urllib.error
    import urllib.request
    import webbrowser

    def wait_then_open() -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(url, timeout=2)
            except urllib.error.HTTPError:
                break  # answering at all is enough
            except Exception:
                time.sleep(0.4)
                continue
            else:
                break
        webbrowser.open(url)

    threading.Thread(target=wait_then_open, daemon=True).start()


def _instance_record() -> Path:
    from recomps.config.store import config_dir

    return config_dir() / "ui-instance.json"


def _streamlit_is_serving(port: int, timeout: float = 0.4) -> bool:
    """Whether *a Streamlit server* is answering on this port.

    Streamlit's health endpoint is the check rather than a bare socket connect,
    because "something has this port" is not evidence that the something is
    ours.
    """
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(
            f"http://localhost:{port}/_stcore/health", timeout=timeout
        ) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _is_our_process(pid: int) -> bool:
    """Whether `pid` is a live Python process.

    PIDs are reused. A stale record naming a PID that now belongs to something
    else must never be enough to kill it, so the recorded PID is corroborated
    against the running image name before anything is terminated. This is a
    guard, not a proof of identity -- which is why the caller also requires a
    Streamlit to be answering on the recorded port.
    """
    import subprocess

    try:
        output = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "python" in output.lower() and str(pid) in output


def _pid_listening_on(port: int) -> int | None:
    """The PID holding `port`, when the system will say.

    The recorded PID is the first choice, but it only exists for instances this
    version launched -- and the interface someone already has open when they
    upgrade is exactly the one in the way. Asking the system who holds the port
    covers that, and covers a record lost to a crash.
    """
    import subprocess

    try:
        output = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True, timeout=15, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[3].upper() != "LISTENING":
            continue
        local = parts[1]
        # Split on the last colon: IPv6 locals are bracketed but still colonful.
        host, _, listening_port = local.rpartition(":")
        if listening_port != str(port):
            continue
        if host.strip("[]") not in ("127.0.0.1", "0.0.0.0", "::1", "::"):
            continue
        try:
            return int(parts[4])
        except ValueError:
            return None
    return None


def _stop_previous_instance(port: int) -> bool:
    """Stop an interface this program left running. Returns True if it did.

    Someone who launches from a desktop shortcut has no terminal to press
    Ctrl+C in, so the old server keeps the port and the new launch cannot bind
    it -- with the error going to a console nobody is looking at. Clearing the
    port here is what makes relaunching the only control they need.

    Two candidates are considered, because neither alone is reliable. The
    recorded PID is the process this program started, but `python -m streamlit`
    does not always hold the socket itself -- observed on Windows, where the
    listener was a child. The port's owner is the process actually in the way,
    but it is unrecorded for an interface opened before this existed. Both are
    tried, and each has to be a live Python process before it is signalled.

    Deliberately narrow: nothing is killed unless a Streamlit is answering on
    the port we are about to bind.
    """
    import json
    import os
    import signal

    record = _instance_record()
    recorded: int | None = None
    saved_port: int | None = None
    if record.is_file():
        try:
            saved = json.loads(record.read_text(encoding="utf-8"))
            recorded = int(saved["pid"])
            saved_port = int(saved["port"])
        except (json.JSONDecodeError, OSError, KeyError, TypeError, ValueError):
            recorded = saved_port = None

    # A record for another port is another instance, deliberately started with
    # --port. Leave it alone.
    if saved_port is not None and saved_port != port:
        return False

    if not _streamlit_is_serving(port):
        # Nothing is listening; any record is stale.
        record.unlink(missing_ok=True)
        return False

    candidates = []
    for pid in (_pid_listening_on(port), recorded if saved_port == port else None):
        if pid and pid != os.getpid() and pid not in candidates and _is_our_process(pid):
            candidates.append(pid)
    if not candidates:
        return False

    stopped = False
    for pid in candidates:
        try:
            os.kill(pid, signal.SIGTERM)
            stopped = True
        except (OSError, ValueError):
            continue
    if not stopped:
        return False

    # Give the port time to come free before the new server tries to bind it.
    for _ in range(40):
        if not _streamlit_is_serving(port, timeout=0.2):
            break
        time.sleep(0.1)
    record.unlink(missing_ok=True)
    return True


def _remember_instance(pid: int, port: int) -> None:
    """Record the running interface so the next launch can replace it."""
    import json

    record = _instance_record()
    try:
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps({"pid": pid, "port": port}), encoding="utf-8")
    except OSError:
        # Only costs the next launch its tidy-up; never a reason not to start.
        pass


def _silence_streamlit_onboarding() -> None:
    """Stop Streamlit asking for an email address on first launch.

    Left alone, Streamlit greets a first run by prompting for an email and
    waiting on standard input. Someone who double-clicks the program gets a
    console asking them to sign up for a newsletter, and the interface never
    opens -- which reads as the program being broken.

    Writing an empty credentials file is Streamlit's own documented way to
    decline. An existing file is never touched: the user may have put a real
    address there deliberately.
    """
    config = Path.home() / ".streamlit" / "credentials.toml"
    if config.exists():
        return
    try:
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text('[general]\nemail = ""\n', encoding="utf-8")
    except OSError:
        # Not being able to write this is not a reason to refuse to start.
        # The worst case is the prompt the user was going to see anyway.
        pass


@main.command()
@market_options
@click.option("--port", type=int, default=8501, show_default=True)
def ui(market, market_path, port) -> None:
    """Open the graphical interface in a browser.

    Everything the command line does is available there too -- saved searches,
    running, browsing past runs, regenerating a spreadsheet from one.
    """
    import subprocess

    try:
        import streamlit  # noqa: F401
    except ImportError:
        _fail(
            "the interface needs Streamlit. Install it with:  "
            'pip install -e ".[ui]"'
        )

    _silence_streamlit_onboarding()

    app = Path(__file__).resolve().parent / "ui" / "app.py"
    url = f"http://localhost:{port}"
    command = [
        sys.executable, "-m", "streamlit", "run", str(app),
        "--server.port", str(port),
        # Bind to this machine only. The default listens on every interface,
        # which puts a page showing what your property is worth on the local
        # network. Nothing here needs to be reachable from another device.
        "--server.address", "localhost",
        # Headless so Streamlit does not open its own browser tab and print its
        # banner; the tab is opened below, once the server is actually up.
        "--server.headless", "true",
        "--browser.gatherUsageStats", "false",
        "--global.developmentMode", "false",
    ]

    if _stop_previous_instance(port):
        click.echo("Stopped the interface that was already running.")

    click.secho(f"REComps is starting at {url}", bold=True)
    click.echo("Your browser should open. Press Ctrl+C here to stop it.")
    click.echo("")

    process = subprocess.Popen(command)
    _remember_instance(process.pid, port)
    _open_when_ready(url)
    try:
        process.wait()
    except KeyboardInterrupt:
        process.terminate()
    finally:
        _instance_record().unlink(missing_ok=True)
    click.echo("Stopped.")


@main.command(hidden=True)
@market_options
@click.option("--name", help="Name for the profile.")
@click.pass_context
def configure(ctx, market, market_path, name) -> None:
    """Deprecated alias for `recomps profiles new`."""
    click.secho("note: `configure` is now `recomps profiles new`\n", fg="yellow")
    market_obj = _open_market(market, market_path)
    _wizard(market_obj, name, None, replacing=None)


if __name__ == "__main__":  # pragma: no cover
    main()
