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


def _seed_jobs(db_path: str, queries: list[str]) -> list[int]:
    from seoserper.storage import (
        complete_job, create_job, update_surface,
    )
    from seoserper.models import Suggestion, SurfaceName, SurfaceStatus
    ids = []
    for q in queries:
        jid = create_job(q, "en", "us", db_path=db_path, render_mode="suggest-only")
        update_surface(
            jid, SurfaceName.SUGGEST, SurfaceStatus.OK,
            items=[Suggestion(text=f"{q} shop", rank=1)], db_path=db_path,
        )
        complete_job(jid, db_path=db_path)
        ids.append(jid)
    return ids


def _find_filter_input(at):
    """Locate the history filter text_input by session-state key — the
    label_visibility='collapsed' setting strips label from the rendered tree,
    so key-based lookup is the stable path."""
    return [i for i in at.sidebar.text_input if i.key == "_history_filter"]


def test_history_filter_hidden_when_few_rows(monkeypatch, tmp_path):
    """Filter box only appears once history grows — below 5 rows it's noise."""
    _patch_key(monkeypatch, None)
    _isolate_db(monkeypatch, tmp_path)
    from seoserper.storage import init_db
    db = str(tmp_path / "ui.db")
    init_db(db)
    _seed_jobs(db, ["coffee", "tea", "matcha"])
    at = AppTest.from_file(APP_PATH).run(timeout=10)
    assert _find_filter_input(at) == []


def test_history_filter_shown_when_many_rows(monkeypatch, tmp_path):
    """With 5+ rows the filter text_input renders in the sidebar."""
    _patch_key(monkeypatch, None)
    _isolate_db(monkeypatch, tmp_path)
    from seoserper.storage import init_db
    db = str(tmp_path / "ui.db")
    init_db(db)
    _seed_jobs(db, ["coffee", "tea", "matcha", "latte", "espresso", "mocha"])
    at = AppTest.from_file(APP_PATH).run(timeout=10)
    assert len(_find_filter_input(at)) == 1


def test_history_filter_narrows_visible_rows(monkeypatch, tmp_path):
    """Typing a substring collapses the sidebar to matching jobs only."""
    _patch_key(monkeypatch, None)
    _isolate_db(monkeypatch, tmp_path)
    from seoserper.storage import init_db
    db = str(tmp_path / "ui.db")
    init_db(db)
    ids = _seed_jobs(db, ["coffee", "tea", "matcha", "latte", "espresso", "mocha"])
    at = AppTest.from_file(APP_PATH).run(timeout=10)
    inputs = _find_filter_input(at)
    assert len(inputs) == 1
    inputs[0].set_value("cha").run(timeout=10)
    keys = [b.key for b in at.sidebar.button if (b.key or "").startswith("hist_")]
    # Only matcha (ids[2]) and mocha (ids[5]) contain "cha" as substring.
    expected_visible = {f"hist_{ids[2]}", f"hist_{ids[5]}"}
    assert set(keys) == expected_visible, keys


def test_history_filter_no_match_shows_caption(monkeypatch, tmp_path):
    _patch_key(monkeypatch, None)
    _isolate_db(monkeypatch, tmp_path)
    from seoserper.storage import init_db
    db = str(tmp_path / "ui.db")
    init_db(db)
    _seed_jobs(db, ["coffee", "tea", "matcha", "latte", "espresso", "mocha"])
    at = AppTest.from_file(APP_PATH).run(timeout=10)
    inputs = _find_filter_input(at)
    inputs[0].set_value("xxyyzz").run(timeout=10)
    captions = [c.value for c in at.sidebar.caption]
    assert any("无匹配" in c and "xxyyzz" in c for c in captions), captions


def test_engine_radio_renders_three_options_with_compare_default(monkeypatch, tmp_path):
    _patch_key(monkeypatch, "fake-key")
    _isolate_db(monkeypatch, tmp_path)
    _stub_quota(monkeypatch, "SerpAPI 剩余 87/100")
    at = AppTest.from_file(APP_PATH).run(timeout=10)
    radios = [r for r in at.radio if r.label == "搜索引擎"]
    assert len(radios) == 1
    options = list(radios[0].options)
    # Plan 006 Unit 1: compare mode is new default (index 0).
    assert options == ["Google + Bing 对比", "Google", "Bing"]
    assert radios[0].value == "Google + Bing 对比"


