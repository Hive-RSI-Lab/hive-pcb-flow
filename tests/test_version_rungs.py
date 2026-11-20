"""Several tool versions active at once (Hive toolver, ladder step 5):
routelib.version_rungs runs one router version per rung from the same
board and keeps the best; route_auto's KRT finish and route_critical use it
when HIVE_TOOL_KRT_RUNGS / --krt-versions names more than one KRT.
Pure tests (fakes for KRT and DRC); the live two-version run is in
test_route_critical.py."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))
import route_auto  # noqa: E402
import routelib  # noqa: E402

BOARD = """(kicad_pcb
  (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (setup)
  (gr_rect (start 0 0) (end 20 20) (stroke (width 0.1)) (fill no)
    (layer "Edge.Cuts"))
)
"""


def _board(tmp_path) -> Path:
    p = tmp_path / "b.kicad_pcb"
    p.write_text(BOARD, encoding="utf-8")
    return p


# ------------------------------------------------------------ version_rungs

def _writer(board: Path, grades: dict, seen: list):
    def attempt(tool):
        seen.append((tool, board.read_text()))
        board.write_text(f"routed by {tool}")
        return grades[tool], {"by": tool}
    return attempt


def test_rungs_keep_the_best_and_start_each_from_the_same_board(tmp_path):
    b = tmp_path / "x.kicad_pcb"
    b.write_text("start")
    seen: list = []
    best, facts, rows = routelib.version_rungs(
        [("0.19", "a"), ("0.24", "b"), ("0.25", "c")],
        _writer(b, {"a": (2, 5), "b": (1, 9), "c": (1, 9)}, seen), b)
    assert [s[1] for s in seen] == ["start"] * 3     # never a rung's leftovers
    assert best == "0.24" and facts == {"by": "b"}   # (1, 9) ties c: b is first
    assert b.read_text() == "routed by b"
    assert [(r["version"], r["grade"], r["kept"]) for r in rows] == [
        ("0.19", [2, 5], False), ("0.24", [1, 9], True), ("0.25", [1, 9], False)]


def test_rungs_restore_the_start_when_every_rung_fails(tmp_path):
    b = tmp_path / "x.kicad_pcb"
    b.write_text("start")
    best, facts, rows = routelib.version_rungs(
        [("0.19", "a"), ("0.24", "b")],
        _writer(b, {"a": None, "b": None}, []), b)
    assert (best, facts) == (None, None)
    assert b.read_text() == "start"
    assert [r["kept"] for r in rows] == [False, False]


def test_rungs_skip_later_versions_past_the_time_cap(tmp_path):
    b = tmp_path / "x.kicad_pcb"
    b.write_text("start")
    seen: list = []
    best, _, rows = routelib.version_rungs(
        [("0.19", "a"), ("0.24", "b")],
        _writer(b, {"a": (3, 3), "b": (0, 0)}, seen), b, budget_s=0)
    assert [s[0] for s in seen] == ["a"]             # the first always runs
    assert best == "0.19" and b.read_text() == "routed by a"
    assert rows[1] == {"version": "0.24", "skipped": "time cap",
                       "budget_s": 0, "kept": False}


# ------------------------------------------------------------ route_auto

def _drc(unrouted: int, errors: int) -> dict:
    return {"violations": [
        *({"source": "unconnected", "net": f"N{i}", "severity": "error"}
          for i in range(unrouted)),
        *({"source": "drc", "check": "clearance", "severity": "error",
           "items": []} for _ in range(errors))]}


def test_krt_finish_keeps_the_best_kept_rung(tmp_path, monkeypatch):
    staged = _board(tmp_path)
    krts = [Path(f"/k/KiCadRoutingTools-{v}/plugins") for v in
            ("0.19.0", "0.24.1", "0.23.9")]
    monkeypatch.setattr(route_auto.env, "find_krt_versions",
                        lambda spec=None: krts if spec == "all" else krts[:1])
    # 0.19 keeps (2 unrouted), 0.24 keeps (1 unrouted), 0.23 is not better
    out = {"0.19.0": (True, _drc(2, 0)), "0.24.1": (True, _drc(1, 3)),
           "0.23.9": (False, _drc(0, 0))}
    starts = []

    def fake_attempt(krt, cli, staged, work, drc, backup, *a, **k):
        v = krt.parent.name.split("-")[1]
        starts.append(staged.read_text() == BOARD)
        kept, after = out[v]
        if kept:
            staged.write_text(BOARD + f";{v}\n")
        f = {"kept": kept, "nets": ["N0"]}
        if kept:
            f["drc"] = after
        return f

    monkeypatch.setattr(route_auto, "_krt_attempt", fake_attempt)
    facts = route_auto._krt_finish(Path("cli"), staged, tmp_path, _drc(3, 0),
                                   refill=False, parity=False, timeout_s=60,
                                   krt_versions="all")
    assert all(starts)
    assert facts["kept"] and facts["krt_version"] == "0.24.1"
    assert staged.read_text().endswith(";0.24.1\n")
    assert [(r["version"], r["kept"]) for r in facts["krt_rungs"]] == [
        ("0.19.0", False), ("0.24.1", True), ("0.23.9", False)]
    # one version (the default): no rung rows, same facts as before
    facts = route_auto._krt_finish(Path("cli"), staged, tmp_path, _drc(3, 0),
                                   refill=False, parity=False, timeout_s=60)
    assert facts["krt_version"] == "0.19.0" and "krt_rungs" not in facts


def test_krt_finish_reports_the_default_when_no_rung_is_kept(tmp_path,
                                                            monkeypatch):
    staged = _board(tmp_path)
    krts = [Path(f"/k/KiCadRoutingTools-{v}/plugins") for v in
            ("0.19.0", "0.24.1")]
    monkeypatch.setattr(route_auto.env, "find_krt_versions",
                        lambda spec=None: krts)

    def fake_attempt(krt, cli, staged, *a, **k):
        staged.write_text("junk")          # the real attempt restores; check
        return {"kept": False, "nets": ["N0"], "errors_after": {krt.parent.name: 1}}

    monkeypatch.setattr(route_auto, "_krt_attempt", fake_attempt)
    facts = route_auto._krt_finish(Path("cli"), staged, tmp_path, _drc(1, 0),
                                   refill=False, parity=False, timeout_s=60)
    assert not facts["kept"] and facts["krt_version"] == "0.19.0"
    assert facts["errors_after"] == {"KiCadRoutingTools-0.19.0": 1}
    assert staged.read_text() == BOARD    # version_rungs put the start back
