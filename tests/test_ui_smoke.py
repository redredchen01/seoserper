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


def _stub_quota(monkeypatch, caption: str | None, is_low: bool = False):
    """Short-circuit the SerpAPI account endpoint so tests stay offline."""
    import seoserper.serpapi_account as m

    def fake_info(_key, timeout=5.0):
        if caption is None:
            return None
        return {"plan_searches_left": 87, "searches_per_month": 100}

    def fake_format(_info):
        return caption

    def fake_is_low(_info, threshold=20):
        return is_low

    monkeypatch.setattr(m, "fetch_quota_info", fake_info)
    monkeypatch.setattr(m, "format_quota_caption", fake_format)
    monkeypatch.setattr(m, "is_quota_low", fake_is_low)
    import app as _app
    monkeypatch.setattr(_app, "fetch_quota_info", fake_info)
    monkeypatch.setattr(_app, "format_quota_caption", fake_format)
    monkeypatch.setattr(_app, "is_quota_low", fake_is_low)


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
    text_labels = [i.label for i in at.text_input]
    assert "关键字" in text_labels
    # Language / country is a single selectbox over SUPPORTED_LOCALES.
    select_labels = [s.label for s in at.selectbox]
    assert "语言 / 地区" in select_labels
    submit_buttons = [b for b in at.button if b.label == "Submit"]
    assert len(submit_buttons) == 1


def test_full_mode_quota_caption_shows_when_available(monkeypatch, tmp_path):
    _patch_key(monkeypatch, "fake-key")
    _isolate_db(monkeypatch, tmp_path)
    _stub_quota(monkeypatch, "SerpAPI 剩余 87/100")
    at = AppTest.from_file(APP_PATH).run(timeout=10)
    captions = [c.value for c in at.caption]
    assert any("SerpAPI 剩余" in c for c in captions), captions
    # Not low → no warning widget
    warnings = [w.value for w in at.warning]
    assert not any("剩余" in w for w in warnings), warnings


def test_full_mode_quota_low_renders_warning(monkeypatch, tmp_path):
    _patch_key(monkeypatch, "fake-key")
    _isolate_db(monkeypatch, tmp_path)
    _stub_quota(monkeypatch, "SerpAPI 剩余 5/100", is_low=True)
    at = AppTest.from_file(APP_PATH).run(timeout=10)
    warnings = [w.value for w in at.warning]
    # Streamlit's AppTest surfaces warning text — the ⚠️ glyph renders as a
    # widget icon, not in the text. The diagnostic tail is what we check.
    assert any(
        "SerpAPI 剩余" in w and "配额即将耗尽" in w for w in warnings
    ), warnings
    # Warning replaces caption — caption should NOT carry the quota text.
    captions = [c.value for c in at.caption]
    assert not any("SerpAPI 剩余" in c for c in captions), captions


def test_full_mode_quota_caption_absent_when_endpoint_fails(monkeypatch, tmp_path):
    _patch_key(monkeypatch, "fake-key")
    _isolate_db(monkeypatch, tmp_path)
    _stub_quota(monkeypatch, None)
    at = AppTest.from_file(APP_PATH).run(timeout=10)
    captions = [c.value for c in at.caption]
    # Full mode caption still present, quota caption absent.
    assert any("Full mode" in c for c in captions), captions
    assert not any("剩余" in c for c in captions), captions


def test_full_mode_shows_bypass_cache_checkbox(monkeypatch, tmp_path):
    _patch_key(monkeypatch, "fake-key")
    _isolate_db(monkeypatch, tmp_path)
    _stub_quota(monkeypatch, "SerpAPI 剩余 87/100")
    at = AppTest.from_file(APP_PATH).run(timeout=10)
    checkboxes = [cb.label for cb in at.checkbox]
    assert any("忽略缓存" in label for label in checkboxes), checkboxes


def test_suggest_only_hides_bypass_cache_checkbox(monkeypatch, tmp_path):
    _patch_key(monkeypatch, None)
    _isolate_db(monkeypatch, tmp_path)
    at = AppTest.from_file(APP_PATH).run(timeout=10)
    checkboxes = [cb.label for cb in at.checkbox]
    # Bypass is meaningless without SerpAPI — checkbox should be absent.
    assert not any("忽略缓存" in label for label in checkboxes), checkboxes


def test_suggest_only_no_quota_caption(monkeypatch, tmp_path):
    _patch_key(monkeypatch, None)
    _isolate_db(monkeypatch, tmp_path)
    _stub_quota(monkeypatch, "should-not-show")
    at = AppTest.from_file(APP_PATH).run(timeout=10)
    captions = [c.value for c in at.caption]
    assert not any("SerpAPI 剩余" in c for c in captions), captions


def test_locale_selectbox_has_mvp_options(monkeypatch, tmp_path):
    _patch_key(monkeypatch, None)
    _isolate_db(monkeypatch, tmp_path)
    at = AppTest.from_file(APP_PATH).run(timeout=10)
    sb = [s for s in at.selectbox if s.label == "语言 / 地区"]
    assert len(sb) == 1
    # AppTest's `options` returns the formatted display strings (post-format_func),
    # not the original option tuples — they're the labels as users see them.
    options = list(sb[0].options)
    assert len(options) == 4  # en/zh-CN/zh-TW/ja per config
    assert "English (US)" in options
    assert "简体中文 (CN)" in options
    assert "繁體中文 (TW)" in options
    assert "日本語 (JP)" in options
