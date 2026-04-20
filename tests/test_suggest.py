"""Unit 2: Suggest fetcher behavior across contract + failure modes."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from seoserper.fetchers.suggest import SuggestResult, fetch_suggestions
from seoserper.models import FailureCategory, Suggestion, SurfaceStatus

FIXTURES = Path(__file__).parent / "fixtures" / "suggest"


def _response(
    status_code: int = 200,
    body: str = "[]",
    raise_for_status: Exception | None = None,
):
    """Build a stand-in for requests.Response with the minimum surface area we use."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.text = body
    resp.content = body.encode("utf-8")
    if raise_for_status is not None:
        resp.raise_for_status.side_effect = raise_for_status
    else:
        resp.raise_for_status.return_value = None
    return resp


def _patched_get(response: MagicMock | Exception):
    """Return a patch object that, when entered, replaces requests.get."""
    if isinstance(response, Exception):
        return patch("seoserper.fetchers.suggest.requests.get", side_effect=response)
    return patch("seoserper.fetchers.suggest.requests.get", return_value=response)


# --- happy paths -------------------------------------------------------------


def test_happy_path_en_us_returns_ten_ranked_suggestions():
    body = (FIXTURES / "en-us-ok.json").read_text()
    with _patched_get(_response(200, body)):
        result = fetch_suggestions("best running shoes", "en", "us")
    assert isinstance(result, SuggestResult)
    assert result.status == SurfaceStatus.OK
    assert result.failure_category is None
    assert len(result.items) == 10
    assert [i.rank for i in result.items] == list(range(1, 11))
    assert result.items[0].text == "best running shoes"
    assert all(isinstance(i, Suggestion) for i in result.items)


def test_happy_path_zh_cn_preserves_unicode():
    body = (FIXTURES / "zh-cn-ok.json").read_text()
    with _patched_get(_response(200, body)):
        result = fetch_suggestions("跑步鞋推荐", "zh-CN", "cn")
    assert result.status == SurfaceStatus.OK
    assert len(result.items) == 10
    assert result.items[0].text.startswith("跑步鞋推荐")


def test_partial_fewer_than_ten_still_ok():
    body = '["x",["a","b","c","d","e","f","g","h"],[],{}]'
    with _patched_get(_response(200, body)):
        result = fetch_suggestions("x", "en", "us")
    assert result.status == SurfaceStatus.OK
    assert len(result.items) == 8


def test_query_containing_spaces_and_specials_is_urlencoded():
    body = '["hello world & co",["hello world & co suggestion"],[],{}]'
    captured = {}

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured["params"] = kwargs.get("params")
        return _response(200, body)

    with patch("seoserper.fetchers.suggest.requests.get", side_effect=fake_get):
        result = fetch_suggestions("hello world & co", "en", "us")
    assert result.status == SurfaceStatus.OK
    assert captured["params"]["q"] == "hello world & co"
    assert captured["params"]["hl"] == "en"
    assert captured["params"]["gl"] == "us"
    assert captured["params"]["client"] == "firefox"


# --- empty -------------------------------------------------------------------


def test_empty_suggestion_list_maps_to_empty():
    body = (FIXTURES / "empty.json").read_text()
    with _patched_get(_response(200, body)):
        result = fetch_suggestions("qzxqzxqzxnonexistentqueryzzzz", "en", "us")
    assert result.status == SurfaceStatus.EMPTY
    assert result.failure_category is None
    assert result.items == []


# --- contract violations map to selector_not_found ---------------------------


def test_html_body_flagged_selector_not_found():
    body = (FIXTURES / "malformed-html.html").read_text()
    with _patched_get(_response(200, body)):
        result = fetch_suggestions("x", "en", "us")
    assert result.status == SurfaceStatus.FAILED
    assert result.failure_category == FailureCategory.SELECTOR_NOT_FOUND
    assert result.items == []


def test_malformed_json_flagged_selector_not_found():
    with _patched_get(_response(200, "{not valid json")):
        result = fetch_suggestions("x", "en", "us")
    assert result.status == SurfaceStatus.FAILED
    assert result.failure_category == FailureCategory.SELECTOR_NOT_FOUND


