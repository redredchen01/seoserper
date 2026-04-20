"""Google autocomplete (`suggestqueries.google.com`) fetcher.

Contract (captured 2026-04-20 from `client=firefox`):
    [query_echo, [suggestion, ...], [], {metadata}]

Content-Type in live responses is `text/javascript; charset=UTF-8`, not
`application/json` — we therefore gate HTML detection on the body prefix
(leading `<`) rather than the Content-Type header (a plan clarification
discovered during fixture capture).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import requests

from seoserper import config
from seoserper.models import FailureCategory, Suggestion, SurfaceStatus

SUGGEST_URL = "https://suggestqueries.google.com/complete/search"
FIREFOX_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:124.0) "
    "Gecko/20100101 Firefox/124.0"
)
MAX_ITEMS = 10


@dataclass
class SuggestResult:
    status: SurfaceStatus
    items: list[Suggestion] = field(default_factory=list)
    failure_category: FailureCategory | None = None
    raw_text: str = ""
    # Library-populated fields (seoserper.suggest.get_suggestions). Fetcher-path
    # callers leave these at defaults; library callers get populated values.
    provider_used: str = ""  # "cache" | "google" | "static" | "none" | ""
    from_cache: bool = False
    latency_ms: int = 0
    normalized_query: str = ""
    warnings: list[str] = field(default_factory=list)


def fetch_suggestions(
    query: str,
    lang: str,
    country: str,
    timeout: float = config.SUGGEST_TIMEOUT_SECONDS,
) -> SuggestResult:
    """Single call, no retry. All failure modes map to FailureCategory."""
    try:
        resp = requests.get(
            SUGGEST_URL,
            params={"client": "firefox", "q": query, "hl": lang, "gl": country},
            headers={"User-Agent": FIREFOX_UA},
            timeout=timeout,
            allow_redirects=False,
        )
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
        return SuggestResult(
            status=SurfaceStatus.FAILED,
            failure_category=FailureCategory.NETWORK_ERROR,
        )

    raw = resp.text or ""

    if resp.status_code in (403, 429):
        return SuggestResult(
            status=SurfaceStatus.FAILED,
            failure_category=FailureCategory.BLOCKED_RATE_LIMIT,
            raw_text=raw,
        )
    if resp.status_code in (301, 302, 303, 307, 308):
        # Redirects for this endpoint in practice mean Google rerouted us to a
        # sorry/consent intercept. We don't follow them; treat as rate-limit.
        return SuggestResult(
            status=SurfaceStatus.FAILED,
            failure_category=FailureCategory.BLOCKED_RATE_LIMIT,
            raw_text=raw,
        )
    if resp.status_code != 200:
        return SuggestResult(
            status=SurfaceStatus.FAILED,
            failure_category=FailureCategory.NETWORK_ERROR,
            raw_text=raw,
        )

    stripped = raw.lstrip()
    if stripped.startswith("<"):
        return SuggestResult(
            status=SurfaceStatus.FAILED,
            failure_category=FailureCategory.SELECTOR_NOT_FOUND,
            raw_text=raw,
        )

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return SuggestResult(
            status=SurfaceStatus.FAILED,
            failure_category=FailureCategory.SELECTOR_NOT_FOUND,
            raw_text=raw,
        )

    shape_ok = (
        isinstance(parsed, list)
        and len(parsed) >= 2
        and isinstance(parsed[0], str)
        and isinstance(parsed[1], list)
        and all(isinstance(s, str) for s in parsed[1])
    )
    if not shape_ok:
        return SuggestResult(
            status=SurfaceStatus.FAILED,
            failure_category=FailureCategory.SELECTOR_NOT_FOUND,
            raw_text=raw,
        )

    if parsed[0].strip().lower() != query.strip().lower():
        return SuggestResult(
            status=SurfaceStatus.FAILED,
            failure_category=FailureCategory.SELECTOR_NOT_FOUND,
            raw_text=raw,
        )

    suggestions = parsed[1]
    if not suggestions:
        return SuggestResult(status=SurfaceStatus.EMPTY, raw_text=raw)

    items = [
        Suggestion(text=text, rank=rank)
        for rank, text in enumerate(suggestions[:MAX_ITEMS], start=1)
    ]
    return SuggestResult(status=SurfaceStatus.OK, items=items, raw_text=raw)
