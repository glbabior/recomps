"""The research layer boundary.

Everything above this line is deterministic arithmetic. Everything below it is
web research: fetching pages, reading them with an LLM, and reconciling what
comes back. Keeping that boundary sharp is what lets the entire analysis be
tested without a network or an API key -- `FixtureResearcher` and
`LiveResearcher` are interchangeable, and every test in the suite runs against
the former.

The layer returns a `Dataset` plus `Diagnostics`. Diagnostics are not an
afterthought: a run that quietly found 12 comps instead of 46 has failed, and
the only thing standing between the user and a confident wrong answer is the
report of what was searched, what was rejected, and what could not be found.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Protocol, runtime_checkable

from lotcomps.config.profile import CompProfile
from lotcomps.model.comp import ActiveListing, SoldComp
from lotcomps.plugin.market import Market


@dataclass
class Diagnostics:
    """What the research layer did, so a thin run can be recognized as thin."""

    sources_used: list[str] = field(default_factory=list)
    sources_failed: list[str] = field(default_factory=list)
    #: Facts the verification pass rejected (S7): a price or date that did not
    #: match the known sale, a geocode that resolved to the wrong street.
    rejected: list[str] = field(default_factory=list)
    #: Items the layer looked for and honestly could not find.
    not_found: list[str] = field(default_factory=list)
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float | None = None
    cache_hits: int = 0

    def to_dict(self) -> dict:
        return {
            "sources_used": self.sources_used,
            "sources_failed": self.sources_failed,
            "rejected": self.rejected,
            "not_found": self.not_found,
            "llm_calls": self.llm_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "estimated_cost_usd": self.estimated_cost_usd,
            "cache_hits": self.cache_hits,
        }


@dataclass
class Dataset:
    sold: list[SoldComp] = field(default_factory=list)
    active: list[ActiveListing] = field(default_factory=list)
    diagnostics: Diagnostics = field(default_factory=Diagnostics)


@runtime_checkable
class Researcher(Protocol):
    """Collects the raw dataset for a market and profile."""

    name: str

    def gather(
        self,
        market: Market,
        profile: CompProfile,
        window_start: date,
        window_end: date,
    ) -> Dataset:
        """Return sold comps and active listings for the window.

        Implementations are responsible for F1, F2, F7, F8 and the geocoding
        half of F11. Everything they return is treated as untrusted until the
        deterministic layer has filtered it.
        """
        ...