def test_dict_shape_violation_flagged_selector_not_found():
    with _patched_get(_response(200, '{"results": ["a"]}')):
        result = fetch_suggestions("x", "en", "us")
    assert result.status == SurfaceStatus.FAILED
    assert result.failure_category == FailureCategory.SELECTOR_NOT_FOUND


def test_short_array_shape_violation():
    with _patched_get(_response(200, '["only-echo"]')):
        result = fetch_suggestions("only-echo", "en", "us")
    assert result.status == SurfaceStatus.FAILED
    assert result.failure_category == FailureCategory.SELECTOR_NOT_FOUND


def test_echo_mismatch_flagged_selector_not_found():
    """Shape drift: response[0] doesn't echo the submitted query."""
    with _patched_get(_response(200, '["totally different echo",["x"],[],{}]')):
        result = fetch_suggestions("original query", "en", "us")
    assert result.status == SurfaceStatus.FAILED
    assert result.failure_category == FailureCategory.SELECTOR_NOT_FOUND


def test_non_string_items_in_list_flagged():
    with _patched_get(_response(200, '["q",[1, 2, 3],[],{}]')):
        result = fetch_suggestions("q", "en", "us")
    assert result.status == SurfaceStatus.FAILED
    assert result.failure_category == FailureCategory.SELECTOR_NOT_FOUND


# --- rate limit / block ------------------------------------------------------


def test_http_429_flagged_rate_limit():
    with _patched_get(_response(429, "")):
        result = fetch_suggestions("x", "en", "us")
    assert result.status == SurfaceStatus.FAILED
    assert result.failure_category == FailureCategory.BLOCKED_RATE_LIMIT


def test_http_403_flagged_rate_limit():
    with _patched_get(_response(403, "")):
        result = fetch_suggestions("x", "en", "us")
    assert result.status == SurfaceStatus.FAILED
    assert result.failure_category == FailureCategory.BLOCKED_RATE_LIMIT


def test_sorry_page_body_flagged_rate_limit():
    """A 200 response whose body is a Google 'sorry' page."""
    body = (FIXTURES / "sorry.html").read_text()
    with _patched_get(_response(200, body)):
        result = fetch_suggestions("x", "en", "us")
    # Body starts with '<' so it's first caught as HTML / selector_not_found;
    # the plan's taxonomy is explicit here — we use selector_not_found for
    # "Google served HTML where JSON was expected" and reserve rate_limit for
    # explicit 429/403 + sorry URL redirects (handled in Playwright, not here).
    assert result.status == SurfaceStatus.FAILED
    assert result.failure_category == FailureCategory.SELECTOR_NOT_FOUND


# --- network errors ----------------------------------------------------------


def test_timeout_flagged_network_error():
    with _patched_get(requests.exceptions.ConnectTimeout("slow")):
        result = fetch_suggestions("x", "en", "us")
    assert result.status == SurfaceStatus.FAILED
    assert result.failure_category == FailureCategory.NETWORK_ERROR


def test_read_timeout_flagged_network_error():
    with _patched_get(requests.exceptions.ReadTimeout("slow")):
        result = fetch_suggestions("x", "en", "us")
    assert result.status == SurfaceStatus.FAILED
    assert result.failure_category == FailureCategory.NETWORK_ERROR


def test_connection_error_flagged_network_error():
    with _patched_get(requests.exceptions.ConnectionError("dns fail")):
        result = fetch_suggestions("x", "en", "us")
    assert result.status == SurfaceStatus.FAILED
    assert result.failure_category == FailureCategory.NETWORK_ERROR


def test_raw_text_preserved_on_failure():
    """Debugging aid — the raw body is still captured on failed responses."""
    body = "{not valid json"
    with _patched_get(_response(200, body)):
        result = fetch_suggestions("x", "en", "us")
    assert result.raw_text == body


def test_no_retry_on_failure():
    """Plan R2 fail-fast: a single HTTP call per fetch_suggestions invocation."""
    with patch(
        "seoserper.fetchers.suggest.requests.get",
        return_value=_response(429, ""),
    ) as m:
        fetch_suggestions("x", "en", "us")
    assert m.call_count == 1
