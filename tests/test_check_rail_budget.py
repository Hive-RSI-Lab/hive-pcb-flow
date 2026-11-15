"""check_rail_budget: a load's supply pin against its minimum after the
source's low limit, the copper and the series parts (#1118)."""
from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))
import check_rail_budget as crb  # noqa: E402

GOLD = REPO / "tests" / "golden"
USBBUCK4 = GOLD / "usbbuck4" / "usbbuck4.kicad_pcb"
MUTANT = GOLD / "mutants" / "rail-bead-short" / "usbbuck4.kicad_pcb"
BLINKY2 = GOLD / "blinky2" / "blinky2.kicad_pcb"


def _mutation():
    spec = importlib.util.spec_from_file_location(
        "rail_bead_short", GOLD / "mutations" / "rail_bead_short.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cons(budget):
    return {"rail_budget": budget}


def _kinds(viols):
    return sorted((v["kind"], v["severity"]) for v in viols)


def test_the_bead_with_no_margin_is_short():
    m = _mutation()
    viols, seen, nj = crb.check(MUTANT, m.constraints())
    shorts = [v for v in viols if v["kind"] == "rail_budget_short"]
    assert len(shorts) == 1, viols
    v = shorts[0]
    assert v["severity"] == "error" and v["load"] == "U1"
    assert -60 < v["margin_mv"] < -40          # 0.4 A x 0.7 ohm on 3.234 V
    assert set(v["refs"]) == {"FB98", "U1", "U2"}
    assert not nj


def test_the_golden_with_the_same_budget_is_quiet():
    m = _mutation()
    budget = copy.deepcopy(m.BUDGET)
    budget["series"].pop("FB98", None)         # no FB98 on the golden
    viols, seen, nj = crb.check(USBBUCK4, _cons(budget))
    assert viols == [], viols
    assert seen["loads"]


def test_a_bead_with_no_dcr_is_a_warning_not_a_pass():
    m = _mutation()
    budget = copy.deepcopy(m.BUDGET)
    budget["series"].pop("FB98")
    viols, _, _ = crb.check(MUTANT, _cons(budget))
    assert _kinds(viols) == [("rail_budget_unknown", "warning")], viols
    assert "FB98" in json.dumps(viols[0])


BLINKY_BUDGET = {"sources": {"U2": {"pins": "2"}},
                 "loads": {"U1": {"vmin_v": 3.25, "current_a": 0.05}}}


def test_an_ams1117_at_its_low_limit_misses_a_3v25_load():
    viols, _, nj = crb.check(BLINKY2, _cons(BLINKY_BUDGET))
    shorts = [v for v in viols if v["kind"] == "rail_budget_short"]
    assert len(shorts) == 1, viols
    assert shorts[0]["load"] == "U1" and -55 < shorts[0]["margin_mv"] < -45


def test_an_unverified_source_row_gives_not_judged_only():
    table = crb.load_table()
    for row in table["sources"]:
        if row["mpn"] == "AMS1117-3.3":
            row["verified"] = False
    viols, _, nj = crb.check(BLINKY2, _cons(BLINKY_BUDGET), table)
    assert viols == [], viols
    assert nj and any("unverified" in n for n in nj)


def test_nothing_to_judge_without_a_budget(tmp_path):
    out = tmp_path / "r.json"
    rc = crb.main(["--pcb", str(BLINKY2), "--out", str(out)])
    assert rc == 0
    rep = json.loads(out.read_text())
    assert rep.get("nothing_to_judge"), rep


def test_a_source_on_a_board_without_pin_names_says_so():
    # 620 of 634 project boards carry no pad pin names; the note must say
    # why the output pin is unknown and how to give it
    budget = {"loads": BLINKY_BUDGET["loads"]}
    viols, _, nj = crb.check(BLINKY2, _cons(budget))
    assert viols == [], viols
    assert any("U2" in n and "no pin names" in n
               and "rail_budget.sources.U2.pins" in n for n in nj), nj


# ---- an unrouted rail is not a fault: not_judged, reason rail_unrouted ------

def _unrouted_blinky(tmp_path, net='"+3V3"'):
    """blinky2 with every segment and via of `net` taken out."""
    out, blk = [], None
    for line in BLINKY2.read_text(encoding="utf-8").splitlines(keepends=True):
        if blk is None and line.startswith(("\t(segment", "\t(via")):
            blk = [line]
            continue
        if blk is not None:
            blk.append(line)
            if line.startswith("\t)"):
                if not any(f"(net {net})" in b for b in blk):
                    out += blk
                blk = None
            continue
        out.append(line)
    pcb = tmp_path / "blinky2.kicad_pcb"
    pcb.write_text("".join(out), encoding="utf-8")
    return pcb


OK_BUDGET = {"sources": {"U2": {"pins": "2"}},
             "loads": {"U1": {"vmin_v": 3.0, "current_a": 0.05}}}


def test_an_unrouted_rail_is_not_judged_not_a_warning(tmp_path):
    # planted: the rail's copper taken out, positive margin -> no finding
    viols, seen, nj = crb.check(_unrouted_blinky(tmp_path), _cons(OK_BUDGET))
    assert viols == [], viols
    assert any(n.startswith("rail_unrouted: U1") for n in nj), nj
    [rec] = [b for b in seen["budgets"] if b["load"] == "U1"]
    assert rec["status"] == "not_judged" and rec["reason"] == "rail_unrouted"


def test_the_routed_twin_is_judged_ok():
    viols, seen, nj = crb.check(BLINKY2, _cons(OK_BUDGET))
    assert viols == [], viols
    assert not any(n.startswith("rail_unrouted") for n in nj), nj
    [rec] = [b for b in seen["budgets"] if b["load"] == "U1"]
    assert rec["status"] == "ok"


def test_a_short_on_an_unrouted_rail_stays_an_error(tmp_path):
    viols, _, _ = crb.check(_unrouted_blinky(tmp_path), _cons(BLINKY_BUDGET))
    assert [v["kind"] for v in viols] == ["rail_budget_short"], viols


def test_unrouted_reasons():
    assert crb.unrouted("+3V3: all sink pads on '+3V3' are disconnected "
                        "from the source copper on the solver grid")
    assert crb.unrouted("+3V3 has no copper")
    assert crb.unrouted("+3V3: U1 not connected to U2 on the solver grid")
    assert not crb.unrouted("zones not filled (x)")
    assert not crb.unrouted(None)
