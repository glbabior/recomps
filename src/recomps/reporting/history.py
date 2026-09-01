"""Saved runs, grouped by the profile that produced them.

A search profile is a question ("what are 5,000-8,000 sqft lots worth around
here?"). Every time you ask it you get an answer, and the answers are only
interesting next to each other -- did the market move, or did the method?

So each run is archived under the profile that produced it:

    data/runs/<market>__<profile>/<timestamp>/
        snapshot.json     every row and every statistic
        comps.xlsx        the workbook, as it was that day
        methodology.md    what the run actually did

Keeping the workbook, not just the data, is deliberate. A saved workbook is
something you can reopen a month later and hand to somebody; a saved data file
is something only this tool can read.

The directory name carries the market and profile so listing run history never
has to open and parse a snapshot. Two underscores separate them because both
names are allowed to contain hyphens.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

SEPARATOR = "__"
RUNS_DIRNAME = "runs"
SNAPSHOT_NAME = "snapshot.json"
WORKBOOK_NAME = "comps.xlsx"
METHODOLOGY_NAME = "methodology.md"
STAMP_FORMAT = "%Y%m%dT%H%M%SZ"

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_name(value: str) -> str:
    """Make a market or profile name usable as a directory name."""
    cleaned = _UNSAFE.sub("-", value.strip()).strip("-.")
    return cleaned or "unnamed"


def runs_root(data_dir: str | Path) -> Path:
    return Path(data_dir) / RUNS_DIRNAME


def profile_dir(data_dir: str | Path, market: str, profile: str) -> Path:
    return runs_root(data_dir) / f"{safe_name(market)}{SEPARATOR}{safe_name(profile)}"


def run_dir(data_dir: str | Path, market: str, profile: str, run_at: datetime) -> Path:
    return profile_dir(data_dir, market, profile) / run_at.strftime(STAMP_FORMAT)


@dataclass
class RunRecord:
    """One archived run."""

    market: str
    profile: str
    run_at: datetime
    directory: Path

    @property
    def snapshot(self) -> Path | None:
        path = self.directory / SNAPSHOT_NAME
        return path if path.is_file() else None

    @property
    def workbook(self) -> Path | None:
        path = self.directory / WORKBOOK_NAME
        return path if path.is_file() else None

    @property
    def methodology(self) -> Path | None:
        path = self.directory / METHODOLOGY_NAME
        return path if path.is_file() else None

    @property
    def label(self) -> str:
        return self.run_at.strftime("%Y-%m-%d %H:%M")


def _parse(directory: Path) -> RunRecord | None:
    parent = directory.parent.name
    if SEPARATOR not in parent:
        return None
    market, profile = parent.split(SEPARATOR, 1)
    try:
        # The stamp ends in Z, so it is UTC. Parsing it naive and comparing it
        # against an aware run timestamp raises, which is exactly what
        # --compare last used to do.
        run_at = datetime.strptime(directory.name, STAMP_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None
    return RunRecord(market=market, profile=profile, run_at=run_at, directory=directory)


def list_runs(
    data_dir: str | Path, market: str | None = None, profile: str | None = None
) -> list[RunRecord]:
    """Archived runs, newest first, optionally narrowed to one profile."""
    root = runs_root(data_dir)
    if not root.is_dir():
        return []
    records: list[RunRecord] = []
    for parent in sorted(root.iterdir()):
        if not parent.is_dir():
            continue
        for directory in parent.iterdir():
            if not directory.is_dir():
                continue
            record = _parse(directory)
            if record is None:
                continue
            if market and record.market != safe_name(market):
                continue
            if profile and record.profile != safe_name(profile):
                continue
            records.append(record)
    return sorted(records, key=lambda r: r.run_at, reverse=True)


def latest_run(
    data_dir: str | Path,
    market: str,
    profile: str,
    before: datetime | None = None,
) -> RunRecord | None:
    """The most recent archived run of this profile.

    `before` excludes the run currently being written, so `--compare last`
    means "the one before this", not "this one".
    """
    for record in list_runs(data_dir, market, profile):
        if record.snapshot is None:
            continue
        if before is not None:
            cutoff = before
            if (cutoff.tzinfo is None) != (record.run_at.tzinfo is None):
                cutoff = cutoff.replace(tzinfo=record.run_at.tzinfo)
            if record.run_at >= cutoff:
                continue
        return record
    return None


def profiles_with_runs(data_dir: str | Path) -> dict[tuple[str, str], int]:
    """(market, profile) -> number of archived runs."""
    counts: dict[tuple[str, str], int] = {}
    for record in list_runs(data_dir):
        key = (record.market, record.profile)
        counts[key] = counts.get(key, 0) + 1
    return counts
