"""Offline, deterministic research from recorded fixtures.

Fixtures are JSON files in a market's fixture directory: ``sold.json`` and
``active.json``, each a list of comp records. This is what ``--offline`` runs
against, what CI runs against, and what makes the golden-number regression
possible at all.

A fixture file is data, not code, so a private market can ship a real recorded
dataset while the public engine ships only a synthetic one.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from recomps.config.profile import CompProfile
from recomps.model.comp import ActiveListing, Quadrant, SoldComp
from recomps.plugin.market import Market
from recomps.research.base import Dataset, Diagnostics


class FixtureMissing(FileNotFoundError):
    pass


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def _common(raw: dict[str, Any]) -> dict[str, Any]:
    area = raw.get("area")
    return {
        "address": raw["address"],
        "lot_sqft": raw.get("lot_sqft"),
        "living_sqft": raw.get("living_sqft"),
        "beds": raw.get("beds"),
        "baths": raw.get("baths"),
        "year_built": raw.get("year_built"),
        "condition": raw.get("condition"),
        "brokerage": raw.get("brokerage"),
        "agent": raw.get("agent"),
        "area": Quadrant(area) if area else None,
        "lat": raw.get("lat"),
        "lon": raw.get("lon"),
        "lot_size_is_rounded": bool(raw.get("lot_size_is_rounded", False)),
        "sources": list(raw.get("sources") or []),
        "notes": list(raw.get("notes") or []),
    }


def sold_from_rows(raw_rows: list[dict[str, Any]]) -> list[SoldComp]:
    """Build sold comps from already-parsed rows.

    Shared with the snapshot reader, so a recorded fixture and an archived run
    are read by exactly the same code and cannot drift apart.
    """
    return [
        SoldComp(
            **_common(r),
            sold_date=_parse_date(r.get("sold_date")),
            sold_price=r.get("sold_price"),
            final_list_price=r.get("final_list_price"),
            original_list_price=r.get("original_list_price"),
            buyer_agent=r.get("buyer_agent"),
        )
        for r in raw_rows
    ]


def active_from_rows(raw_rows: list[dict[str, Any]]) -> list[ActiveListing]:
    return [
        ActiveListing(
            **_common(r),
            list_price=r.get("list_price"),
            listed_date=_parse_date(r.get("listed_date")),
        )
        for r in raw_rows
    ]


def load_sold(path: Path) -> list[SoldComp]:
    return sold_from_rows(json.loads(path.read_text(encoding="utf-8")))


def load_active(path: Path) -> list[ActiveListing]:
    return active_from_rows(json.loads(path.read_text(encoding="utf-8")))


class FixtureResearcher:
    """Reads a recorded dataset. Never touches the network."""

    name = "fixture"

    def __init__(self, fixture_dir: str | Path | None = None) -> None:
        self._override = Path(fixture_dir) if fixture_dir else None

    def gather(
        self,
        market: Market,
        profile: CompProfile,
        window_start: date,
        window_end: date,
    ) -> Dataset:
        directory = self._override or (
            Path(market.fixture_dir()) if market.fixture_dir() else None
        )
        if directory is None or not directory.is_dir():
            raise FixtureMissing(
                f"market {market.name!r} has no fixture directory; offline runs need one"
            )
        sold_path = directory / f"{profile.name}.sold.json"
        active_path = directory / f"{profile.name}.active.json"
        if not sold_path.exists():
            sold_path, active_path = directory / "sold.json", directory / "active.json"
        if not sold_path.exists():
            raise FixtureMissing(f"no sold fixtures for profile {profile.name!r} in {directory}")

        sold = load_sold(sold_path)
        active = load_active(active_path) if active_path.exists() else []

        # Fixtures may hold more history than the requested window (F12 compare
        # runs re-read the same file), so the window is enforced here rather
        # than assumed to have been baked in.
        in_window = [
            c
            for c in sold
            if c.sold_date is None or window_start <= c.sold_date <= window_end
        ]
        dropped = len(sold) - len(in_window)

        diagnostics = Diagnostics(sources_used=[f"fixtures:{directory.name}"])
        if dropped:
            # A recording covers the days it covers; the window slides with the
            # run date. Replay a March dataset in September and most of it
            # silently disappears, leaving a smaller market that reads exactly
            # like a real one -- and every figure, including the agent tables,
            # quietly describes a different set of sales.
            recorded = sorted(c.sold_date for c in sold if c.sold_date)
            if recorded:
                where = (
                    f"The recording holds sales from {recorded[0]} to "
                    f"{recorded[-1]} -- {recorded[-1]} is the last sale in it, "
                    "not a setting. Dating a run to that day, or shortly after, "
                    "covers the whole recording."
                )
            else:
                where = ""
            diagnostics.not_found.append(
                f"This run replayed recorded data and asked for sales from "
                f"{window_start} to {window_end}, so {dropped} of its {len(sold)} "
                f"recorded sales are outside the window and in no figure. {where}"
            )

        return Dataset(sold=in_window, active=active, diagnostics=diagnostics)
