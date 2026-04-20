"""Tests for seoserper.core.render.RenderThread + preflight().

All tests use injected fake launch/render/teardown callables — no real
Chromium subprocess is started. Ported from the canonical claude-crawler
test suite, with SEOSERPER-specific additions for:
  - Typed consent / captcha exceptions raised by _real_render probes
  - Restart policy (by query count and by elapsed time)
  - Typed RenderError subclasses do not retry
"""

from __future__ import annotations

import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from seoserper.core import render as render_mod
from seoserper.core.render import (
    BlockedByCaptchaError,
    BlockedByConsentError,
    BlockedRateLimitError,
    BrowserCrashError,
    RenderError,
    RenderQueueFullError,
    RenderThread,
    ShutdownError,
    _is_browser_dead_error,
    preflight,
)


# --- shared fake factory -----------------------------------------------------


def _make_fake_launch(html_factory=None, crash_after: int | None = None,
                      raise_on_launch: bool = False):
    state = SimpleNamespace(launch_calls=0, render_calls=0, teardown_calls=0, handles=[])

    def fake_launch():
        state.launch_calls += 1
        if raise_on_launch:
            raise RuntimeError("simulated launch failure")
        handle = SimpleNamespace(alive=True, id=state.launch_calls, renders_on_handle=0)
        state.handles.append(handle)
        return handle

    def fake_render(handle, url, timeout_ms):
        state.render_calls += 1
        handle.renders_on_handle += 1
        if not handle.alive:
            raise RuntimeError("Browser has been closed")
        if crash_after is not None and handle.renders_on_handle > crash_after:
            handle.alive = False
            raise RuntimeError("Browser has been closed")
        if html_factory is not None:
            return html_factory(url)
        return f"<html><body>{url}</body></html>"

    def fake_teardown(handle, timeout):
        state.teardown_calls += 1
        handle.alive = False

    return state, fake_launch, fake_render, fake_teardown


@pytest.fixture
def thread_factory():
    threads: list[RenderThread] = []

    def build(**kwargs):
        rt = RenderThread(**kwargs)
        rt.start()
        threads.append(rt)
        return rt

    yield build

    for rt in threads:
        try:
            rt.shutdown(timeout=2.0)
        except Exception:
            pass


# --- preflight ---------------------------------------------------------------


class TestPreflight:
    def test_returns_ok_when_chromium_present(self, tmp_path):
        fake_path = tmp_path / "chromium"
        fake_path.write_text("")
        fake_pw = MagicMock()
        fake_pw.__enter__.return_value.chromium.executable_path = str(fake_path)
        fake_pw.__exit__.return_value = False
        with patch("playwright.sync_api.sync_playwright", return_value=fake_pw, create=True):
            ok, msg = preflight()
        assert ok is True
        assert msg == ""

    def test_returns_remediation_on_missing_binary(self):
        fake_pw = MagicMock()
        fake_pw.__enter__.return_value.chromium.executable_path = "/nonexistent/path"
        fake_pw.__exit__.return_value = False
        with patch("playwright.sync_api.sync_playwright", return_value=fake_pw, create=True):
            ok, msg = preflight()
        assert ok is False
        assert "playwright install chromium" in msg.lower()

    def test_handles_missing_playwright_module(self):
        original = sys.modules.pop("playwright.sync_api", None)
        sys.modules["playwright.sync_api"] = None
        try:
            ok, msg = preflight()
        finally:
            if original is not None:
                sys.modules["playwright.sync_api"] = original
            else:
                sys.modules.pop("playwright.sync_api", None)
        assert ok is False
        assert "playwright" in msg.lower()


# --- browser-dead heuristic --------------------------------------------------


class TestBrowserDeadHeuristic:
    @pytest.mark.parametrize(
        "msg",
        [
            "Browser has been closed",
            "Browser has been closed.",
            "BROWSER HAS DISCONNECTED",
            "Target closed unexpectedly",
            "Target page, context or browser has been closed",
        ],
    )
    def test_recognizes_dead_browser_messages(self, msg):
        assert _is_browser_dead_error(RuntimeError(msg))

    @pytest.mark.parametrize(
        "msg",
        [
            "page.goto: Timeout 30000ms exceeded",
            "Network unreachable",
        ],
    )
    def test_does_not_flag_unrelated_errors(self, msg):
        assert not _is_browser_dead_error(RuntimeError(msg))


