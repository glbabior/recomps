"""Agent and brokerage analysis (F10).

What this produces is a shortlist of people to *interview*, not a ranking. The
caveats below are part of the output, not decoration around it:

* Samples run one to three sales per agent. That is not a performance measure.
* A high sold-to-ask ratio partly reflects a deliberately low list price, so
  volume and ratio together are a signal and not an endorsement. The rate an
  agent actually achieved is reported beside the ratio for exactly that reason:
  beating a low ask is not the same as getting a good price.
* That rate carries the same size confound as everywhere else in this project,
  so the median size it was achieved on sits next to it. An agent working small
  parcels shows a higher $/sqft without being a better agent.
* The question to judge a candidate on is projected sale price against the
  comps -- which is why the recommended interview test is to ask each candidate
  for both a list price and an expected close *before* showing them your
  numbers.

Brokerages are grouped into families because the same firm appears under many
legal names across sources ("Berkshire Hathaway HomeServices", "BHHS Golden
Properties"). Grouping is by glob pattern so a market plugin can add local
franchises without touching this module.

Per the project's privacy rules the *pattern* checks live here and are generic;
any named shortlist or caution judgement belongs to a private market plugin.
"""

from __future__ import annotations

import fnmatch
import statistics
from collections import defaultdict
from dataclasses import dataclass, field

from recomps.model.comp import SoldComp

#: Glob patterns collapsing a brokerage's trading names into one family.
DEFAULT_BROKERAGE_FAMILIES: dict[str, list[str]] = {
    "Berkshire Hathaway HomeServices": ["Berkshire*", "BHHS*"],
    "Keller Williams": ["Keller Williams*", "KW *", "KW*"],
    "eXp Realty": ["eXp*", "EXP *"],
    "Sotheby's International Realty": ["*Sotheby*"],
    "Coldwell Banker": ["Coldwell Banker*"],
    "Compass": ["Compass*"],
    "RE/MAX": ["RE/MAX*", "REMAX*"],
    "The Agency": ["The Agency*"],
}

#: An agent needs at least this many closings before the shortlist rule applies.
SHORTLIST_MIN_CLOSINGS = 2
#: ...and a mean sold-to-ask at or above this.
SHORTLIST_MIN_RATIO = 1.00
#: The caution pattern: listings cut from their original ask that still closed
#: below the reduced ask.
CAUTION_MAX_RATIO = 0.95

CAVEATS = [
    "Sample sizes are 1-3 sales per agent. Volume is a signal, not an endorsement.",
    "A high sold-to-ask ratio can reflect a deliberately low list price as much as skill.",
    "Judge candidates on projected sold price against the comps, not on sold-vs-ask alone.",
    (
        "Read an agent's $/sqft beside the size it was achieved on: a rate looks better "
        "on smaller parcels, which is the same size premium the valuation corrects for."
    ),
    (
        "Interview test: ask each candidate for a list price AND an expected close "
        "before showing them your numbers."
    ),
]


def brokerage_family(name: str | None, families: dict[str, list[str]] | None = None) -> str | None:
    """Collapse a brokerage's trading name to its family name."""
    if not name:
        return None
    patterns = families if families is not None else DEFAULT_BROKERAGE_FAMILIES
    for family, globs in patterns.items():
        if any(fnmatch.fnmatch(name, g) or fnmatch.fnmatch(name.upper(), g.upper()) for g in globs):
            return family
    return name


@dataclass
class BrokerageRow:
    family: str
    closings: int
    share: float | None = None
    #: Listings this firm currently holds. Closings are history; this is who is
    #: working the market now, and the two answer different questions.
    active_listings: int = 0

    def to_dict(self) -> dict:
        return {
            "family": self.family,
            "closings": self.closings,
            "share": self.share,
            "active_listings": self.active_listings,
        }


@dataclass
class AgentRow:
    agent: str
    brokerage: str | None
    closings: int
    avg_sold_to_ask: float | None
    #: The middle sale against ask. Preferred over the mean for the same reason
    #: medians lead elsewhere: one bidding war on one lot moves an average of
    #: two or three sales a long way.
    median_sold_to_ask: float | None = None
    #: The rate this agent's sales actually achieved. Read beside `avg_size`:
    #: a rate without the size that produced it inverts the real comparison.
    avg_ppsf: float | None = None
    avg_size: float | None = None
    #: Listings held right now. An agent with no closings here and three live
    #: listings is working this market, and is worth a conversation.
    active_listings: int = 0
    #: "shortlist" | "caution" | "" -- a generic pattern flag, not a judgement
    #: about a named individual.
    flag: str = ""
    dual_agency: bool = False

    def to_dict(self) -> dict:
        return {
            "agent": self.agent,
            "brokerage": self.brokerage,
            "closings": self.closings,
            "avg_sold_to_ask": self.avg_sold_to_ask,
            "median_sold_to_ask": self.median_sold_to_ask,
            "avg_ppsf": self.avg_ppsf,
            "avg_size": self.avg_size,
            "active_listings": self.active_listings,
            "flag": self.flag,
            "dual_agency": self.dual_agency,
        }


