"""Cached wrapper around fetch_serp_raw / extract_surfaces.

Caches OK responses by (query, lang, country) key with TTL enforced at
read-time. Failed fetches are NOT cached — each Submit after a failure
reruns the upstream call.

Cache stores the raw SerpAPI payload, not the extracted dict. This lets
extractor logic evolve without invalidating stored rows — re-extraction
runs on every hit for a few ms.
"""

from __future__ import annotations

from seoserper import config
from seoserper.fetchers.serp import (
    _both_failed,
    extract_surfaces,
    fetch_serp_raw,
)
from seoserper.models import ParseResult, SurfaceName, SurfaceStatus
from seoserper.storage import cache_get, cache_put


def _cache_key(query: str, lang: str, country: str) -> str:
    """ASCII-debuggable key. SQLite PK handles uniqueness; no hashing."""
    return f"{query}|{lang}|{country}"


def _result_is_cacheable(result: dict[SurfaceName, ParseResult]) -> bool:
    """Cache only responses where every surface is OK or EMPTY.

    A FAILED surface signals a transient issue (rate limit, network,
    malformed body); don't lock it into the cache.
    """
    return all(
        r.status in (SurfaceStatus.OK, SurfaceStatus.EMPTY)
        for r in result.values()
    )


def fetch_serp_data_cached(
    query: str,
    lang: str,
    country: str,
    *,
    api_key: str,
    db_path: str | None = None,
    ttl_seconds: int | None = None,
    timeout: float = config.SERPAPI_TIMEOUT_SECONDS,
) -> dict[SurfaceName, ParseResult]:
    """Cache-aware fetch. Signature matches fetch_serp_data + db_path kwarg.

    Args:
        query / lang / country: request parameters.
        api_key: SerpAPI key (required on cache miss, ignored on hit).
        db_path: SQLite path for the cache table. None uses config.DB_PATH.
        ttl_seconds: override for the cache window. None uses config default.
        timeout: upstream HTTP timeout on miss.

    Returns the same ``dict[SurfaceName, ParseResult]`` shape as the
    uncached fetch_serp_data — callers (engine) don't need to know which
    code path served them.
    """
    ttl = ttl_seconds if ttl_seconds is not None else config.SERP_CACHE_TTL_SECONDS
    key = _cache_key(query, lang, country)

    cached_payload = cache_get(key, ttl, db_path=db_path)
    if cached_payload is not None:
        return extract_surfaces(cached_payload, query=query)

    payload, failure = fetch_serp_raw(
        query, lang, country, api_key=api_key, timeout=timeout
    )
    if failure is not None:
        # Don't cache failures. Next call gets a fresh retry.
        return _both_failed(failure)

    assert payload is not None  # invariant from fetch_serp_raw
    result = extract_surfaces(payload, query=query)

    if _result_is_cacheable(result):
        cache_put(key, payload, db_path=db_path, ttl_seconds=ttl)

    return result
