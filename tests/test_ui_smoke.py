"""Unit 7: Streamlit UI smoke — boot, preflight branches, simple flow.

These are AppTest-based smoke checks; no real browser involved. The engine
and render thread are monkey-patched so we never touch Playwright.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

APP_PATH = str(Path(__file__).parent.parent / "app.py")


def _patch_preflight_ok(monkeypatch):
    from seoserper.core import render
    monkeypatch.setattr(render, "preflight", lambda: (True, ""))


def _patch_preflight_fail(monkeypatch):
    from seoserper.core import render
    monkeypatch.setattr(render, "preflight", lambda: (False, "Run: playwright install chromium"))


def test_app_boots_and_renders_title(monkeypatch, tmp_path):
    _patch_preflight_ok(monkeypatch)
    monkeypatch.setenv("SEOSERPER_DB", str(tmp_path / "ui.db"))
    at = AppTest.from_file(APP_PATH).run(timeout=10)
    assert not at.exception
    assert any("SEOSERPER" in t.value for t in at.title)


def test_app_shows_preflight_failure_banner(monkeypatch, tmp_path):
    _patch_preflight_fail(monkeypatch)
    monkeypatch.setenv("SEOSERPER_DB", str(tmp_path / "ui.db"))
    at = AppTest.from_file(APP_PATH).run(timeout=10)
    assert not at.exception
    # The error banner carries the preflight remediation hint.
    errors = [e.value for e in at.error]
    assert any("playwright install chromium" in e for e in errors), errors


def test_app_shows_empty_history_message(monkeypatch, tmp_path):
    _patch_preflight_ok(monkeypatch)
    monkeypatch.setenv("SEOSERPER_DB", str(tmp_path / "ui.db"))
    at = AppTest.from_file(APP_PATH).run(timeout=10)
    assert not at.exception
    # Sidebar caption "暂无历史"
    captions = [c.value for c in at.sidebar.caption]
    assert any("暂无历史" in c for c in captions), captions


def test_app_renders_input_row(monkeypatch, tmp_path):
    _patch_preflight_ok(monkeypatch)
    monkeypatch.setenv("SEOSERPER_DB", str(tmp_path / "ui.db"))
    at = AppTest.from_file(APP_PATH).run(timeout=10)
    assert not at.exception
    # Three text inputs (query / lang / country)
    labels = [i.label for i in at.text_input]
    assert "关键字" in labels
    assert "语言" in labels
    assert "地区" in labels
    # And a Submit button
    submit_buttons = [b for b in at.button if b.label == "Submit"]
    assert len(submit_buttons) == 1