# --- happy path --------------------------------------------------------------


class TestHappyPath:
    def test_three_submits_share_one_browser(self, thread_factory):
        state, launch, render, teardown = _make_fake_launch()
        rt = thread_factory(launch_fn=launch, render_fn=render, teardown_fn=teardown)

        futures = [rt.submit(f"https://example.com/{i}") for i in range(3)]
        results = [f.result(timeout=2.0) for f in futures]

        assert results == [
            "<html><body>https://example.com/0</body></html>",
            "<html><body>https://example.com/1</body></html>",
            "<html><body>https://example.com/2</body></html>",
        ]
        assert state.launch_calls == 1
        assert state.render_calls == 3


class TestLazyInit:
    def test_no_submits_means_no_launch(self, thread_factory):
        state, launch, render, teardown = _make_fake_launch()
        rt = thread_factory(launch_fn=launch, render_fn=render, teardown_fn=teardown)
        time.sleep(0.1)
        rt.shutdown(timeout=2.0)

        assert state.launch_calls == 0
        assert state.teardown_calls == 0


# --- retry + circuit breaker -------------------------------------------------


class TestRetryAndFailure:
    def test_retries_non_typed_error_then_fails(self, thread_factory):
        state = SimpleNamespace(calls=0)

        def flaky_render(handle, url, timeout_ms):
            state.calls += 1
            raise RuntimeError(f"transient #{state.calls}")

        rt = thread_factory(
            retry_count=2,
            launch_fn=lambda: SimpleNamespace(alive=True),
            render_fn=flaky_render,
            teardown_fn=lambda h, t: None,
        )

        future = rt.submit("https://flaky.test/")
        with pytest.raises(RuntimeError, match="transient #3"):
            future.result(timeout=2.0)
        assert state.calls == 3

    def test_does_not_retry_on_browser_dead_error(self, thread_factory):
        state = SimpleNamespace(calls=0)

        def render(handle, url, timeout_ms):
            state.calls += 1
            raise RuntimeError("Browser has been closed")

        rt = thread_factory(
            retry_count=2,
            launch_fn=lambda: SimpleNamespace(),
            render_fn=render,
            teardown_fn=lambda h, t: None,
        )
        future = rt.submit("https://crashy.test/")
        with pytest.raises(RuntimeError, match="Browser has been closed"):
            future.result(timeout=2.0)
        assert state.calls == 1


class TestTypedErrorsDoNotRetry:
    """SEOSERPER addition: BlockedBy*Error are not retryable."""

    @pytest.mark.parametrize(
        "exc_cls",
        [BlockedByCaptchaError, BlockedByConsentError, BlockedRateLimitError],
    )
    def test_typed_render_error_breaks_out_of_retry_loop(self, thread_factory, exc_cls):
        state = SimpleNamespace(calls=0)

        def render(handle, url, timeout_ms):
            state.calls += 1
            raise exc_cls("blocked")

        rt = thread_factory(
            retry_count=3,
            launch_fn=lambda: SimpleNamespace(),
            render_fn=render,
            teardown_fn=lambda h, t: None,
        )
        future = rt.submit("https://blocked.test/")
        with pytest.raises(exc_cls):
            future.result(timeout=2.0)
        assert state.calls == 1


class TestCrashCircuitBreaker:
    def test_three_consecutive_launch_failures_disable_thread(self, thread_factory):
        attempts = SimpleNamespace(n=0)

        def always_fail():
            attempts.n += 1
            raise RuntimeError(f"launch failure {attempts.n}")

        rt = thread_factory(
            retry_count=0,
            restart_backoffs=(0.0, 0.0),
            max_consecutive_failures=3,
            launch_fn=always_fail,
            render_fn=lambda h, u, t: "",
            teardown_fn=lambda h, t: None,
        )

        for i in range(3):
            f = rt.submit(f"https://attempt{i}/")
            with pytest.raises(BrowserCrashError):
                f.result(timeout=2.0)

        f = rt.submit("https://after-disabled/")
        with pytest.raises(BrowserCrashError, match="disabled after repeated crashes"):
            f.result(timeout=2.0)

        assert attempts.n == 3
        assert rt.is_disabled() is True


