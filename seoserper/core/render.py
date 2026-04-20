"""Render thread: owns Chromium subprocess + sync Playwright on a single thread.

Ported from workspace sibling ``claude-crawler-clean/crawler/core/render.py``
with three SEOSERPER-specific deltas on top:

1. Typed exception hierarchy (``RenderError`` + 5 subclasses) so the engine
   can map render failures to ``FailureCategory`` enum values without
   string-sniffing.
2. Consent + captcha probes in ``_real_render`` — URL prefix and DOM
   fingerprints mapped to ``BlockedByConsentError`` / ``BlockedByCaptchaError``
   / ``BlockedRateLimitError``.
3. Restart policy: after N queries or T seconds of browser uptime, the
   thread tears down Chromium and re-launches on the next submit.
   Prevents RSS drift on long Streamlit sessions and reduces Google's
   ability to fingerprint a single sustained CDP session.

The threading model, queue plumbing, shutdown watchdog, atexit safety net,
and ``_is_browser_dead_error`` heuristic are preserved verbatim (patterns
proven in production under the claude-crawler workload).
"""

from __future__ import annotations

import atexit
import logging
import os
import queue
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any

from seoserper import config

logger = logging.getLogger(__name__)

_SHUTDOWN_SENTINEL = None
_QUEUE_GET_TIMEOUT = 0.5
_DEFAULT_RESTART_BACKOFFS = (1.0, 5.0)
_DEFAULT_MAX_CONSECUTIVE_FAILURES = 3
_DEVTOOLS_PORT_FILE = "DevToolsActivePort"
_CHROMIUM_BOOT_TIMEOUT = 10.0
_DEFAULT_QUEUE_SIZE = 16
_DEFAULT_SUBMIT_TIMEOUT = 60.0
_DEFAULT_SHUTDOWN_TIMEOUT = 5.0


# --- Typed exceptions --------------------------------------------------------


class RenderError(RuntimeError):
    """Base class for render-path failures that carry a FailureCategory."""


class BlockedByCaptchaError(RenderError):
    """Google served a /sorry interstitial or DOM captcha artifact."""


class BlockedByConsentError(RenderError):
    """Google redirected to consent.google.com or served a consent form."""


class BlockedRateLimitError(RenderError):
    """Explicit rate-limit / sorry page observed after goto."""


class SelectorNotFoundError(RenderError):
    """Expected DOM structure missing — treated as selector drift."""


class BrowserCrashError(RenderError):
    """Chromium crashed or the Playwright CDP session disconnected."""


class ShutdownError(RuntimeError):
    """Raised on a Future when the render thread shuts down before completing."""


class RenderQueueFullError(RuntimeError):
    """submit() could not enqueue within ``submit_timeout`` — saturation signal."""


# --- Inter-thread transport --------------------------------------------------


@dataclass
class RenderRequest:
    url: str
    future: Future  # Future[str] — resolved HTML or exception


@dataclass
class _ChromiumHandle:
    proc: subprocess.Popen | None
    playwright: Any
    browser: Any
    user_data_dir: str | None
    context: Any = None


# --- Preflight ---------------------------------------------------------------


