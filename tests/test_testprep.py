"""testprep --plan (#549): per net without probe access it plans the
untented via, the TP on a stub, or the via + TP the check then counts, and
writes the ICT probe list. Known answers on check_testpoints' fixture
helpers; a board with every net covered plans nothing (the quiet twin)."""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
sys.path.insert(0, str(ROOT / "tests"))

import check_testpoints  # noqa: E402
import testprep  # noqa: E402
from test_check_testpoints import _board, _r0603, _tp, _via  # noqa: E402


def _track(x1, y1, x2, y2, net, layer="F", w=0.25) -> str:
    return (f'(segment (start {x1} {y1}) (end {x2} {y2}) (width {w}) '
            f'(layer "{layer}.Cu") (net "{net}"))\n')


def _acts(rep):
    return {a["net"]: a for a in rep["actions"]}


def test_plans_untent_stub_and_via(tmp_path):
    pcb = _board(
        tmp_path,
        # N1: two top resistors and a tented via between them -> untent it
        _r0603("R1", 10, 10, "N1", "N2"), _r0603("R2", 14, 10, "N1", "N3"),
        _track(9.225, 10, 13.225, 10, "N1"),
        _track(11, 10, 11, 12, "N1"), _via(11, 12, "N1", size=0.9, drill=0.4),
        # N2/N3: top copper only, no via -> a via + TP on the bottom
        _track(10.775, 10, 10.775, 7, "N2"),
        _track(14.775, 10, 14.775, 7, "N3"),
        _r0603("R4", 20, 25, "N2", "N3"),      # their second pads
        # N5: a bottom track -> a TP on a short stub
        _r0603("R3", 25, 15, "N5", "N6"),
        _tp("TP9", 30, 15, 1.0, "N6", layer="B"),
        _r0603("R5", 32, 25, "N5", "N6"),
        _track(24.225, 15, 24.225, 18, "N5"), _via(24.225, 18, "N5",
                                                  tent="yes"),
        _track(24.225, 18, 26, 22, "N5", layer="B"),
        name="tpa")
    rep = testprep.plan(pcb, side="bottom")
    a = _acts(rep)
    assert rep["plan_only"] is True
    assert "N6" not in a                    # TP9 covers N6
    assert a["N1"]["action"] == "untent_via"
    assert a["N1"]["via_at"] == [11, 12]
    # N5's via is 0.6 mm: under the 0.8 mm fixture target, so a TP instead
    assert a["N5"]["action"] == "tp_stub"
    assert a["N5"]["stub_layer"] == "B.Cu"
    for n in ("N2", "N3"):
        assert a[n]["action"] == "via_tp", a[n]
        assert a[n]["stub_layer"] == "F.Cu"
        assert a[n]["new_via"] == [0.6, 0.3]
        assert a[n]["stub_mm"] <= 5.0
    # N2 and N3 sites keep the fixture pitch from each other
    p2, p3 = a["N2"]["at"], a["N3"]["at"]
    assert ((p2[0] - p3[0]) ** 2 + (p2[1] - p3[1]) ** 2) ** 0.5 >= 1.27
    assert rep["covered_after"] == rep["nets"]
    assert rep["coverage_after_pct"] == 100.0


def test_planned_points_pass_the_check(tmp_path):
    """Each planned TP, written on the board as a TP footprint, is what
    check_testpoints counts as fixture access."""
    items = [_r0603("R1", 10, 10, "N1", "N2"), _r0603("R2", 14, 10, "N1",
                                                      "N3"),
             _track(9.225, 10, 13.225, 10, "N1"),
             _track(10.775, 10, 10.775, 7, "N2"),
             _track(14.775, 10, 14.775, 7, "N3"),
             _r0603("R4", 20, 25, "N2", "N3")]
    rep = testprep.plan(_board(tmp_path, *items, name="tpb"), side="bottom")
    a = _acts(rep)
    assert {a[n]["action"] for n in ("N1", "N2", "N3")} == {"via_tp"}
    extra = []
    for i, n in enumerate(("N1", "N2", "N3")):
        x, y = a[n]["at"]
        extra.append(_tp(f"TP{20 + i}", x, y, 1.0, n, layer="B"))
        extra.append(_via(x, y, n))
    pcb2 = _board(tmp_path, *items, *extra, name="tpb2")
    _v, seen = check_testpoints.check(pcb2, all_points=True)
    ok = {p["net"] for p in seen["points"]
          if p["side"] == "bottom" and p["fixture_ok"]}
    assert {"N1", "N2", "N3"} <= ok
    # and testprep, rerun on that board, plans nothing more for them
    again = _acts(testprep.plan(pcb2, side="bottom"))
    assert not ({"N1", "N2", "N3"} & set(again))


