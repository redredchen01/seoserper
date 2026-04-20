"""Config surface: SERPAPI_KEY env-var coercion + legacy flag removal."""

from __future__ import annotations

import importlib

from seoserper import config


def _reload_with_env(monkeypatch, value: str | None):
    if value is None:
        monkeypatch.delenv("SERPAPI_KEY", raising=False)
    else:
        monkeypatch.setenv("SERPAPI_KEY", value)
    return importlib.reload(config)


def test_unset_env_yields_none(monkeypatch):
    reloaded = _reload_with_env(monkeypatch, None)
    assert reloaded.SERPAPI_KEY is None


def test_nonempty_value_round_trips(monkeypatch):
    reloaded = _reload_with_env(monkeypatch, "abc123")
    assert reloaded.SERPAPI_KEY == "abc123"


def test_empty_string_yields_none(monkeypatch):
    reloaded = _reload_with_env(monkeypatch, "")
    assert reloaded.SERPAPI_KEY is None


def test_whitespace_only_yields_none(monkeypatch):
    reloaded = _reload_with_env(monkeypatch, "   ")
    assert reloaded.SERPAPI_KEY is None


def test_value_with_leading_trailing_whitespace_is_stripped(monkeypatch):
    reloaded = _reload_with_env(monkeypatch, "  mykey  ")
    assert reloaded.SERPAPI_KEY == "mykey"


def test_coerce_key_helper_direct():
    # Direct helper unit-test — no module reload, no env mutation.
    assert config._coerce_key(None) is None
    assert config._coerce_key("") is None
    assert config._coerce_key("   ") is None
    assert config._coerce_key("abc") == "abc"
    assert config._coerce_key("  abc  ") == "abc"
    assert config._coerce_key("\t\nkey\n\t") == "key"


def test_legacy_enable_serp_render_is_removed():
    # Forcing function: any stale caller of the Playwright-era flag breaks
    # loudly at attribute access, not silently with a wrong boolean. Unit 3
    # (engine) and Unit 4 (UI) are the cleanup downstream; this test guards
    # the cleanup completeness of Unit 1.
    assert not hasattr(config, "ENABLE_SERP_RENDER")
    assert not hasattr(config, "_coerce_flag")


def test_serpapi_url_constant_shape():
    assert config.SERPAPI_URL == "https://serpapi.com/search.json"
