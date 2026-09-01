"""The research layer: fetching, reading, and checking.

Every test here runs offline. The fetcher is exercised against a fake HTTP
client and the extractor against a fake model client, because the point of the
research layer's design is that it can be tested without either.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from lotcomps.adapters.cache import CacheEntry, PageCache
from lotcomps.adapters.embedded import (
    EmbeddedSpec,
    as_money,
    as_sqft,
    as_tag_date,
    dig,
    extract_records,
    find_embedded_json,
)
from lotcomps.adapters.fetch import PoliteFetcher
from lotcomps.adapters.geocode import street_matches, street_tokens
from lotcomps.adapters.html import looks_like_a_block_page, reduce_html
from lotcomps.model.comp import SoldComp
from lotcomps.research.verify import (
    Claim,
    apply_claims,
    clean_name,
    dates_match,
    flag_anomalous_rows,
    prices_match,
    verify_claim,
)

# ---------------------------------------------------------------------------
# Reading a page's embedded data
# ---------------------------------------------------------------------------

PAGE = """
<html><head><title>Sold lots</title></head><body>
<script>var tracking = {"noise": true};</script>
<script id="__NEXT_DATA__" type="application/json">
{"props": {"searchData": {"homes": [
  {"location": {"streetAddress": "12 Foundry St",
                "coordinates": {"latitude": 40.01, "longitude": -100.02}},
   "price": {"formattedPrice": "$550,000"},
   "fullTags": [{"formattedName": "SOLD"}, {"formattedName": "AUG 28, 2026"}],
   "lotSize": {"formattedDimension": "0.27 acres"},
   "bedrooms": null,
   "metadata": {"unifiedListingType": "RESALE_LOT_LAND"},
   "url": "/home/12-foundry-st"},
  {"location": {"streetAddress": "34 Kestrel Ln",
                "coordinates": {"latitude": 40.02, "longitude": -99.98}},
   "price": {"formattedPrice": "$1.2M"},
   "fullTags": [{"formattedName": "SOLD"}, {"formattedName": "JUL 4, 2026"}],
   "lotSize": null, "bedrooms": 3,
   "metadata": {"unifiedListingType": "RESALE_SINGLE_FAMILY"},
   "url": "/home/34-kestrel-ln"}
]}}}
</script>
</body></html>
"""

SPEC = EmbeddedSpec.from_config(
    {
        "script_id": "__NEXT_DATA__",
        "records_path": "props.searchData.homes",
        "url_base": "https://example.test",
        "fields": {
            "address": {"path": "location.streetAddress", "transform": "text"},
            "lat": {"path": "location.coordinates.latitude", "transform": "float"},
            "lon": {"path": "location.coordinates.longitude", "transform": "float"},
            "sold_price": {"path": "price.formattedPrice", "transform": "money"},
            "sold_date": {"path": "fullTags", "transform": "tag_date"},
            "lot_sqft": {"path": "lotSize.formattedDimension", "transform": "sqft"},
            "beds": {"path": "bedrooms", "transform": "float"},
            "property_type": {"path": "metadata.unifiedListingType", "transform": "text"},
            "source_url": {"path": "url", "transform": "text"},
        },
    }
)


def test_embedded_data_is_found_and_code_is_not():
    payload = find_embedded_json(PAGE, "__NEXT_DATA__")
    assert payload is not None
    assert dig(payload, "props.searchData.homes.0.location.streetAddress") == "12 Foundry St"
    assert find_embedded_json(PAGE, "__NOT_THERE__") is None


def test_records_extract_with_every_field():
    out = extract_records(PAGE, SPEC)
    assert out.count == 2 and not out.problems
    first = out.rows[0]
    assert first["address"] == "12 Foundry St"
    assert first["sold_price"] == 550000.0
    assert first["sold_date"] == date(2026, 8, 28)
    assert first["lat"] == pytest.approx(40.01)
    assert first["source_url"] == "https://example.test/home/12-foundry-st"


def test_acreage_is_converted_and_flagged_as_derived():
    """A rounded acre is +/-4% on a small parcel; the flag carries that."""
    out = extract_records(PAGE, SPEC)
    assert out.rows[0]["lot_sqft"] == pytest.approx(0.27 * 43560)
    assert out.rows[0]["lot_size_is_rounded"] is True
    assert out.rows[1]["lot_sqft"] is None


def test_a_missing_field_is_none_not_an_error():
    """A source that stops publishing a field degrades the run, not ends it."""
    out = extract_records(PAGE, SPEC)
    assert out.rows[1]["lot_sqft"] is None
    assert dig({"a": {"b": 1}}, "a.x.y") is None


def test_a_page_with_no_data_block_says_so():
    out = extract_records("<html><body>nothing here</body></html>", SPEC)
    assert out.count == 0
    assert any("no readable" in p for p in out.problems)


@pytest.mark.parametrize(
    "raw,expected",
    [("$550,000", 550000.0), ("$1.2M", 1200000.0), ("$550K", 550000.0),
     (615000, 615000.0), ("", None), (None, None), ("no price", None)],
)
def test_money_parsing(raw, expected):
    assert as_money(raw) == (pytest.approx(expected) if expected else expected)


@pytest.mark.parametrize(
    "raw,sqft,rounded",
    [("0.27 acres", 0.27 * 43560, True), ("5,227 sq ft", 5227.0, False),
     ("1 acre", 43560.0, True), (6098, 6098.0, False), (None, None, False)],
)
def test_dimension_parsing(raw, sqft, rounded):
    got, was_rounded = as_sqft(raw)
    assert got == (pytest.approx(sqft) if sqft else sqft)
    assert was_rounded is rounded


def test_the_date_is_found_by_shape_not_by_position():
    """Sites do not order their display tags consistently."""
    assert as_tag_date([{"formattedName": "SOLD"}, {"formattedName": "AUG 28, 2026"}]) == date(
        2026, 8, 28
    )
    assert as_tag_date([{"formattedName": "AUG 28, 2026"}, {"formattedName": "SOLD"}]) == date(
        2026, 8, 28
    )
    assert as_tag_date([{"formattedName": "SOLD"}]) is None


# ---------------------------------------------------------------------------
# Reducing a page
# ---------------------------------------------------------------------------


def test_reduction_drops_code_and_keeps_data():
    out = reduce_html(PAGE)
    assert "var tracking" not in out
    assert "RESALE_LOT_LAND" in out, "embedded data must survive"
    assert "Sold lots" in out


def test_a_challenge_page_served_as_200_is_recognized():
    """Otherwise it reads as 'no listings found', which is a silent zero."""
    assert looks_like_a_block_page("<html>Please verify you are a human</html>")
    assert looks_like_a_block_page("<html>Checking your browser before access</html>")
    assert not looks_like_a_block_page(PAGE)


# ---------------------------------------------------------------------------
# Fetching politely
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status: int, text: str) -> None:
        self.status_code, self.text = status, text


class FakeClient:
    def __init__(self, responses: dict[str, FakeResponse]) -> None:
        self.responses = responses
        self.requested: list[str] = []

    def get(self, url: str) -> FakeResponse:
        self.requested.append(url)
        return self.responses.get(url, FakeResponse(404, ""))

    def close(self) -> None:
        pass


def _fetcher(tmp_path, responses, **kwargs):
    fetcher = PoliteFetcher(
        PageCache(tmp_path / "cache"), delay_seconds=0.0, respect_robots=False, **kwargs
    )
    fetcher._client = FakeClient(responses)
    return fetcher


def test_a_cached_page_costs_the_source_nothing(tmp_path):
    url = "https://example.test/a"
    fetcher = _fetcher(tmp_path, {url: FakeResponse(200, "hello")})
    assert fetcher.fetch(url).text == "hello"
    assert fetcher.fetch(url).from_cache is True
    assert fetcher._client.requested == [url], "the second call must not hit the network"


def test_a_new_adapter_version_ignores_old_cache_entries(tmp_path):
    url = "https://example.test/a"
    fetcher = _fetcher(tmp_path, {url: FakeResponse(200, "hello")})
    fetcher.fetch(url, adapter_version="1")
    fetcher.fetch(url, adapter_version="2")
    assert len(fetcher._client.requested) == 2


def test_robots_disallowed_pages_are_never_requested(tmp_path, monkeypatch):
    fetcher = _fetcher(tmp_path, {})
    fetcher.respect_robots = True
    monkeypatch.setattr(fetcher.robots, "allows", lambda url: False)
    result = fetcher.fetch("https://example.test/private")
    assert result.blocked_by_robots and result.refused
    assert fetcher._client.requested == [], "a disallowed URL must not be fetched at all"


def test_a_refusal_is_reported_not_retried_differently(tmp_path):
    url = "https://example.test/blocked"
    fetcher = _fetcher(tmp_path, {url: FakeResponse(403, "")})
    result = fetcher.fetch(url)
    assert result.refused and not result.ok
    assert len(fetcher._client.requested) == 1, "a refusal is an answer, not a retry"
    assert "declined" in result.describe()


def test_a_server_error_is_retried_once_then_given_up_on(tmp_path):
    url = "https://example.test/flaky"
    fetcher = _fetcher(tmp_path, {url: FakeResponse(503, "")}, max_retries=1)
    result = fetcher.fetch(url)
    assert not result.ok
    assert len(fetcher._client.requested) == 2


def test_a_network_failure_returns_rather_than_raises(tmp_path):
    class Exploding(FakeClient):
        def get(self, url):
            raise TimeoutError("took too long")

    fetcher = _fetcher(tmp_path, {})
    fetcher._client = Exploding({})
    result = fetcher.fetch("https://example.test/a")
    assert not result.ok and "TimeoutError" in result.error


def test_a_corrupt_cache_entry_is_a_miss_not_a_crash(tmp_path):
    cache = PageCache(tmp_path / "cache")
    cache.put(CacheEntry(url="https://x.test/a", status=200, text="ok", fetched_at=9e9))
    path = next((tmp_path / "cache").rglob("*.json"))
    path.write_text("{not json", encoding="utf-8")
    assert cache.get("https://x.test/a") is None


def test_the_user_agent_names_the_tool(tmp_path):
    fetcher = PoliteFetcher(PageCache(tmp_path / "c"))
    assert "LotComps" in fetcher.user_agent
    assert "Mozilla" not in fetcher.user_agent, "never pretend to be a browser"


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def _sale(price=550000.0, when=date(2026, 8, 28)) -> SoldComp:
    return SoldComp(address="12 Foundry St", sold_price=price, sold_date=when)


def _claim(**kwargs) -> Claim:
    base = dict(
        address="12 Foundry St", field_name="agent", value="A. Example",
        source_url="https://example.test/p",
    )
    base.update(kwargs)
    return Claim(**base)


def test_a_matching_price_accepts_even_when_the_date_drifts():
    """Recording date and close of escrow routinely differ by a day or two."""
    verdict = verify_claim(
        _claim(claimed_price=550000.0, claimed_date=date(2026, 8, 30)), _sale()
    )
    assert verdict.accepted


def test_a_wrong_price_is_rejected_however_right_the_date_looks():
    verdict = verify_claim(
        _claim(claimed_price=420000.0, claimed_date=date(2026, 8, 28)), _sale()
    )
    assert not verdict.accepted and "does not match" in verdict.reason


def test_a_decades_old_transaction_is_rejected_even_at_the_same_price():
    """The stale-attribution failure: a real agent, for the wrong sale."""
    verdict = verify_claim(
        _claim(claimed_price=550000.0, claimed_date=date(1998, 5, 1)), _sale()
    )
    assert not verdict.accepted and "different transaction" in verdict.reason


def test_a_claim_with_no_evidence_is_rejected():
    assert not verify_claim(_claim(), _sale()).accepted


def test_a_date_alone_is_not_enough_by_default():
    assert not verify_claim(_claim(claimed_date=date(2026, 8, 28)), _sale()).accepted


@pytest.mark.parametrize("known,claimed,ok", [
    (550000, 550000, True), (550000, 550100, True), (550000, 500000, False),
    (550000, None, False), (None, 550000, False),
])
def test_price_tolerance(known, claimed, ok):
    assert prices_match(known, claimed) is ok


@pytest.mark.parametrize("days,ok", [(0, True), (3, True), (4, False), (-2, True)])
def test_date_tolerance(days, ok):
    from datetime import timedelta

    assert dates_match(date(2026, 8, 28), date(2026, 8, 28) + timedelta(days=days)) is ok


def test_verified_claims_land_on_the_comp():
    comp = _sale()
    log = apply_claims(
        [comp],
        [
            _claim(field_name="agent", value="A. Example", claimed_price=550000.0),
            _claim(field_name="brokerage", value="Example Realty", claimed_price=550000.0),
        ],
    )
    assert comp.agent == "A. Example"
    assert comp.brokerage == "Example Realty"
    assert len(log.accepted) == 2


def test_a_rejected_claim_leaves_the_field_empty_and_is_logged():
    comp = _sale()
    log = apply_claims([comp], [_claim(value="Wrong Person", claimed_price=999999.0)])
    assert comp.agent is None
    assert len(log.rejected) == 1 and "does not match" in log.rejected[0]


def test_a_claim_never_overwrites_what_is_already_known():
    comp = _sale()
    comp.agent = "From The Index"
    apply_claims([comp], [_claim(value="From A Page", claimed_price=550000.0)])
    assert comp.agent == "From The Index"


def test_a_claim_about_an_unknown_address_is_logged_not_applied():
    log = apply_claims([_sale()], [_claim(address="99 Elsewhere Rd", claimed_price=550000.0)])
    assert len(log.unverifiable) == 1


# ---------------------------------------------------------------------------
# Placeholder names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [
    "Out Of Area Out Of Area", "Out of Area", "Public Record", "Non-Member",
    "None", "N/A", "Unknown", "  ", "—",
])
def test_mls_filler_is_not_a_person(raw):
    """Seen live: an 'agent' called Out Of Area Out Of Area would otherwise
    have earned closings and a ranking in the agent table."""
    assert clean_name(raw) is None


@pytest.mark.parametrize("raw", ["A. Example", "B. Lindqvist", "C. Marchetti"])
def test_real_names_survive(raw):
    assert clean_name(raw) == raw


def test_a_placeholder_agent_is_rejected_with_a_reason():
    comp = _sale()
    log = apply_claims(
        [comp],
        [_claim(field_name="buyer_agent", value="Out Of Area Out Of Area",
                claimed_price=550000.0)],
    )
    assert comp.buyer_agent is None
    assert any("placeholder" in r for r in log.rejected)


# ---------------------------------------------------------------------------
# Anomalies
# ---------------------------------------------------------------------------


def test_a_parcel_selling_twice_in_one_window_is_flagged_for_a_human():
    notes = flag_anomalous_rows([
        SoldComp(address="1 A St", sold_price=800000, sold_date=date(2026, 1, 26)),
        SoldComp(address="1 A St", sold_price=615000, sold_date=date(2026, 5, 12)),
    ])
    assert len(notes) == 1 and "sold twice" in notes[0]


def test_distinct_parcels_are_not_flagged():
    assert flag_anomalous_rows([
        SoldComp(address="1 A St", sold_price=1, sold_date=date(2026, 1, 1)),
        SoldComp(address="2 A St", sold_price=2, sold_date=date(2026, 1, 2)),
    ]) == []


# ---------------------------------------------------------------------------
# Geocode verification
# ---------------------------------------------------------------------------


def test_a_geocoder_answering_about_a_different_street_is_caught():
    assert not street_matches("0 Larkspur Vista Rd", "LARKSPUR ST, DEMOVILLE, ZZ")
    assert street_matches("0 Larkspur Vista Rd", "LARKSPUR VISTA RD, DEMOVILLE, ZZ")


def test_street_comparison_ignores_type_and_direction_but_not_name():
    assert street_tokens("125 W Thistle Street") == {"thistle"}
    assert street_matches("125 W Thistle St", "125 THISTLE STREET")


def test_budget_stops_a_run_before_it_overspends():
    from lotcomps.research.llm import Budget, BudgetExhausted, Usage

    budget = Budget(total=2)
    budget.spent = Usage(calls=2)
    with pytest.raises(BudgetExhausted):
        budget.check("claude-opus-5")


def test_cost_is_estimated_from_real_usage():
    from lotcomps.research.llm import Usage

    usage = Usage(calls=1, input_tokens=1_000_000, output_tokens=100_000)
    assert usage.cost_usd("claude-opus-5") == pytest.approx(5.00 + 2.50)
    assert usage.cost_usd("some-unknown-model") is None


def test_the_extraction_schema_allows_every_field_to_be_absent():
    """A page that does not name an agent has told us something true."""
    from lotcomps.research.llm import PropertyPageFacts

    facts = PropertyPageFacts()
    assert facts.listing_agent is None
    assert facts.dual_agency_stated is False
    assert json.loads(facts.model_dump_json())["listing_brokerage"] is None


def test_a_sale_listed_twice_at_the_same_price_is_not_an_anomaly():
    """Dedup already collapses these. Reporting them trains readers to skip
    the anomaly list, which is where the real ones live."""
    assert flag_anomalous_rows([
        SoldComp(address="607 Larkspur St", sold_price=550000, sold_date=date(2026, 7, 13)),
        SoldComp(address="607 Larkspur St #34", sold_price=550000, sold_date=date(2026, 7, 13)),
    ]) == []


def test_a_near_identical_price_is_still_the_same_sale():
    """Sources round differently: $821,000 and $820,999 are one transaction."""
    assert flag_anomalous_rows([
        SoldComp(address="677 E Chandler St", sold_price=821000, sold_date=date(2026, 8, 11)),
        SoldComp(address="677 E Chandler St #48", sold_price=820999, sold_date=date(2026, 8, 11)),
    ]) == []


def test_a_genuinely_different_sale_is_still_flagged():
    notes = flag_anomalous_rows([
        SoldComp(address="1 A St", sold_price=800000, sold_date=date(2026, 1, 26)),
        SoldComp(address="1 A St", sold_price=615000, sold_date=date(2026, 5, 12)),
    ])
    assert len(notes) == 1


# ---------------------------------------------------------------------------
# Trimming a page before reading it
# ---------------------------------------------------------------------------


def test_trimming_keeps_the_lines_that_carry_facts():
    from lotcomps.adapters.html import focus_text

    page = (
        "Address and headline. " + ("navigation filler. " * 400)
        + "Listed by A. Example DRE# 01234567 with Example Realty. "
        + ("mortgage calculator copy. " * 400)
        + "Price history: Sold on Aug 28, 2026 for $550,000. List price $615,000. "
        + ("neighbourhood reviews. " * 400)
        + "Lot size 11,645 sq ft. "
        + ("similar listings. " * 400)
    )
    out = focus_text(page, max_chars=6000)
    assert len(out) < len(page) / 3
    for fact in ("A. Example", "Example Realty", "$550,000", "$615,000", "11,645"):
        assert fact in out, f"{fact} was cut"
    assert "Address and headline" in out, "the top of the page is always kept"


def test_trimming_marks_where_it_cut():
    """A reader must not infer across a gap it cannot see."""
    from lotcomps.adapters.html import focus_text

    page = "Listed by X. " + ("filler " * 5000) + "Price history here."
    assert "[...]" in focus_text(page, max_chars=2000)


def test_a_short_page_is_left_alone():
    from lotcomps.adapters.html import focus_text

    page = "Listed by A. Example with Example Realty."
    assert focus_text(page) == page
