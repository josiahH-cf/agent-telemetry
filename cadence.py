"""Deterministic refresh policy for the existing collection/publication path."""

from __future__ import annotations

import argparse


COLLECTION_INTERVAL_MINUTES = 5
PUBLISH_INTERVAL_MINUTES = 5
BROWSER_CHECK_INTERVAL_MINUTES = 1
WINDOWS_FRESH_MINUTES = 3
GAP_THRESHOLD_MINUTES = COLLECTION_INTERVAL_MINUTES * 1.5
PUBLISH_STALE_MINUTES = 15
LEGACY_INTERVAL_MINUTES = 30


def page_policy() -> dict[str, int]:
    return {
        "collection_interval_minutes": COLLECTION_INTERVAL_MINUTES,
        "publication_interval_minutes": PUBLISH_INTERVAL_MINUTES,
        "browser_check_interval_minutes": BROWSER_CHECK_INTERVAL_MINUTES,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval-minutes", action="store_true")
    parser.add_argument("--windows-fresh-minutes", action="store_true")
    args = parser.parse_args()
    if args.interval_minutes:
        print(COLLECTION_INTERVAL_MINUTES)
    elif args.windows_fresh_minutes:
        print(WINDOWS_FRESH_MINUTES)
    else:
        parser.error("select a policy value")
