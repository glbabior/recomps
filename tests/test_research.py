"""The research layer: fetching, reading, and checking.

Every test here runs offline. The fetcher is exercised against a fake HTTP
client and the extractor against a fake model client, because the point of the
research layer's design is that it can be tested without either.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

# The research layer's schemas need pydantic, which ships with the "live"
# extra. Everything else in this file runs on a bare install.
pytest.importorskip("pydantic", reason="the live layer is an optional extra")

from recomps.adapters.cache import CacheEntry, PageCache
from recomps.adapters.embedded import (
    EmbeddedSpec,
    as_money,
    as_sqft,
    as_tag_date,
    dig,
    extract_records,
    find_embedded_json,
)
from recomps.adapters.fetch import PoliteFetcher
from recomps.adapters.geocode import street_matches, street_tokens
from recomps.adapters.html import looks_like_a_block_page, reduce_html
from recomps.model.comp import SoldComp
from recomps.research.verify import (
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
    assert "REComps" in fetcher.user_agent
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
    from recomps.research.llm import Budget, BudgetExhausted, Usage

    budget = Budget(total=2)
    budget.spent = Usage(calls=2)
    with pytest.raises(BudgetExhausted):
        budget.check("claude-opus-5")


def test_cost_is_estimated_from_real_usage():
    from recomps.research.llm import Usage

    usage = Usage(calls=1, input_tokens=1_000_000, output_tokens=100_000)
    assert usage.cost_usd("claude-opus-5") == pytest.approx(5.00 + 2.50)
    assert usage.cost_usd("some-unknown-model") is None


def test_the_extraction_schema_allows_every_field_to_be_absent():
    """A page that does not name an agent has told us something true."""
    from recomps.research.llm import PropertyPageFacts

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
    from recomps.adapters.html import focus_text

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
    from recomps.adapters.html import focus_text

    page = "Listed by X. " + ("filler " * 5000) + "Price history here."
    assert "[...]" in focus_text(page, max_chars=2000)


def test_a_short_page_is_left_alone():
    from recomps.adapters.html import focus_text

    page = "Listed by A. Example with Example Realty."
    assert focus_text(page) == page


# ---------------------------------------------------------------------------
# A page that serves a subset of its own results
# ---------------------------------------------------------------------------
#
# The worst failure an index adapter can have, because it does not look like
# one: the run completes, the workbook builds, and the market merely appears
# smaller than it is. One observed index claimed 166 properties and surfaced 9.


def _payload_html(records: list[dict], total: int | str | None) -> str:
    import json

    payload = {"props": {"searchData": {"homes": records}}}
    if total is not None:
        payload["props"]["searchData"]["totalCount"] = total
    return f'<html><script id="__NEXT_DATA__">{json.dumps(payload)}</script></html>'


def _spec(total_path: str = "props.searchData.totalCount") -> EmbeddedSpec:
    return EmbeddedSpec.from_config(
        {
            "script_id": "__NEXT_DATA__",
            "records_path": "props.searchData.homes",
            "total_path": total_path,
            "fields": {"address": {"path": "addr", "transform": "text"}},
        }
    )


def test_a_page_serving_fewer_results_than_it_claims_says_so():
    html = _payload_html([{"addr": f"{i} Example St"} for i in range(9)], total=166)
    found = extract_records(html, _spec())
    assert found.count == 9
    assert found.claimed_total == 166
    assert found.is_short
    assert any("claims 166" in p for p in found.problems)


def test_a_page_that_agrees_with_itself_raises_nothing():
    html = _payload_html([{"addr": f"{i} Example St"} for i in range(34)], total=34)
    found = extract_records(html, _spec())
    assert found.count == 34
    assert not found.is_short
    assert not found.problems


def test_a_claimed_total_written_as_text_is_still_read():
    """Sites render their own counts as prose: 'Total Listings: 36 parcels'."""
    html = _payload_html([{"addr": f"{i} Example St"} for i in range(9)], total="166 properties")
    found = extract_records(html, _spec())
    assert found.claimed_total == 166
    assert found.is_short


def test_a_source_that_declares_no_total_is_not_penalised():
    """Most payloads carry no count. That is not a problem to report."""
    html = _payload_html([{"addr": f"{i} Example St"} for i in range(9)], total=None)
    found = extract_records(html, EmbeddedSpec.from_config({
        "script_id": "__NEXT_DATA__",
        "records_path": "props.searchData.homes",
        "fields": {"address": {"path": "addr", "transform": "text"}},
    }))
    assert found.count == 9
    assert found.claimed_total is None
    assert not found.is_short
    assert not found.problems


def test_an_unreadable_total_is_reported_rather_than_assumed_fine():
    html = _payload_html([{"addr": "1 Example St"}], total=None)
    found = extract_records(html, _spec("props.searchData.nothingHere"))
    assert found.claimed_total is None
    assert any("could not be checked" in p for p in found.problems)


def test_more_extracted_than_claimed_is_not_treated_as_short():
    """A stale or rounded site-side count must not fail a complete extraction."""
    html = _payload_html([{"addr": f"{i} Example St"} for i in range(40)], total=38)
    found = extract_records(html, _spec())
    assert not found.is_short
    assert not any("claims" in p for p in found.problems)


# ---------------------------------------------------------------------------
# The active side of an index
# ---------------------------------------------------------------------------
#
# Which side of the market a source describes is declared by the plugin, not
# guessed from its name or its role text -- otherwise a market's wording
# becomes load-bearing.


def _index_source(side: str | None) -> object:
    from recomps.plugin.market import SourceSpec

    config = {
        "embedded": {
            "script_id": "__NEXT_DATA__",
            "records_path": "props.searchData.homes",
            "total_path": "props.searchData.totalHomes",
            "fields": {
                "address": {"path": "addr", "transform": "text"},
                "list_price": {"path": "price", "transform": "float"},
                "sold_price": {"path": "price", "transform": "float"},
                "lot_sqft": {"path": "lot", "transform": "sqft"},
            },
        }
    }
    if side:
        config["side"] = side
    return SourceSpec(
        name=f"index-{side or 'sold'}", adapter="x", url="https://example.invalid/i",
        config=config,
    )


def _index_html(count: int, total: int | None = None) -> str:
    homes = [
        {"addr": f"{i} Example St", "price": 500_000 + i, "lot": "0.25 acres"}
        for i in range(count)
    ]
    data = {"props": {"searchData": {"homes": homes}}}
    if total is not None:
        data["props"]["searchData"]["totalHomes"] = total
    return f'<html><script id="__NEXT_DATA__">{json.dumps(data)}</script></html>'


def _reader(html: str):
    from recomps.research.live import LiveResearcher, LiveRunLog

    class Response:
        ok, text, status = True, html, 200

        def describe(self):
            return ""

    class Fetcher:
        def fetch(self, url, adapter_version=""):
            return Response()

    reader = LiveResearcher.__new__(LiveResearcher)
    reader.fetcher = Fetcher()
    reader.log = LiveRunLog()
    return reader


def test_a_source_declared_active_yields_listings_not_sales():
    from recomps.config.profile import vacant_land_profile
    from recomps.model.comp import ActiveListing

    source = _index_source("active")
    rows, outcome = _reader(_index_html(3, total=3))._read_index(
        source, vacant_land_profile("p"), source.config
    )
    assert not outcome.error
    assert len(rows) == 3
    assert all(isinstance(r, ActiveListing) for r in rows)
    assert rows[0].list_price == 500_000


def test_a_source_with_no_declared_side_is_still_the_sold_backbone():
    """Existing plugins declare no side; they must keep working unchanged."""
    from recomps.config.profile import vacant_land_profile
    from recomps.model.comp import SoldComp

    source = _index_source(None)
    rows, _ = _reader(_index_html(3))._read_index(
        source, vacant_land_profile("p"), source.config
    )
    assert all(isinstance(r, SoldComp) for r in rows)


def test_rounded_acreage_from_an_index_is_flagged_as_rounded():
    """A rate built on 0.25 acres is not precise to the cent, and the caveat
    has to travel with it."""
    from recomps.config.profile import vacant_land_profile

    source = _index_source("active")
    rows, _ = _reader(_index_html(2, total=2))._read_index(
        source, vacant_land_profile("p"), source.config
    )
    assert all(r.lot_size_is_rounded for r in rows)


def test_an_index_serving_fewer_than_it_claims_is_reported_as_incomplete():
    from recomps.config.profile import vacant_land_profile

    source = _index_source("active")
    rows, outcome = _reader(_index_html(9, total=166))._read_index(
        source, vacant_land_profile("p"), source.config
    )
    assert len(rows) == 9
    assert "claims 166" in (outcome.error or "")


def test_attribution_splits_only_where_a_licence_says_a_person_is_named():
    """The obvious rule -- split on the comma -- invents an agent called
    "Sender Realty" and a brokerage called "Inc." """
    from recomps.adapters.embedded import TRANSFORMS

    agent, broker = TRANSFORMS["attribution_agent"], TRANSFORMS["attribution_brokerage"]

    named = "Jane Roe DRE # 01234567, Some Brokerage"
    assert agent(named) == "Jane Roe"
    assert broker(named) == "Some Brokerage"

    incorporated = "Sender Realty, Inc."
    assert agent(incorporated) is None
    assert broker(incorporated) == "Sender Realty, Inc."

    both = "Letrice Barge DRE # 02091064, LA Top Broker, Inc."
    assert agent(both) == "Letrice Barge"
    assert broker(both) == "LA Top Broker, Inc."

    assert agent("COMPASS") is None
    assert broker("COMPASS") == "COMPASS"
    assert agent(None) is None and broker(None) is None


def test_other_licence_labels_are_recognised():
    from recomps.adapters.embedded import TRANSFORMS

    assert TRANSFORMS["attribution_agent"]("Ann Lee CalBRE #123, X Realty") == "Ann Lee"
    assert TRANSFORMS["attribution_agent"]("Bo Ng License 99, Y Realty") == "Bo Ng"


def test_a_surname_beginning_with_a_licence_label_is_not_truncated():
    """'Licata' starts with 'Lic'. Word boundaries, not prefixes."""
    from recomps.adapters.embedded import TRANSFORMS

    assert TRANSFORMS["attribution_brokerage"]("Licata Rossi Realty") == "Licata Rossi Realty"
    assert TRANSFORMS["attribution_agent"]("Licata Rossi Realty") is None


# ---------------------------------------------------------------------------
# A window is a request, not a promise
# ---------------------------------------------------------------------------


def test_a_run_says_when_the_sources_could_not_reach_back_that_far():
    """Widening the window is the natural next move and does nothing: an index
    serves its most recent page. A comp that is not published looks exactly
    like a comp that does not exist."""
    from recomps.model.comp import SoldComp
    from recomps.research.base import Diagnostics
    from recomps.research.live import _report_reach

    diagnostics = Diagnostics()
    comps = [
        SoldComp(address="1 Example St", sold_date=date(2026, 7, 2)),
        SoldComp(address="2 Example St", sold_date=date(2026, 8, 9)),
    ]
    _report_reach(diagnostics, comps, date(2026, 5, 18))
    note = " ".join(diagnostics.not_found)
    assert "reached back only to 2026-07-02" in note
    assert "45 days" in note
    assert "absent, not nonexistent" in note


def test_a_run_that_reached_the_whole_window_says_nothing():
    from recomps.model.comp import SoldComp
    from recomps.research.base import Diagnostics
    from recomps.research.live import _report_reach

    diagnostics = Diagnostics()
    _report_reach(
        diagnostics,
        [SoldComp(address="1 Example St", sold_date=date(2026, 5, 1))],
        date(2026, 5, 18),
    )
    assert not diagnostics.not_found


def test_thin_agent_attribution_on_listings_is_declared():
    """A zero in an agent's live column must not read as 'nothing on the
    market' when it means 'the index did not say'."""
    from recomps.model.comp import ActiveListing
    from recomps.research.base import Diagnostics
    from recomps.research.live import _report_active_attribution

    diagnostics = Diagnostics()
    listings = [ActiveListing(address=f"{i} Example St") for i in range(34)]
    for listing in listings[:6]:
        listing.agent = "A. Agent"
    _report_active_attribution(diagnostics, listings)
    note = " ".join(diagnostics.not_found)
    assert "Only 6 of 34" in note
    assert "a floor, not a tally" in note


