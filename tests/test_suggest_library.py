"""Plan 005 Unit 3: seoserper.suggest.get_suggestions library core."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest
import requests

from seoserper import config
from seoserper.fetchers.suggest import SuggestResult
from seoserper.models import FailureCategory, SurfaceStatus
from seoserper.suggest import (
    _cache_key,
    _normalize_cache_form,
    _q_hash,
    _validate_and_strip,
    get_suggestions,
)


# --- shared helpers ----------------------------------------------------------


def _ok_response(items: list[str]) -> MagicMock:
    """Mock requests.Response matching what the raw fetcher expects for OK."""
    import json as _json

    body = _json.dumps(["ECHO", items, [], {}])
    resp = MagicMock(spec=requests.Response)
    resp.status_code = 200
    resp.text = body
    resp.content = body.encode("utf-8")
    return resp


def _ok_response_for(query: str, items: list[str]) -> MagicMock:
    """OK response whose echo exactly matches the query (case-insensitive)."""
    import json as _json

    body = _json.dumps([query, items, [], {}])
    resp = MagicMock(spec=requests.Response)
    resp.status_code = 200
    resp.text = body
    resp.content = body.encode("utf-8")
    return resp


def _empty_response_for(query: str) -> MagicMock:
    import json as _json

    body = _json.dumps([query, [], [], {}])
    resp = MagicMock(spec=requests.Response)
    resp.status_code = 200
    resp.text = body
    resp.content = body.encode("utf-8")
    return resp


def _rate_limit_response() -> MagicMock:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = 429
    resp.text = "rate limited"
    resp.content = b"rate limited"
    return resp


def _html_redirect_response() -> MagicMock:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = 200
    resp.text = "<html>sorry</html>"
    resp.content = b"<html>sorry</html>"
    return resp


def _patched_get(*, return_value=None, side_effect=None):
    return patch(
        "seoserper.fetchers.suggest.requests.get",
        return_value=return_value,
        side_effect=side_effect,
    )


def _patched_sleep():
    """Patch the library-local time.sleep so tests don't wait the 200ms delay."""
    return patch("seoserper.suggest.time.sleep")


@pytest.fixture(autouse=True)
def _use_tmp_db(db_path, monkeypatch):
    """Route library cache reads/writes to the per-test temp DB."""
    monkeypatch.setattr(config, "DB_PATH", db_path)


# --- pure helpers ------------------------------------------------------------


def test_validate_and_strip_preserves_case():
    assert _validate_and_strip("  iPhone 15  ") == "iPhone 15"


def test_validate_and_strip_rejects_empty():
    with pytest.raises(ValueError, match="empty"):
        _validate_and_strip("   ")


def test_validate_and_strip_rejects_too_long():
    with pytest.raises(ValueError, match="exceeds"):
        _validate_and_strip("a" * (config.SUGGEST_Q_MAX_LENGTH + 1))


def test_validate_and_strip_rejects_control_chars():
    with pytest.raises(ValueError, match="control"):
        _validate_and_strip("hello\x00world")


def test_validate_and_strip_rejects_non_string():
    with pytest.raises(ValueError, match="must be str"):
        _validate_and_strip(123)  # type: ignore[arg-type]


def test_normalize_cache_form_lowercases_and_folds():
    assert _normalize_cache_form("iPhone") == "iphone"
    assert _normalize_cache_form("ＡＢＣ") == "abc"
    assert _normalize_cache_form("  Hello   World  ") == "hello world"


def test_cache_key_shape():
    assert _cache_key("coffee", "en", "us") == "google|coffee|en|us"


def test_q_hash_is_8_hex_chars():
    h = _q_hash("coffee")
    assert len(h) == 8
    assert all(c in "0123456789abcdef" for c in h)


# --- happy path --------------------------------------------------------------


