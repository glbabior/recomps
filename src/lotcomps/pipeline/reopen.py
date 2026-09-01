"""Reopening a saved run.

An archived run holds every row it collected, so it can be opened again later
and re-analyzed without going back to any website. That matters for three
things the interface needs:

  * **Looking at what a past run found**, rather than only the summary of it.
  * **Regenerating its workbook**, months later, from the run's own data.
  * **Asking a different question of the same data** -- change the subject
    size, widen what counts as comparable, drop a sale you have decided is not
    a real comp -- and see the answer move, without re-fetching anything.

The implementation deliberately does *not* deserialize the computed results.
It restores the collected rows and then runs the ordinary analysis over them
again. Restoring the computed figures would let a saved run and a fresh one
disagree about what the same numbers mean; recomputing cannot.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from lotcomps.config.profile import CompProfile
from lotcomps.model.snapshot import Snapshot
from lotcomps.pipeline.run import RunResult, analyze
from lotcomps.plugin.market import Market
from lotcomps.research.base import Dataset, Diagnostics
from lotcomps.research.fixture import active_from_rows, sold_from_rows


class SnapshotUnreadable(ValueError):
    pass


def _run_block(snapshot: Snapshot) -> dict[str, Any]:
    block = snapshot.payload.get("run")
    if not block:
        raise SnapshotUnreadable("snapshot has no run metadata")
    return block


def profile_from_snapshot(snapshot: Snapshot) -> CompProfile:
    """The profile as it was when the run happened.

    A profile can be edited after a run. Reopening uses the settings that
    actually produced the numbers, not today's version of the profile -- so a
    past run keeps meaning what it meant.
    """
    raw = _run_block(snapshot).get("profile")
    if not raw:
        raise SnapshotUnreadable("snapshot records no profile")
    return CompProfile.from_dict(raw)


def dataset_from_snapshot(snapshot: Snapshot) -> Dataset:
    """The rows the run collected, as a dataset the pipeline can re-analyze."""
    diagnostics_raw = snapshot.payload.get("diagnostics") or {}
    diagnostics = Diagnostics(
        sources_used=list(diagnostics_raw.get("sources_used") or []),
        sources_failed=list(diagnostics_raw.get("sources_failed") or []),
        rejected=list(diagnostics_raw.get("rejected") or []),
        not_found=list(diagnostics_raw.get("not_found") or []),
        llm_calls=int(diagnostics_raw.get("llm_calls") or 0),
        input_tokens=int(diagnostics_raw.get("input_tokens") or 0),
        output_tokens=int(diagnostics_raw.get("output_tokens") or 0),
        estimated_cost_usd=diagnostics_raw.get("estimated_cost_usd"),
        cache_hits=int(diagnostics_raw.get("cache_hits") or 0),
    )
    return Dataset(
        sold=sold_from_rows(snapshot.payload.get("sold") or []),
        active=active_from_rows(snapshot.payload.get("active") or []),
        diagnostics=diagnostics,
    )


def window_from_snapshot(snapshot: Snapshot) -> tuple[date, date]:
    block = _run_block(snapshot)
    try:
        return (
            date.fromisoformat(block["window_start"]),
            date.fromisoformat(block["window_end"]),
        )
    except (KeyError, ValueError) as exc:
        raise SnapshotUnreadable(f"snapshot has no usable window: {exc}") from exc


def reopen(
    snapshot: Snapshot,
    market: Market,
    *,
    profile: CompProfile | None = None,
    exclude_addresses: list[str] | None = None,
) -> RunResult:
    """Re-analyze a saved run.

    `profile` substitutes different settings for the ones the run used, which
    is how "what if my lot were 7,000 sqft" is answered without re-fetching.
    `exclude_addresses` drops sales the user has judged not comparable.
    """
    block = _run_block(snapshot)
    dataset = dataset_from_snapshot(snapshot)
    active_profile = profile or profile_from_snapshot(snapshot)
    window_start, window_end = window_from_snapshot(snapshot)

    if exclude_addresses:
        from lotcomps.model.address import AddressKey

        drop = {AddressKey.of(a) for a in exclude_addresses}
        dataset.sold = [c for c in dataset.sold if c.key not in drop]
        dataset.active = [c for c in dataset.active if c.key not in drop]

    try:
        run_at = datetime.fromisoformat(block["run_at"])
    except (KeyError, ValueError):
        run_at = datetime.now().astimezone()

    result = analyze(
        dataset,
        market,
        active_profile,
        window_start,
        window_end,
        run_at=run_at,
        researcher_name=block.get("researcher", ""),
    )
    if profile is not None or exclude_addresses:
        result.caveats.append(
            "Reopened from a saved run and re-analyzed with changed settings. The "
            "underlying sales are unchanged and are as of the original run date."
        )
    return result


def reopen_path(
    path: str,
    market: Market,
    *,
    profile: CompProfile | None = None,
    exclude_addresses: list[str] | None = None,
) -> RunResult:
    return reopen(
        Snapshot.load(path),
        market,
        profile=profile,
        exclude_addresses=exclude_addresses,
    )
