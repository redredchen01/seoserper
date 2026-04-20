"""Unit B: SerpAPI response cache CRUD + wrapper behavior."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from seoserper.fetchers.serp_cache import (
    _cache_key,
    _result_is_cacheable,
    fetch_serp_data_cached,
)
from seoserper.models import (
    FailureCategory,
    ParseResult,
    SurfaceName,
    SurfaceStatus,
)
from seoserper.storage import cache_clear_all, cache_get, cache_prune, cache_put

FIXTURES = Path(__file__).parent / "fixtures" / "serp"


def _response(status_code: int = 200, body: str = "{}"):
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.text = body
    resp.content = body.encode("utf-8")
    return resp


def _patched_get(response):
    return patch("seoserper.fetchers.serp.requests.get", return_value=response)


# --- cache_key + cacheability helpers ----------------------------------------


def test_cache_key_shape():
    assert _cache_key("coffee", "en", "us") == "coffee|en|us"
    assert _cache_key("跑步鞋", "zh-CN", "cn") == "跑步鞋|zh-CN|cn"


def test_cacheable_all_ok():
    result = {
        SurfaceName.PAA: ParseResult(status=SurfaceStatus.OK, items=[]),
        SurfaceName.RELATED: ParseResult(status=SurfaceStatus.OK, items=[]),
    }
    assert _result_is_cacheable(result)


def test_cacheable_mixed_ok_empty():
    result = {
        SurfaceName.PAA: ParseResult(status=SurfaceStatus.EMPTY),
        SurfaceName.RELATED: ParseResult(status=SurfaceStatus.OK, items=[]),
    }
    assert _result_is_cacheable(result)


def test_not_cacheable_one_failed():
    result = {
        SurfaceName.PAA: ParseResult(
            status=SurfaceStatus.FAILED,
            failure_category=FailureCategory.BLOCKED_RATE_LIMIT,
        ),
        SurfaceName.RELATED: ParseResult(status=SurfaceStatus.OK, items=[]),
    }
    assert not _result_is_cacheable(result)


# --- storage CRUD ------------------------------------------------------------


def test_cache_put_and_get_round_trip(db_path):
    payload = {"foo": "bar", "related_questions": [{"question": "q?"}]}
    cache_put("coffee|en|us", payload, db_path=db_path)
    got = cache_get("coffee|en|us", ttl_seconds=3600, db_path=db_path)
    assert got == payload


def test_cache_get_miss_returns_none(db_path):
    assert cache_get("never-seen", ttl_seconds=3600, db_path=db_path) is None


def test_cache_get_stale_returns_none(db_path):
    cache_put("stale|en|us", {"foo": "old"}, db_path=db_path)
    # Backdate the row so it appears stale with TTL=1s.
    from seoserper.storage import get_connection
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE serp_cache SET created_at = datetime('now', '-2 seconds') "
            "WHERE cache_key = ?",
            ("stale|en|us",),
        )
    assert cache_get("stale|en|us", ttl_seconds=1, db_path=db_path) is None


def test_cache_put_overwrites_existing(db_path):
    cache_put("k|en|us", {"v": 1}, db_path=db_path)
    cache_put("k|en|us", {"v": 2}, db_path=db_path)
    assert cache_get("k|en|us", ttl_seconds=3600, db_path=db_path) == {"v": 2}


def test_cache_put_with_ttl_prunes_stale_rows(db_path):
    cache_put("old|en|us", {"a": 1}, db_path=db_path)
    from seoserper.storage import get_connection
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE serp_cache SET created_at = datetime('now', '-100 seconds') "
            "WHERE cache_key = ?",
            ("old|en|us",),
        )
    # Put a new row with aggressive TTL — old row should be pruned.
    cache_put("new|en|us", {"b": 2}, db_path=db_path, ttl_seconds=10)
    assert cache_get("old|en|us", ttl_seconds=3600, db_path=db_path) is None
    assert cache_get("new|en|us", ttl_seconds=3600, db_path=db_path) == {"b": 2}


def test_cache_prune_drops_only_stale(db_path):
    cache_put("fresh|en|us", {"v": 1}, db_path=db_path)
    cache_put("stale|en|us", {"v": 2}, db_path=db_path)
    from seoserper.storage import get_connection
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE serp_cache SET created_at = datetime('now', '-100 seconds') "
            "WHERE cache_key = ?",
            ("stale|en|us",),
        )
    dropped = cache_prune(10, db_path=db_path)
    assert dropped == 1
    assert cache_get("fresh|en|us", ttl_seconds=3600, db_path=db_path) is not None
    assert cache_get("stale|en|us", ttl_seconds=3600, db_path=db_path) is None


def test_cache_clear_all_empties_table(db_path):
    cache_put("a|en|us", {}, db_path=db_path)
    cache_put("b|en|us", {}, db_path=db_path)
    cleared = cache_clear_all(db_path=db_path)
    assert cleared == 2
    assert cache_get("a|en|us", ttl_seconds=3600, db_path=db_path) is None


def test_cache_get_malformed_json_returns_none(db_path):
    from seoserper.storage import get_connection
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO serp_cache (cache_key, response_json) VALUES (?, ?)",
            ("corrupt|en|us", "{not valid"),
        )
    assert cache_get("corrupt|en|us", ttl_seconds=3600, db_path=db_path) is None


# --- fetch_serp_data_cached wrapper behavior ---------------------------------


def test_wrapper_miss_fetches_and_stores(db_path):
    body = (FIXTURES / "ok_en_us_coffee.json").read_text()
    with _patched_get(_response(200, body)) as m:
        result = fetch_serp_data_cached(
            "coffee", "en", "us", api_key="fake-key", db_path=db_path
        )
    assert result[SurfaceName.PAA].status == SurfaceStatus.OK
    assert m.call_count == 1
    # Row is now in cache.
    cached = cache_get("coffee|en|us", ttl_seconds=3600, db_path=db_path)
    assert cached is not None
    assert "related_questions" in cached


def test_wrapper_hit_skips_http(db_path):
    body = (FIXTURES / "ok_en_us_coffee.json").read_text()
    # Pre-populate cache.
    cache_put("coffee|en|us", json.loads(body), db_path=db_path)
    with patch("seoserper.fetchers.serp.requests.get") as m:
        result = fetch_serp_data_cached(
            "coffee", "en", "us", api_key="fake-key", db_path=db_path
        )
    assert m.call_count == 0  # cache hit — no HTTP
    assert result[SurfaceName.PAA].status == SurfaceStatus.OK
    assert result[SurfaceName.RELATED].status == SurfaceStatus.OK


def test_wrapper_different_locale_misses(db_path):
    body = (FIXTURES / "ok_en_us_coffee.json").read_text()
    cache_put("coffee|en|us", json.loads(body), db_path=db_path)
    # Different locale → miss.
    with _patched_get(_response(200, body)) as m:
        fetch_serp_data_cached(
            "coffee", "zh-CN", "cn", api_key="fake-key", db_path=db_path
        )
    assert m.call_count == 1


def test_wrapper_failed_response_not_cached(db_path):
    with _patched_get(_response(429, "{}")):
        fetch_serp_data_cached(
            "qqqq", "en", "us", api_key="fake-key", db_path=db_path
        )
    # Rate-limit failure must not populate cache.
    assert cache_get("qqqq|en|us", ttl_seconds=3600, db_path=db_path) is None


def test_wrapper_ttl_expiration_triggers_refetch(db_path):
    body = (FIXTURES / "ok_en_us_coffee.json").read_text()
    cache_put("coffee|en|us", json.loads(body), db_path=db_path)
    # Backdate
    from seoserper.storage import get_connection
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE serp_cache SET created_at = datetime('now', '-200 seconds') "
            "WHERE cache_key = ?",
            ("coffee|en|us",),
        )
    with _patched_get(_response(200, body)) as m:
        fetch_serp_data_cached(
            "coffee", "en", "us", api_key="fake-key",
            db_path=db_path, ttl_seconds=60,
        )
    assert m.call_count == 1  # stale → refetched


def test_wrapper_empty_both_surfaces_is_cacheable(db_path):
    """Plan decision: EMPTY counts as cacheable alongside OK."""
    body = (FIXTURES / "empty_both.json").read_text()
    with _patched_get(_response(200, body)):
        fetch_serp_data_cached(
            "xyzzzzz", "en", "us", api_key="fake-key", db_path=db_path
        )
    assert cache_get("xyzzzzz|en|us", ttl_seconds=3600, db_path=db_path) is not None


def test_wrapper_non_ok_surface_not_cached(db_path):
    """Synthetic: payload shape is valid but one surface fails — don't cache."""
    # Construct a payload that parses to one OK + one FAILED surface is tricky
    # at the extract level (extract_surfaces only returns OK/EMPTY given a
    # structurally valid payload). Instead, use a payload with missing/
    # malformed surface keys that the extractor would treat as EMPTY — which
    # IS cacheable. So the realistic non-ok-at-extract case is a structural
    # violation caught earlier in fetch_serp_raw (which returns failure and
    # is already covered by test_wrapper_failed_response_not_cached).
    # This scenario confirms the cacheable rule is strictly enforced.
    result = {
        SurfaceName.PAA: ParseResult(status=SurfaceStatus.OK, items=[]),
        SurfaceName.RELATED: ParseResult(
            status=SurfaceStatus.FAILED,
            failure_category=FailureCategory.SELECTOR_NOT_FOUND,
        ),
    }
    assert not _result_is_cacheable(result)


def test_wrapper_second_hit_within_ttl_does_not_prune_self(db_path):
    """Put-then-put of same key should leave the fresh row readable."""
    body = (FIXTURES / "ok_en_us_coffee.json").read_text()
    with _patched_get(_response(200, body)):
        fetch_serp_data_cached(
            "coffee", "en", "us", api_key="fake-key",
            db_path=db_path, ttl_seconds=3600,
        )
    # Hit
    with patch("seoserper.fetchers.serp.requests.get") as m:
        result = fetch_serp_data_cached(
            "coffee", "en", "us", api_key="fake-key",
            db_path=db_path, ttl_seconds=3600,
        )
    assert m.call_count == 0
    assert result[SurfaceName.PAA].status == SurfaceStatus.OK
