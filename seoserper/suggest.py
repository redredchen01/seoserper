"""Suggest library — stable Python API over the raw fetcher + resilience layer.

Public API: ``get_suggestions(q, hl, gl, limit, fresh, retry) -> SuggestResult``.

Wraps ``seoserper.fetchers.suggest.fetch_suggestions`` with:
- query normalization (cache + echo form) separate from the upstream wire form
- SQLite-backed status-aware cache (see ``suggest_cache_get`` / ``suggest_cache_put``)
- one transient-only retry (NETWORK_ERROR), gated by the ``retry`` kwarg
- safe-degrade: never raises on upstream failure
- structured logging with privacy-aware query hashing (raw ``q`` never logged)

Streamlit and any Python agent/script/notebook should call this function
instead of the raw fetcher. Engine context (``AnalysisEngine``) pins
``retry=False`` so ``retry_failed_surfaces`` is the sole retry layer.
"""

from __future__ import annotations

import hashlib
import logging
import time
import unicodedata
from time import monotonic

from seoserper import config
from seoserper.fetchers.suggest import SuggestResult, fetch_suggestions
from seoserper.models import FailureCategory, Suggestion, SurfaceStatus
from seoserper.storage import suggest_cache_get, suggest_cache_put

logger = logging.getLogger("seoserper.suggest")

# Hardcoded — no current consumer tuning this (plan 005 Key Decision).
_MAX_RETRIES = 1

# O(1) lookup for hl validation. Extracted from config.SUPPORTED_LOCALES.
_SUPPORTED_HL: frozenset[str] = frozenset(
    lang for lang, _country, _label in config.SUPPORTED_LOCALES
)


def _validate_and_strip(q: str) -> str:
    """Return the upstream-wire form of q: stripped + length/charset validated.

    Raises ValueError on programmer errors (non-str, empty, too-long, control
    chars). The returned string preserves the caller's case and Unicode form —
    this is what gets sent to Google so case-sensitive ranking is untouched.
    """
    if not isinstance(q, str):
        raise ValueError(f"q must be str, got {type(q).__name__}")
    upstream = q.strip()
    if not upstream:
        raise ValueError("q is empty after strip")
    if len(upstream) > config.SUGGEST_Q_MAX_LENGTH:
        raise ValueError(
            f"q length {len(upstream)} exceeds SUGGEST_Q_MAX_LENGTH={config.SUGGEST_Q_MAX_LENGTH}"
        )
    if any(ord(c) < 0x20 or 0x7F <= ord(c) < 0xA0 for c in upstream):
        raise ValueError("q contains control characters")
    return upstream


def _normalize_cache_form(upstream_q: str) -> str:
    """Cache-key + echo + q_hash form: NFKC + lowercase + whitespace-collapsed."""
    folded = unicodedata.normalize("NFKC", upstream_q).lower()
    return " ".join(folded.split())


def _cache_key(normalized_q: str, hl: str, gl: str) -> str:
    """Cache key shape; `google` prefix reserved for future providers."""
    return f"google|{normalized_q}|{hl}|{gl}"


def _q_hash(normalized_q: str) -> str:
    """8-char sha256 prefix for log correlation; raw q is never logged."""
    return hashlib.sha256(normalized_q.encode("utf-8")).hexdigest()[:8]


def _google_fetch_with_retry(
    upstream_q: str, hl: str, gl: str, retry: bool
) -> SuggestResult:
    """Call raw fetcher; retry once on NETWORK_ERROR when retry=True.

    Does NOT retry on BLOCKED_RATE_LIMIT (won't heal in 200 ms) or
    SELECTOR_NOT_FOUND (upstream shape drift doesn't fix itself).
    """
    result = fetch_suggestions(upstream_q, hl, gl)
    if (
        retry
        and result.status is SurfaceStatus.FAILED
        and result.failure_category is FailureCategory.NETWORK_ERROR
    ):
        time.sleep(config.SUGGEST_RETRY_DELAY_SECONDS)
        result = fetch_suggestions(upstream_q, hl, gl)
    return result


def _static_fallback(
    normalized_q: str, hl: str, gl: str, limit: int
) -> SuggestResult:
    """Stub — real implementation deferred to a follow-up plan.

    TODO(static-fallback): full implementation (SQL over jobs ⋈ surfaces,
    dedup, rank sort, locale filter) lives in a follow-up brainstorm/plan
    triggered by observable cache-miss + upstream-down signal. For now this
    returns empty items so the flag-on code path is real (not
    NotImplementedError) but produces no data.
    """
    return SuggestResult(status=SurfaceStatus.FAILED, items=[])


