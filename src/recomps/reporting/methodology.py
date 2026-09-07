"""The methodology document (F12).

Every run rewrites a markdown record of what it actually did: which sources,
which window, which rules, which counts, and which numbers came out. The
audience is a future replicator -- this app six months from now, or a person
with an AI assistant and no access to this code -- who needs to reproduce the
result and check whether the market moved or the method did.

It is written from the run's own data rather than from a template of what the
run was supposed to do, so a run that silently fell back to two sources says so.
"""

from __future__ import annotations

from pathlib import Path

from recomps.clock import to_local
from recomps.pipeline.run import RunResult


def _money(value: float | None) -> str:
    return f"${value:,.0f}" if value is not None else "—"


def _rate(value: float | None) -> str:
    return f"${value:,.2f}" if value is not None else "—"


def _pct(value: float | None) -> str:
    return f"{value:.1%}" if value is not None else "—"


def _num(value: float | None) -> str:
    return f"{value:,.0f}" if value is not None else "—"


def render(result: RunResult) -> str:
    profile = result.profile
    sold, active = result.sold_stats, result.active_stats
    lines: list[str] = []
    add = lines.append

    add(f"# {result.market_description or result.market_name} — comp methodology")
    add("")
    # Local, but with the offset kept, so it is both readable and exact.
    add(f"**Run:** {to_local(result.run_at).isoformat()}  ")
    add(f"**Window:** {result.window_start} to {result.window_end} ({profile.window_days} days)  ")
    add(f"**Profile:** `{profile.name}` — {profile.property_type.value}, "
        f"priced on {profile.metric.value}  ")
    add(f"**Research layer:** {result.researcher or 'unknown'}")
    add("")
    if profile.is_experimental:
        add("> **Experimental profile.** This property type is wired through the whole "
            "pipeline but its identification heuristics have not been validated against a "
            "live run. Treat the numbers as provisional.")
        add("")
    add("> Market research, not an appraisal. The author is not a licensed appraiser.")
    add("")

    add("## What was run")
    add("")
    identification = (
        "no bed/bath counts on the listing"
        if profile.identification.requires_absent_bed_bath
        else "bed/bath counts present"
    )
    add(f"1. **Collect sold** — {profile.property_type.value} sales in the window. "
        f"Identification: {identification}.")
    add("2. **Collect active** — current listings matching the same profile.")
    cap = (
        f"parcels over {_num(profile.exclusions.max_lot_sqft)} sqft"
        if profile.exclusions.max_lot_sqft
        else "no size cap"
    )
    named = profile.exclusions.explicit_address_keys
    named_note = f", plus {len(named)} named parcels" if named else ""
    add(f"3. **Exclusions** — {cap}{named_note}. "
        f"{len(result.excluded)} parcel(s) excluded this run.")
    add(f"4. **Core view** — $/sqft outside "
        f"{_rate(profile.exclusions.core_min_ppsf)} to {_rate(profile.exclusions.core_max_ppsf)} "
        "is kept in the dataset and filtered only from the core statistics.")
    add("5. **Statistics** — medians lead; means are reported beside them and are skewed by "
        "property quality.")
    add(f"6. **Valuation** — four bases against a "
        f"{_num(profile.subject_size())} sqft subject.")
    add("7. **Guidance** — three list-price strategies and a walk-away floor.")
    add("8. **Quadrants** — classified from geocoded coordinates against the market's "
        "divider centerline. Street-name directionals are never used.")
    add("")

    add("## Sources")
    add("")
    if result.diagnostics.sources_used:
        for source in result.diagnostics.sources_used:
            add(f"- {source}")
    else:
        add("- (none recorded)")
    for source in result.diagnostics.sources_failed:
        add(f"- **failed:** {source}")
    add("")

    add("## Results")
    add("")
    add("| Statistic | Sold | Active |")
    add("|---|---|---|")
    add(f"| Count | {sold.count} | {active.count} |")
    add(f"| Average $/sqft | {_rate(sold.avg_ppsf)} | {_rate(active.avg_ppsf)} |")
    add(f"| Median $/sqft | {_rate(sold.median_ppsf)} | {_rate(active.median_ppsf)} |")
    add(f"| Core median $/sqft | {_rate(sold.core_median_ppsf)} "
        f"| {_rate(active.core_median_ppsf)} |")
    add(f"| Average price | {_money(sold.avg_price)} | {_money(active.avg_price)} |")
    add(f"| Median price | {_money(sold.median_price)} | {_money(active.median_price)} |")
    add(f"| Average size | {_num(sold.avg_size)} | {_num(active.avg_size)} |")
    add(f"| Min / max $/sqft | {_rate(sold.min_ppsf)} / {_rate(sold.max_ppsf)} "
        f"| {_rate(active.min_ppsf)} / {_rate(active.max_ppsf)} |")
    add("")

    ratio = result.sold_to_ask
    add("### Sold to ask")
    add("")
    add(f"Mean {_pct(ratio.mean_ratio)}, median {_pct(ratio.median_ratio)}, "
        f"{ratio.at_or_above_ask} of {ratio.count} at or above the final ask "
        f"({ratio.at_or_above_105} at 105% or better, {ratio.below_90} below 90%).")
    add("")
    add("The interpretive frame this supports: a list price in this market behaves as bait "
        "rather than a ceiling. Under-priced listings get bid up; over-priced ones take "
        "serial cuts and still close below the reduced ask.")
    add("")

    if result.valuation:
        add("### Subject valuation")
        add("")
        add(f"Subject size {_num(result.valuation.subject_size)}; similar-size bracket "
            f"{_num(result.valuation.bracket_low)} to {_num(result.valuation.bracket_high)} "
            f"({result.valuation.bracket_count} comps).")
        add("")
        add("| Basis | $/sqft | n | Value |")
        add("|---|---|---|---|")
        for basis in result.valuation.bases:
            marker = " **(primary)**" if basis.is_primary else ""
            add(f"| {basis.label}{marker} | {_rate(basis.ppsf)} | {basis.sample_size} "
                f"| {_money(basis.value)} |")
        add("")

    if result.guidance:
        add("### Pricing strategies")
        add("")
        add("| Strategy | List | Expected range |")
        add("|---|---|---|")
        for strategy in result.guidance.strategies:
            span = (
                f"{_money(strategy.expected_low)} to {_money(strategy.expected_high)}"
                if strategy.expected_low
                else "—"
            )
            add(f"| {strategy.label} | {_money(strategy.list_price)} | {span} |")
        add("")
        add(f"Floor / walk-away: {_money(result.guidance.floor)} ({result.guidance.floor_basis}).")
        add("")

    if result.ladder.rows:
        add("### How wide is \"similar\"?")
        add("")
        add(
            "| Size range | Sold | Median $/sqft | Avg $/sqft "
            "| Value at median | Value at average |"
        )
        add("|---|---|---|---|---|---|")
        for rung in result.ladder.rows:
            mark = ""
            if rung.is_profile_bracket:
                mark = " **(the figures above)**"
            elif rung.is_thin:
                mark = " *(too thin to lead on)*"
            add(f"| {rung.label}{mark} | {rung.count} | {_rate(rung.median_ppsf)} "
                f"| {_rate(rung.avg_ppsf)} | {_money(rung.value_from_median)} "
                f"| {_money(rung.value_from_avg)} |")
        add("")
        for note in result.ladder.notes:
            add(note)
            add("")

    if result.size_bands.rows:
        add("### By size")
        add("")
        add("| Band | Sold | Median $/sqft | Avg $/sqft | Median size | Median price |")
        add("|---|---|---|---|---|---|")
        for band in result.size_bands.rows:
            mark = " **(your property)**" if band.holds_subject else ""
            add(f"| {band.label}{mark} | {band.count} | {_rate(band.median_ppsf)} "
                f"| {_rate(band.avg_ppsf)} | {_num(band.median_size)} "
                f"| {_money(band.median_price)} |")
        add("")
        for note in result.size_bands.notes:
            add(note)
            add("")
        add("Bands hold equal numbers of sales rather than equal size ranges, so every "
            "row's median rests on a comparable sample. The size range each band actually "
            "covers is in its label.")
        add("")

    add("### By area")
    add("")
    add("| Area | Sold | Avg $/sqft | Median $/sqft | Median price | Avg size | Active |")
    add("|---|---|---|---|---|---|---|")
    for row in result.area_table.rows:
        add(f"| {row.area} | {row.sold_count} | {_rate(row.sold_avg_ppsf)} "
            f"| {_rate(row.sold_median_ppsf)} | {_money(row.sold_median_price)} "
            f"| {_num(row.sold_avg_size)} | {row.active_count} |")
    add("")
    add("Read the size column alongside the rate: a quadrant can carry higher absolute "
        "prices and a lower average $/sqft purely because its parcels are larger, which "
        "makes a naive rate comparison invert the real premium.")
    add("")

    add("## Data quality")
    add("")
    add(f"- Attributed: {result.agent_analysis.attributed} of {result.agent_analysis.total} sales.")
    if result.excluded:
        add(f"- Excluded {len(result.excluded)}:")
        for record in result.excluded:
            add(f"  - {record.address} — {record.reason}")
    if result.reconciled:
        add(f"- Moved from active to sold: {', '.join(result.reconciled)}")
    if result.missing_metric:
        add(f"- No usable size, kept but absent from $/sqft statistics: "
            f"{', '.join(result.missing_metric)}")
    for note in result.caveats:
        add(f"- {note}")
    for warning in result.warnings:
        add(f"- **Warning:** {warning}")
    if result.diagnostics.rejected:
        add(f"- Verification rejected {len(result.diagnostics.rejected)} extracted fact(s):")
        for item in result.diagnostics.rejected:
            add(f"  - {item}")
    if result.diagnostics.not_found:
        for item in result.diagnostics.not_found:
            add(f"- Not found: {item}")
    add("")

    if result.diagnostics.llm_calls:
        add("## Run cost")
        add("")
        add(f"- LLM calls: {result.diagnostics.llm_calls} "
            f"({result.diagnostics.cache_hits} served from cache)")
        add(f"- Tokens: {result.diagnostics.input_tokens:,} in / "
            f"{result.diagnostics.output_tokens:,} out")
        if result.diagnostics.estimated_cost_usd is not None:
            add(f"- Estimated cost: ${result.diagnostics.estimated_cost_usd:,.2f}")
        add("")

    add("## Reproducing this run")
    add("")
    add("```")
    add(f"recomps run --market {result.market_name} --profile {profile.name} "
        f"--window {profile.window_days}")
    add("```")
    add("")
    add("Add `--offline` to run against the recorded fixtures instead of live research, and "
        "`--compare <snapshot>` to diff against a previous run.")
    return "\n".join(lines) + "\n"


def write(result: RunResult, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(result), encoding="utf-8")
    return target