@dataclass
class AgentAnalysis:
    agents: list[AgentRow] = field(default_factory=list)
    brokerages: list[BrokerageRow] = field(default_factory=list)
    attributed: int = 0
    total: int = 0
    caveats: list[str] = field(default_factory=lambda: list(CAVEATS))

    def to_dict(self) -> dict:
        return {
            "agents": [a.to_dict() for a in self.agents],
            "brokerages": [b.to_dict() for b in self.brokerages],
            "attributed": self.attributed,
            "total": self.total,
            "caveats": self.caveats,
        }


def _flag(closings: int, ratio: float | None, reduced: bool) -> str:
    if ratio is None:
        return ""
    if closings >= SHORTLIST_MIN_CLOSINGS and ratio >= SHORTLIST_MIN_RATIO:
        return "shortlist"
    if reduced and ratio < CAUTION_MAX_RATIO:
        return "caution"
    return ""


def analyze(
    sold: list[SoldComp],
    families: dict[str, list[str]] | None = None,
    denominator: str = "lot_sqft",
    active: list | None = None,
) -> AgentAnalysis:
    active = list(active or [])
    live_by_agent: dict[str, int] = defaultdict(int)
    for listing in active:
        if listing.agent:
            live_by_agent[listing.agent] += 1

    by_agent: dict[str, list[SoldComp]] = defaultdict(list)
    for comp in sold:
        if comp.agent:
            by_agent[comp.agent].append(comp)
    # An agent with live listings and no closings here still belongs in the
    # table: they are working this market now, which is the question a seller
    # is actually asking.
    for name in live_by_agent:
        by_agent.setdefault(name, [])

    agents: list[AgentRow] = []
    for name, comps in by_agent.items():
        ratios = [r for c in comps if (r := c.sold_to_ask()) is not None]
        mean_ratio = statistics.fmean(ratios) if ratios else None
        middle_ratio = statistics.median(ratios) if ratios else None
        rates = [r for c in comps if (r := c.price_per_sqft(denominator)) is not None]
        sizes = [v for c in comps if (v := c.metric_sqft(denominator)) is not None]
        agents.append(
            AgentRow(
                agent=name,
                brokerage=brokerage_family(
                    next((c.brokerage for c in comps if c.brokerage), None), families
                ),
                closings=len(comps),
                avg_sold_to_ask=mean_ratio,
                median_sold_to_ask=middle_ratio,
                avg_ppsf=statistics.fmean(rates) if rates else None,
                avg_size=statistics.fmean(sizes) if sizes else None,
                active_listings=live_by_agent.get(name, 0),
                flag=_flag(len(comps), mean_ratio, any(c.was_reduced() for c in comps)),
                dual_agency=any(
                    c.buyer_agent and c.agent and c.buyer_agent.strip() == c.agent.strip()
                    for c in comps
                ),
            )
        )
    # Interview order: the flagged shortlist first, then volume, then how their
    # sales landed against ask. Volume comes before ratio deliberately -- a
    # single sale bid up 22% says less about an agent than three sales at par,
    # and the flag already requires both repeat closings and a ratio at or
    # above ask.
    agents.sort(
        key=lambda a: (
            a.flag != "shortlist",
            -a.closings,
            -(a.median_sold_to_ask or 0.0),
            a.agent,
        )
    )

    counts: dict[str, int] = defaultdict(int)
    for comp in sold:
        family = brokerage_family(comp.brokerage, families)
        if family:
            counts[family] += 1
    attributed = sum(1 for c in sold if c.brokerage or c.agent)
    live_counts: dict[str, int] = defaultdict(int)
    for listing in active:
        family = brokerage_family(listing.brokerage, families)
        if family:
            live_counts[family] += 1
    for family in live_counts:
        counts.setdefault(family, 0)
    brokerages = [
        BrokerageRow(
            family=f,
            closings=n,
            share=(n / attributed if attributed else None),
            active_listings=live_counts.get(f, 0),
        )
        for f, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]

    analysis = AgentAnalysis(
        agents=agents, brokerages=brokerages, attributed=attributed, total=len(sold)
    )
    if any(a.dual_agency for a in analysis.agents):
        analysis.caveats.insert(
            0,
            "Dual agency detected: at least one agent represented both sides of a sale. "
            "Ask directly how that was handled.",
        )
    unattributed = len(sold) - attributed
    if unattributed:
        analysis.caveats.append(
            f"{unattributed} of {len(sold)} sales could not be attributed and are shown as "
            "a dash. These may be off-MLS or investor-direct transactions."
        )
    return analysis