def get_suggestions(
    q: str,
    hl: str = "zh-TW",
    gl: str = "TW",
    limit: int = 10,
    fresh: bool = False,
    retry: bool = True,
) -> SuggestResult:
    """Keyword suggestion with cache + retry + safe-degrade.

    Never raises on upstream failure — recoverable errors (timeout, non-2xx,
    parse failure) translate to ``status=FAILED`` with
    ``warnings=["upstream_unavailable"]`` and empty items. Only programmer
    errors (bad q / hl / limit / fresh types) raise ``ValueError`` synchronously.

    Args:
        q: caller query. Stripped + NFKC-lowercased for cache and echo form,
            but the stripped-but-unlowercased original is sent on the wire —
            Google's case-sensitive ranking is not silently altered.
        hl / gl: locale. ``hl`` must be in ``config.SUPPORTED_LOCALES``;
            ``gl`` is case-folded to lowercase for cache-key consistency.
        limit: 1..20. Applied as a post-read slice; the cache stores the full
            upstream list so varied-limit callers share one cache row.
        fresh: skip cache read; still write-through on success.
        retry: allow one NETWORK_ERROR retry. Default True preserves
            resilience for direct callers. AnalysisEngine pins retry=False
            so its retry_failed_surfaces is the only retry layer, preventing
            compound library+engine retries from amplifying upstream load.

    Returns:
        ``SuggestResult`` with ``status / items (sliced to limit) /
        provider_used / from_cache / latency_ms / normalized_query / warnings``.
    """
    if hl not in _SUPPORTED_HL:
        raise ValueError(
            f"hl={hl!r} not in config.SUPPORTED_LOCALES "
            f"({sorted(_SUPPORTED_HL)})"
        )
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 20:
        raise ValueError(f"limit must be int in 1..20, got {limit!r}")
    if not isinstance(fresh, bool):
        raise ValueError(f"fresh must be bool, got {type(fresh).__name__}")
    if not isinstance(retry, bool):
        raise ValueError(f"retry must be bool, got {type(retry).__name__}")

    upstream_q = _validate_and_strip(q)
    gl_norm = gl.lower()
    normalized_q = _normalize_cache_form(upstream_q)
    cache_key = _cache_key(normalized_q, hl, gl_norm)
    q_hash_val = _q_hash(normalized_q)
    start = monotonic()

    def _elapsed_ms() -> int:
        return int((monotonic() - start) * 1000)

    def _log(status_str: str, provider_used: str, from_cache: bool) -> None:
        logger.info(
            "suggest_call",
            extra={
                "q_hash": q_hash_val,
                "hl": hl,
                "gl": gl_norm,
                "limit": limit,
                "fresh": fresh,
                "retry": retry,
                "provider_used": provider_used,
                "status": status_str,
                "latency_ms": _elapsed_ms(),
                "from_cache": from_cache,
            },
        )

    # 1. Cache read (unless fresh).
    if not fresh:
        hit = suggest_cache_get(
            cache_key,
            config.SUGGEST_CACHE_TTL_SECONDS,
            config.SUGGEST_EMPTY_TTL_SECONDS,
        )
        if hit is not None:
            items_full = [Suggestion(**i) for i in hit["items"]]
            status = (
                SurfaceStatus.OK if hit["status"] == "ok" else SurfaceStatus.EMPTY
            )
            _log(status.value, "cache", True)
            return SuggestResult(
                status=status,
                items=items_full[:limit],
                provider_used="cache",
                from_cache=True,
                latency_ms=_elapsed_ms(),
                normalized_query=normalized_q,
            )

    # 2. Upstream fetch (with optional retry).
    try:
        raw = _google_fetch_with_retry(upstream_q, hl, gl, retry=retry)
    except Exception:
        # Defensive net — raw fetcher already translates known error classes
        # to FAILED. This catches unexpected stdlib / dependency bugs so the
        # R3 "never raises on upstream failure" invariant holds.
        logger.exception(
            "suggest_call: unexpected upstream exception",
            extra={"q_hash": q_hash_val},
        )
        _log("failed", "none", False)
        return SuggestResult(
            status=SurfaceStatus.FAILED,
            items=[],
            provider_used="none",
            from_cache=False,
            latency_ms=_elapsed_ms(),
            normalized_query=normalized_q,
            warnings=["upstream_error"],
        )

    # 3. Cache-write on OK / EMPTY. Return items sliced to `limit`.
    if raw.status is SurfaceStatus.OK:
        items_full = list(raw.items)
        items_dicts = [{"text": it.text, "rank": it.rank} for it in items_full]
        suggest_cache_put(
            cache_key,
            "ok",
            items_dicts,
            ttl_seconds=config.SUGGEST_CACHE_TTL_SECONDS,
        )
        _log("ok", "google", False)
        return SuggestResult(
            status=SurfaceStatus.OK,
            items=items_full[:limit],
            provider_used="google",
            from_cache=False,
            latency_ms=_elapsed_ms(),
            normalized_query=normalized_q,
        )

    if raw.status is SurfaceStatus.EMPTY:
        suggest_cache_put(
            cache_key,
            "empty",
            [],
            ttl_seconds=config.SUGGEST_EMPTY_TTL_SECONDS,
        )
        _log("empty", "google", False)
        return SuggestResult(
            status=SurfaceStatus.EMPTY,
            items=[],
            provider_used="google",
            from_cache=False,
            latency_ms=_elapsed_ms(),
            normalized_query=normalized_q,
        )

    # 4. FAILED: optional static fallback, else degraded-empty. Never cached.
    if config.SUGGEST_STATIC_FALLBACK:
        static = _static_fallback(normalized_q, hl, gl_norm, limit)
        if static.items:
            _log("ok", "static", False)
            return SuggestResult(
                status=SurfaceStatus.OK,
                items=list(static.items)[:limit],
                provider_used="static",
                from_cache=False,
                latency_ms=_elapsed_ms(),
                normalized_query=normalized_q,
            )

    _log("failed", "none", False)
    return SuggestResult(
        status=SurfaceStatus.FAILED,
        items=[],
        failure_category=raw.failure_category,
        provider_used="none",
        from_cache=False,
        latency_ms=_elapsed_ms(),
        normalized_query=normalized_q,
        warnings=["upstream_unavailable"],
    )


__all__ = ["get_suggestions"]
