"""SerpAPI (serpapi.com) fetcher for PAA + Related Searches.

Single call to SerpAPI's ``engine=google`` endpoint populates two of our three
surfaces in one round trip — PAA (from ``related_questions``) and Related
(from ``related_searches``). The Suggest surface is a separate provider
(``suggestqueries.google.com``, free) and lives in ``fetchers/suggest.py``.

Contract (per https://serpapi.com/search-api, verified 2026-04):

    {
      "search_metadata": {...},
      "search_parameters": {...},
      "organic_results": [...],
      "related_questions": [{"question": "...", "snippet": "..."}, ...],
      "related_searches": [{"query": "..."}, ...]
    }

Quota exhaustion arrives as a 200 + ``"error"`` field in the body; we detect
and map to ``BLOCKED_RATE_LIMIT``. Bad/missing keys surface as 401/403.

Function is pure: no filesystem I/O, no global state mutations. Tests patch
the module-level ``requests.get`` to inject synthetic payloads.
"""

from __future__ import annotations

import json

import requests

from seoserper import config
from seoserper.models import (
    FailureCategory,
    PAAQuestion,
    ParseResult,
    RelatedSearch,
    SurfaceName,
    SurfaceStatus,
)

MAX_ITEMS = 10

# Locale → google_domain table. Keys use the same (lang, country) tuple the
# UI passes down so matching is exact. Variants for `zh-CN` / `zh` / `zh-TW`
# are listed separately because different call sites normalize casing
# differently. Unknown locales fall back to ``google.com``.
_GOOGLE_DOMAIN: dict[tuple[str, str], str] = {
    ("en", "us"): "google.com",
    ("en-US", "us"): "google.com",
    ("zh", "cn"): "google.com.hk",
    ("zh-CN", "cn"): "google.com.hk",
    ("zh-cn", "cn"): "google.com.hk",
    ("zh", "tw"): "google.com.tw",
    ("zh-TW", "tw"): "google.com.tw",
    ("zh-tw", "tw"): "google.com.tw",
    ("ja", "jp"): "google.co.jp",
    ("ja-JP", "jp"): "google.co.jp",
}

# Bing market codes (plan 005 Unit 2). Keys mirror _GOOGLE_DOMAIN casing
# variants so the UI's (lang, country) passes through regardless of how
# the locale string was normalized upstream. Unknown locales fall back to
# en-US per Bing docs default — SerpAPI also accepts an omitted mkt param
# but falling back to en-US keeps results deterministic.
_BING_MKT: dict[tuple[str, str], str] = {
    ("en", "us"): "en-US",
    ("en-US", "us"): "en-US",
    ("zh", "cn"): "zh-CN",
    ("zh-CN", "cn"): "zh-CN",
    ("zh-cn", "cn"): "zh-CN",
    ("zh", "tw"): "zh-TW",
    ("zh-TW", "tw"): "zh-TW",
    ("zh-tw", "tw"): "zh-TW",
    ("ja", "jp"): "ja-JP",
    ("ja-JP", "jp"): "ja-JP",
}

# SerpAPI quota-exhausted substrings we've observed in live payloads / docs.
# Listed as lowercase needles; payload is lower()'d before match.
_QUOTA_EXHAUSTED_NEEDLES = (
    "ran out of searches",
    "run out of searches",
    "plan limit",
    "account has run out",
    "please upgrade",
)


def _resolve_domain(lang: str, country: str) -> str:
    key = (lang, country.lower() if isinstance(country, str) else country)
    return _GOOGLE_DOMAIN.get(key, "google.com")


def _resolve_bing_mkt(lang: str, country: str) -> str:
    key = (lang, country.lower() if isinstance(country, str) else country)
    return _BING_MKT.get(key, "en-US")


def _build_engine_params(
    query: str, lang: str, country: str, api_key: str, engine: str
) -> dict[str, str]:
    """Per-engine SerpAPI param builder. google = hl+gl+google_domain; bing = mkt.

    Query param stays 'q' for both engines (SerpAPI convention; Yandex would
    be 'text' but Yandex is out of scope per plan 005).
    """
    base = {"engine": engine, "q": query, "api_key": api_key, "no_cache": "false"}
    if engine == "bing":
        base["mkt"] = _resolve_bing_mkt(lang, country)
        return base
    # Default: google
    base.update(
        {
            "hl": lang,
            "gl": country,
            "google_domain": _resolve_domain(lang, country),
        }
    )
    return base


def _both_failed(category: FailureCategory) -> dict[SurfaceName, ParseResult]:
    return {
        SurfaceName.PAA: ParseResult(
            status=SurfaceStatus.FAILED, failure_category=category
        ),
        SurfaceName.RELATED: ParseResult(
            status=SurfaceStatus.FAILED, failure_category=category
        ),
    }