def test_well_attributed_listings_raise_nothing():
    from recomps.model.comp import ActiveListing
    from recomps.research.base import Diagnostics
    from recomps.research.live import _report_active_attribution

    diagnostics = Diagnostics()
    listings = [ActiveListing(address=f"{i} Example St", agent="A") for i in range(10)]
    _report_active_attribution(diagnostics, listings)
    assert not diagnostics.not_found


# ---------------------------------------------------------------------------
# A source may only point at itself
# ---------------------------------------------------------------------------
#
# A record's URL is chosen by the site being read, and whatever it says gets
# fetched next from the owner's machine, cached to disk, and put in a model
# prompt. Passed through unchecked it is a redirect the site controls.


def _url_spec() -> EmbeddedSpec:
    return EmbeddedSpec.from_config(
        {
            "script_id": "__NEXT_DATA__",
            "records_path": "p",
            "url_base": "https://listings.invalid",
            "fields": {
                "address": {"path": "a", "transform": "text"},
                "source_url": {"path": "u", "transform": "text"},
            },
        }
    )


def _extract_url(raw: str):
    html = (
        '<html><script id="__NEXT_DATA__">'
        + json.dumps({"p": [{"a": "1 Example St", "u": raw}]})
        + "</script></html>"
    )
    found = extract_records(html, _url_spec())
    return found.rows[0]["source_url"], found.problems


