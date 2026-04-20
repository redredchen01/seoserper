"""Config surface: ENABLE_SERP_RENDER env-var coercion."""

from __future__ import annotations

import importlib

import pytest

from seoserper import config


def _reload_with_env(monkeypatch, value: str | None):
    if value is None:
        monkeypatch.delenv("SEOSERPER_ENABLE_SERP_RENDER", raising=False)
    else:
        monkeypatch.setenv("SEOSERPER_ENABLE_SERP_RENDER", value)
    return importlib.reload(config)


def test_unset_env_yields_false(monkeypatch):
    reloaded = _reload_with_env(monkeypatch, None)
    assert reloaded.ENABLE_SERP_RENDER is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "True", "yes", "YES", "on", "ON"])
def test_truthy_values_yield_true(monkeypatch, value):
    reloaded = _reload_with_env(monkeypatch, value)
    assert reloaded.ENABLE_SERP_RENDER is True


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "", "maybe", "2", "no", "off"])
def test_falsy_values_yield_false(monkeypatch, value):
    reloaded = _reload_with_env(monkeypatch, value)
    assert reloaded.ENABLE_SERP_RENDER is False


def test_coerce_flag_helper_handles_whitespace():
    # Direct helper test — doesn't require module reload
    assert config._coerce_flag("  true  ") is True
    assert config._coerce_flag(" 1 ") is True
    assert config._coerce_flag("") is False
    assert config._coerce_flag(None) is False