class TestBrowserCrashRecovery:
    def test_relaunches_on_next_request(self, thread_factory):
        state, launch, render, teardown = _make_fake_launch(crash_after=1)

        rt = thread_factory(
            retry_count=0,
            restart_backoffs=(0.0, 0.0),
            launch_fn=launch,
            render_fn=render,
            teardown_fn=teardown,
        )

        first = rt.submit("https://a/").result(timeout=2.0)
        assert first == "<html><body>https://a/</body></html>"

        second = rt.submit("https://b/")
        with pytest.raises(RuntimeError, match="Browser has been closed"):
            second.result(timeout=2.0)

        third = rt.submit("https://c/").result(timeout=3.0)
        assert third == "<html><body>https://c/</body></html>"

        assert state.launch_calls == 2
        assert state.teardown_calls >= 1


# --- shutdown ----------------------------------------------------------------


class TestShutdownDrain:
    def test_queued_request_gets_shutdown_error(self, thread_factory):
        block = threading.Event()
        proceed = threading.Event()

        def slow_render(handle, url, timeout_ms):
            block.set()
            proceed.wait(timeout=2.0)
            return "<html/>"

        rt = thread_factory(
            launch_fn=lambda: SimpleNamespace(),
            render_fn=slow_render,
            teardown_fn=lambda h, t: None,
        )

        in_flight = rt.submit("https://blocked/")
        queued = rt.submit("https://queued/")

        assert block.wait(timeout=2.0)

        shutdown_done = threading.Event()

        def do_shutdown():
            rt.shutdown(timeout=3.0)
            shutdown_done.set()

        threading.Thread(target=do_shutdown, daemon=True).start()
        time.sleep(0.1)

        proceed.set()
        assert in_flight.result(timeout=2.0) == "<html/>"
        with pytest.raises(ShutdownError):
            queued.result(timeout=2.0)
        assert shutdown_done.wait(timeout=3.0)

    def test_teardown_called_on_shutdown(self, thread_factory):
        state, launch, render, teardown = _make_fake_launch()
        rt = thread_factory(launch_fn=launch, render_fn=render, teardown_fn=teardown)
        rt.submit("https://x/").result(timeout=2.0)
        rt.shutdown(timeout=2.0)
        assert state.teardown_calls == 1


class TestStartShutdownGuards:
    def test_double_start_raises(self, thread_factory):
        rt = thread_factory(
            launch_fn=lambda: SimpleNamespace(),
            render_fn=lambda h, u, t: "",
            teardown_fn=lambda h, t: None,
        )
        with pytest.raises(RuntimeError, match="already started"):
            rt.start()

    def test_double_shutdown_is_noop(self, thread_factory):
        rt = thread_factory(
            launch_fn=lambda: SimpleNamespace(),
            render_fn=lambda h, u, t: "",
            teardown_fn=lambda h, t: None,
        )
        rt.shutdown(timeout=2.0)
        rt.shutdown(timeout=2.0)


# --- daemon + atexit ---------------------------------------------------------


class TestDaemonAndAtexit:
    def test_render_thread_is_daemon(self, thread_factory):
        rt = thread_factory(
            launch_fn=lambda: SimpleNamespace(),
            render_fn=lambda h, u, t: "",
            teardown_fn=lambda h, t: None,
        )
        assert rt._thread is not None
        assert rt._thread.daemon is True

    def test_atexit_handler_kills_alive_proc(self):
        rt = RenderThread(
            launch_fn=lambda: SimpleNamespace(),
            render_fn=lambda h, u, t: "",
            teardown_fn=lambda h, t: None,
        )
        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 99999
        rt._handle = SimpleNamespace(proc=proc, user_data_dir=None)

        with patch("os.kill") as mock_kill:
            rt._atexit_kill_chromium()
            mock_kill.assert_called_once_with(99999, render_mod.signal.SIGKILL)

    def test_atexit_handler_skips_already_dead_proc(self):
        rt = RenderThread(
            launch_fn=lambda: SimpleNamespace(),
            render_fn=lambda h, u, t: "",
            teardown_fn=lambda h, t: None,
        )
        proc = MagicMock()
        proc.poll.return_value = 0
        proc.pid = 99999
        rt._handle = SimpleNamespace(proc=proc, user_data_dir=None)

        with patch("os.kill") as mock_kill:
            rt._atexit_kill_chromium()
            mock_kill.assert_not_called()

    def test_atexit_with_no_handle_is_noop(self):
        rt = RenderThread(
            launch_fn=lambda: SimpleNamespace(),
            render_fn=lambda h, u, t: "",
            teardown_fn=lambda h, t: None,
        )
        rt._atexit_kill_chromium()