def test_fresh_call_ok_populates_extended_fields(db_path):
    with _patched_get(return_value=_ok_response_for("coffee", ["coffee shop", "coffee bean"])):
        result = get_suggestions("coffee", "en", "US", limit=10)

    assert result.status is SurfaceStatus.OK
    assert result.provider_used == "google"
    assert result.from_cache is False
    assert result.normalized_query == "coffee"
    assert result.latency_ms >= 0
    assert result.warnings == []
    assert [it.text for it in result.items] == ["coffee shop", "coffee bean"]


def test_second_call_hits_cache(db_path):
    with _patched_get(return_value=_ok_response_for("coffee", ["a", "b"])) as mock_get:
        first = get_suggestions("coffee", "en", "US")
        second = get_suggestions("coffee", "en", "US")

    assert first.from_cache is False
    assert second.from_cache is True
    assert second.provider_used == "cache"
    assert [it.text for it in second.items] == ["a", "b"]
    assert mock_get.call_count == 1  # second call did NOT hit upstream


def test_fresh_true_skips_cache_read(db_path):
    with _patched_get(return_value=_ok_response_for("coffee", ["x"])) as mock_get:
        get_suggestions("coffee", "en", "US")
        get_suggestions("coffee", "en", "US", fresh=True)

    assert mock_get.call_count == 2


def test_limit_is_post_read_slice(db_path):
    items = ["a", "b", "c", "d", "e"]
    with _patched_get(return_value=_ok_response_for("coffee", items)) as mock_get:
        r10 = get_suggestions("coffee", "en", "US", limit=10)
        r2 = get_suggestions("coffee", "en", "US", limit=2)

    assert len(r10.items) == 5  # upstream gave 5, limit=10 returns all 5
    assert len(r2.items) == 2  # limit=2 slices
    assert mock_get.call_count == 1  # both calls share one cache row


def test_upstream_sees_raw_case_not_lowercased(db_path):
    with _patched_get(return_value=_ok_response_for("iPhone", ["iPhone 15"])) as mock_get:
        get_suggestions("iPhone", "en", "US")

    sent_q = mock_get.call_args.kwargs["params"]["q"]
    assert sent_q == "iPhone"  # case preserved on the wire


def test_nfkc_fullwidth_and_ascii_share_cache(db_path):
    # Upstream call for fullwidth input.
    with _patched_get(return_value=_ok_response_for("ＡＢＣ", ["abc x"])) as mock_get:
        r1 = get_suggestions("ＡＢＣ", "en", "US")
        r2 = get_suggestions("abc", "en", "US")

    # Fullwidth collapses to lowercase ascii in the normalized form,
    # so both callers hit the same cache row on the second call.
    assert r1.from_cache is False
    assert r2.from_cache is True
    assert mock_get.call_count == 1


def test_empty_upstream_is_success_not_failure(db_path):
    with _patched_get(return_value=_empty_response_for("weirdquery")) as mock_get:
        first = get_suggestions("weirdquery", "en", "US")
        second = get_suggestions("weirdquery", "en", "US")

    assert first.status is SurfaceStatus.EMPTY
    assert first.provider_used == "google"
    assert first.items == []
    # EMPTY is cached within the empty TTL window → second call serves from cache.
    assert second.from_cache is True
    assert second.status is SurfaceStatus.EMPTY
    assert mock_get.call_count == 1


# --- validation errors -------------------------------------------------------


def test_invalid_hl_raises(db_path):
    with pytest.raises(ValueError, match="hl="):
        get_suggestions("q", "de", "US")


def test_limit_out_of_range_raises(db_path):
    with pytest.raises(ValueError, match="limit"):
        get_suggestions("q", "en", "US", limit=21)
    with pytest.raises(ValueError, match="limit"):
        get_suggestions("q", "en", "US", limit=0)


def test_limit_bool_rejected(db_path):
    # bool is a subclass of int in Python — don't let True/False sneak through.
    with pytest.raises(ValueError, match="limit"):
        get_suggestions("q", "en", "US", limit=True)  # type: ignore[arg-type]


def test_fresh_wrong_type_raises(db_path):
    with pytest.raises(ValueError, match="fresh"):
        get_suggestions("q", "en", "US", fresh="yes")  # type: ignore[arg-type]


# --- retry behavior ----------------------------------------------------------


