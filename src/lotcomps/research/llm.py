"""Reading a page with Claude, under a budget.

This is the fallback, not the default. Where a page publishes its data as
structured JSON the deterministic reader takes it, because a parser cannot
misread a price and costs nothing. What is left over is genuine prose --
"Listed by Jane Smith DRE# 01234567 with Example Realty" buried in a property
page -- and that is what a language model is actually good at.

Three rules shape everything here.

**A missing field is a result.** Every schema field is optional, and the
instructions say plainly that a null is the correct answer when the page does
not say. This has to be stated repeatedly and rewarded, because the natural
failure mode of extraction is a confident, plausible, invented value that is
indistinguishable from a real one.

**Every claim carries its evidence.** The extractor is asked for the sale price
and date *as the page states them*, not because the pipeline needs them -- it
already knows them -- but so the verification pass can check that the page is
describing the sale we asked about rather than one from 1998.

**Spending is bounded and reported.** Each item gets a small call budget, the
run gets a total, and the tokens and estimated cost are printed at the end.
A research tool whose cost is invisible is a research tool nobody dares re-run.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel, Field

#: Default model. Overridable, but not downgraded silently to save money --
#: that is the owner's decision to make, not this module's.
DEFAULT_MODEL = "claude-opus-5"
#: Extraction is a bounded task against a strict schema with a verification
#: pass behind it, so it does not need the deepest reasoning setting. Raise it
#: if extraction quality turns out to be the limiting factor.
DEFAULT_EFFORT = "medium"
DEFAULT_MAX_TOKENS = 8000

#: US dollars per million tokens, input / output.
PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-fable-5": (10.00, 50.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

T = TypeVar("T", bound=BaseModel)


class BudgetExhausted(RuntimeError):
    pass


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    failures: int = 0

    def add(self, other: Usage) -> None:
        self.calls += other.calls
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.failures += other.failures

    def cost_usd(self, model: str) -> float | None:
        prices = PRICES.get(model)
        if prices is None:
            return None
        input_rate, output_rate = prices
        # Cached reads bill at roughly a tenth; writes at roughly 1.25x.
        billable_input = (
            self.input_tokens
            + self.cache_read_tokens * 0.1
            + self.cache_write_tokens * 1.25
        )
        return (billable_input * input_rate + self.output_tokens * output_rate) / 1_000_000

    def describe(self, model: str) -> str:
        cost = self.cost_usd(model)
        money = f", about ${cost:,.2f}" if cost is not None else ""
        return (
            f"{self.calls} call(s), {self.input_tokens:,} in / "
            f"{self.output_tokens:,} out{money}"
        )


@dataclass
class Budget:
    """What one run is allowed to spend."""

    #: Calls allowed per item before giving up on it and reporting not-found.
    per_item: int = 3
    #: Calls allowed across the whole run. None means no ceiling.
    total: int | None = 400
    #: Stop the run if the estimated spend passes this. None means no ceiling.
    max_cost_usd: float | None = 25.0
    spent: Usage = field(default_factory=Usage)

    def check(self, model: str) -> None:
        if self.total is not None and self.spent.calls >= self.total:
            raise BudgetExhausted(
                f"run reached its {self.total}-call budget. Raise --max-calls to continue."
            )
        cost = self.spent.cost_usd(model)
        if self.max_cost_usd is not None and cost is not None and cost >= self.max_cost_usd:
            raise BudgetExhausted(
                f"run reached its ${self.max_cost_usd:,.2f} budget "
                f"(estimated ${cost:,.2f} spent). Raise --max-cost to continue."
            )

    @property
    def remaining_calls(self) -> int | None:
        return None if self.total is None else max(0, self.total - self.spent.calls)


# ---------------------------------------------------------------------------
# What we ask a page for
# ---------------------------------------------------------------------------


class PropertyPageFacts(BaseModel):
    """Attribution and price history, as one page states them.

    Every field is optional on purpose. A page that does not name an agent has
    told us something true, and recording that is more useful than a guess.
    """

    listing_agent: str | None = Field(
        None, description="Name of the listing (seller's) agent, exactly as written."
    )
    listing_brokerage: str | None = Field(
        None, description="Brokerage or firm the listing agent works for."
    )
    buyer_agent: str | None = Field(
        None, description="Name of the buyer's agent, if the page names one separately."
    )
    buyer_brokerage: str | None = Field(None, description="The buyer agent's firm.")
    dual_agency_stated: bool = Field(
        False,
        description=(
            "True ONLY if the page explicitly says one agent represented both "
            "sides. Do not infer this from two names looking similar."
        ),
    )
    stated_sold_price: float | None = Field(
        None, description="Sale price as this page states it, in dollars, digits only."
    )
    stated_sold_date: str | None = Field(
        None, description="Sale date as this page states it, as YYYY-MM-DD."
    )
    final_list_price: float | None = Field(
        None,
        description=(
            "The last asking price before this sale closed. From the price "
            "history, the most recent list price at or before the sale date."
        ),
    )
    original_list_price: float | None = Field(
        None,
        description=(
            "The first asking price of the SAME listing effort that led to this "
            "sale. If an earlier, separate listing was withdrawn and the property "
            "was relisted later, use the relist price, not the older one."
        ),
    )
    lot_sqft: float | None = Field(None, description="Lot size in square feet.")
    living_sqft: float | None = Field(None, description="Living area in square feet.")
    page_is_about_a_different_property: bool = Field(
        False,
        description=(
            "True if this page is clearly about some other address than the one "
            "requested. Say so rather than reporting its details."
        ),
    )
    notes: str | None = Field(
        None, description="Anything odd worth a human seeing. Keep it to one sentence."
    )


EXTRACTION_SYSTEM = """\
You read a single real-estate page and report only what it actually says.

