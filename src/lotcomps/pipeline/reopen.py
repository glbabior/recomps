"""Reopening a saved run.

An archived run holds every row it collected, so it can be opened again later
and re-analyzed without going back to any website. That matters for three
things the interface needs:

  * **Looking at what a past run found**, rather than only the summary of it.
  * **Regenerating its workbook**, months later, from the run's own data.
  * **Asking a different question of the same data** -- change the subject
    size, widen what counts as comparable, drop a sale you have decided is not
    a real comp -- and see the answer move, without re-fetching anything.

Two ways in, and the difference matters:

`read_stored()` returns the figures the run itself reported, read straight back
off disk with nothing recalculated. That is what viewing a past run should use.

`reopen()` restores the collected rows and runs the ordinary analysis over them
again, which is what you want when the *question* changes -- a different subject
size, a comp dropped. It uses the profile as it was at the time of the run, so a
profile edited since cannot retroactively change what a past run meant.

Neither touches the network, and both read every row the run collected, so no
comparable sale can go missing from a saved run. A listing vanishing from a
website afterwards is irrelevant: the row was captured, and the archive also
keeps that day's workbook as a plain file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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


@dataclass
class StoredRun:
    """A past run's figures exactly as they were saved. Nothing is recomputed.

    This is what "show me what I got last time" should use. `reopen()` below
    re-derives the figures from the saved rows, which is what you want when the
    question changes; this returns what the run actually reported, which is what
    you want when it does not.

    Both read the same file and neither touches the network, so a saved comp
    cannot disappear either way. The distinction is only about which answer is
    authoritative: the one the run gave, or the one today's code would give.
    """

    market: str
    profile_name: str
    run_at: str
    window_start: str
    window_end: str
    researcher: str
    sold: list[dict[str, Any]] = field(default_factory=list)
    active: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    valuation: dict[str, Any] = field(default_factory=dict)
    guidance: dict[str, Any] = field(default_factory=dict)
    areas: dict[str, Any] = field(default_factory=dict)
    agents: dict[str, Any] = field(default_factory=dict)
    excluded: list[dict[str, Any]] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def primary_value(self) -> float | None:
        for basis in (self.valuation or {}).get("bases", []):
            if basis.get("is_primary"):
                return basis.get("value")
        return None

    @property
    def sold_count(self) -> int:
        return int((self.stats.get("sold") or {}).get("count") or 0)

    @property
    def active_count(self) -> int:
        return int((self.stats.get("active") or {}).get("count") or 0)


def read_stored(snapshot: Snapshot) -> StoredRun:
    """Read a saved run's own figures back, without recomputing anything."""
    block = _run_block(snapshot)
    payload = snapshot.payload
    return StoredRun(
        market=block.get("market", ""),
        profile_name=(block.get("profile") or {}).get("name", ""),
        run_at=block.get("run_at", ""),
        window_start=block.get("window_start", ""),
        window_end=block.get("window_end", ""),
        researcher=block.get("researcher", ""),
        sold=list(payload.get("sold") or []),
        active=list(payload.get("active") or []),
        stats=dict(payload.get("stats") or {}),
        valuation=dict(payload.get("valuation") or {}),
        guidance=dict(payload.get("guidance") or {}),
        areas=dict(payload.get("areas") or {}),
        agents=dict(payload.get("agents") or {}),
        excluded=list(payload.get("excluded") or []),
        caveats=list(payload.get("caveats") or []),
        warnings=list(payload.get("warnings") or []),
    )


def read_stored_path(path: str) -> StoredRun:
    return read_stored(Snapshot.load(path))


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
