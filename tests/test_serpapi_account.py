"""Unit D: serpapi_account.fetch_quota_info + format_quota_caption."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from seoserper.serpapi_account import (
    QUOTA_LOW_THRESHOLD,
    fetch_quota_info,
    format_quota_caption,
    is_quota_low,
)


def _response(status_code: int = 200, body: str = "{}"):
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.text = body

    def _json():
        import json as _json_mod
        return _json_mod.loads(body)

    resp.json = _json
    return resp


def _patched_get(response_or_exc):
    if isinstance(response_or_exc, Exception):
        return patch(
            "seoserper.serpapi_account.requests.get", side_effect=response_or_exc
        )
    return patch(
        "seoserper.serpapi_account.requests.get", return_value=response_or_exc
    )


# --- fetch_quota_info --------------------------------------------------------


def test_happy_path_returns_dict():
    body = '{"plan_searches_left": 87, "searches_per_month": 100, "plan_id": "free"}'
    with _patched_get(_response(200, body)):
        info = fetch_quota_info("valid-key")
    assert info == {
        "plan_searches_left": 87,
        "searches_per_month": 100,
        "plan_id": "free",
    }


def test_none_key_returns_none():
    assert fetch_quota_info(None) is None


def test_empty_key_returns_none():
    assert fetch_quota_info("") is None


def test_401_returns_none():
    with _patched_get(_response(401, '{"error": "invalid"}')):
        assert fetch_quota_info("bad-key") is None


def test_500_returns_none():
    with _patched_get(_response(500, "")):
        assert fetch_quota_info("key") is None


def test_network_error_returns_none():
    with _patched_get(requests.exceptions.ConnectionError("dns")):
        assert fetch_quota_info("key") is None


def test_timeout_returns_none():
    with _patched_get(requests.exceptions.Timeout("slow")):
        assert fetch_quota_info("key") is None


def test_malformed_json_returns_none():
    with _patched_get(_response(200, "{not valid")):
        assert fetch_quota_info("key") is None


def test_non_dict_json_returns_none():
    with _patched_get(_response(200, "[1, 2, 3]")):
        assert fetch_quota_info("key") is None


# --- format_quota_caption ----------------------------------------------------


def test_caption_with_left_and_total():
    assert format_quota_caption({"plan_searches_left": 87, "searches_per_month": 100}) == "SerpAPI 剩余 87/100"


def test_caption_without_total():
    assert format_quota_caption({"plan_searches_left": 87}) == "SerpAPI 剩余 87"


def test_caption_none_info_returns_none():
    assert format_quota_caption(None) is None


def test_caption_missing_left_returns_none():
    assert format_quota_caption({"searches_per_month": 100}) is None


def test_caption_non_int_left_returns_none():
    assert format_quota_caption({"plan_searches_left": "lots"}) is None


def test_caption_zero_total_falls_back_to_left_only():
    assert format_quota_caption({"plan_searches_left": 0, "searches_per_month": 0}) == "SerpAPI 剩余 0"


# --- is_quota_low ------------------------------------------------------------


def test_is_quota_low_true_when_below_threshold():
    assert is_quota_low({"plan_searches_left": 5}) is True
    assert is_quota_low({"plan_searches_left": QUOTA_LOW_THRESHOLD - 1}) is True


def test_is_quota_low_false_at_threshold():
    assert is_quota_low({"plan_searches_left": QUOTA_LOW_THRESHOLD}) is False


def test_is_quota_low_false_above_threshold():
    assert is_quota_low({"plan_searches_left": 100}) is False


def test_is_quota_low_false_on_none():
    assert is_quota_low(None) is False


def test_is_quota_low_false_on_non_int_left():
    assert is_quota_low({"plan_searches_left": "many"}) is False


def test_is_quota_low_respects_custom_threshold():
    assert is_quota_low({"plan_searches_left": 50}, threshold=100) is True
    assert is_quota_low({"plan_searches_left": 50}, threshold=10) is False


def test_is_quota_low_zero_is_low():
    assert is_quota_low({"plan_searches_left": 0}) is True