@pytest.mark.parametrize(
    "hostile",
    [
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://localhost:8501/_stcore/health",      # a service on this machine
        "http://127.0.0.1:22/",
        "https://elsewhere.invalid/x",
        "file:///etc/passwd",
    ],
)
def test_a_record_cannot_send_the_fetcher_somewhere_else(hostile):
    resolved, problems = _extract_url(hostile)
    assert resolved is None, f"would have fetched {hostile}"
    assert problems, "a source naming another host is worth reporting, not silent"


def test_a_relative_path_still_resolves_against_its_own_site():
    resolved, problems = _extract_url("/home/1-Example-St/99/")
    assert resolved == "https://listings.invalid/home/1-Example-St/99/"
    assert not problems


def test_an_absolute_url_on_the_same_site_is_kept():
    resolved, problems = _extract_url("https://listings.invalid/home/ok/")
    assert resolved == "https://listings.invalid/home/ok/"
    assert not problems


# ---------------------------------------------------------------------------
# Nothing is spent without being asked
# ---------------------------------------------------------------------------
#
# The count of property lookups comes from the fetched index, so the ceiling is
# set by the site rather than by the user. An index returning four thousand
# records instead of forty would otherwise spend four thousand lookups.


def _plan(lookups: int, via: str = "api"):
    from recomps.research.live import LookupPlan

    return LookupPlan(lookups=lookups, via=via)