def test_quiet_twin_nothing_to_do(tmp_path):
    pcb = _board(tmp_path, _r0603("R1", 10, 10, "N1", "N2"),
                 _tp("TP1", 10, 15, 1.0, "N1", layer="B"),
                 _tp("TP2", 15, 15, 1.0, "N2", layer="B"), name="tpq")
    assert testprep.board_state(pcb, check_testpoints._cfg(None))["nets"] \
        == ["N1", "N2"]
    rep = testprep.plan(pcb, side="bottom")
    assert rep["actions"] == [] and rep["actions_by_kind"] == {}
    assert rep["coverage_before_pct"] == 100.0
    rows = {r["net"]: r for r in rep["probe_list"]}
    assert rows["N1"]["source"] == "existing" and rows["N1"]["ref"] == "TP1"
    assert rows["N2"]["x"] == 15


def test_unplaceable_says_why(tmp_path):
    # N1's copper is boxed in by an N2 pour on both sides: no site
    zone = ('(zone (net 2) (net_name "N2") (layer "{L}.Cu") (hatch edge 0.5)'
            ' (connect_pads (clearance 0.2)) (min_thickness 0.2)'
            ' (fill yes) (polygon (pts (xy 0 0) (xy 40 0) (xy 40 30)'
            ' (xy 0 30))) (filled_polygon (layer "{L}.Cu") (pts (xy 0.5 0.5)'
            ' (xy 39.5 0.5) (xy 39.5 29.5) (xy 0.5 29.5))))\n')
    rep = testprep.plan(_board(
        tmp_path, _r0603("R1", 20, 15, "N1", "N2"),
        _r0603("R2", 23, 15, "N1", "N2"),
        _track(20.775, 15, 22.225, 15, "N2"),
        zone.format(L="F"), zone.format(L="B"), name="tpu"),
        side="bottom", nets=["N1"])
    a = _acts(rep)
    # the pours are other-net copper only in pass 1; pass 2 goes through
    # them and says so
    assert a["N1"]["action"] in ("via_tp", "unplaceable")
    if a["N1"]["action"] == "via_tp":
        assert a["N1"]["in_pour"] == ["N2"]


def test_cli_plan_writes_csv(tmp_path):
    pcb = _board(tmp_path, _r0603("R1", 10, 10, "N1", "N2"),
                 _tp("TP1", 10, 15, 1.0, "N1", layer="B"),
                 _track(10.775, 10, 10.775, 7, "N2"),
                 _r0603("R2", 20, 20, "N2", "N1"), name="tpc")
    out = tmp_path / "ict.csv"
    assert testprep.main(["--pcb", str(pcb), "--plan", "--csv", str(out),
                          "--out", str(tmp_path / "r.json")]) == 0
    rows = list(csv.DictReader(out.open()))
    assert [r["net"] for r in rows] == ["N1", "N2"]
    assert [r["source"] for r in rows] == ["existing", "planned"]
    assert rows[1]["side"] == "bottom" and rows[1]["kind"] == "tp"


def test_no_plan_flag_edits_nothing(tmp_path):
    pcb = _board(tmp_path, _r0603("R1", 10, 10, "N1", "N2"), name="tpn")
    before = pcb.read_bytes()
    assert testprep.main(["--pcb", str(pcb)]) != 0
    assert pcb.read_bytes() == before
