"""SEOSERPER — Streamlit UI entry.

MVP scope (plan §Unit 7):
- Keyword + lang + country + Submit row.
- Preflight check (Playwright / Chromium), DB init + orphan sweep at boot.
- 3 surfaces stacked vertically (Suggestions / PAA / Related), badges per
  surface, progressive reveal via queue.drain + st.rerun ticks.
- Export MD button (completed job), Retry failed surfaces button.
- Sidebar history (recent 50, grouped Today / This week / Older).
- Historical view banner when loading a past snapshot.

Out-of-scope for this commit: custom clipboard component (Streamlit's
st.code block provides built-in copy); per-surface animation; settings page.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone, timedelta

import streamlit as st

from seoserper import config
from seoserper.core.engine import AnalysisEngine, ProgressEvent
from seoserper.core.render import RenderThread, preflight
from seoserper.export import build_filename, render_analysis_to_md
from seoserper.models import (
    AnalysisJob,
    FailureCategory,
    JobStatus,
    SurfaceName,
    SurfaceStatus,
)
from seoserper.storage import (
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
    FailureCategory.BLOCKED_BY_CAPTCHA: "Google captcha 拦截 — 稍等几分钟点 Retry",
    FailureCategory.BLOCKED_BY_CONSENT: "Google consent 屏 — 点 Retry 重走",
    FailureCategory.BLOCKED_RATE_LIMIT: "被限流 — 等 5 分钟点 Retry",
    FailureCategory.SELECTOR_NOT_FOUND: "页面结构未匹配 (selector drift?)",
    FailureCategory.NETWORK_ERROR: "网络错误 — 检查连接",
    FailureCategory.BROWSER_CRASH: "浏览器异常 — Retry 触发重启",
}


def _ensure_session_state() -> None:
    ss = st.session_state
    if "_db_path" not in ss:
        ss._db_path = config.DB_PATH
        init_db(ss._db_path)
        reap_orphaned(db_path=ss._db_path)
    if "_render_thread" not in ss:
        ss._render_thread = None
    if "_engine" not in ss:
        ss._engine = None
    if "_current_job_id" not in ss:
        ss._current_job_id = None
    if "_historical_job_id" not in ss:
        ss._historical_job_id = None
    if "_preflight_ok" not in ss:
        ok, msg = preflight()
        ss._preflight_ok = ok
        ss._preflight_msg = msg


def _boot_engine() -> AnalysisEngine | None:
    """Lazily build the engine on first Submit. Returns None if preflight failed."""
    ss = st.session_state
    if not ss._preflight_ok:
        return None
    if ss._engine is not None:
        return ss._engine

    # Import parser lazily — it may not exist until Unit 4 ships. If the
    # import fails we fall back to a stub that flags both surfaces as
    # selector-drift, keeping Suggest usable.
    parse_fn = _load_parser_or_stub()

    rt = RenderThread()
    rt.start()
    ss._render_thread = rt
    ss._engine = AnalysisEngine(
        render_thread=rt, parse_fn=parse_fn, db_path=ss._db_path
    )
    return ss._engine


def _load_parser_or_stub():
    try:
        from seoserper.parsers.serp import parse_serp  # type: ignore
        return parse_serp
    except ImportError:
        from seoserper.models import ParseResult

        def stub(html: str, locale: str):
            # Unit 4 not yet shipped — both SERP surfaces flag as selector drift.
            return {
                SurfaceName.PAA: ParseResult(
                    status=SurfaceStatus.FAILED,
                    failure_category=FailureCategory.SELECTOR_NOT_FOUND,
                ),
                SurfaceName.RELATED: ParseResult(
                    status=SurfaceStatus.FAILED,
                    failure_category=FailureCategory.SELECTOR_NOT_FOUND,
                ),
            }

        return stub


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
    # After drain, check storage to determine final state for the current job.
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
                badges = "".join(
                    _BADGES.get(job.surfaces.get(n, None).status, "·") if job.surfaces.get(n) else "·"
                    for n in (SurfaceName.SUGGEST, SurfaceName.PAA, SurfaceName.RELATED)
                )
                key = f"hist_{job.id}"
                if st.button(f"{label_line}\n{job.language}-{job.country} {badges}",
                             key=key, use_container_width=True):
                    ss._historical_job_id = job.id


def _render_current(job: AnalysisJob) -> None:
    st.caption(
        f"Suggest: {job.source_suggest} · PAA+Related: {job.source_serp} "
        f"· started {job.started_at} UTC"
    )
    _render_surface(job, SurfaceName.SUGGEST)
    st.divider()
    _render_surface(job, SurfaceName.PAA)
    st.divider()
    _render_surface(job, SurfaceName.RELATED)

    if job.status != JobStatus.RUNNING:
        any_failed = any(
            s.status != SurfaceStatus.OK for s in job.surfaces.values()
        )
        cols = st.columns(2)
        with cols[0]:
            st.download_button(
                "📄 导出 Markdown",
                data=render_analysis_to_md(job),
                file_name=build_filename(job),
                mime="text/markdown",
                use_container_width=True,
            )
        with cols[1]:
            if any_failed:
                if st.button("🔁 重跑失败版位", use_container_width=True):
                    engine = _boot_engine()
                    if engine is not None:
                        engine.retry_failed_surfaces(job.id)
                        st.rerun()


def main() -> None:
    st.set_page_config(page_title="SEOSERPER", layout="wide")
    st.title("SEOSERPER · Google SERP Analyzer")

    _ensure_session_state()
    ss = st.session_state

    if not ss._preflight_ok:
        st.error(ss._preflight_msg)
        st.info("安装后请刷新页面")
        return

    # Input row
    cols = st.columns([4, 1, 1, 1])
    with cols[0]:
        query = st.text_input("关键字", key="_query_input")
    with cols[1]:
        lang = st.text_input("语言", value="en", key="_lang_input")
    with cols[2]:
        country = st.text_input("地区", value="us", key="_country_input")
    with cols[3]:
        st.write("")
        submitted = st.button("Submit", use_container_width=True, type="primary")

    if submitted and query.strip():
        engine = _boot_engine()
        if engine is not None:
            job_id = engine.submit(query.strip(), lang.strip(), country.strip())
            ss._current_job_id = job_id
            ss._historical_job_id = None

    _render_history_sidebar()

    # Render either historical snapshot or live current job
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

    # Tick while running
    if job.status == JobStatus.RUNNING:
        still = _drain_progress()
        if still:
            time.sleep(0.25)
            st.rerun()


if __name__ == "__main__":
    main()
