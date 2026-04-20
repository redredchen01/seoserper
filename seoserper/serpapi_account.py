"""SerpAPI account / quota lookup.

One-shot read of https://serpapi.com/account to surface the monthly
remaining searches in the UI. Never raises — returns None on any failure
(no key, network error, non-200, malformed JSON, unexpected shape).

Not part of the core fetch path. UI treats this as best-effort display;
SerpAPI's dashboard at https://serpapi.com/manage-api-key is the source
of truth.
"""

from __future__ import annotations

import requests

ACCOUNT_URL = "https://serpapi.com/account"
DEFAULT_TIMEOUT = 5.0


def fetch_quota_info(api_key: str | None, timeout: float = DEFAULT_TIMEOUT) -> dict | None:
    """Return the SerpAPI account dict or None.

    Expected fields (per https://serpapi.com/account-api):
      - ``plan_searches_left`` (int): monthly quota remaining
      - ``searches_per_month`` (int): plan ceiling
      - ``plan_id`` (str): e.g. "free" / "developer"
      - ``this_month_usage`` (int): monthly spend so far
    """
    if not api_key:
        return None

    try:
        resp = requests.get(
            ACCOUNT_URL, params={"api_key": api_key}, timeout=timeout
        )
    except requests.exceptions.RequestException:
        return None

    if resp.status_code != 200:
        return None

    try:
        data = resp.json()
    except ValueError:
        return None

    if not isinstance(data, dict):
        return None

    return data


def format_quota_caption(info: dict | None) -> str | None:
    """Format a single-line caption from the account dict. None if no info."""
    if not isinstance(info, dict):
        return None
    left = info.get("plan_searches_left")
    total = info.get("searches_per_month")
    if not isinstance(left, int):
        return None
    if isinstance(total, int) and total > 0:
        return f"SerpAPI 剩余 {left}/{total}"
    return f"SerpAPI 剩余 {left}"