def _extract_paa(questions) -> ParseResult:
    if not isinstance(questions, list) or not questions:
        return ParseResult(status=SurfaceStatus.EMPTY)
    items: list[PAAQuestion] = []
    for rank, entry in enumerate(questions[:MAX_ITEMS], start=1):
        if not isinstance(entry, dict):
            continue
        question = entry.get("question")
        if not isinstance(question, str) or not question.strip():
            continue
        snippet = entry.get("snippet")
        answer_preview = (
            snippet.strip()[:200] if isinstance(snippet, str) else ""
        )
        items.append(
            PAAQuestion(
                question=question.strip(),
                rank=rank,
                answer_preview=answer_preview,
            )
        )
    if not items:
        return ParseResult(status=SurfaceStatus.EMPTY)
    return ParseResult(status=SurfaceStatus.OK, items=items)


def _extract_related(related, *, query: str) -> ParseResult:
    if not isinstance(related, list) or not related:
        return ParseResult(status=SurfaceStatus.EMPTY)
    seen: set[str] = set()
    texts: list[str] = []
    for entry in related:
        if not isinstance(entry, dict):
            continue
        q = entry.get("query")
        if not isinstance(q, str):
            continue
        q_clean = q.strip()
        if not q_clean:
            continue
        if q_clean.lower() == query.strip().lower():
            # Skip echoes of the submitted query itself.
            continue
        if q_clean.lower() in seen:
            continue
        seen.add(q_clean.lower())
        texts.append(q_clean)
        if len(texts) >= MAX_ITEMS:
            break
    if not texts:
        return ParseResult(status=SurfaceStatus.EMPTY)
    return ParseResult(
        status=SurfaceStatus.OK,
        items=[
            RelatedSearch(query=text, rank=rank)
            for rank, text in enumerate(texts, start=1)
        ],
    )


def fetch_serp_raw(
    query: str,
    lang: str,
    country: str,
    *,
    api_key: str,
    engine: str = "google",
    timeout: float = config.SERPAPI_TIMEOUT_SECONDS,
) -> tuple[dict | None, FailureCategory | None]:
    """Call SerpAPI and return the raw parsed JSON dict or a FailureCategory.

    Exactly one of the return tuple elements is non-None:
      - (dict, None)    on HTTP 200 with valid JSON shape and no error field
      - (None, category) on any failure mode (HTTP, JSON, quota, shape)

    ``engine`` dispatches param construction: google uses hl+gl+google_domain;
    bing uses mkt. The HTTP/JSON/error handling is identical across engines.
    """
    params = _build_engine_params(query, lang, country, api_key, engine)

    try:
        resp = requests.get(config.SERPAPI_URL, params=params, timeout=timeout)
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
        return None, FailureCategory.NETWORK_ERROR
    except requests.exceptions.RequestException:
        return None, FailureCategory.NETWORK_ERROR

    if resp.status_code in (401, 403):
        return None, FailureCategory.NETWORK_ERROR
    if resp.status_code == 429:
        return None, FailureCategory.BLOCKED_RATE_LIMIT
    if resp.status_code != 200:
        return None, FailureCategory.NETWORK_ERROR

    try:
        payload = json.loads(resp.text or "")
    except (json.JSONDecodeError, ValueError):
        return None, FailureCategory.SELECTOR_NOT_FOUND

    if not isinstance(payload, dict):
        return None, FailureCategory.SELECTOR_NOT_FOUND

    err = payload.get("error")
    if err:
        err_lower = str(err).lower()
        if any(needle in err_lower for needle in _QUOTA_EXHAUSTED_NEEDLES):
            return None, FailureCategory.BLOCKED_RATE_LIMIT
        return None, FailureCategory.NETWORK_ERROR

    return payload, None


def extract_surfaces(payload: dict, *, query: str) -> dict[SurfaceName, ParseResult]:
    """Pure transform: SerpAPI payload → per-surface ParseResults.

    Separated from fetch_serp_raw so cached raw payloads can be re-extracted
    on each hit (cache survives extractor logic changes).
    """
    return {
        SurfaceName.PAA: _extract_paa(payload.get("related_questions")),
        SurfaceName.RELATED: _extract_related(
            payload.get("related_searches"), query=query
        ),
    }


def fetch_serp_data(
    query: str,
    lang: str,
    country: str,
    *,
    api_key: str,
    engine: str = "google",
    timeout: float = config.SERPAPI_TIMEOUT_SECONDS,
) -> dict[SurfaceName, ParseResult]:
    """Uncached single-call fetch. Composes fetch_serp_raw + extract_surfaces.

    Never raises. All error conditions map to per-surface FAILED ParseResults.
    The engine treats this function exactly like the former ``parse_fn`` it
    replaced, so upstream code at ``_apply_parsed_surface`` is unchanged.

    ``engine`` selects Google vs Bing; response parsing is shared (both
    engines expose ``related_questions`` + ``related_searches`` keys on
    SerpAPI, though Bing's PAA is opportunistic ~20-40% of queries).
    """
    payload, failure = fetch_serp_raw(
        query, lang, country, api_key=api_key, engine=engine, timeout=timeout
    )
    if failure is not None:
        return _both_failed(failure)
    assert payload is not None
    return extract_surfaces(payload, query=query)