The rules, in order of importance:

1. If the page does not state something, the answer is null. Never infer,
   estimate, or fill a field with a plausible value. A null is a correct,
   useful answer; an invented value is a corruption that nobody downstream can
   detect.

2. Report the sale price and date exactly as this page states them, even
   though you were told what they are expected to be. Your figures are used to
   check that the page is about the right transaction. If the page shows a
   different sale than the one described in the request, report what the page
   says and set page_is_about_a_different_property when the address differs.

3. Price history needs care. A property may have been listed, withdrawn, and
   relisted later at a different price. The original list price is the first
   price of the listing effort that ended in THIS sale -- not an earlier,
   abandoned listing of the same address. Prices go up as well as down; do not
   assume the earliest number is the highest.

4. Attribution wording varies: "Listed by", "Listing courtesy of", "Presented
   by". A DRE or licence number beside a name confirms it is the agent. If the
   page attributes the record to "Public Record" or similar, there is no agent:
   report null.

5. Only set dual_agency_stated when the page says outright that one agent
   represented both buyer and seller. Two similar names is not evidence.
"""


@dataclass
class ExtractionResult:
    parsed: Any | None = None
    error: str = ""
    usage: Usage = field(default_factory=Usage)

    @property
    def ok(self) -> bool:
        return self.parsed is not None and not self.error


class ExtractionClient:
    """A thin wrapper that keeps the spending visible."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        effort: str = DEFAULT_EFFORT,
        budget: Budget | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        client: Any = None,
    ) -> None:
        self.model = model
        self.effort = effort
        self.budget = budget or Budget()
        self.max_tokens = max_tokens
        self._client = client

    @property
    def available(self) -> bool:
        return bool(
            self._client
            or os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        )

    def _anthropic(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    def extract(
        self,
        page_text: str,
        schema: type[T],
        request: str,
        system: str = EXTRACTION_SYSTEM,
    ) -> ExtractionResult:
        """Read `page_text` into `schema`. Never raises for a model failure."""
        usage = Usage()
        try:
            self.budget.check(self.model)
        except BudgetExhausted as exc:
            return ExtractionResult(error=str(exc), usage=usage)

        try:
            response = self._anthropic().messages.parse(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                output_config={"effort": self.effort},
                messages=[
                    {
                        "role": "user",
                        "content": f"{request}\n\n--- PAGE CONTENT ---\n{page_text}",
                    }
                ],
                output_format=schema,
            )
        except Exception as exc:
            usage.calls += 1
            usage.failures += 1
            self.budget.spent.add(usage)
            return ExtractionResult(error=f"{type(exc).__name__}: {exc}", usage=usage)

        usage.calls = 1
        raw = getattr(response, "usage", None)
        if raw is not None:
            usage.input_tokens = getattr(raw, "input_tokens", 0) or 0
            usage.output_tokens = getattr(raw, "output_tokens", 0) or 0
            usage.cache_read_tokens = getattr(raw, "cache_read_input_tokens", 0) or 0
            usage.cache_write_tokens = getattr(raw, "cache_creation_input_tokens", 0) or 0
        self.budget.spent.add(usage)

        if getattr(response, "stop_reason", None) == "refusal":
            return ExtractionResult(error="the model declined this page", usage=usage)
        return ExtractionResult(parsed=response.parsed_output, usage=usage)


def build_request(
    address: str, known_price: float | None, known_date: Any, purpose: str
) -> str:
    """The per-page instruction, carrying what we already know as a check."""
    known = []
    if known_price:
        known.append(f"sold for about ${known_price:,.0f}")
    if known_date:
        known.append(f"around {known_date}")
    context = (" It is expected to have " + " ".join(known) + ".") if known else ""
    return (
        f"This page should be about {address}.{context}\n\n"
        f"{purpose}\n\n"
        "Report only what the page states. Use null for anything it does not."
    )
