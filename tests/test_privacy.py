"""Guards against real data reaching the public repository.

The obvious implementation -- grep for the real addresses and names -- cannot
work here, because the denylist would itself be the leak. So the public checks
are *structural*: everything shipped must look synthetic.

  * every coordinate must sit inside Demoville's declared synthetic box,
  * no shipped file may carry a real-looking postal address,
  * no market-specific site URL may appear in the engine.

A private market plugin runs the complementary check with its own denylist,
which stays private. See `docs/plugin-authoring.md`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "recomps"

#: Demoville sits in empty farmland well away from any real market this project
#: has touched. Any coordinate outside this box is a leak, not a fixture.
SYNTHETIC_BOX = {"lon": (-100.10, -99.90), "lat": (39.95, 40.10)}

SCANNED_SUFFIXES = {".py", ".json", ".toml", ".md", ".cfg", ".yml", ".yaml"}

#: Portals whose data and scraping quirks belong in private plugins (S10).
LISTING_PORTALS = (
    "zillow.com", "redfin.com", "trulia.com", "movoto.com", "homes.com",
    "compass.com", "landsearch.com", "realtor.com", "crmls",
)


def _shipped_files() -> list[Path]:
    return [
        p for p in SRC.rglob("*")
        if p.is_file() and p.suffix in SCANNED_SUFFIXES and "__pycache__" not in p.parts
    ]


def _fixture_files() -> list[Path]:
    """Only the declared fixture sets. Run output is not source and never ships."""
    return sorted(p for p in (SRC / "markets").rglob("fixtures/*.json"))


def test_there_are_fixtures_to_check():
    assert _fixture_files(), "no fixtures found; this test would pass vacuously"


def test_no_run_output_is_committed():
    """Snapshots hold every row of a run and belong in the plugin's data dir."""
    strays = [
        p.relative_to(ROOT) for p in SRC.rglob("*.json")
        if "fixtures" not in p.parts and p.name.endswith(".snapshot.json")
    ]
    assert not strays, f"run snapshots found inside the package: {strays}"


@pytest.mark.parametrize("path", _fixture_files(), ids=lambda p: p.name)
def test_fixture_coordinates_are_inside_the_synthetic_box(path):
    rows = json.loads(path.read_text(encoding="utf-8"))
    for row in rows:
        for axis, (low, high) in SYNTHETIC_BOX.items():
            value = row.get(axis)
            if value is None:
                continue
            assert low <= value <= high, (
                f"{path.name}: {row.get('address')} has {axis}={value}, outside the "
                "synthetic box. Real coordinates must never ship in the public repo."
            )


@pytest.mark.parametrize("path", _fixture_files(), ids=lambda p: p.name)
def test_fixtures_carry_no_city_state_zip(path):
    """A real address is usually betrayed by its tail, not its street."""
    text = path.read_text(encoding="utf-8")
    matches = re.findall(r"\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b", text)
    assert not matches, f"{path.name} contains a postal tail: {matches[:3]}"


@pytest.mark.parametrize("path", _fixture_files(), ids=lambda p: p.name)
def test_fixture_sources_are_marked_synthetic(path):
    rows = json.loads(path.read_text(encoding="utf-8"))
    for row in rows:
        for source in row.get("sources", []):
            assert source.startswith("fixture://"), (
                f"{path.name}: {row.get('address')} cites {source!r}; public fixtures may "
                "only cite fixture:// sources."
            )


def test_engine_names_no_listing_portal():
    """Site-specific behaviour belongs in a private plugin, not the engine (S10)."""
    offenders = []
    for path in _shipped_files():
        lowered = path.read_text(encoding="utf-8", errors="ignore").lower()
        for portal in LISTING_PORTALS:
            if portal in lowered:
                offenders.append(f"{path.relative_to(ROOT)} -> {portal}")
    assert not offenders, "\n".join(offenders)


def test_bundled_market_declares_itself_synthetic():
    from recomps.markets.demoville import MARKET

    assert MARKET.metadata().get("synthetic") is True
    for profile in MARKET.profiles().values():
        assert not profile.exclusions.explicit_address_keys, (
            "an explicit exclusion list names real parcels; it belongs in a private plugin"
        )