def test_a_metered_plan_states_the_money():
    plan = _plan(40)
    assert plan.estimated_cost_usd == pytest.approx(1.60)
    assert "$1.60" in plan.describe()
    assert "40 properties" in plan.describe()


def test_a_subscription_plan_states_there_is_no_bill():
    plan = _plan(40, via="subscription")
    assert plan.estimated_cost_usd is None
    assert "not billed per page" in plan.describe()
    assert "$" not in plan.describe()


def test_a_small_run_is_not_interrupted():
    """A prompt nobody reads is worse than no prompt."""
    from recomps.research.live import CONFIRM_ABOVE_LOOKUPS

    assert CONFIRM_ABOVE_LOOKUPS > 1
    researcher, comps, profile = _capped_researcher(CONFIRM_ABOVE_LOOKUPS - 1)
    asked = []
    researcher.confirm = lambda plan: asked.append(plan) or True
    researcher._fan_out(comps, profile)
    assert not asked, "a small run should not stop to ask"


def test_a_large_run_asks_first_and_declining_spends_nothing():
    from recomps.research.live import CONFIRM_ABOVE_LOOKUPS

    researcher, comps, profile = _capped_researcher(CONFIRM_ABOVE_LOOKUPS + 5)
    asked = []

    def refuse(plan):
        asked.append(plan)
        return False

    researcher.confirm = refuse
    claims = researcher._fan_out(comps, profile)
    assert asked and asked[0].lookups == CONFIRM_ABOVE_LOOKUPS + 5
    assert claims == []
    assert researcher.log.lookups_attempted == 0
    note = " ".join(researcher.log.verification.unverifiable)
    assert "not told to go ahead" in note
    assert "missing rather than absent from the source" in note


def test_with_no_one_to_ask_the_run_raises_rather_than_spending():
    """A caller that cannot prompt -- the interface -- gets the plan handed to
    it instead of a bill."""
    from recomps.research.live import CONFIRM_ABOVE_LOOKUPS, LookupsNeedConfirmation

    researcher, comps, profile = _capped_researcher(CONFIRM_ABOVE_LOOKUPS + 1)
    researcher.confirm = None
    with pytest.raises(LookupsNeedConfirmation) as raised:
        researcher._fan_out(comps, profile)
    assert raised.value.plan.lookups == CONFIRM_ABOVE_LOOKUPS + 1


def test_an_answered_yes_proceeds_without_asking_again():
    from recomps.research.live import CONFIRM_ABOVE_LOOKUPS

    researcher, comps, profile = _capped_researcher(CONFIRM_ABOVE_LOOKUPS + 1)
    researcher.confirm = True
    researcher._fan_out(comps, profile)
    assert researcher.log.lookups_attempted == CONFIRM_ABOVE_LOOKUPS + 1


def _capped_researcher(count: int):
    """A researcher whose fan-out has `count` targets and never actually looks
    anything up."""
    from recomps.config.profile import vacant_land_profile
    from recomps.model.comp import SoldComp
    from recomps.research.live import LiveResearcher, LiveRunLog

    researcher = LiveResearcher.__new__(LiveResearcher)
    researcher.max_lookups = None
    researcher.via = "api"
    researcher.workers = 1
    researcher.log = LiveRunLog()
    researcher._look_up = lambda comp: (comp, [], "")
    researcher._needs_lookup = lambda comp, profile: True

    comps = [
        SoldComp(address=f"{i} Example St", sold_price=500_000.0,
                 sold_date=date(2026, 7, 1), sources=["https://x.invalid/1"])
        for i in range(count)
    ]
    return researcher, comps, vacant_land_profile("p")