def test_engine_radio_present_in_suggest_only_mode_too(monkeypatch, tmp_path):
    """Radio is unconditional — user may set SERPAPI_KEY later and Bing needs it."""
    _patch_key(monkeypatch, None)
    _isolate_db(monkeypatch, tmp_path)
    at = AppTest.from_file(APP_PATH).run(timeout=10)
    radios = [r for r in at.radio if r.label == "搜索引擎"]
    assert len(radios) == 1


def test_compare_submit_creates_two_jobs(monkeypatch, tmp_path):
    """Default compare-mode Submit creates 2 job rows (google + bing)."""
    _patch_key(monkeypatch, "fake-key")
    _isolate_db(monkeypatch, tmp_path)
    _stub_quota(monkeypatch, "SerpAPI 剩余 87/100")

    # Stub the engine's underlying serp_fn so no real HTTP fires.
    from seoserper.models import ParseResult, SurfaceName, SurfaceStatus
    def fake_serp(q, l, c, *, engine="google"):
        return {
            SurfaceName.PAA: ParseResult(status=SurfaceStatus.OK, items=[]),
            SurfaceName.RELATED: ParseResult(status=SurfaceStatus.OK, items=[]),
        }
    # Patch the fetcher the app wires via partial.
    import seoserper.fetchers.serp_cache as _sc
    monkeypatch.setattr(_sc, "fetch_serp_data_cached", fake_serp)
    import app as _app
    monkeypatch.setattr(_app, "fetch_serp_data_cached", fake_serp)

    # Stub the suggest fetcher too.
    from seoserper.fetchers.suggest import SuggestResult
    import seoserper.core.engine as _eng
    monkeypatch.setattr(
        _eng, "fetch_suggestions",
        lambda q, l, c: SuggestResult(status=SurfaceStatus.EMPTY),
    )

    at = AppTest.from_file(APP_PATH).run(timeout=10)
    # Compare is default — just type a query + click Submit.
    [i for i in at.text_input if i.label == "关键字"][0].set_value("coffee").run(timeout=10)
    [b for b in at.button if b.label == "Submit"][0].click().run(timeout=10)

    # Two jobs should now exist in the isolated DB.
    from seoserper.storage import list_recent_jobs
    import time as _time
    # list_recent_jobs excludes running jobs — wait for both to complete.
    deadline = _time.monotonic() + 3.0
    jobs = []
    while _time.monotonic() < deadline:
        jobs = list_recent_jobs(db_path=str(tmp_path / "ui.db"))
        if len(jobs) >= 2:
            break
        _time.sleep(0.1)

    engines_seen = {j.engine for j in jobs}
    assert "google" in engines_seen and "bing" in engines_seen, (
        f"expected both engines among completed jobs; saw {[j.engine for j in jobs]}"
    )


def test_bing_job_renders_no_suggest_advisory_caption(monkeypatch, tmp_path):
    """A Bing job's view carries an explanatory caption about no Suggest."""
    _patch_key(monkeypatch, "fake-key")
    _isolate_db(monkeypatch, tmp_path)
    _stub_quota(monkeypatch, "SerpAPI 剩余 87/100")
    from seoserper.storage import (
        complete_job, create_job, init_db, update_surface,
    )
    from seoserper.models import SurfaceName, SurfaceStatus, PAAQuestion
    db = str(tmp_path / "ui.db")
    init_db(db)
    jid = create_job("coffee", "en", "us", db_path=db, engine="bing")
    update_surface(
        jid, SurfaceName.PAA, SurfaceStatus.OK,
        items=[PAAQuestion(question="Is coffee good?", rank=1)], db_path=db,
    )
    update_surface(jid, SurfaceName.RELATED, SurfaceStatus.OK, items=[], db_path=db)
    complete_job(jid, db_path=db)

    # Load the Bing job via session state by clicking its history row.
    at = AppTest.from_file(APP_PATH).run(timeout=10)
    [b for b in at.sidebar.button if b.key == f"hist_{jid}"][0].click().run(timeout=10)

    captions = [c.value for c in at.caption]
    # Metadata caption should name the engine.
    assert any("engine: bing" in c for c in captions), captions
    # No-Suggest advisory caption should be present.
    assert any("Bing 未提供公开 autocomplete" in c for c in captions), captions


def test_google_job_does_not_render_bing_advisory(monkeypatch, tmp_path):
    _patch_key(monkeypatch, None)
    _isolate_db(monkeypatch, tmp_path)
    from seoserper.storage import (
        complete_job, create_job, init_db, update_surface,
    )
    from seoserper.models import SurfaceName, SurfaceStatus, Suggestion
    db = str(tmp_path / "ui.db")
    init_db(db)
    jid = create_job("coffee", "en", "us", db_path=db, render_mode="suggest-only")
    update_surface(
        jid, SurfaceName.SUGGEST, SurfaceStatus.OK,
        items=[Suggestion(text="coffee shop", rank=1)], db_path=db,
    )
    complete_job(jid, db_path=db)

    at = AppTest.from_file(APP_PATH).run(timeout=10)
    [b for b in at.sidebar.button if b.key == f"hist_{jid}"][0].click().run(timeout=10)
    captions = [c.value for c in at.caption]
    assert not any("Bing 未提供公开" in c for c in captions), captions