# --- pid exposure + watchdog -------------------------------------------------


class TestPidAndWatchdog:
    def test_pid_reflects_handle(self, thread_factory):
        proc_stub = SimpleNamespace(pid=12345)
        handle = SimpleNamespace(proc=proc_stub, alive=True)
        rendered = threading.Event()

        def launch():
            return handle

        def render(h, u, t):
            rendered.set()
            return "<html/>"

        rt = thread_factory(launch_fn=launch, render_fn=render, teardown_fn=lambda h, t: None)
        assert rt.chromium_pid is None

        rt.submit("https://x/").result(timeout=2.0)
        assert rt.chromium_pid == 12345

        rt.shutdown(timeout=2.0)
        assert rt.chromium_pid is None

    def test_shutdown_spawns_watchdog(self, thread_factory):
        proc_stub = SimpleNamespace(pid=12345, poll=lambda: None)
        handle = SimpleNamespace(proc=proc_stub, alive=True)

        def launch():
            return handle

        def render(h, u, t):
            return "<html/>"

        rt = thread_factory(launch_fn=launch, render_fn=render, teardown_fn=lambda h, t: None)
        rt.submit("https://x/").result(timeout=2.0)

        with patch("seoserper.core.render._force_kill_pid_after") as mock_kill:
            rt.shutdown(timeout=2.0)
            time.sleep(0.1)
            mock_kill.assert_called_once_with(12345, 2.0)


# --- backpressure ------------------------------------------------------------


class TestQueueBackpressure:
    def test_submit_fails_when_queue_saturated(self, thread_factory):
        block_forever = threading.Event()

        def hung_render(handle, url, timeout_ms):
            block_forever.wait()
            return "<html/>"

        rt = thread_factory(
            queue_size=2,
            submit_timeout=0.3,
            launch_fn=lambda: SimpleNamespace(),
            render_fn=hung_render,
            teardown_fn=lambda h, t: None,
        )
        rt.submit("https://a/")
        rt.submit("https://b/")
        rt.submit("https://c/")

        start = time.monotonic()
        with pytest.raises(RenderQueueFullError):
            rt.submit("https://d/")
        elapsed = time.monotonic() - start
        assert 0.2 < elapsed < 1.0
        block_forever.set()


# --- restart policy (SEOSERPER addition) -------------------------------------


class TestRestartPolicyByCount:
    def test_restart_after_N_queries(self, thread_factory):
        state, launch, render, teardown = _make_fake_launch()
        rt = thread_factory(
            restart_after_queries=3,
            restart_after_seconds=3600,
            restart_backoffs=(0.0, 0.0),
            launch_fn=launch,
            render_fn=render,
            teardown_fn=teardown,
        )

        # 3 queries consume the budget; the 4th triggers a teardown+relaunch.
        for i in range(4):
            rt.submit(f"https://q{i}/").result(timeout=2.0)

        assert state.launch_calls == 2
        assert state.teardown_calls == 1
        assert state.render_calls == 4


class TestRestartPolicyByTime:
    def test_restart_after_elapsed_seconds(self, thread_factory):
        state, launch, render, teardown = _make_fake_launch()
        rt = thread_factory(
            restart_after_queries=10_000,
            restart_after_seconds=0.05,  # tight threshold for test
            restart_backoffs=(0.0, 0.0),
            launch_fn=launch,
            render_fn=render,
            teardown_fn=teardown,
        )

        rt.submit("https://first/").result(timeout=2.0)
        time.sleep(0.1)  # exceed the restart-after-seconds threshold
        rt.submit("https://second/").result(timeout=2.0)

        assert state.launch_calls == 2
        assert state.teardown_calls == 1


