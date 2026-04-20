"""
Pre-plan captcha baseline spike.

Purpose: per plan `2026-04-20-001-feat-google-serp-analyzer-mvp-plan.md`
`Pre-plan gate`. Runs N real Playwright queries against Google, classifies
each as ok / blocked_by_consent / blocked_by_captcha / blocked_rate_limit /
selector_not_found / network_error, saves the raw HTML as fixture candidate,
and appends the outcome to JSONL. A separate `analyze` subcommand computes
Wilson CI and prints the ship / note-and-ship / brainstorm verdict.

Usage:
    # one burst (3-5 queries recommended by plan)
    python scripts/spike.py run --locale en-us --limit 5 --pacing 90

    # repeat over 3 working days (~60 total)
    python scripts/spike.py analyze
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
SEEDS_PATH = ROOT / "scripts" / "spike_keywords.txt"
RESULTS_PATH = ROOT / "scripts" / "spike_results.jsonl"
FIXTURES_ROOT = ROOT / "tests" / "fixtures" / "serp"

LOCALES = {
    "en-us": {"hl": "en", "gl": "us"},
    "zh-cn": {"hl": "zh-CN", "gl": "cn"},
    "ja-jp": {"hl": "ja", "gl": "jp"},
}

FIREFOX_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:124.0) "
    "Gecko/20100101 Firefox/124.0"
)


@dataclass
class SpikeOutcome:
    timestamp: str
    locale: str
    query: str
    status: str  # ok / blocked_by_captcha / blocked_by_consent / blocked_rate_limit / selector_not_found / network_error
    final_url: str
    fixture_path: str | None
    elapsed_ms: int
    note: str


def load_seeds(path: Path = SEEDS_PATH) -> dict[str, list[str]]:
    seeds: dict[str, list[str]] = {loc: [] for loc in LOCALES}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        locale, query = parts[0].strip(), parts[1].strip()
        if locale in seeds:
            seeds[locale].append(query)
    return seeds


def slugify(text: str, max_len: int = 40) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    if not text:
        # non-ASCII only — hex of utf-8 bytes
        text = "q" + text.encode().hex()[:12]
    return text[:max_len].strip("-") or "empty"


def classify(page, html: str) -> tuple[str, str]:
    """Return (status, note). status is the FailureCategory enum value (or 'ok')."""
    url = page.url or ""
    if "consent.google." in url:
        return "blocked_by_consent", f"consent redirect: {url}"
    if "/sorry/index" in url or "/sorry?" in url:
        return "blocked_rate_limit", f"sorry page: {url}"

    low = html.lower()
    if "id=\"recaptcha\"" in low or "g-recaptcha" in low or 'id="captcha-form"' in low:
        return "blocked_by_captcha", "captcha DOM present"
    if 'action="https://consent.google' in low or "cookieyes" in low:
        return "blocked_by_consent", "consent form DOM present"

    # plan R9 — selector check is on the SERP primary container; if absent we can't
    # tell ok from selector_not_found, so report selector_not_found. Parser unit
    # will own the finer PAA/Related selector drift check.
    if "id=\"search\"" not in low and 'id="rso"' not in low:
        return "selector_not_found", "no #search / #rso container"

    return "ok", ""


def run_query(page, locale: str, query: str, timeout_ms: int) -> tuple[str, str, str, int]:
    params = LOCALES[locale]
    url = (
        "https://www.google.com/search?q="
        + query.replace(" ", "+")
        + f"&hl={params['hl']}&gl={params['gl']}&pws=0"
    )
    started = time.monotonic()
    try:
        page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
    except Exception as exc:  # playwright timeout or network
        return "network_error", f"goto failed: {exc}", page.url or "", int((time.monotonic() - started) * 1000)
    html = page.content()
    status, note = classify(page, html)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    return status, note, html, elapsed_ms


def save_fixture(locale: str, query: str, html: str, status: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest_dir = FIXTURES_ROOT / locale
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = f"{stamp}-{status}-{slugify(query)}.html"
    dest = dest_dir / name
    dest.write_text(html, encoding="utf-8")
    return dest


def append_result(outcome: SpikeOutcome) -> None:
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(outcome), ensure_ascii=False) + "\n")


def cmd_run(args: argparse.Namespace) -> int:
    # Lazy import: allow `analyze` subcommand to run on any machine without playwright.
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright not installed. Run: pip install playwright && playwright install chromium", file=sys.stderr)
        return 2

    seeds = load_seeds()
    chosen_locales = [args.locale] if args.locale else list(seeds)
    pool: list[tuple[str, str]] = []
    for loc in chosen_locales:
        pool.extend((loc, q) for q in seeds.get(loc, []))
    if not pool:
        print(f"no seeds for locale={args.locale!r}", file=sys.stderr)
        return 2
    random.shuffle(pool)
    pool = pool[: args.limit]

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=args.headless)
        try:
            for i, (locale, query) in enumerate(pool, 1):
                context = browser.new_context(
                    user_agent=FIREFOX_UA,
                    locale=LOCALES[locale]["hl"],
                    viewport={"width": 1280, "height": 900},
                )
                page = context.new_page()
                try:
                    status, note, html, elapsed = run_query(
                        page, locale, query, timeout_ms=args.timeout * 1000
                    )
                    fixture_path = None
                    if html:
                        fp = save_fixture(locale, query, html, status)
                        fixture_path = str(fp.relative_to(ROOT))
                    outcome = SpikeOutcome(
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        locale=locale,
                        query=query,
                        status=status,
                        final_url=page.url or "",
                        fixture_path=fixture_path,
                        elapsed_ms=elapsed,
                        note=note,
                    )
                    append_result(outcome)
                    print(f"[{i}/{len(pool)}] {locale:6} {status:25} {elapsed:5}ms  {query}")
                finally:
                    context.close()
                if i < len(pool):
                    jitter = random.uniform(args.pacing * 0.7, args.pacing * 1.3)
                    time.sleep(jitter)
        finally:
            browser.close()
    return 0


def wilson_ci(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total == 0:
        return 0.0, 0.0
    p = successes / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return max(0.0, center - half), min(1.0, center + half)


def cmd_analyze(args: argparse.Namespace) -> int:
    if not RESULTS_PATH.exists():
        print(f"no results at {RESULTS_PATH}", file=sys.stderr)
        return 2
    rows: list[dict] = []
    with RESULTS_PATH.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    total = len(rows)
    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    blocked = sum(
        by_status.get(k, 0)
        for k in ("blocked_by_captcha", "blocked_by_consent", "blocked_rate_limit")
    )
    ok = by_status.get("ok", 0)
    lo, hi = wilson_ci(blocked, total)

    print(f"total queries: {total}")
    print(f"status breakdown:")
    for k, v in sorted(by_status.items(), key=lambda kv: -kv[1]):
        print(f"  {k:28} {v:4}  ({v/total:.1%})")
    print(f"blocked (any flavor): {blocked}/{total}  = {blocked/total:.1%}")
    print(f"ok:                   {ok}/{total}  = {ok/total:.1%}")
    print(f"Wilson 95% CI for block rate: [{lo:.1%}, {hi:.1%}]")

    # plan thresholds (absolute count, not rate):
    if total < 60:
        print(f"\nverdict: INSUFFICIENT SAMPLE (plan requires N>=60, got {total}). Continue spike.")
        return 0
    if blocked == 0:
        verdict = "SHIP"
    elif blocked <= 2:
        verdict = "SHIP (baseline noted)"
    elif blocked <= 5:
        verdict = "NOTE-AND-SHIP (re-test monthly post-launch)"
    else:
        verdict = "BACK TO /ce:brainstorm (re-evaluate proxy decision)"
    print(f"\nverdict: {verdict}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.strip())
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="execute a spike burst")
    r.add_argument("--locale", choices=list(LOCALES), default=None, help="restrict to one locale")
    r.add_argument("--limit", type=int, default=5, help="queries this burst (plan: 3-5)")
    r.add_argument("--pacing", type=float, default=90.0, help="base seconds between queries")
    r.add_argument("--timeout", type=float, default=30.0, help="goto timeout seconds")
    r.add_argument("--headless", action=argparse.BooleanOptionalAction, default=True)
    r.set_defaults(func=cmd_run)

    a = sub.add_parser("analyze", help="summarize captcha rate + Wilson CI + verdict")
    a.set_defaults(func=cmd_analyze)

    return p


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
