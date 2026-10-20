"""past_days has to show up in the Realtor search query.

Issue #143: omitting listing_type, or passing a list of types, built a query
with no date predicate, so past_days=1 still walked results up to the
10,000 cap. These tests stub the GraphQL client and never hit the network.
"""

import json
from datetime import datetime, timedelta, timezone

from homeharvest import scrape_property
from homeharvest.core.scrapers import ScraperInput
from homeharvest.core.scrapers.models import ListingType, ReturnType
from homeharvest.core.scrapers.realtor import RealtorScraper


def _scraper(**overrides) -> RealtorScraper:
    params = {
        "location": "Warren, MI",
        "listing_type": None,
        "return_type": ReturnType.raw,
        "extra_property_data": False,
    }
    params.update(overrides)
    return RealtorScraper(ScraperInput(**params))


def _capture_search(scraper: RealtorScraper, search_results=None, geo_result=None):
    calls = []
    if geo_result is None:
        geo_result = {
            "text": "Warren, MI",
            "geo": {
                "area_type": "city",
                "city": "Warren",
                "state_code": "MI",
            },
        }

    def fake_post(query, variables, operation_name):
        calls.append({"operation": operation_name, "query": query, "variables": variables})
        if operation_name == "Search_suggestions":
            return {"data": {"search_suggestions": {"geo_results": [geo_result]}}}
        if operation_name == "GetHomeSearch":
            results = search_results(query) if search_results else []
            return {"data": {"homeSearch": {"total": len(results), "results": results}}}
        if operation_name == "GetHomeDetails":
            return {"data": {"home": None}}
        raise AssertionError(operation_name)

    scraper._graphql_post = fake_post
    homes = scraper.search()
    queries = [call["query"] for call in calls if call["operation"] == "GetHomeSearch"]
    return homes, queries, calls


def test_past_days_is_sent_when_listing_type_is_omitted():
    _, queries, calls = _capture_search(_scraper(last_x_days=1))

    listed = [query for query in queries if 'list_date: { min: "$today-1D" }' in query]
    sold = [query for query in queries if 'sold_date: { min: "$today-1D" }' in query]
    assert len(listed) == 1
    assert "status: [for_sale, for_rent, off_market]" in listed[0]
    assert len(sold) == 1
    assert "status: sold" in sold[0]

    # Pending has no working server-side date field. Every other query must
    # carry the window, or the client will page until the 10,000 cap.
    undated = [query for query in queries if "$today-1D" not in query]
    assert len(undated) == 1
    assert "or_filters:" in undated[0]
    assert "pending: true" in undated[0]

    suggestions = [call for call in calls if call["operation"] == "Search_suggestions"]
    assert len(suggestions) == 1


def test_past_days_is_sent_for_a_list_of_listing_types():
    _, queries, _ = _capture_search(
        _scraper(listing_type=[ListingType.FOR_SALE, ListingType.SOLD], last_x_days=1)
    )

    assert len(queries) == 2
    assert any(
        'list_date: { min: "$today-1D" }' in query and "status: for_sale" in query
        for query in queries
    )
    assert any(
        'sold_date: { min: "$today-1D" }' in query and "status: sold" in query
        for query in queries
    )
    assert all("$today-1D" in query for query in queries)


def test_listing_types_that_share_a_date_field_stay_in_one_query():
    _, queries, _ = _capture_search(
        _scraper(listing_type=[ListingType.FOR_SALE, ListingType.FOR_RENT], last_x_days=7)
    )

    assert len(queries) == 1
    assert 'list_date: { min: "$today-7D" }' in queries[0]
    assert "status: [for_sale, for_rent]" in queries[0]
    assert "sold_date:" not in queries[0]


def test_single_listing_type_still_sends_one_dated_query():
    _, queries, _ = _capture_search(_scraper(listing_type=ListingType.SOLD, last_x_days=1))

    assert len(queries) == 1
    assert 'sold_date: { min: "$today-1D" }' in queries[0]
    assert "status: sold" in queries[0]


