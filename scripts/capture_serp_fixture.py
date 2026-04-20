#!/usr/bin/env python3
"""Opt-in utility: capture one live SerpAPI response to tests/fixtures/serp/.

Usage (requires a valid SERPAPI_KEY in the environment):

    python3 scripts/capture_serp_fixture.py "coffee" en us
    python3 scripts/capture_serp_fixture.py "跑步鞋" zh-CN cn
    python3 scripts/capture_serp_fixture.py "ラーメン" ja jp

Writes a timestamped JSON file next to the existing fixtures. The captured
payload has ``search_parameters.api_key`` stripped before write so the key
never lands in a file. Manual review before committing is advised.

Not part of CI. Not part of the regular test run. Safe to delete if unused.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "serp"

# Reuse the production mapping without importing the fetcher (avoids any
# circular module-load surprise when running this script as a standalone).
_GOOGLE_DOMAIN = {
    ("en", "us"): "google.com",
    ("zh", "cn"): "google.com.hk",
    ("zh-CN", "cn"): "google.com.hk",
    ("zh", "tw"): "google.com.tw",
    ("zh-TW", "tw"): "google.com.tw",
    ("ja", "jp"): "google.co.jp",
}


def _scrub_key(payload: dict) -> dict:
    """Drop api_key from search_parameters before writing to disk."""
    sp = payload.get("search_parameters")
    if isinstance(sp, dict):
        sp.pop("api_key", None)
    return payload


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(__doc__.strip())
        return 2
    _, query, lang, country = argv

    api_key = os.environ.get("SERPAPI_KEY")
    if not api_key:
        print("ERROR: SERPAPI_KEY env var is not set.", file=sys.stderr)
        return 1

    domain = _GOOGLE_DOMAIN.get((lang, country.lower()), "google.com")
    params = {
        "engine": "google",
        "q": query,
        "hl": lang,
        "gl": country,
        "google_domain": domain,
        "api_key": api_key,
        "no_cache": "false",
    }

    resp = requests.get("https://serpapi.com/search.json", params=params, timeout=30)
    payload = resp.json()
    payload = _scrub_key(payload)

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_query = "".join(c if c.isalnum() else "_" for c in query)[:40]
    out = FIXTURE_DIR / f"live_{lang}_{country}_{safe_query}_{ts}.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(f"Captured to {out.relative_to(ROOT)}")
    print(f"HTTP {resp.status_code} · {len(payload.get('related_questions') or [])} PAA · "
          f"{len(payload.get('related_searches') or [])} related")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
