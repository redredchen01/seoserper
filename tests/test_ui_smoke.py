"""Unit 7 / 4: Streamlit UI smoke — boot, preflight branches, simple flow.

These are AppTest-based smoke checks; no real browser involved. The engine
and render thread are monkey-patched so we never touch Playwright.

Behavior differs between Suggest-only (flag=False, default) and full mode
(flag=True). Each test explicitly sets the flag via monkeypatch.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

APP_PATH = str(Path(__file__).parent.parent / "app.py")


def _patch_preflight(monkeypatch, ok: bool):
    from seoserper.core import render
    msg = "" if ok else "Run: playwright install chromium"
    monkeypatch.setattr(render, "preflight", lambda: (ok, msg))


def _set_flag(monkeypatch, enabled: bool):
    from seoserper import config
    monkeypatch.setattr(config, "ENABLE_SERP_RENDER", enabled)


def _isolate_db(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("SEOSERPER_DB", str(tmp_path / "ui.db"))


# --- full mode (ENABLE_SERP_RENDER=True) -------------------------------------


def test_full_mode_boots_and_renders_title(monkeypatch, tmp_path):
    _set_flag(monkeypatch, True)
    _patch_preflight(monkeypatch, True)
    _isolate_db(monkeypatch, tmp_path)
    at = AppTest.from_file(APP_PATH).run(timeout=10)
    assert not at.exception
    assert any("SEOSERPER" in t.value for t in at.title)


def test_full_mode_preflight_failure_hard_blocks(monkeypatch, tmp_path):
    _set_flag(monkeypatch, True)
    _patch_preflight(monkeypatch, False)
    _isolate_db(monkeypatch, tmp_path)
    at = AppTest.from_file(APP_PATH).run(timeout=10)
    assert not at.exception
    errors = [e.value for e in at.error]
    assert any("playwright install chromium" in e for e in errors), errors


def test_full_mode_no_top_notice(monkeypatch, tmp_path):
    """In full mode, the Suggest-only top-of-page notice must NOT appear."""
    _set_flag(monkeypatch, True)
    _patch_preflight(monkeypatch, True)
    _isolate_db(monkeypatch, tmp_path)
    at = AppTest.from_file(APP_PATH).run(timeout=10)
    captions = [c.value for c in at.caption]
    assert not any("Suggest-only 模式" in c for c in captions), captions


# --- suggest-only mode (ENABLE_SERP_RENDER=False, default) -------------------


def test_suggest_only_boots_and_renders_title(monkeypatch, tmp_path):
    _set_flag(monkeypatch, False)
    _patch_preflight(monkeypatch, True)
    _isolate_db(monkeypatch, tmp_path)
    at = AppTest.from_file(APP_PATH).run(timeout=10)
    assert not at.exception
    assert any("SEOSERPER" in t.value for t in at.title)


def test_suggest_only_shows_top_notice(monkeypatch, tmp_path):
    _set_flag(monkeypatch, False)
    _patch_preflight(monkeypatch, True)
    _isolate_db(monkeypatch, tmp_path)
    at = AppTest.from_file(APP_PATH).run(timeout=10)
    captions = [c.value for c in at.caption]
    assert any("Suggest-only 模式" in c for c in captions), captions
    # Must NOT embed the config identifier
    assert not any("ENABLE_SERP_RENDER" in c for c in captions), captions


def test_suggest_only_preflight_failure_soft_warning(monkeypatch, tmp_path):
    """Suggest has no Chromium dep — preflight failure degrades to a notice."""
    _set_flag(monkeypatch, False)
    _patch_preflight(monkeypatch, False)
    _isolate_db(monkeypatch, tmp_path)
    at = AppTest.from_file(APP_PATH).run(timeout=10)
    assert not at.exception
    # No hard-block error (that's full-mode behavior)
    errors = [e.value for e in at.error]
    assert not any("playwright install chromium" in e for e in errors), errors
    # Merged caption mentions both the mode AND Playwright missing
    captions = [c.value for c in at.caption]
    assert any(
        "Suggest-only 模式" in c and "Playwright 未安装但当前模式无需" in c
        for c in captions
    ), captions
    # Submit button still renders (not disabled / absent)
    submit_buttons = [b for b in at.button if b.label == "Submit"]
    assert len(submit_buttons) == 1


# --- common / unchanged ------------------------------------------------------


def test_app_shows_empty_history_message(monkeypatch, tmp_path):
    _set_flag(monkeypatch, False)
    _patch_preflight(monkeypatch, True)
    _isolate_db(monkeypatch, tmp_path)
    at = AppTest.from_file(APP_PATH).run(timeout=10)
    assert not at.exception
    captions = [c.value for c in at.sidebar.caption]
    assert any("暂无历史" in c for c in captions), captions


def test_app_renders_input_row(monkeypatch, tmp_path):
    _set_flag(monkeypatch, False)
    _patch_preflight(monkeypatch, True)
    _isolate_db(monkeypatch, tmp_path)
    at = AppTest.from_file(APP_PATH).run(timeout=10)
    assert not at.exception
    labels = [i.label for i in at.text_input]
    assert "关键字" in labels
    assert "语言" in labels
    assert "地区" in labels
    submit_buttons = [b for b in at.button if b.label == "Submit"]
    assert len(submit_buttons) == 1