def _network_error_response() -> MagicMock:
    """500 response → raw fetcher maps to FAILED/NETWORK_ERROR."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = 500
    resp.text = "server error"
    resp.content = b"server error"
    return resp


def test_network_error_retry_recovers(db_path):
    recovered = _ok_response_for("coffee", ["coffee shop"])
    with _patched_get(
        side_effect=[_network_error_response(), recovered]
    ) as mock_get, _patched_sleep() as mock_sleep:
        result = get_suggestions("coffee", "en", "US", retry=True)

    assert result.status is SurfaceStatus.OK
    assert result.provider_used == "google"
    assert mock_get.call_count == 2
    # The single retry called sleep once.
    assert mock_sleep.call_count == 1


def test_network_error_retry_exhausts_returns_degraded(db_path):
    with _patched_get(
        side_effect=[_network_error_response(), _network_error_response()]
    ) as mock_get, _patched_sleep():
        result = get_suggestions("coffee", "en", "US", retry=True)

    assert result.status is SurfaceStatus.FAILED
    assert result.provider_used == "none"
    assert result.items == []
    assert result.warnings == ["upstream_unavailable"]
    assert mock_get.call_count == 2

    # FAILED is never cached — next call with retry=False still hits upstream.
    with _patched_get(return_value=_network_error_response()) as mock_get2:
        get_suggestions("coffee", "en", "US", retry=False)
    assert mock_get2.call_count == 1


def test_retry_false_no_retry(db_path):
    with _patched_get(return_value=_network_error_response()) as mock_get, _patched_sleep() as mock_sleep:
        result = get_suggestions("coffee", "en", "US", retry=False)

    assert result.status is SurfaceStatus.FAILED
    assert mock_get.call_count == 1
    assert mock_sleep.call_count == 0


def test_rate_limit_no_retry(db_path):
    with _patched_get(return_value=_rate_limit_response()) as mock_get, _patched_sleep() as mock_sleep:
        result = get_suggestions("coffee", "en", "US", retry=True)

    assert result.status is SurfaceStatus.FAILED
    assert mock_get.call_count == 1
    assert mock_sleep.call_count == 0


def test_selector_not_found_no_retry(db_path):
    with _patched_get(return_value=_html_redirect_response()) as mock_get, _patched_sleep() as mock_sleep:
        result = get_suggestions("coffee", "en", "US", retry=True)

    assert result.status is SurfaceStatus.FAILED
    assert mock_get.call_count == 1
    assert mock_sleep.call_count == 0


def test_unexpected_exception_is_caught(db_path):
    # Simulate a bug mid-stack — fetcher raises something the raw fetcher
    # didn't catch. Library must not propagate.
    with patch("seoserper.suggest._google_fetch_with_retry", side_effect=RuntimeError("boom")):
        result = get_suggestions("coffee", "en", "US")

    assert result.status is SurfaceStatus.FAILED
    assert result.provider_used == "none"
    assert result.warnings == ["upstream_error"]


# --- logging -----------------------------------------------------------------


def test_log_contains_q_hash_not_raw_q(db_path, caplog):
    with _patched_get(return_value=_ok_response_for("secret term", ["x"])):
        with caplog.at_level(logging.INFO, logger="seoserper.suggest"):
            get_suggestions("secret term", "en", "US")

    rec = next(r for r in caplog.records if r.name == "seoserper.suggest")
    assert rec.message == "suggest_call"
    assert rec.q_hash == _q_hash("secret term")
    assert rec.provider_used == "google"
    assert rec.from_cache is False
    # Raw query is never persisted to the LogRecord.
    assert not any("secret term" in str(v) for v in rec.__dict__.values())


# --- cache invariants --------------------------------------------------------


def test_failed_is_never_cached(db_path):
    from seoserper.storage import suggest_cache_get

    with _patched_get(return_value=_rate_limit_response()), _patched_sleep():
        get_suggestions("coffee", "en", "US")

    # No row was written.
    key = _cache_key("coffee", "en", "us")
    assert suggest_cache_get(key, 43200, 300) is None
