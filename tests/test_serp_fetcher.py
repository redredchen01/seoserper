"""Unit 2: SerpAPI fetcher contract + failure mode tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from seoserper.fetchers.serp import (
    _GOOGLE_DOMAIN,
    _resolve_domain,
    fetch_serp_data,
)
from seoserper.models import (
    FailureCategory,
    PAAQuestion,
    ParseResult,
    RelatedSearch,
    SurfaceName,
    SurfaceStatus,
)

FIXTURES = Path(__file__).parent / "fixtures" / "serp"


def _response(status_code: int = 200, body: str = "{}"):
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.text = body
    resp.content = body.encode("utf-8")
    return resp


def _patched_get(response_or_exc):
    if isinstance(response_or_exc, Exception):
        return patch(
            "seoserper.fetchers.serp.requests.get", side_effect=response_or_exc
        )
    return patch(
        "seoserper.fetchers.serp.requests.get", return_value=response_or_exc
    )


# --- happy paths -------------------------------------------------------------


def test_happy_path_en_us_returns_paa_and_related():
    body = (FIXTURES / "ok_en_us_coffee.json").read_text()
    with _patched_get(_response(200, body)):
        result = fetch_serp_data("coffee", "en", "us", api_key="fake-key")

    paa = result[SurfaceName.PAA]
    related = result[SurfaceName.RELATED]

    assert paa.status == SurfaceStatus.OK
    assert paa.failure_category is None
    assert len(paa.items) == 5
    assert [i.rank for i in paa.items] == [1, 2, 3, 4, 5]
    assert all(isinstance(i, PAAQuestion) for i in paa.items)
    assert paa.items[0].question == "What exactly is coffee?"
    assert paa.items[0].answer_preview.startswith("Coffee is a brewed drink")

    assert related.status == SurfaceStatus.OK
    assert related.failure_category is None
    assert len(related.items) == 8
    assert [i.rank for i in related.items] == list(range(1, 9))
    assert all(isinstance(i, RelatedSearch) for i in related.items)
    assert related.items[0].query == "coffee near me"


def test_happy_path_zh_cn_preserves_unicode():
    body = (FIXTURES / "ok_zh_cn_sample.json").read_text()
    with _patched_get(_response(200, body)):
        result = fetch_serp_data("跑步鞋推荐", "zh-CN", "cn", api_key="fake-key")

    paa = result[SurfaceName.PAA]
    assert paa.status == SurfaceStatus.OK
    assert paa.items[0].question == "跑步鞋如何选择？"
    assert "支撑型" in paa.items[0].answer_preview  # encoding preserved

    related = result[SurfaceName.RELATED]
    assert related.status == SurfaceStatus.OK
    assert related.items[0].query == "跑步鞋女"


def test_happy_path_ja_jp_preserves_unicode():
    body = (FIXTURES / "ok_ja_jp_sample.json").read_text()
    with _patched_get(_response(200, body)):
        result = fetch_serp_data("ラーメン 東京", "ja", "jp", api_key="fake-key")

    paa = result[SurfaceName.PAA]
    assert paa.status == SurfaceStatus.OK
    assert paa.items[0].question.startswith("東京で")
    assert "一蘭" in paa.items[0].answer_preview

    related = result[SurfaceName.RELATED]
    assert related.status == SurfaceStatus.OK
    assert any("新宿" in r.query for r in related.items)


def test_paa_truncates_answer_preview_to_200_chars():
    long_snippet = "a" * 500
    body = (
        '{"related_questions": [{"question": "q?", "snippet": "'
        + long_snippet
        + '"}], "related_searches": [{"query": "other"}]}'
    )
    with _patched_get(_response(200, body)):
        result = fetch_serp_data("q", "en", "us", api_key="fake-key")
    assert len(result[SurfaceName.PAA].items[0].answer_preview) == 200


def test_related_caps_at_max_items():
    entries = ",".join(f'{{"query": "r{i}"}}' for i in range(1, 20))
    body = (
        '{"related_questions": [{"question": "q?", "snippet": "x"}], '
        '"related_searches": [' + entries + "]}"
    )
    with _patched_get(_response(200, body)):
        result = fetch_serp_data("q", "en", "us", api_key="fake-key")
    assert len(result[SurfaceName.RELATED].items) == 10
    assert result[SurfaceName.RELATED].items[-1].rank == 10


def test_related_skips_echo_of_original_query():
    body = (
        '{"related_questions": [{"question": "q?", "snippet": "x"}], '
        '"related_searches": [{"query": "coffee"}, {"query": "tea"}]}'
    )
    with _patched_get(_response(200, body)):
        result = fetch_serp_data("coffee", "en", "us", api_key="fake-key")
    related = result[SurfaceName.RELATED]
    assert [r.query for r in related.items] == ["tea"]


def test_related_dedups_case_insensitive():
    body = (
        '{"related_questions": [{"question": "q?", "snippet": "x"}], '
        '"related_searches": ['
        '{"query": "tea time"}, {"query": "Tea Time"}, {"query": "tea time"},'
        '{"query": "herbal tea"}'
        "]}"
    )
    with _patched_get(_response(200, body)):
        result = fetch_serp_data("coffee", "en", "us", api_key="fake-key")
    related = result[SurfaceName.RELATED]
    assert len(related.items) == 2
    assert related.items[0].query == "tea time"
    assert related.items[1].query == "herbal tea"


# --- empty / absent -----------------------------------------------------------


def test_missing_paa_block_yields_empty():
    body = (FIXTURES / "empty_no_paa.json").read_text()
    with _patched_get(_response(200, body)):
        result = fetch_serp_data(
            "xq42zzz nonexistent tail query", "en", "us", api_key="fake-key"
        )
    assert result[SurfaceName.PAA].status == SurfaceStatus.EMPTY
    assert result[SurfaceName.PAA].failure_category is None
    assert result[SurfaceName.PAA].items == []
    assert result[SurfaceName.RELATED].status == SurfaceStatus.OK


def test_both_surfaces_absent_yields_both_empty():
    body = (FIXTURES / "empty_both.json").read_text()
    with _patched_get(_response(200, body)):
        result = fetch_serp_data("xyzzzzz", "en", "us", api_key="fake-key")
    assert result[SurfaceName.PAA].status == SurfaceStatus.EMPTY
    assert result[SurfaceName.RELATED].status == SurfaceStatus.EMPTY


def test_paa_entry_missing_question_is_skipped():
    body = (
        '{"related_questions": [{"snippet": "no question field"}, '
        '{"question": "real?", "snippet": "s"}], '
        '"related_searches": [{"query": "r"}]}'
    )
    with _patched_get(_response(200, body)):
        result = fetch_serp_data("q", "en", "us", api_key="fake-key")
    paa = result[SurfaceName.PAA]
    assert len(paa.items) == 1
    assert paa.items[0].question == "real?"


# --- HTTP error paths ---------------------------------------------------------


def test_401_bad_key_flags_network_error():
    body = (FIXTURES / "error_bad_key.json").read_text()
    with _patched_get(_response(401, body)):
        result = fetch_serp_data("q", "en", "us", api_key="invalid")
    for surface in (SurfaceName.PAA, SurfaceName.RELATED):
        assert result[surface].status == SurfaceStatus.FAILED
        assert result[surface].failure_category == FailureCategory.NETWORK_ERROR


def test_403_flags_network_error():
    with _patched_get(_response(403, "{}")):
        result = fetch_serp_data("q", "en", "us", api_key="blocked")
    for surface in (SurfaceName.PAA, SurfaceName.RELATED):
        assert result[surface].failure_category == FailureCategory.NETWORK_ERROR


def test_429_flags_rate_limit():
    with _patched_get(_response(429, "{}")):
        result = fetch_serp_data("q", "en", "us", api_key="fake-key")
    for surface in (SurfaceName.PAA, SurfaceName.RELATED):
        assert result[surface].status == SurfaceStatus.FAILED
        assert result[surface].failure_category == FailureCategory.BLOCKED_RATE_LIMIT


def test_500_flags_network_error():
    with _patched_get(_response(500, "{}")):
        result = fetch_serp_data("q", "en", "us", api_key="fake-key")
    for surface in (SurfaceName.PAA, SurfaceName.RELATED):
        assert result[surface].failure_category == FailureCategory.NETWORK_ERROR


# --- SerpAPI-level error inside 200 payload ----------------------------------


def test_200_with_quota_exhausted_error_flags_rate_limit():
    body = (FIXTURES / "error_quota_exhausted.json").read_text()
    with _patched_get(_response(200, body)):
        result = fetch_serp_data("q", "en", "us", api_key="fake-key")
    for surface in (SurfaceName.PAA, SurfaceName.RELATED):
        assert result[surface].status == SurfaceStatus.FAILED
        assert result[surface].failure_category == FailureCategory.BLOCKED_RATE_LIMIT


def test_200_with_generic_error_flags_network_error():
    body = (FIXTURES / "error_generic_500.json").read_text()
    with _patched_get(_response(200, body)):
        result = fetch_serp_data("q", "en", "us", api_key="fake-key")
    for surface in (SurfaceName.PAA, SurfaceName.RELATED):
        assert result[surface].failure_category == FailureCategory.NETWORK_ERROR


# --- JSON / contract violations ----------------------------------------------


def test_malformed_json_flags_selector_not_found():
    with _patched_get(_response(200, "{not valid json")):
        result = fetch_serp_data("q", "en", "us", api_key="fake-key")
    for surface in (SurfaceName.PAA, SurfaceName.RELATED):
        assert result[surface].failure_category == FailureCategory.SELECTOR_NOT_FOUND


def test_html_body_flags_selector_not_found():
    """Cloudflare interstitial / HTML fallback."""
    html = "<html><body>Challenge</body></html>"
    with _patched_get(_response(200, html)):
        result = fetch_serp_data("q", "en", "us", api_key="fake-key")
    for surface in (SurfaceName.PAA, SurfaceName.RELATED):
        assert result[surface].failure_category == FailureCategory.SELECTOR_NOT_FOUND


def test_non_dict_root_flags_selector_not_found():
    with _patched_get(_response(200, '["unexpected array root"]')):
        result = fetch_serp_data("q", "en", "us", api_key="fake-key")
    for surface in (SurfaceName.PAA, SurfaceName.RELATED):
        assert result[surface].failure_category == FailureCategory.SELECTOR_NOT_FOUND


# --- network errors ----------------------------------------------------------


def test_timeout_flags_network_error():
    with _patched_get(requests.exceptions.ConnectTimeout("slow")):
        result = fetch_serp_data("q", "en", "us", api_key="fake-key")
    for surface in (SurfaceName.PAA, SurfaceName.RELATED):
        assert result[surface].failure_category == FailureCategory.NETWORK_ERROR


def test_connection_error_flags_network_error():
    with _patched_get(requests.exceptions.ConnectionError("dns fail")):
        result = fetch_serp_data("q", "en", "us", api_key="fake-key")
    for surface in (SurfaceName.PAA, SurfaceName.RELATED):
        assert result[surface].failure_category == FailureCategory.NETWORK_ERROR


def test_generic_requests_exception_flags_network_error():
    with _patched_get(requests.exceptions.RequestException("weird")):
        result = fetch_serp_data("q", "en", "us", api_key="fake-key")
    for surface in (SurfaceName.PAA, SurfaceName.RELATED):
        assert result[surface].failure_category == FailureCategory.NETWORK_ERROR


# --- locale → google_domain mapping ------------------------------------------


@pytest.mark.parametrize(
    "lang,country,expected",
    [
        ("en", "us", "google.com"),
        ("en", "US", "google.com"),
        ("zh", "cn", "google.com.hk"),
        ("zh-CN", "cn", "google.com.hk"),
        ("zh", "tw", "google.com.tw"),
        ("zh-TW", "tw", "google.com.tw"),
        ("ja", "jp", "google.co.jp"),
        ("ja-JP", "jp", "google.co.jp"),
        ("fr", "fr", "google.com"),  # unknown locale → default
        ("de", "de", "google.com"),
    ],
)
def test_locale_to_google_domain_mapping(lang, country, expected):
    assert _resolve_domain(lang, country) == expected


def test_domain_flows_through_to_request_params():
    captured = {}

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured["params"] = kwargs.get("params")
        return _response(
            200,
            '{"related_questions": [{"question":"q?","snippet":"s"}], '
            '"related_searches": [{"query":"r"}]}',
        )

    with patch("seoserper.fetchers.serp.requests.get", side_effect=fake_get):
        fetch_serp_data("coffee", "zh-CN", "cn", api_key="fake-key")
    assert captured["params"]["google_domain"] == "google.com.hk"
    assert captured["params"]["hl"] == "zh-CN"
    assert captured["params"]["gl"] == "cn"
    assert captured["params"]["engine"] == "google"
    assert captured["params"]["api_key"] == "fake-key"


# --- purity / no side effects ------------------------------------------------


def test_same_fixture_yields_identical_output_across_calls():
    body = (FIXTURES / "ok_en_us_coffee.json").read_text()
    with _patched_get(_response(200, body)):
        first = fetch_serp_data("coffee", "en", "us", api_key="fake-key")
    with _patched_get(_response(200, body)):
        second = fetch_serp_data("coffee", "en", "us", api_key="fake-key")
    assert first[SurfaceName.PAA].items == second[SurfaceName.PAA].items
    assert first[SurfaceName.RELATED].items == second[SurfaceName.RELATED].items


def test_no_retry_on_failure():
    """Single HTTP call per fetch_serp_data invocation."""
    with patch(
        "seoserper.fetchers.serp.requests.get",
        return_value=_response(429, "{}"),
    ) as m:
        fetch_serp_data("q", "en", "us", api_key="fake-key")
    assert m.call_count == 1


def test_return_shape_is_exactly_two_surfaces():
    body = (FIXTURES / "ok_en_us_coffee.json").read_text()
    with _patched_get(_response(200, body)):
        result = fetch_serp_data("coffee", "en", "us", api_key="fake-key")
    assert set(result.keys()) == {SurfaceName.PAA, SurfaceName.RELATED}
    assert SurfaceName.SUGGEST not in result


def test_all_return_values_are_parseresult_instances():
    body = (FIXTURES / "ok_en_us_coffee.json").read_text()
    with _patched_get(_response(200, body)):
        result = fetch_serp_data("coffee", "en", "us", api_key="fake-key")
    for surface in (SurfaceName.PAA, SurfaceName.RELATED):
        assert isinstance(result[surface], ParseResult)
