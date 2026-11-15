#!/usr/bin/env python
"""errata_ratings.py - check_ratings as the errata corpus runs it, plus where
each part's rating came from.

Same arguments and the same report as check_ratings.py. The report also gets
`rated_from`, ref -> source, for every part check_ratings compared with a
known voltage (verified, listed or failed in its ledger facts): `datasheet`,
`distributor` (a parts.json listing or a family table), `part_number_code`
(a voltage code in the part number), `value` (a voltage written in the Value
field) or `package` (a package table). A part rated from more than one source
is counted under its weakest, as the ledger does. Harness only: the check
and its rules are not changed, only read (errata_corpus counts these as the
parts check_ratings judged; its own pins_checked counts datasheet pins only).
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "lib"))

import check_ratings  # noqa: E402
import checklib  # noqa: E402

RATED = ("verified", "listed", "failed")


def source_of(fact: dict) -> str | None:
    """The source of one rating fact, or None when it wasn't rated."""
    if fact.get("kind") != "rating" or fact.get("status") not in RATED:
        return None
    tier = fact.get("rated_from")
    if tier is None:          # a fact held verified by someone else
        return None
    if tier == "part_code":
        return ("value" if str(fact.get("source") or "").startswith("Value field")
                else "part_number_code")
    return tier


def run(argv=None):
    seen: list[list[dict]] = []
    orig = check_ratings.ledger_facts

    def keep(*a, **k):
        facts = orig(*a, **k)
        seen.append(facts)
        return facts

    check_ratings.ledger_facts = keep
    try:
        payload, out = check_ratings.run(argv)
    finally:
        check_ratings.ledger_facts = orig
    rated = {}
    for f in seen[-1] if seen else ():
        src = source_of(f)
        if src:
            rated[f["subject"]] = src
    payload["rated_from"] = dict(sorted(rated.items()))
    return payload, out


def main(argv=None) -> int:
    return checklib.cli_wrap("check_ratings", lambda: run(argv))


if __name__ == "__main__":
    sys.exit(main())