# --- real-render consent / captcha probes (SEOSERPER addition) ---------------


def _make_page(url="https://www.google.com/search?q=x", title="Google Search",
               content_html="<html><body>ok</body></html>",
               dom_counts: dict | None = None):
    """Build a MagicMock page with controllable url/title/content/locators."""
    dom_counts = dom_counts or {}

    page = MagicMock()
    page.url = url
    page.title.return_value = title
    page.content.return_value = content_html

    def make_locator(selector):
        loc = MagicMock()
        loc.count.return_value = dom_counts.get(selector, 0)
        return loc

    page.locator.side_effect = make_locator
    return page


def _make_handle(page):
    ctx = MagicMock()
    ctx.new_page.return_value = page
    browser = MagicMock()
    browser.new_context.return_value = ctx
    return render_mod._ChromiumHandle(
        proc=None, playwright=None, browser=browser, user_data_dir=None
    )


class TestRealRenderConsentProbe:
    def test_consent_redirect_url_raises(self):
        page = _make_page(url="https://consent.google.com/m?continue=...")
        handle = _make_handle(page)
        with pytest.raises(BlockedByConsentError):
            render_mod._real_render(handle, "https://www.google.com/search?q=x", 1000)

    def test_consent_form_dom_raises(self):
        page = _make_page(dom_counts={"form[action*='consent']": 1})
        handle = _make_handle(page)
        with pytest.raises(BlockedByConsentError):
            render_mod._real_render(handle, "https://www.google.com/search?q=x", 1000)


class TestRealRenderCaptchaProbe:
    def test_sorry_url_raises_rate_limit(self):
        page = _make_page(url="https://www.google.com/sorry/index?continue=...")
        handle = _make_handle(page)
        with pytest.raises(BlockedRateLimitError):
            render_mod._real_render(handle, "https://www.google.com/search?q=x", 1000)

    @pytest.mark.parametrize(
        "selector", ["#recaptcha", "#captcha-form", ".g-recaptcha"]
    )
    def test_captcha_dom_raises(self, selector):
        page = _make_page(dom_counts={selector: 1})
        handle = _make_handle(page)
        with pytest.raises(BlockedByCaptchaError):
            render_mod._real_render(handle, "https://www.google.com/search?q=x", 1000)

    def test_before_you_continue_title_raises(self):
        page = _make_page(title="Before you continue to Google")
        handle = _make_handle(page)
        with pytest.raises(BlockedByCaptchaError):
            render_mod._real_render(handle, "https://www.google.com/search?q=x", 1000)

    def test_happy_page_returns_content(self):
        page = _make_page(content_html="<html><body>result</body></html>")
        handle = _make_handle(page)
        html = render_mod._real_render(handle, "https://www.google.com/search?q=x", 1000)
        assert html == "<html><body>result</body></html>"


class TestContextReuse:
    def test_first_render_creates_context_second_recycles(self):
        page = _make_page()
        ctx = MagicMock()
        ctx.new_page.return_value = page
        browser = MagicMock()
        browser.new_context.return_value = ctx
        handle = render_mod._ChromiumHandle(
            proc=None, playwright=None, browser=browser, user_data_dir=None
        )

        render_mod._real_render(handle, "https://a/", 1000)
        render_mod._real_render(handle, "https://b/", 1000)

        assert browser.new_context.call_count == 1
        assert ctx.clear_cookies.called
        assert ctx.clear_permissions.called


# --- typed exception hierarchy -----------------------------------------------


class TestTypedExceptionHierarchy:
    def test_all_blocked_errors_subclass_render_error(self):
        assert issubclass(BlockedByCaptchaError, RenderError)
        assert issubclass(BlockedByConsentError, RenderError)
        assert issubclass(BlockedRateLimitError, RenderError)
        assert issubclass(BrowserCrashError, RenderError)
