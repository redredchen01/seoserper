"""Unit 4: Streamlit UI smoke — boot, mode notice, sidebar, input row.

AppTest-based smoke checks; no real SerpAPI calls. Behavior differs between
Suggest-only (``SERPAPI_KEY`` unset, default) and Full mode (``SERPAPI_KEY``
set). Each test explicitly sets the key via monkeypatch.
"""

from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

APP_PATH = str(Path(__file__).parent.parent / "app.py")


def _patch_key(monkeypatch, key: str | None):
    from seoserper import config
    monkeypatch.setattr(config, "SERPAPI_KEY", key)


def _isolate_db(monkeypatch, tmp_path: Path):
    """Patch both the env var (for config reload paths) and the module
    attribute (for already-imported modules reading config.DB_PATH)."""
    from seoserper import config
    db_path = str(tmp_path / "ui.db")
    monkeypatch.setenv("SEOSERPER_DB", db_path)
    monkeypatch.setattr(config, "DB_PATH", db_path)


# --- full mode (SERPAPI_KEY set) --------------------------------------------


def test_full_mode_boots_and_renders_title(monkeypatch, tmp_path):
    _patch_key(monkeypatch, "fake-key")
    _isolate_db(monkeypatch, tmp_path)
    at = AppTest.from_file(APP_PATH).run(timeout=10)
    assert not at.exception
    assert any("SEOSERPER" in t.value for t in at.title)


def test_full_mode_shows_full_mode_caption(monkeypatch, tmp_path):
    _patch_key(monkeypatch, "fake-key")
    _isolate_db(monkeypatch, tmp_path)
    at = AppTest.from_file(APP_PATH).run(timeout=10)
    captions = [c.value for c in at.caption]
    assert any("Full mode" in c and "SerpAPI" in c for c in captions), captions
    # Must NOT show the Suggest-only prompt.
    assert not any("SERPAPI_KEY 未设置" in c for c in captions), captions


def test_full_mode_does_not_embed_literal_key_value(monkeypatch, tmp_path):
    """Security: the UI must never echo the actual key value."""
    _patch_key(monkeypatch, "super-secret-live-key-xyz")
    _isolate_db(monkeypatch, tmp_path)
    at = AppTest.from_file(APP_PATH).run(timeout=10)
    captions = [c.value for c in at.caption]
    for c in captions:
        assert "super-secret-live-key-xyz" not in c, c


# --- suggest-only mode (SERPAPI_KEY unset) ----------------------------------


def test_suggest_only_boots_and_renders_title(monkeypatch, tmp_path):
    _patch_key(monkeypatch, None)
    _isolate_db(monkeypatch, tmp_path)
    at = AppTest.from_file(APP_PATH).run(timeout=10)
    assert not at.exception
    assert any("SEOSERPER" in t.value for t in at.title)


def test_suggest_only_shows_setup_caption(monkeypatch, tmp_path):
    _patch_key(monkeypatch, None)
    _isolate_db(monkeypatch, tmp_path)
    at = AppTest.from_file(APP_PATH).run(timeout=10)
    captions = [c.value for c in at.caption]
    assert any("Suggest-only" in c for c in captions), captions
    assert any("SERPAPI_KEY" in c for c in captions), captions
    assert any("config.py" in c for c in captions), captions


def test_suggest_only_submit_button_still_enabled(monkeypatch, tmp_path):
    """Suggest-only is a real working mode — Submit must not be disabled."""
    _patch_key(monkeypatch, None)
    _isolate_db(monkeypatch, tmp_path)
    at = AppTest.from_file(APP_PATH).run(timeout=10)
    submit_buttons = [b for b in at.button if b.label == "Submit"]
    assert len(submit_buttons) == 1


# --- common / unchanged ------------------------------------------------------


def test_app_shows_empty_history_message(monkeypatch, tmp_path):
    _patch_key(monkeypatch, None)
    _isolate_db(monkeypatch, tmp_path)
    at = AppTest.from_file(APP_PATH).run(timeout=10)
    assert not at.exception
    captions = [c.value for c in at.sidebar.caption]
    assert any("暂无历史" in c for c in captions), captions


def test_app_shows_empty_history_message_in_full_mode(monkeypatch, tmp_path):
    _patch_key(monkeypatch, "fake-key")
    _isolate_db(monkeypatch, tmp_path)
    at = AppTest.from_file(APP_PATH).run(timeout=10)
    captions = [c.value for c in at.sidebar.caption]
    assert any("暂无历史" in c for c in captions), captions


def test_app_renders_input_row(monkeypatch, tmp_path):
    _patch_key(monkeypatch, None)
    _isolate_db(monkeypatch, tmp_path)
    at = AppTest.from_file(APP_PATH).run(timeout=10)
    assert not at.exception
    labels = [i.label for i in at.text_input]
    assert "关键字" in labels
    assert "语言" in labels
    assert "地区" in labels
    submit_buttons = [b for b in at.button if b.label == "Submit"]
    assert len(submit_buttons) == 1