def preflight() -> tuple[bool, str]:
    """Quick check that Playwright + Chromium are installed. Cheap; call at boot."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return (
            False,
            "Playwright 未安装。请运行: pip install playwright && playwright install chromium",
        )
    try:
        with sync_playwright() as p:
            path = p.chromium.executable_path
            if not path or not os.path.exists(path):
                return (False, "Chromium 二进制缺失。请运行: playwright install chromium")
        return (True, "")
    except Exception as exc:  # pragma: no cover — exercised via mocked sync_playwright
        msg = str(exc)
        lowered = msg.lower()
        if (
            "executable doesn't exist" in lowered
            or "browsertype.executable_path" in lowered
            or "no such file" in lowered
        ):
            return (False, "Chromium 二进制缺失。请运行: playwright install chromium")
        return (False, f"Playwright preflight 失败: {msg}")


# --- Real launch / render / teardown -----------------------------------------


def _real_launch() -> _ChromiumHandle:
    from playwright.sync_api import sync_playwright

    chromium_exe = _resolve_chromium_path()
    user_data_dir = tempfile.mkdtemp(prefix="seoserper-chromium-")
    args = [
        chromium_exe,
        "--remote-debugging-port=0",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={user_data_dir}",
        "--headless=new",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-blink-features=AutomationControlled",
        "--disable-features=Translate,BackForwardCache",
        "--disable-dev-shm-usage",
        "--no-sandbox",
    ]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    port = _wait_for_devtools_port(proc, user_data_dir, _CHROMIUM_BOOT_TIMEOUT)

    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
    except Exception:
        playwright.stop()
        _kill_proc(proc, _DEFAULT_SHUTDOWN_TIMEOUT)
        shutil.rmtree(user_data_dir, ignore_errors=True)
        raise

    return _ChromiumHandle(
        proc=proc,
        playwright=playwright,
        browser=browser,
        user_data_dir=user_data_dir,
    )


def _resolve_chromium_path() -> str:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        path = p.chromium.executable_path
    if not path or not os.path.exists(path):
        raise RuntimeError("Chromium binary missing. Run: playwright install chromium")
    return path


def _wait_for_devtools_port(
    proc: subprocess.Popen, user_data_dir: str, timeout: float
) -> int:
    port_file = os.path.join(user_data_dir, _DEVTOOLS_PORT_FILE)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"Chromium exited prematurely (code={proc.returncode}) "
                f"before publishing DevToolsActivePort"
            )
        if os.path.exists(port_file):
            try:
                with open(port_file) as fh:
                    first_line = fh.readline().strip()
                return int(first_line)
            except (OSError, ValueError):
                pass
        time.sleep(0.05)
    raise TimeoutError(f"Chromium did not publish DevToolsActivePort within {timeout:.1f}s")


_CONSENT_URL_PREFIXES = ("https://consent.google.", "http://consent.google.")
_SORRY_URL_MARKERS = ("/sorry/index", "/sorry?")


def _real_render(handle: _ChromiumHandle, url: str, timeout_ms: int) -> str:
    """Render one URL via the live browser, probing for consent/captcha post-goto.

    Context reuse saves the ~50-200ms new_context() cost per page. Consent /
    captcha probes translate Google's anti-bot interstitials into typed
    exceptions the engine maps to FailureCategory enum values.
    """
    if handle.context is None:
        handle.context = handle.browser.new_context()
    else:
        try:
            handle.context.clear_cookies()
            handle.context.clear_permissions()
        except Exception:
            logger.warning("context.clear_* raised; rebuilding context", exc_info=True)
            try:
                handle.context.close()
            except Exception:
                pass
            handle.context = None
            handle.context = handle.browser.new_context()

    page = handle.context.new_page()
    try:
        page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")

        current_url = ""
        try:
            current_url = page.url or ""
        except Exception:
            pass

        for prefix in _CONSENT_URL_PREFIXES:
            if current_url.startswith(prefix):
                raise BlockedByConsentError(f"consent redirect: {current_url}")
        for marker in _SORRY_URL_MARKERS:
            if marker in current_url:
                raise BlockedRateLimitError(f"sorry page: {current_url}")

        # DOM probes. Failures of the probes themselves (e.g. page already
        # navigated) are swallowed — we only act on positive hits.
        if _safe_locator_count(page, "form[action*='consent']") > 0:
            raise BlockedByConsentError("consent form DOM present")
        if (
            _safe_locator_count(page, "#recaptcha") > 0
            or _safe_locator_count(page, "#captcha-form") > 0
            or _safe_locator_count(page, ".g-recaptcha") > 0
        ):
            raise BlockedByCaptchaError("captcha DOM present")

        try:
            title = page.title() or ""
        except Exception:
            title = ""
        if title.strip().startswith("Before you continue"):
            raise BlockedByCaptchaError(f"captcha-like title: {title!r}")

        return page.content()
    finally:
        try:
            page.close()
        except Exception:
            logger.debug("page.close raised during render cleanup", exc_info=True)


def _safe_locator_count(page: Any, selector: str) -> int:
    try:
        return page.locator(selector).count()
    except Exception:
        return 0


def _real_teardown(handle: _ChromiumHandle, timeout: float) -> None:
    try:
        if handle.context is not None:
            try:
                handle.context.close()
            except Exception:
                logger.debug("context.close raised", exc_info=True)
        if handle.browser is not None:
            try:
                handle.browser.close()
            except Exception:
                logger.debug("browser.close raised", exc_info=True)
        if handle.playwright is not None:
            try:
                handle.playwright.stop()
            except Exception:
                logger.debug("playwright.stop raised", exc_info=True)
    finally:
        if handle.proc is not None:
            _kill_proc(handle.proc, timeout)
        if handle.user_data_dir:
            shutil.rmtree(handle.user_data_dir, ignore_errors=True)


def _force_kill_pid_after(pid: int, delay: float) -> None:
    """Watchdog: sleep ``delay`` then SIGKILL ``pid`` if still alive."""
    time.sleep(delay)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    except PermissionError:
        logger.error("Watchdog cannot signal PID %d (permission denied)", pid)
        return
    try:
        os.kill(pid, signal.SIGKILL)
        logger.warning("Watchdog SIGKILLed Chromium PID %d after %.1fs", pid, delay)
    except ProcessLookupError:
        pass
    except Exception:
        logger.exception("Watchdog SIGKILL of PID %d raised", pid)


def _kill_proc(proc: subprocess.Popen, timeout: float) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
            return
        except subprocess.TimeoutExpired:
            pass
        proc.kill()
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            logger.error("Chromium PID %d did not exit after SIGKILL", proc.pid)
    except Exception:
        logger.exception("Error tearing down Chromium PID %d", proc.pid)


# --- Browser-dead heuristic (preserved from reference) -----------------------

_BROWSER_DEAD_MARKERS = (
    "browser has been closed",
    "browser has disconnected",
    "target closed",
    "target page, context or browser has been closed",
)

try:  # pragma: no cover
    from playwright.sync_api import Error as _PlaywrightError
except ImportError:
    _PlaywrightError = None  # type: ignore[assignment]
try:  # pragma: no cover
    from playwright._impl._errors import TargetClosedError as _TargetClosedError
except (ImportError, AttributeError):
    _TargetClosedError = None  # type: ignore[assignment]


def _is_browser_dead_error(exc: BaseException) -> bool:
    """Detect 'browser crashed / disconnected' Playwright errors."""
    if _TargetClosedError is not None and isinstance(exc, _TargetClosedError):
        return True
    msg = str(exc).lower()
    msg_matches = any(marker in msg for marker in _BROWSER_DEAD_MARKERS)
    if _PlaywrightError is not None and isinstance(exc, _PlaywrightError):
        return msg_matches
    return msg_matches


# --- The thread --------------------------------------------------------------

LaunchFn = Callable[[], Any]
RenderFn = Callable[[Any, str, int], str]
TeardownFn = Callable[[Any, float], None]


class RenderThread:
    """Owns a single Chromium subprocess + Playwright CDP connection on its own thread."""

    def __init__(
        self,
        *,
        timeout: float = config.RENDER_TIMEOUT_SECONDS,
        retry_count: int = 0,
        shutdown_timeout: float = _DEFAULT_SHUTDOWN_TIMEOUT,
        max_consecutive_failures: int = _DEFAULT_MAX_CONSECUTIVE_FAILURES,
        restart_backoffs: tuple[float, ...] = _DEFAULT_RESTART_BACKOFFS,
        queue_size: int = _DEFAULT_QUEUE_SIZE,
        submit_timeout: float = _DEFAULT_SUBMIT_TIMEOUT,
        restart_after_queries: int = config.BROWSER_RESTART_AFTER_QUERIES,
        restart_after_seconds: float = config.BROWSER_RESTART_AFTER_SECONDS,
        launch_fn: LaunchFn | None = None,
        render_fn: RenderFn | None = None,
        teardown_fn: TeardownFn | None = None,
    ):
        self._timeout = timeout
        self._retry_count = retry_count
        self._shutdown_timeout = shutdown_timeout
        self._max_consecutive_failures = max_consecutive_failures
        self._restart_backoffs = restart_backoffs
        self._submit_timeout = submit_timeout
        self._restart_after_queries = restart_after_queries
        self._restart_after_seconds = restart_after_seconds

        self._launch_fn: LaunchFn = launch_fn or _real_launch
        self._render_fn: RenderFn = render_fn or _real_render
        self._teardown_fn: TeardownFn = teardown_fn or _real_teardown

        self._queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self._thread: threading.Thread | None = None
        self._started = False
        self._shutdown_called = False
        self._shutdown_event = threading.Event()

        self._handle: Any = None
        self._consecutive_failures = 0
        self._disabled = False
        self._queries_since_restart = 0
        self._browser_started_at: float = 0.0

        atexit.register(self._atexit_kill_chromium)

    # --- public API ---

    def start(self) -> None:
        if self._started:
            raise RuntimeError("RenderThread already started")
        self._started = True
        self._thread = threading.Thread(target=self._run, name="seoserper-render", daemon=True)
        self._thread.start()

    def submit(self, url: str) -> Future:
        future: Future = Future()
        try:
            self._queue.put(
                RenderRequest(url=url, future=future),
                timeout=self._submit_timeout,
            )
        except queue.Full as exc:
            raise RenderQueueFullError(
                f"render queue saturated for >{self._submit_timeout}s "
                f"(disabled={self._disabled})"
            ) from exc
        return future

    def shutdown(self, timeout: float | None = None) -> None:
        if self._shutdown_called:
            return
        self._shutdown_called = True
        self._shutdown_event.set()
        if self._thread is None:
            return

        wait = timeout if timeout is not None else self._shutdown_timeout + 5.0

        chromium_pid = self.chromium_pid
        if chromium_pid is not None:
            watchdog = threading.Thread(
                target=_force_kill_pid_after,
                args=(chromium_pid, wait),
                name=f"seoserper-render-watchdog-{chromium_pid}",
                daemon=True,
            )
            watchdog.start()

        try:
            self._queue.put(_SHUTDOWN_SENTINEL, timeout=wait)
        except queue.Full:
            logger.error("RenderThread queue full during shutdown — sentinel not enqueued")

        self._thread.join(timeout=wait)
        if self._thread.is_alive():
            logger.error("RenderThread did not exit within %.1fs", wait)

        try:
            atexit.unregister(self._atexit_kill_chromium)
        except Exception:
            pass

    @property
    def chromium_pid(self) -> int | None:
        if self._handle is None or getattr(self._handle, "proc", None) is None:
            return None
        return self._handle.proc.pid

    def is_disabled(self) -> bool:
        return self._disabled

    def _atexit_kill_chromium(self) -> None:
        try:
            handle = self._handle
            if handle is None:
                return
            proc = getattr(handle, "proc", None)
            if proc is None:
                return
            if proc.poll() is not None:
                return
            try:
                os.kill(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            user_data_dir = getattr(handle, "user_data_dir", None)
            if user_data_dir:
                shutil.rmtree(user_data_dir, ignore_errors=True)
        except Exception:
            pass

    # --- internal ---

    def _run(self) -> None:
        try:
            while True:
                try:
                    request = self._queue.get(timeout=_QUEUE_GET_TIMEOUT)
                except queue.Empty:
                    continue

                if request is _SHUTDOWN_SENTINEL:
                    self._drain_queue_with_shutdown_error()
                    return

                if self._shutdown_event.is_set():
                    if isinstance(request, RenderRequest) and not request.future.done():
                        request.future.set_exception(
                            ShutdownError("render thread shutting down")
                        )
                    continue

                self._handle_render(request)
        finally:
            self._teardown_browser()

    def _handle_render(self, req: RenderRequest) -> None:
        if self._disabled:
            req.future.set_exception(
                BrowserCrashError("render thread disabled after repeated crashes")
            )
            return

        self._maybe_restart_browser()

        if not self._ensure_browser_or_disable():
            req.future.set_exception(
                BrowserCrashError("render thread disabled after repeated crashes")
            )
            return

        last_exc: BaseException | None = None
        for attempt in range(self._retry_count + 1):
            try:
                html = self._render_fn(self._handle, req.url, int(self._timeout * 1000))
                self._consecutive_failures = 0
                self._queries_since_restart += 1
                req.future.set_result(html)
                return
            except RenderError as exc:
                # Typed Google-blocking errors (captcha / consent / rate_limit)
                # are not retryable — the engine owns retry semantics.
                last_exc = exc
                break
            except BaseException as exc:
                last_exc = exc
                if _is_browser_dead_error(exc):
                    logger.warning("Browser died during render of %s: %s", req.url, exc)
                    self._teardown_browser()
                    self._consecutive_failures += 1
                    if self._consecutive_failures >= self._max_consecutive_failures:
                        self._disabled = True
                    break
                logger.warning(
                    "Render attempt %d/%d failed for %s: %s",
                    attempt + 1,
                    self._retry_count + 1,
                    req.url,
                    exc,
                )

        if last_exc is None:
            last_exc = RuntimeError("render failed without raising — should not happen")
        req.future.set_exception(last_exc)

    def _maybe_restart_browser(self) -> None:
        """Check restart thresholds; if tripped, tear down so next launch is fresh."""
        if self._handle is None:
            return
        if self._queries_since_restart >= self._restart_after_queries:
            logger.info(
                "Restart policy: %d queries elapsed — recycling Chromium",
                self._queries_since_restart,
            )
            self._teardown_browser()
            return
        if (
            self._browser_started_at > 0
            and time.monotonic() - self._browser_started_at >= self._restart_after_seconds
        ):
            logger.info("Restart policy: %.0fs elapsed — recycling Chromium", self._restart_after_seconds)
            self._teardown_browser()

    def _ensure_browser_or_disable(self) -> bool:
        if self._handle is not None:
            return True
        if self._consecutive_failures > 0:
            idx = min(self._consecutive_failures - 1, len(self._restart_backoffs) - 1)
            backoff = self._restart_backoffs[idx]
            logger.info(
                "Restarting Chromium after %d failures (backoff %.1fs)",
                self._consecutive_failures,
                backoff,
            )
            time.sleep(backoff)

        try:
            self._handle = self._launch_fn()
            self._queries_since_restart = 0
            self._browser_started_at = time.monotonic()
            return True
        except BaseException as exc:
            logger.exception("Chromium launch failed: %s", exc)
            self._handle = None
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._max_consecutive_failures:
                self._disabled = True
            return False

    def _teardown_browser(self) -> None:
        if self._handle is None:
            return
        try:
            self._teardown_fn(self._handle, self._shutdown_timeout)
        except BaseException:
            logger.exception("Render teardown raised")
        finally:
            self._handle = None

    def _drain_queue_with_shutdown_error(self) -> None:
        while True:
            try:
                request = self._queue.get_nowait()
            except queue.Empty:
                return
            if request is _SHUTDOWN_SENTINEL:
                continue
            if isinstance(request, RenderRequest) and not request.future.done():
                request.future.set_exception(ShutdownError("render thread shutting down"))
