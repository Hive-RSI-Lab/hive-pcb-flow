"""A sealed holdout round stays under holdout/ and nowhere else.

A round under `reference/holdout/round-N.yaml` is the blind test for the
rules written after it was swept. Its defects may be read only by the
round's scorers, from `reference/holdout/` and `docs/holdout/`. If a defect
or board id turns up in any other tracked file (the taxonomy, the corpus
doc, a rule, a reference row, a test), a rule builder has read it or can,
and the round no longer tests that rule. See "A new holdout round" in
docs/errata-corpus.md.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import errata_corpus as ec  # noqa: E402

SEALED_DIRS = ("reference/holdout/", "docs/holdout/")
SELF = "tests/test_holdout_sealed.py"


def _tracked() -> list[str]:
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"],
                             capture_output=True, check=True).stdout
        return [p for p in out.decode().split("\0") if p]
    except (OSError, subprocess.CalledProcessError):
        return [str(p.relative_to(ROOT)) for p in ROOT.rglob("*")
                if p.is_file() and ".git" not in p.parts]


def sealed_ids(manifest: Path = ec.MANIFEST) -> dict[str, int]:
    """Every defect and board id of a sealed round, with its round."""
    ids = {}
    for n, f in ec.sealed_files(manifest).items():
        for b in yaml.safe_load(f.read_text(encoding="utf-8")).get("holdout") or []:
            ids[b["id"]] = n
            for d in b["defects"]:
                ids[d["id"]] = n
    return ids


def leaks(ids: dict[str, int], files: list[str], root: Path = ROOT) -> list[str]:
    if not ids:
        return []
    pat = re.compile(r"(?<![\w-])(" + "|".join(
        re.escape(i) for i in sorted(ids, key=len, reverse=True)) + r")(?![\w-])")
    found = []
    for rel in files:
        if rel.startswith(SEALED_DIRS) or rel == SELF:
            continue
        p = root / rel
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for ln, line in enumerate(text.splitlines(), 1):
            for m in pat.finditer(line):
                found.append(f"{rel}:{ln}: {m.group(1)} (round {ids[m.group(1)]})")
    return found


def test_sealed_rounds_exist_and_load():
    sealed = ec.sealed_files()
    assert 3 in sealed, "round 3 is the blind test; it should be sealed"
    holdout = ec.load(section="holdout")
    assert {b["swept"]["round"] for b in holdout} >= set(sealed)
    main = yaml.safe_load(ec.MANIFEST.read_text(encoding="utf-8"))["holdout"]
    assert not [b["id"] for b in main if b["swept"]["round"] in sealed], \
        "a sealed round's board is still in reference/errata_corpus.yaml"


def test_no_sealed_id_outside_holdout():
    found = leaks(sealed_ids(), _tracked())
    assert not found, (
        "sealed holdout ids outside holdout/ (move them under docs/holdout/ "
        "or reference/holdout/, and don't write rules from them):\n  "
        + "\n  ".join(found[:40]))


def test_the_seal_check_sees_a_leak(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "miss-taxonomy.md").write_text("- foo-board-2: a leak\n")
    (tmp_path / "docs" / "ok.md").write_text("foo-board-20 and xfoo-board-2 are not it\n")
    ids = {"foo-board-2": 3}
    assert leaks(ids, ["docs/miss-taxonomy.md", "docs/ok.md"], tmp_path) == \
        ["docs/miss-taxonomy.md:1: foo-board-2 (round 3)"]
    assert leaks(ids, ["docs/holdout/round-3-members.md"], tmp_path) == []


def _board(bid: str, rnd: int) -> dict:
    return {"id": bid, "url": "https://example.com/x", "commit": "a" * 40,
            "licence": "MIT", "source": "errata.md",
            "rev_a": {"name": "A"}, "rev_b": {"name": "B"},
            "swept": {"flow": "b" * 40, "date": "2026-11-15", "round": rnd},
            "defects": [{"id": f"{bid}-1", "summary": "s", "fault_class": "SCH",
                         "blind": "missed", "verdict": "missed", "follow_up": "f"}]}


def test_sealed_round_merges_and_its_table_stays_apart(tmp_path):
    m = tmp_path / "errata_corpus.yaml"
    m.write_text(yaml.safe_dump({"boards": [], "holdout": [_board("open", 1)]}))
    (tmp_path / "holdout").mkdir()
    (tmp_path / "holdout" / "round-2.yaml").write_text(
        yaml.safe_dump({"holdout": [_board("shut", 2)]}))
    holdout = ec.load(m, "holdout")
    assert [b["id"] for b in holdout] == ["open", "shut"]
    assert set(ec.sealed_files(m)) == {2}
    t = ec.table(tmp_path, [], holdout, {2})
    assert "open-1" in t and "shut-1" not in t
    assert "Holdout round 2, blind: 0 of 1 caught" in t
    assert "docs/holdout/round-2-errata.md" in t
    s = ec.sealed_table(tmp_path, holdout, 2)
    assert "shut-1" in s and "open-1" not in s


def test_a_board_in_the_wrong_sealed_file_is_refused(tmp_path):
    m = tmp_path / "errata_corpus.yaml"
    m.write_text(yaml.safe_dump({"boards": [], "holdout": []}))
    (tmp_path / "holdout").mkdir()
    (tmp_path / "holdout" / "round-2.yaml").write_text(
        yaml.safe_dump({"holdout": [_board("shut", 3)]}))
    with pytest.raises(SystemExit, match="round-2.yaml but swept round 3"):
        ec.load(m, "holdout")
