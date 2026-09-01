"""The market plugin interface.

A *market* is everything that is specific to one place: where its boundaries
are, which sources describe it, what the user owns there, and (for a private
plugin) any prior-run data. The public engine knows nothing about any real
market; it only knows this interface and ships the synthetic `demoville`
implementation against it.

Two discovery mechanisms, one protocol -- see `lotcomps.plugin.loader`:

  * an installed package advertising a `lotcomps.markets` entry point, and
  * ``--market-path DIR``, a directory holding ``market.toml`` plus fixtures,

so a private market can live in a separate repository cloned side by side and
never needs to be published to be used.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from lotcomps.config.profile import CompProfile


@dataclass(frozen=True)
class Waypoint:
    """A geocoded point on a divider centerline (F11)."""

    name: str
    lon: float
    lat: float


@dataclass
class MarketGeometry:
    """Quadrant geometry for F11.

    The N/S divider is a *piecewise-linear centerline*, not a single latitude:
    real arterials curve, and classifying against a constant latitude misplaces
    parcels at the ends of the market. The E/W divider is a single longitude.

    Street-name directionals are never used to infer E/W. In the reference
    market the E/W naming grid splits at a different street than the E/W
    divider, so "88 E Chandler St" lies *west* of the divider. Any implementation
    that reads the prefix instead of the coordinate will be confidently wrong.
    """

    #: Ordered west-to-east waypoints of the N/S divider street.
    ns_centerline: list[Waypoint] = field(default_factory=list)
    #: Longitude of the E/W divider street.
    ew_longitude: float | None = None
    ns_divider_name: str = ""
    ew_divider_name: str = ""

    def is_configured(self) -> bool:
        return bool(self.ns_centerline) and self.ew_longitude is not None


@dataclass
class SourceSpec:
    """One data source the market draws on (S6).

    The public engine documents the adapter contract; the URLs, selectors and
    per-site quirks belong to the plugin that owns the market.
    """

    name: str
    adapter: str
    role: str = ""
    url: str | None = None
    #: Free-form notes describing behavior a live adapter must handle.
    quirks: list[str] = field(default_factory=list)
    #: Seconds between requests to this host. Politeness is not optional.
    rate_limit_seconds: float = 2.0
    enabled: bool = True
    #: Everything else the plugin declared for this source, passed through
    #: untouched. This is where site-specific detail lives -- the shape of an
    #: embedded data payload, a search template, a skip list -- so the engine
    #: can act on it without containing it.
    config: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Market(Protocol):
    """What a market plugin must provide."""

    #: Stable identifier used as ``--market <name>``.
    name: str
    #: Human-readable description for CLI listings.
    description: str

    def profiles(self) -> dict[str, CompProfile]:
        """Named comp profiles this market ships, keyed by profile name."""
        ...

    def default_profile(self) -> str:
        """Name of the profile used when ``--profile`` is omitted."""
        ...

    def geometry(self) -> MarketGeometry:
        """Quadrant dividers (F11). May be unconfigured; F11 then reports no areas."""
        ...

    def sources(self) -> list[SourceSpec]:
        """Sources for the live research layer (S6)."""
        ...

    def fixture_dir(self) -> str | None:
        """Directory of offline fixtures, or None if the market has none."""
        ...

    def data_dir(self) -> str:
        """Where run snapshots are written (F12). Stays with the plugin."""
        ...

    def metadata(self) -> dict[str, Any]:
        """Anything else the plugin wants recorded in the run snapshot."""
        ...


@dataclass
class BaseMarket:
    """Convenience base implementing `Market` from plain data.

    A plugin can subclass this, or ignore it entirely and satisfy the protocol
    however it likes -- the loader duck-types.
    """

    name: str
    description: str = ""
    _profiles: dict[str, CompProfile] = field(default_factory=dict)
    _default_profile: str = ""
    _geometry: MarketGeometry = field(default_factory=MarketGeometry)
    _sources: list[SourceSpec] = field(default_factory=list)
    _fixture_dir: str | None = None
    _data_dir: str = "data"
    _metadata: dict[str, Any] = field(default_factory=dict)

    def profiles(self) -> dict[str, CompProfile]:
        return self._profiles

    def default_profile(self) -> str:
        return self._default_profile or next(iter(self._profiles), "")

    def geometry(self) -> MarketGeometry:
        return self._geometry

    def sources(self) -> list[SourceSpec]:
        return self._sources

    def fixture_dir(self) -> str | None:
        return self._fixture_dir

    def data_dir(self) -> str:
        return self._data_dir

    def metadata(self) -> dict[str, Any]:
        return self._metadata