def test_off_market_past_days_uses_list_date():
    _, queries, _ = _capture_search(_scraper(listing_type=ListingType.OFF_MARKET, last_x_days=3))

    assert len(queries) == 1
    assert 'list_date: { min: "$today-3D" }' in queries[0]
    assert "status: off_market" in queries[0]


def test_omitted_listing_type_without_past_days_stays_one_query():
    _, queries, _ = _capture_search(_scraper())

    assert len(queries) == 1
    assert "list_date:" not in queries[0]
    assert "sold_date:" not in queries[0]
    assert "status: [for_sale, for_rent, sold, off_market]" in queries[0]


def test_date_range_is_passed_through_for_mixed_listing_types():
    _, queries, _ = _capture_search(
        _scraper(
            listing_type=[ListingType.SOLD, ListingType.FOR_SALE],
            date_from="2024-05-01",
            date_to="2024-05-15",
        )
    )

    assert any('sold_date: { min: "2024-05-01", max: "2024-05-15" }' in query for query in queries)
    assert any('list_date: { min: "2024-05-01", max: "2024-05-15" }' in query for query in queries)


def test_exclude_pending_does_not_add_an_undated_pending_query():
    _, queries, _ = _capture_search(_scraper(last_x_days=1, exclude_pending=True))

    assert queries
    assert all("or_filters:" not in query for query in queries)
    assert all("$today-1D" in query for query in queries)


def test_pending_rows_outside_past_days_are_dropped_client_side():
    now = datetime.now(timezone.utc)

    def results(query):
        if "or_filters:" not in query:
            return []
        return [
            {"property_id": "new", "pending_date": now.isoformat()},
            {"property_id": "old", "pending_date": (now - timedelta(days=30)).isoformat()},
        ]

    homes, _, _ = _capture_search(_scraper(last_x_days=1), search_results=results)
    assert [home["property_id"] for home in homes] == ["new"]


def test_single_address_lookup_is_not_repeated_when_past_days_is_set():
    _, queries, calls = _capture_search(
        _scraper(last_x_days=1),
        geo_result={
            "text": "1 Main St, Warren, MI",
            "geo": {"area_type": "address", "mpr_id": "123"},
        },
    )

    assert queries == []
    detail_calls = [call for call in calls if call["operation"] == "GetHomeDetails"]
    assert len(detail_calls) == 1


def test_radius_search_still_sends_past_days():
    _, queries, _ = _capture_search(
        _scraper(last_x_days=1, radius=1.0),
        geo_result={
            "text": "1 Main St, Warren, MI",
            "geo": {
                "area_type": "address",
                "mpr_id": "123",
                "centroid": {"lon": -83.0, "lat": 42.5},
            },
        },
    )

    assert any('sold_date: { min: "$today-1D" }' in query for query in queries)
    assert any('list_date: { min: "$today-1D" }' in query for query in queries)


def test_scrape_property_passes_past_days_into_the_search_query(monkeypatch):
    posted = []

    class _Response:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    def fake_post(url, headers=None, data=None, proxies=None, **kwargs):
        body = json.loads(data)
        posted.append(body)
        query = body.get("query", "")
        if "Search_suggestions" in query:
            return _Response(
                {
                    "data": {
                        "search_suggestions": {
                            "geo_results": [
                                {
                                    "text": "Warren, MI",
                                    "geo": {"area_type": "city", "city": "Warren", "state_code": "MI"},
                                }
                            ]
                        }
                    }
                }
            )
        return _Response({"data": {"homeSearch": {"total": 0, "results": []}}})

    monkeypatch.setattr("homeharvest.core.scrapers.realtor.requests.post", fake_post)

    result = scrape_property(location="Warren, MI", past_days=1)

    search_queries = [
        body["query"]
        for body in posted
        if body.get("operationName") == "GetHomeSearch"
    ]
    assert search_queries
    assert any('sold_date: { min: "$today-1D" }' in query for query in search_queries)
    assert any('list_date: { min: "$today-1D" }' in query for query in search_queries)
    assert result.empty
