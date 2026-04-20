#!/usr/bin/env python3
"""Opt-in utility: wipe the SerpAPI response cache.

Usage:

    python3 scripts/reset_serp_cache.py              # clear all rows
    python3 scripts/reset_serp_cache.py --prune-only # drop only expired rows

No arguments, no side effects beyond the DB. Safe to run any time —
the next Submit will repopulate on miss.
"""

from __future__ import annotations

import argparse
import sys

from seoserper import config
from seoserper.storage import cache_clear_all, cache_prune, init_db


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prune-only",
        action="store_true",
        help="Delete only expired rows (older than SERP_CACHE_TTL_SECONDS).",
    )
    args = parser.parse_args(argv[1:])

    init_db(config.DB_PATH)  # ensure the table exists

    if args.prune_only:
        n = cache_prune(config.SERP_CACHE_TTL_SECONDS, db_path=config.DB_PATH)
        print(f"Pruned {n} expired serp_cache rows.")
    else:
        n = cache_clear_all(db_path=config.DB_PATH)
        print(f"Cleared {n} serp_cache rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
