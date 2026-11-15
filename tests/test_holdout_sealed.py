"""A sealed holdout round stays under holdout/ and nowhere else.

A round under `holdout/round-N.yaml` (the repo's top `holdout/` folder,
outside `reference/` and `docs/`) is the blind test for the rules written
after it was swept. Its defects may be read only by the round's scorers,
from `holdout/`. If a defect or board id turns up in any other tracked file
(the taxonomy, the corpus doc, a rule, a reference row, a test), a rule
builder has read it or can,
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

SEALED_DIRS = ("holdout/",)
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
        "sealed holdout ids outside holdout/ (move them under holdout/, "
        "and don't write rules from them):\n  "
        + "\n  ".join(found[:40]))


def test_the_seal_check_sees_a_leak(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "miss-taxonomy.md").write_text("- foo-board-2: a leak\n")
    (tmp_path / "docs" / "ok.md").write_text("foo-board-20 and xfoo-board-2 are not it\n")
    (tmp_path / "reference").mkdir()
    (tmp_path / "reference" / "parts.yaml").write_text("follow_up: see foo-board-2\n")
    ids = {"foo-board-2": 3}
    assert leaks(ids, ["docs/miss-taxonomy.md", "docs/ok.md",
                       "reference/parts.yaml"], tmp_path) == \
        ["docs/miss-taxonomy.md:1: foo-board-2 (round 3)",
         "reference/parts.yaml:1: foo-board-2 (round 3)"]
    assert leaks(ids, ["holdout/round-3-members.md"], tmp_path) == []


def _board(bid: str, rnd: int) -> dict:
    return {"id": bid, "url": "https://example.com/x", "commit": "a" * 40,
            "licence": "MIT", "source": "errata.md",
            "rev_a": {"name": "A"}, "rev_b": {"name": "B"},
            "swept": {"flow": "b" * 40, "date": "2026-11-15", "round": rnd},
            "defects": [{"id": f"{bid}-1", "summary": "s", "fault_class": "SCH",
                         "blind": "missed", "verdict": "missed", "follow_up": "f"}]}


def test_sealed_round_merges_and_its_table_stays_apart(tmp_path):
    (tmp_path / "reference").mkdir()
    m = tmp_path / "reference" / "errata_corpus.yaml"
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
    assert "holdout/round-2-errata.md" in t
    s = ec.sealed_table(tmp_path, holdout, 2)
    assert "shut-1" in s and "open-1" not in s


def test_a_board_in_the_wrong_sealed_file_is_refused(tmp_path):
    (tmp_path / "reference").mkdir()
    m = tmp_path / "reference" / "errata_corpus.yaml"
    m.write_text(yaml.safe_dump({"boards": [], "holdout": []}))
    (tmp_path / "holdout").mkdir()
    (tmp_path / "holdout" / "round-2.yaml").write_text(
        yaml.safe_dump({"holdout": [_board("shut", 3)]}))
    with pytest.raises(SystemExit, match="round-2.yaml but swept round 3"):
        ec.load(m, "holdout")


def _two_rounds(tmp_path):
    (tmp_path / "reference").mkdir()
    m = tmp_path / "reference" / "errata_corpus.yaml"
    m.write_text(yaml.safe_dump({"boards": [], "holdout": [_board("open", 1)]}))
    (tmp_path / "holdout").mkdir()
    (tmp_path / "holdout" / "round-2.yaml").write_text(
        yaml.safe_dump({"holdout": [_board("shut", 2)]}))
    return m


def _record(monkeypatch):
    seen = []
    monkeypatch.setattr(ec, "fetch", lambda c, b: seen.append(("fetch", b["id"], c)))
    monkeypatch.setattr(ec, "sweep", lambda c, b: seen.append(("sweep", b["id"], c)) or {})
    monkeypatch.setattr(ec, "run", lambda c, b: seen.append(("run", b["id"], c)) or {})
    return seen


def test_a_plain_sweep_leaves_sealed_rounds_out(tmp_path, monkeypatch, capsys):
    m = _two_rounds(tmp_path)
    cache, sealed = tmp_path / "train", tmp_path / "sealed"
    seen = _record(monkeypatch)
    base = ["--manifest", str(m), "--cache", str(cache), "--sealed-cache", str(sealed)]
    for cmd in ("sweep", "fetch", "run"):
        seen.clear()
        ec.main([cmd, *base])
        assert {i for _, i, _ in seen} == {"open"}, cmd
        assert {c for _, _, c in seen} == {cache.resolve()}, cmd
        err = capsys.readouterr().err
        assert "left out 1 sealed-round board" in err and "shut" not in err
    # naming a sealed board is not enough: it takes --sealed
    seen.clear()
    ec.main(["sweep", "shut", *base])
    assert seen == []


def test_scorers_get_a_sealed_round_from_the_sealed_cache(tmp_path, monkeypatch):
    m = _two_rounds(tmp_path)
    cache, sealed = tmp_path / "train", tmp_path / "sealed"
    seen = _record(monkeypatch)
    ec.main(["sweep", "--sealed", "2", "--manifest", str(m), "--cache", str(cache),
             "--sealed-cache", str(sealed)])
    assert ("sweep", "shut", sealed.resolve()) in seen
    assert ("sweep", "open", cache.resolve()) in seen
    assert not [s for s in seen if s[1] == "shut" and s[2] != sealed.resolve()]


def test_the_sealed_cache_cannot_sit_in_the_training_cache(tmp_path):
    cache = (tmp_path / "train").resolve()
    for bad in (cache, cache / "holdout", tmp_path):
        with pytest.raises(SystemExit, match="overlaps the training cache"):
            ec.sealed_cache_root(str(bad), cache)
    assert ec.sealed_cache_root(str(tmp_path / "sealed"), cache) == \
        (tmp_path / "sealed").resolve()


def test_no_sealed_board_in_the_training_cache():
    """On a machine with the corpus fetched: the training cache holds no
    sealed-round checkout or report (a count only, never the ids)."""
    cache = ec.cache_root(None)
    ids = set(ec.sealed_rounds())
    if not cache.is_dir() or not ids:
        pytest.skip("no training cache here")
    names = {p.name.split("@")[0] for d in ("repos", "runs")
             if (cache / d).is_dir() for p in (cache / d).iterdir()}
    n = len(names & ids)
    assert not n, (f"{n} sealed-round board(s) in the training cache {cache}; "
                   "move them to the sealed cache (see \"A new holdout round\")")
