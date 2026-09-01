"""Market discovery: entry points and ``--market-path`` directories.

Both routes resolve to the same `Market` protocol. The directory route exists so
a private market plugin can be used straight from a sibling clone with nothing
installed, which is the normal case for a market whose data must not be
published.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

from lotcomps.config.profile import CompProfile
from lotcomps.plugin.market import BaseMarket, Market, MarketGeometry, SourceSpec, Waypoint

ENTRY_POINT_GROUP = "lotcomps.markets"
MARKET_FILE = "market.toml"


class MarketNotFound(LookupError):
    pass


class MarketInvalid(ValueError):
    pass


@dataclass(frozen=True)
class DiscoveredMarket:
    name: str
    description: str
    origin: str  # "entry-point" or a filesystem path


def _iter_entry_points():
    try:
        return list(entry_points(group=ENTRY_POINT_GROUP))
    except TypeError:  # pragma: no cover - very old importlib.metadata
        return list(entry_points().get(ENTRY_POINT_GROUP, []))


def discover() -> list[DiscoveredMarket]:
    """List markets advertised via entry points."""
    found: list[DiscoveredMarket] = []
    for ep in _iter_entry_points():
        try:
            market = ep.load()
        except Exception as exc:  # a broken plugin must not break the CLI
            found.append(DiscoveredMarket(ep.name, f"<failed to load: {exc}>", "entry-point"))
            continue
        found.append(
            DiscoveredMarket(ep.name, getattr(market, "description", ""), "entry-point")
        )
    return sorted(found, key=lambda m: m.name)


def load_market(name: str | None = None, path: str | Path | None = None) -> Market:
    """Load a market by entry-point name or from a directory."""
    if path is not None:
        return load_market_from_path(path)
    if not name:
        raise MarketNotFound("no market specified; pass --market NAME or --market-path DIR")
    for ep in _iter_entry_points():
        if ep.name == name:
            market = ep.load()
            _check(market, f"entry point {name!r}")
            return market
    known = ", ".join(m.name for m in discover()) or "none installed"
    raise MarketNotFound(f"unknown market {name!r}; available: {known}")


def load_market_from_path(path: str | Path) -> Market:
    """Build a Market from a directory containing ``market.toml``."""
    directory = Path(path).expanduser().resolve()
    config = directory / MARKET_FILE
    if not config.is_file():
        raise MarketNotFound(f"{directory} has no {MARKET_FILE}")
    with config.open("rb") as fh:
        raw = tomllib.load(fh)
    market = _market_from_toml(raw, directory)
    _check(market, str(config))
    return market


def _market_from_toml(raw: dict[str, Any], directory: Path) -> Market:
    section = raw.get("market") or {}
    name = section.get("name")
    if not name:
        raise MarketInvalid(f"{directory / MARKET_FILE}: [market] needs a name")

    profiles: dict[str, CompProfile] = {}
    for pname, pdata in (raw.get("profiles") or {}).items():
        data = dict(pdata)
        data.setdefault("name", pname)
        profiles[pname] = CompProfile.from_dict(data)

    geom_raw = raw.get("geometry") or {}
    geometry = MarketGeometry(
        ns_centerline=[
            Waypoint(name=w.get("name", ""), lon=float(w["lon"]), lat=float(w["lat"]))
            for w in (geom_raw.get("ns_centerline") or [])
        ],
        ew_longitude=(
            float(geom_raw["ew_longitude"]) if geom_raw.get("ew_longitude") is not None else None
        ),
        ns_divider_name=geom_raw.get("ns_divider_name", ""),
        ew_divider_name=geom_raw.get("ew_divider_name", ""),
    )

    sources = [
        SourceSpec(
            name=s.get("name", ""),
            adapter=s.get("adapter", ""),
            role=s.get("role", ""),
            url=s.get("url"),
            quirks=list(s.get("quirks") or []),
            rate_limit_seconds=float(s.get("rate_limit_seconds", 2.0)),
            enabled=bool(s.get("enabled", True)),
        )
        for s in (raw.get("sources") or [])
    ]

    def _resolve(key: str, default: str) -> str:
        return str((directory / section.get(key, default)).resolve())

    fixture_dir = _resolve("fixture_dir", "fixtures")
    return BaseMarket(
        name=name,
        description=section.get("description", ""),
        _profiles=profiles,
        _default_profile=section.get("default_profile", ""),
        _geometry=geometry,
        _sources=sources,
        _fixture_dir=fixture_dir if Path(fixture_dir).is_dir() else None,
        _data_dir=_resolve("data_dir", "data"),
        _metadata=dict(raw.get("metadata") or {}),
    )


_REQUIRED = ("profiles", "default_profile", "geometry", "sources", "fixture_dir", "data_dir")


def _check(market: Any, origin: str) -> None:
    missing = [m for m in _REQUIRED if not callable(getattr(market, m, None))]
    if missing:
        raise MarketInvalid(f"{origin} is not a valid Market; missing: {', '.join(missing)}")
    if not getattr(market, "name", ""):
        raise MarketInvalid(f"{origin} is not a valid Market; missing: name")
