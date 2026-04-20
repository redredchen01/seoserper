"""SEOSERPER — Streamlit UI entry.

Behavior branches on ``config.SERPAPI_KEY`` (see seoserper/config.py module
docstring for setup + quota + locale notes):

- Key unset (default): Suggest-only mode. One Suggestions section. Muted
  top-of-page notice reading ``Suggest-only · SERPAPI_KEY 未设置``.
- Key set: Full 3-section layout (Suggest + PAA + Related). Top notice
  reads ``Full mode · SerpAPI``. PAA + Related come from a single
  ``engine=google`` SerpAPI call per Submit.

Restart required after changing ``SERPAPI_KEY`` — the env var is read once
at module import in ``seoserper.config``.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone, timedelta
from functools import partial

import streamlit as st

from seoserper import config
from seoserper.core.engine import AnalysisEngine, ProgressEvent
from seoserper.export import (
    build_csv_filename,
    build_filename,
    render_analysis_to_csv,
    render_analysis_to_md,
)
from seoserper.fetchers.serp_cache import fetch_serp_data_cached
from seoserper.serpapi_account import (
    fetch_quota_info,
    format_quota_caption,
    is_quota_low,
)
from seoserper.models import (
    AnalysisJob,
    FailureCategory,
    JobStatus,
    SurfaceName,
    SurfaceStatus,
)
from seoserper.fetchers.serp_cache import _cache_key
from seoserper.storage import (
    cache_invalidate,
    get_job,
    init_db,
    list_recent_jobs,
    reap_orphaned,
)


_BADGES = {
    SurfaceStatus.OK: "🟢",
    SurfaceStatus.EMPTY: "⚪",
    SurfaceStatus.FAILED: "🔴",
    SurfaceStatus.RUNNING: "⏳",
}

_SURFACE_LABELS = {
    SurfaceName.SUGGEST: "Suggestions",
    SurfaceName.PAA: "People Also Ask",
    SurfaceName.RELATED: "Related Searches",
}

_FAILURE_MSG = {
    FailureCategory.BLOCKED_RATE_LIMIT: "被限流 (SerpAPI 月度配额用尽 或 Suggest 限流) — 重试 或 等 quota 重置",
    FailureCategory.SELECTOR_NOT_FOUND: "响应结构异常 (provider drift 或 被拦截返回 HTML)",
    FailureCategory.NETWORK_ERROR: "网络错误 — 检查连接 / API key",
}


def _full_mode_available() -> bool:
    return config.SERPAPI_KEY is not None


def _ensure_session_state() -> None:
    ss = st.session_state
    if "_db_path" not in ss:
        ss._db_path = config.DB_PATH
        init_db(ss._db_path)
        reap_orphaned(db_path=ss._db_path)
    if "_engine" not in ss:
        ss._engine = None
    if "_current_job_id" not in ss:
        ss._current_job_id = None
    if "_historical_job_id" not in ss:
        ss._historical_job_id = None
    if "_quota_caption" not in ss:
        # One-shot quota lookup per Streamlit session — the dashboard at
        # https://serpapi.com/manage-api-key is the source of truth for exact
        # count; this is a best-effort UI hint. Refreshes only on full restart.
        if _full_mode_available():
            info = fetch_quota_info(config.SERPAPI_KEY)
            ss._quota_caption = format_quota_caption(info)
            ss._quota_is_low = is_quota_low(info)
        else:
            ss._quota_caption = None
            ss._quota_is_low = False


def _boot_engine() -> AnalysisEngine:
    """Lazily build the engine on first Submit.

    Under ``SERPAPI_KEY=None`` (Suggest-only): engine is built with
    ``serp_fn=None``; no provider call is made for PAA/Related. Under
    ``SERPAPI_KEY=<key>``: engine gets a ``serp_fn`` closure that calls
    ``fetch_serp_data`` with the key curried in. The key is **never** stored
    on the engine instance — it's captured inside the closure from config
    at engine construction time, and the engine object itself has no
    ``api_key`` attribute.
    """
    ss = st.session_state
    if ss._engine is not None:
        return ss._engine

    if _full_mode_available():
        key = config.SERPAPI_KEY
        db_path = ss._db_path
        # Closure carries the key + DB path; engine signature stays (q, l, c).
        # Cached wrapper short-circuits on repeated (query, lang, country)
        # within SERP_CACHE_TTL_SECONDS — saves free-tier quota.
        serp_fn = partial(
            fetch_serp_data_cached, api_key=key, db_path=db_path
        )
        ss._engine = AnalysisEngine(serp_fn=serp_fn, db_path=db_path)
    else:
        ss._engine = AnalysisEngine(serp_fn=None, db_path=ss._db_path)
    return ss._engine


def _drain_progress() -> bool:
    """Pull all queued events; return True if job still running."""
    ss = st.session_state
    if ss._engine is None:
        return False
    still_running = False
    while not ss._engine.progress_queue.empty():
        ev: ProgressEvent = ss._engine.progress_queue.get_nowait()
        if ev.kind in ("complete", "error"):
            still_running = False
        elif ev.kind == "start":
            still_running = True
    if ss._current_job_id is not None:
        job = get_job(ss._current_job_id, db_path=ss._db_path)
        if job is not None and job.status == JobStatus.RUNNING:
            still_running = True
    return still_running


def _render_surface(job: AnalysisJob, name: SurfaceName) -> None:
    surface = job.surfaces.get(name)
    label = _SURFACE_LABELS[name]
    if surface is None or surface.status == SurfaceStatus.RUNNING:
        st.markdown(f"### {_BADGES[SurfaceStatus.RUNNING]} {label}")
        st.caption("运行中…")
        return

    badge = _BADGES[surface.status]
    count_suffix = f" ({surface.rank_count})" if surface.status == SurfaceStatus.OK else ""
    st.markdown(f"### {badge} {label}{count_suffix}")

    if surface.status == SurfaceStatus.EMPTY:
        st.caption("该查询无返回内容")
        return
    if surface.status == SurfaceStatus.FAILED:
        msg = _FAILURE_MSG.get(surface.failure_category, "未知失败")
        st.error(msg)
        return

    # status == OK
    if name == SurfaceName.SUGGEST:
        for item in surface.items:
            st.markdown(f"{item.rank}. {item.text}")
    elif name == SurfaceName.PAA:
        for item in surface.items:
            st.markdown(f"**{item.rank}. {item.question}**")
            if getattr(item, "answer_preview", ""):
                st.caption(item.answer_preview)
    elif name == SurfaceName.RELATED:
        for item in surface.items:
            st.markdown(f"- {item.query}")


def _render_history_sidebar() -> None:
    ss = st.session_state
    with st.sidebar:
        st.header("历史")
        jobs = list_recent_jobs(db_path=ss._db_path)
        if not jobs:
            st.caption("暂无历史")
            return

        # Filter box — only render when history has enough rows to make
        # scanning painful. Below 5 jobs the filter UI is more clutter
        # than value.
        if len(jobs) >= 5:
            filter_text = st.text_input(
                "过滤 (query 子串匹配，大小写不敏感)",
                key="_history_filter",
                placeholder="输入关键字片段…",
                label_visibility="collapsed",
            ).strip().lower()
            if filter_text:
                jobs = [j for j in jobs if filter_text in (j.query or "").lower()]
                if not jobs:
                    st.caption(f"无匹配 · 过滤词 `{filter_text}`")
                    return

        now = datetime.now(timezone.utc)
        groups = {"今天": [], "本周": [], "更早": []}
        for job in jobs:
            try:
                started = datetime.fromisoformat(
                    (job.started_at or "").replace(" ", "T")
                ).replace(tzinfo=timezone.utc)
            except ValueError:
                started = now
            delta = now - started
            if delta < timedelta(days=1):
                groups["今天"].append((job, started))
            elif delta < timedelta(days=7):
                groups["本周"].append((job, started))
            else:
                groups["更早"].append((job, started))

        for label, entries in groups.items():
            if not entries:
                continue
            st.subheader(label)
            for job, _ in entries:
                label_line = (
                    (job.query[:40] + "…") if len(job.query) > 40 else job.query
                )
                # Iterate only the surfaces actually present on the job
                # (suggest-only jobs have just one; full jobs have three).
                badges = "".join(
                    _BADGES.get(s.status, "·") for s in job.surfaces.values()
                )
                mode_tag = "·S" if job.render_mode == "suggest-only" else ""
                # Two-column row: main load button (wide) + re-run (narrow).
                load_col, rerun_col = st.columns([5, 1])
                with load_col:
                    if st.button(
                        f"{label_line}\n{job.language}-{job.country} {badges}{mode_tag}",
                        key=f"hist_{job.id}", use_container_width=True,
                    ):
                        ss._historical_job_id = job.id
                with rerun_col:
                    if st.button(
                        "🔄",
                        key=f"rerun_{job.id}",
                        help="用同样的关键字+语言+地区重新抓一次（走缓存；如要强制刷新见输入行的忽略缓存勾选）",
                        use_container_width=True,
                    ):
                        engine = _boot_engine()
                        new_id = engine.submit(
                            job.query, job.language, job.country
                        )
                        ss._current_job_id = new_id
                        ss._historical_job_id = None
                        st.rerun()


def _render_current(job: AnalysisJob) -> None:
    metadata_bits = [f"Suggest: {job.source_suggest}"]
    if job.render_mode == "full":
        metadata_bits.append(f"PAA+Related: {job.source_serp}")
    metadata_bits.append(f"started {job.started_at} UTC")
    st.caption(" · ".join(metadata_bits))

    # Iterate only the surfaces the job actually has — suggest-only jobs
    # render one section; full jobs render three with dividers between.
    present_names = [n for n in (SurfaceName.SUGGEST, SurfaceName.PAA, SurfaceName.RELATED)
                     if n in job.surfaces]
    for i, name in enumerate(present_names):
        if i > 0:
            st.divider()
        _render_surface(job, name)

    if job.status != JobStatus.RUNNING:
        any_failed = any(
            s.status != SurfaceStatus.OK for s in job.surfaces.values()
        )
        cols = st.columns(3)
        with cols[0]:
            st.download_button(
                "📄 导出 Markdown",
                data=render_analysis_to_md(job),
                file_name=build_filename(job),
                mime="text/markdown",
                use_container_width=True,
            )
        with cols[1]:
            st.download_button(
                "📊 导出 CSV",
                data=render_analysis_to_csv(job).encode("utf-8"),
                file_name=build_csv_filename(job),
                mime="text/csv",
                use_container_width=True,
            )
        with cols[2]:
            if any_failed:
                if st.button("🔁 重跑失败版位", use_container_width=True):
                    engine = _boot_engine()
                    engine.retry_failed_surfaces(job.id)
                    st.rerun()


def _render_mode_notice() -> None:
    """Top-of-page notice: muted grey, non-dismissible.

    Full mode (SERPAPI_KEY set): ``Full mode · SerpAPI``, plus an optional
    quota caption when the account endpoint returned data at session init.
    Suggest-only (SERPAPI_KEY unset): prompt to set the env var; recovery
    checklist lives in ``seoserper/config.py`` module docstring.
    """
    if _full_mode_available():
        st.caption("Full mode · SerpAPI")
        quota = st.session_state.get("_quota_caption")
        if quota:
            if st.session_state.get("_quota_is_low", False):
                st.warning(f"⚠️ {quota} — 配额即将耗尽，慎用 Submit")
            else:
                st.caption(quota)
    else:
        st.caption(
            "Suggest-only · SERPAPI_KEY 未设置 · 启用 Full mode 见 seoserper/config.py"
        )


def main() -> None:
    st.set_page_config(page_title="SEOSERPER", layout="wide")
    st.title("SEOSERPER · Google SERP Analyzer")

    _ensure_session_state()
    ss = st.session_state

    _render_mode_notice()

    # Input row
    cols = st.columns([4, 2, 1])
    with cols[0]:
        query = st.text_input("关键字", key="_query_input")
    with cols[1]:
        # Single selectbox over the MVP locale set — options are the full
        # (lang, country, label) tuple; format_func renders the label only.
        locale = st.selectbox(
            "语言 / 地区",
            options=config.SUPPORTED_LOCALES,
            format_func=lambda opt: opt[2],
            key="_locale_input",
        )
    with cols[2]:
        st.write("")
        submitted = st.button("Submit", use_container_width=True, type="primary")

    # Secondary controls — only show when Full mode is active (cache bypass
    # is meaningless without SerpAPI in the loop).
    bypass_cache = False
    if _full_mode_available():
        bypass_cache = st.checkbox(
            "忽略缓存（强制调用 SerpAPI 拉新数据）",
            value=False,
            key="_bypass_cache_input",
            help="勾选后本次 Submit 不读 24h 缓存；新数据依然会写回缓存。",
        )

    lang, country, _label = locale
    if submitted and query.strip():
        if bypass_cache and _full_mode_available():
            # Pre-invalidate the exact key so the downstream cached wrapper
            # misses → live SerpAPI call → stores fresh row.
            cache_invalidate(
                _cache_key(query.strip(), lang, country),
                db_path=ss._db_path,
            )
        engine = _boot_engine()
        job_id = engine.submit(query.strip(), lang, country)
        ss._current_job_id = job_id
        ss._historical_job_id = None

    _render_history_sidebar()

    viewing_id = ss._historical_job_id or ss._current_job_id
    if viewing_id is None:
        st.info("输入关键字 + 点 Submit 开始分析")
        return

    job = get_job(viewing_id, db_path=ss._db_path)
    if job is None:
        st.warning("历史记录已丢失")
        return

    if ss._historical_job_id is not None:
        st.warning(f"正在回看历史 (job #{job.id}, {job.started_at} UTC)")
        if st.button("返回当前"):
            ss._historical_job_id = None
            st.rerun()

    _render_current(job)

    if job.status == JobStatus.RUNNING:
        still = _drain_progress()
        if still:
            time.sleep(0.25)
            st.rerun()


if __name__ == "__main__":
    main()
