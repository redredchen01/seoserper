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


def _cache_key(query: str, lang: str, country: str, engine: str = "google") -> str:
    """ASCII-debuggable key. Engine dimension added in plan 005 Unit 3.

    Legacy 3-part keys (pre-plan-005) become unreadable but harmless —
    cache_get misses on a mismatched key, and old rows age out via TTL.
    """
    return f"{engine}|{query}|{lang}|{country}"


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
    engine: str = "google",
    db_path: str | None = None,
    ttl_seconds: int | None = None,
    timeout: float = config.SERPAPI_TIMEOUT_SECONDS,
) -> dict[SurfaceName, ParseResult]:
    """Cache-aware fetch. Signature matches fetch_serp_data + db_path kwarg.

    Cache key includes engine, so Google and Bing responses for the same
    (query, lang, country) never cross-contaminate.
    """
    ttl = ttl_seconds if ttl_seconds is not None else config.SERP_CACHE_TTL_SECONDS
    key = _cache_key(query, lang, country, engine)

    cached_payload = cache_get(key, ttl, db_path=db_path)
    if cached_payload is not None:
        return extract_surfaces(cached_payload, query=query)

    payload, failure = fetch_serp_raw(
        query, lang, country, api_key=api_key, engine=engine, timeout=timeout
    )
    if failure is not None:
        return _both_failed(failure)

    assert payload is not None
    result = extract_surfaces(payload, query=query)

    if _result_is_cacheable(result):
        cache_put(key, payload, db_path=db_path, ttl_seconds=ttl)

    return result
