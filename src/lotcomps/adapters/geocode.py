"""Turning addresses into coordinates, and checking the answer.

Quadrant classification is only as good as the geocoding under it, and a
geocoder's confident wrong answer is the most dangerous input this pipeline
takes: it produces a plausible coordinate, in the right town, on the wrong side
of the divider, with no error to notice.

The specific failure this module exists to catch: a geocoder asked for a street
that does not exist in its data will happily match a *similar* street name and
return it with a high score. A real case resolved a "Vista Rd" onto a "St" of a
similar name half a mile away -- close enough to look right, far enough to land
in the wrong quadrant.

So every result is checked: the street name that comes back must match the
street name that went in. A mismatch is rejected and re-queried against the
fallback service, and if that also fails the parcel is reported as not geocoded
rather than placed somewhere plausible.

Two services, both public and neither a listing site:

* **US Census** -- street-interpolated to roughly 30 m, which is far inside what
  quadrant assignment needs. Free, no key, no robots restrictions.
* **ArcGIS World Geocoder** -- the fallback, and the one that handles street
  *intersections*, which is how a divider centerline's waypoints are obtained.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import urlencode

from lotcomps.adapters.fetch import PoliteFetcher
from lotcomps.model.address import normalize_address

CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
ARCGIS_URL = (
    "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/"
    "findAddressCandidates"
)

#: Street types are written inconsistently across sources, so they are stripped
#: before comparing. What must match is the *name*, not whether it was written
#: Road or Rd.
_STREET_TYPES = {
    "st", "ave", "rd", "dr", "blvd", "ln", "ct", "pl", "ter", "way", "cir",
    "trl", "canyon", "pkwy", "hwy", "sq", "loop", "row", "walk", "path",
}
_DIRECTIONALS = {"n", "s", "e", "w", "ne", "nw", "se", "sw"}


@dataclass
class GeocodeResult:
    query: str
    lat: float | None = None
    lon: float | None = None
    matched_address: str = ""
    source: str = ""
    score: float | None = None
    rejected: str = ""

    @property
    def ok(self) -> bool:
        return self.lat is not None and self.lon is not None and not self.rejected


def street_tokens(address: str) -> set[str]:
    """The distinctive words of a street name, minus number, type and direction."""
    tokens = normalize_address(address).split()
    if tokens and (tokens[0].isdigit() or tokens[0] == "0"):
        tokens = tokens[1:]
    return {
        t for t in tokens
        if t not in _STREET_TYPES and t not in _DIRECTIONALS and not t.startswith("#")
    }


def street_matches(query: str, matched: str) -> bool:
    """Whether a geocoder answered about the street it was asked about.

    Requires the query's distinctive words to appear in the answer. "Larkspur
    Vista Rd" against a returned "Larkspur St" fails, because *Vista* is
    missing -- which is exactly the near-miss that puts a parcel in the wrong
    quadrant while looking correct.
    """
    wanted, got = street_tokens(query), street_tokens(matched)
    if not wanted:
        return True
    return wanted <= got


class CensusGeocoder:
    """The primary. Street-interpolated, free, and accurate enough."""

    name = "us-census"

    def __init__(self, fetcher: PoliteFetcher, benchmark: str = "Public_AR_Current") -> None:
        self.fetcher = fetcher
        self.benchmark = benchmark

    def locate(self, address: str) -> GeocodeResult:
        query = urlencode(
            {"address": address, "benchmark": self.benchmark, "format": "json"}
        )
        response = self.fetcher.fetch(f"{CENSUS_URL}?{query}", adapter_version="census-1")
        if not response.ok:
            return GeocodeResult(query=address, source=self.name, rejected=response.describe())
        try:
            matches = json.loads(response.text)["result"]["addressMatches"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            return GeocodeResult(
                query=address, source=self.name, rejected=f"unreadable response: {exc}"
            )
        if not matches:
            return GeocodeResult(query=address, source=self.name, rejected="no match")

        best = matches[0]
        coordinates = best.get("coordinates") or {}
        matched = best.get("matchedAddress", "")
        result = GeocodeResult(
            query=address,
            lat=coordinates.get("y"),
            lon=coordinates.get("x"),
            matched_address=matched,
            source=self.name,
        )
        if not street_matches(address, matched):
            result.rejected = f"matched a different street: {matched!r}"
        return result


class ArcGISGeocoder:
    """The fallback, and the one that understands intersections."""

    name = "arcgis"

    def __init__(self, fetcher: PoliteFetcher, min_score: float = 85.0) -> None:
        self.fetcher = fetcher
        self.min_score = min_score

    def locate(self, address: str) -> GeocodeResult:
        query = urlencode(
            {
                "SingleLine": address,
                "f": "json",
                "outFields": "Match_addr,Addr_type",
                "maxLocations": 1,
            }
        )
        response = self.fetcher.fetch(f"{ARCGIS_URL}?{query}", adapter_version="arcgis-1")
        if not response.ok:
            return GeocodeResult(query=address, source=self.name, rejected=response.describe())
        try:
            candidates = json.loads(response.text).get("candidates") or []
        except (json.JSONDecodeError, TypeError) as exc:
            return GeocodeResult(
                query=address, source=self.name, rejected=f"unreadable response: {exc}"
            )
        if not candidates:
            return GeocodeResult(query=address, source=self.name, rejected="no match")

        best = candidates[0]
        location = best.get("location") or {}
        matched = best.get("address", "")
        score = best.get("score")
        result = GeocodeResult(
            query=address,
            lat=location.get("y"),
            lon=location.get("x"),
            matched_address=matched,
            source=self.name,
            score=score,
        )
        if score is not None and score < self.min_score:
            result.rejected = f"low confidence ({score:.0f})"
        elif not street_matches(address, matched):
            result.rejected = f"matched a different street: {matched!r}"
        return result

    def locate_intersection(self, street_a: str, street_b: str, city: str) -> GeocodeResult:
        """Where two streets cross -- how divider centerline waypoints are found."""
        return self.locate(f"{street_a} & {street_b}, {city}")


class VerifyingGeocoder:
    """Primary, then fallback, then an honest failure."""

    def __init__(self, primary: CensusGeocoder, fallback: ArcGISGeocoder | None = None) -> None:
        self.primary = primary
        self.fallback = fallback
        self.rejections: list[str] = []

    def locate(self, address: str) -> GeocodeResult:
        result = self.primary.locate(address)
        if result.ok:
            return result
        self.rejections.append(f"{self.primary.name}: {address} -- {result.rejected}")
        if self.fallback is None:
            return result
        second = self.fallback.locate(address)
        if second.ok:
            return second
        self.rejections.append(f"{self.fallback.name}: {address} -- {second.rejected}")
        # Return the first attempt so the caller sees the primary's reason.
        return result

    def locate_all(self, addresses: list[str]) -> dict[str, GeocodeResult]:
        return {address: self.locate(address) for address in addresses}