def test_history_row_renders_delete_button(monkeypatch, tmp_path):
    """Each history row gets a 🗑️ delete button next to the main + 🔄."""
    _patch_key(monkeypatch, None)
    _isolate_db(monkeypatch, tmp_path)
    from seoserper.storage import init_db
    db = str(tmp_path / "ui.db")
    init_db(db)
    ids = _seed_jobs(db, ["coffee"])

    at = AppTest.from_file(APP_PATH).run(timeout=10)
    keys = [b.key for b in at.sidebar.button]
    assert f"del_{ids[0]}" in keys, keys


def test_delete_button_arm_and_confirm_removes_row(monkeypatch, tmp_path):
    """First click arms (changes label ⚠️), second click deletes + row gone."""
    _patch_key(monkeypatch, None)
    _isolate_db(monkeypatch, tmp_path)
    from seoserper.storage import init_db, get_job
    db = str(tmp_path / "ui.db")
    init_db(db)
    ids = _seed_jobs(db, ["coffee", "tea"])
    coffee_id, tea_id = ids

    at = AppTest.from_file(APP_PATH).run(timeout=10)

    # First click on coffee's 🗑️ — arms only, no deletion.
    del_btns = [b for b in at.sidebar.button if b.key == f"del_{coffee_id}"]
    assert len(del_btns) == 1
    del_btns[0].click().run(timeout=10)

    assert get_job(coffee_id, db_path=db) is not None, "armed state shouldn't delete"
    # After arm, the button rerenders — same key, primary type now (per code).
    del_btns_armed = [b for b in at.sidebar.button if b.key == f"del_{coffee_id}"]
    assert len(del_btns_armed) == 1

    # Second click on the same armed button — actually deletes.
    del_btns_armed[0].click().run(timeout=10)

    assert get_job(coffee_id, db_path=db) is None, "second click should delete"
    # Tea row still present, and its delete button still has the pristine key.
    assert get_job(tea_id, db_path=db) is not None


def test_delete_on_different_job_rearms(monkeypatch, tmp_path):
    """Arming one job then clicking another job's 🗑️ moves the arm — no
    collateral deletes."""
    _patch_key(monkeypatch, None)
    _isolate_db(monkeypatch, tmp_path)
    from seoserper.storage import init_db, get_job
    db = str(tmp_path / "ui.db")
    init_db(db)
    ids = _seed_jobs(db, ["coffee", "tea"])
    coffee_id, tea_id = ids

    at = AppTest.from_file(APP_PATH).run(timeout=10)
    # Arm coffee
    [b for b in at.sidebar.button if b.key == f"del_{coffee_id}"][0].click().run(timeout=10)
    # Click tea's delete — should re-arm on tea, NOT delete coffee
    [b for b in at.sidebar.button if b.key == f"del_{tea_id}"][0].click().run(timeout=10)

    # Both rows still present; only one is armed now (tea).
    assert get_job(coffee_id, db_path=db) is not None
    assert get_job(tea_id, db_path=db) is not None


def test_history_row_renders_both_load_and_rerun_buttons(monkeypatch, tmp_path):
    """When history exists, each row gets a main button + a 🔄 re-run button."""
    _patch_key(monkeypatch, None)
    _isolate_db(monkeypatch, tmp_path)
    # Seed a completed job so the sidebar has something to render.
    from seoserper.storage import (
        complete_job, create_job, init_db, update_surface,
    )
    from seoserper.models import Suggestion, SurfaceName, SurfaceStatus
    db = str(tmp_path / "ui.db")
    init_db(db)
    jid = create_job("coffee", "en", "us", db_path=db, render_mode="suggest-only")
    update_surface(
        jid, SurfaceName.SUGGEST, SurfaceStatus.OK,
        items=[Suggestion(text="coffee shop", rank=1)], db_path=db,
    )
    complete_job(jid, db_path=db)

    at = AppTest.from_file(APP_PATH).run(timeout=10)
    keys = [b.key for b in at.sidebar.button]
    assert f"hist_{jid}" in keys, keys
    assert f"rerun_{jid}" in keys, keys


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
