"""Pad pin names from the schematic (#1220): board_init's netlist reader keeps
each node's pinfunction/pintype, and pin_backfill gives an existing board's
pads the names it lacks, touching nothing else.

The planted fault: blinky2's AMS1117 (U2) can sit at 3.201 V, short of a
3.25 V load, but the board carries no pin names, so check_rail_budget cannot
find U2's output and leaves it not_judged. Backfilled, it finds the short.
The quiet twin: the same board with a 3.0 V load stays quiet once judged.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest
import sexpdata

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))
import board_init  # noqa: E402
import check_rail_budget as crb  # noqa: E402
import pin_backfill as pb  # noqa: E402

GOLD = REPO / "tests" / "golden" / "blinky2"
BLINKY2 = GOLD / "blinky2.kicad_pcb"
AMS1117 = {"1": ("GND", "power_in"), "2": ("VOUT", "power_out"),
           "3": ("VIN", "power_in"), "4": ("VOUT", "power_out")}


def _netlist(pcb: Path, names: dict, *, renet: dict | None = None) -> str:
    """A kicadsexpr netlist of the board's own parts and nets, U2's pins
    named as an AMS1117's and every other pin a passive with no name."""
    fps = pb.footprints(pcb.read_text("utf-8"))
    comps, nets = [], {}
    for fp in fps:
        comps.append(f'(comp (ref "{fp["ref"]}") (value "x") '
                     f'(footprint "Lib:Fp"))')
        for pad in fp["pads"]:
            key = f'{fp["ref"]}.{pad["num"]}'
            net = (renet or {}).get(key, pad["net"])
            if not net:
                continue
            fn, ty = names.get(fp["ref"], {}).get(pad["num"], ("", "passive"))
            node = f'(node (ref "{fp["ref"]}") (pin "{pad["num"]}")'
            node += f' (pinfunction "{fn}")' if fn else ""
            nets.setdefault(net, []).append(node + f' (pintype "{ty}"))')
    body = "".join(f'(net (code "{i}") (name "{n}") {" ".join(ns)})'
                   for i, (n, ns) in enumerate(sorted(nets.items()), 1))
    return (f'(export (version "E") (components {" ".join(comps)}) '
            f'(nets {body}))')


def _write_net(tmp: Path, text: str) -> Path:
    p = tmp / "b.net"
    p.write_text(text, encoding="utf-8")
    return p


def _backfill(tmp: Path, net: Path, *extra) -> tuple[dict, Path]:
    out, rep = tmp / "out.kicad_pcb", tmp / "rep.json"
    rc = pb.main(["--pcb", str(BLINKY2), "--netlist", str(net),
                  "--out", str(out), "--out-report", str(rep), *extra])
    assert rc == 0
    return json.loads(rep.read_text("utf-8")), out


def _pads(pcb: Path) -> dict:
    """(ref, pad) -> the pad's name and type as the board text holds them."""
    from types import SimpleNamespace as NS
    return {(fp["ref"], p["num"]): NS(fn=p["fn"] or "", type=p["type"] or "")
            for fp in pb.footprints(pcb.read_text("utf-8"))
            for p in fp["pads"]}


def _budget(vmin):
    return {"rail_budget": {"loads": {"U1": {"vmin_v": vmin,
                                             "current_a": 0.05}}}}


def test_parse_netlist_carries_pin_names(tmp_path):
    net = _write_net(tmp_path, _netlist(BLINKY2, {"U2": AMS1117}))
    comps, netmap = board_init.parse_netlist(net)
    by = {c["ref"]: c for c in comps}
    assert by["U2"]["pins"]["2"] == ["VOUT", "power_out"]
    assert by["U2"]["pins"]["1"] == ["GND", "power_in"]
    # a pin with no name keeps its type, its name is ""
    r = next(c for c in comps if c["ref"].startswith("R"))
    assert all(v[0] == "" and v[1] == "passive" for v in r["pins"].values())


def test_parse_netlist_drops_pin_number_suffix(tmp_path):
    # kicad-cli writes "IN-_1" for pin 1 named "IN-"; the pad gets "IN-",
    # as Update PCB writes it, so name-matching checks (kelvin's sense-pin
    # regex) still find it. A name that is only the suffix stays as is.
    names = {"U2": {"1": ("IN-_1", "input"), "2": ("VOUT_2", "power_out"),
                    "3": ("_3", "power_in")}}
    net = _write_net(tmp_path, _netlist(BLINKY2, names))
    comps, _ = board_init.parse_netlist(net)
    u2 = next(c for c in comps if c["ref"] == "U2")["pins"]
    assert u2["1"] == ["IN-", "input"]
    assert u2["2"] == ["VOUT", "power_out"]
    assert u2["3"] == ["_3", "power_in"]   # nothing would be left


def test_blinky2_carries_no_pin_names():
    # the golden is a board_init board from before #1220
    assert "(pinfunction" not in BLINKY2.read_text("utf-8")


def test_backfilled_board_lets_rail_budget_find_the_short(tmp_path):
    # before: U2's output pin can't be found by name - not judged
    viols, _, nj = crb.check(BLINKY2, _budget(3.25))
    assert viols == [] and any("U2" in n and "no pin names" in n
                               for n in nj), nj
    net = _write_net(tmp_path, _netlist(BLINKY2, {"U2": AMS1117}))
    rep, out = _backfill(tmp_path, net)
    assert rep["status"] == "written" and rep["agree"] == 1.0, rep
    assert rep["counts"]["pads_net_differs"] == 0
    pads = _pads(out)
    assert pads[("U2", "2")].fn == "VOUT"
    assert pads[("U2", "2")].type == "power_out"
    # after: judged, and the AMS1117's 3.201 V low limit misses 3.25 V
    viols, _, nj = crb.check(out, _budget(3.25))
    shorts = [v for v in viols if v["kind"] == "rail_budget_short"]
    assert len(shorts) == 1 and shorts[0]["load"] == "U1", viols
    assert not any("no pin names" in n for n in nj), nj


def test_backfilled_board_with_a_3v0_load_is_quiet(tmp_path):
    net = _write_net(tmp_path, _netlist(BLINKY2, {"U2": AMS1117}))
    _, out = _backfill(tmp_path, net)
    viols, seen, nj = crb.check(out, _budget(3.0))
    assert viols == [], viols
    assert not any("no pin names" in n for n in nj), nj


def test_backfill_only_adds_pad_lines(tmp_path):
    net = _write_net(tmp_path, _netlist(BLINKY2, {"U2": AMS1117}))
    _, out = _backfill(tmp_path, net)
    old = BLINKY2.read_text("utf-8").splitlines()
    new = out.read_text("utf-8").splitlines()
    added = [ln.strip() for ln in new if ln not in set(old)]
    assert len(new) - len(old) == len(added)
    assert added and all(a.startswith(("(pinfunction ", "(pintype "))
                         for a in added), added[:5]
    # every original line survives, in order
    it = iter(new)
    assert all(any(o == n for n in it) for o in old)
    sexpdata.loads(out.read_text("utf-8"))           # still one s-expr


def test_backfill_again_is_a_no_op(tmp_path):
    net = _write_net(tmp_path, _netlist(BLINKY2, {"U2": AMS1117}))
    _, out = _backfill(tmp_path, net)
    rep = tmp_path / "again.json"
    pb.main(["--pcb", str(out), "--netlist", str(net), "--write",
             "--out-report", str(rep)])
    r = json.loads(rep.read_text("utf-8"))
    assert r["status"] == "nothing_to_do", r
    assert r["counts"]["pads_already_named"] == r["counts"]["pads_agree"]


def test_backfill_keeps_a_name_the_board_already_has(tmp_path):
    named = {"U2": dict(AMS1117, **{"2": ("OUT", "power_out")})}
    _, first = _backfill(tmp_path, _write_net(
        tmp_path, _netlist(BLINKY2, named)))
    sub = tmp_path / "s"
    sub.mkdir()
    net = _write_net(sub, _netlist(BLINKY2, {"U2": AMS1117}))
    rep = sub / "r.json"
    pb.main(["--pcb", str(first), "--netlist", str(net), "--write",
             "--out-report", str(rep)])
    pads = _pads(first)
    assert pads[("U2", "2")].fn == "OUT"     # never overwritten


def test_a_pad_whose_net_moved_is_not_named(tmp_path):
    net = _write_net(tmp_path, _netlist(BLINKY2, {"U2": AMS1117},
                                        renet={"U2.2": "SOMETHING_ELSE"}))
    rep, out = _backfill(tmp_path, net)
    # U2's tab is a second pad "2": both moved
    assert rep["counts"]["pads_net_differs"] == 2, rep
    pads = _pads(out)
    assert not pads[("U2", "2")].fn
    assert pads[("U2", "3")].fn == "VIN"


def test_the_wrong_schematic_writes_nothing(tmp_path):
    fps = pb.footprints(BLINKY2.read_text("utf-8"))
    renet = {f'{fp["ref"]}.{p["num"]}': f"X{i}"
             for fp in fps for i, p in enumerate(fp["pads"])}
    net = _write_net(tmp_path, _netlist(BLINKY2, {"U2": AMS1117},
                                        renet=renet))
    rep, out = _backfill(tmp_path, net)
    assert rep["status"] == "mismatch" and not out.exists(), rep


def test_one_line_pads_get_the_fields_on_their_line():
    text = ('(kicad_pcb (footprint "L:F" (property "Reference" "R1")\n'
            '  (pad "1" smd rect (at 0 0) (size 1 1) (net 1 "A") '
            '(uuid "u1"))\n'
            '  (pad "2" smd rect (at 1 0) (size 1 1))))\n')
    p = pb.plan(text, [{"ref": "R1", "pins": {"1": ["P", "passive"],
                                              "2": ["Q", "passive"]}}],
                {"R1.1": "A", "R1.2": "unconnected-(R1-Pad2)"})
    assert p["counts"]["pads_agree"] == 2
    new = pb.apply(text, p["edits"])
    assert ('(net 1 "A") (pinfunction "P") (pintype "passive") (uuid "u1")'
            in new)
    assert '(size 1 1) (pinfunction "Q") (pintype "passive"))' in new
    sexpdata.loads(new)


def test_a_name_with_a_quote_is_escaped():
    text = ('(kicad_pcb (footprint "L:F" (property "Reference" "U1")\n'
            '\t(pad "1" smd rect\n\t\t(net "A")\n\t\t(uuid "u")\n\t)))\n')
    p = pb.plan(text, [{"ref": "U1", "pins": {"1": ['~{CS}"x', "input"]}}],
                {"U1.1": "A"})
    new = pb.apply(text, p["edits"])
    assert '\t\t(net "A")\n\t\t(pinfunction "~{CS}\\"x")\n' in new
    assert pb.footprints(new)[0]["pads"][0]["fn"] == '~{CS}"x'


def test_candidates_walk_up_to_the_project_kicad_folder(tmp_path):
    proj = tmp_path / "PCB-0001"
    (proj / "kicad").mkdir(parents=True)
    (proj / "ledger.json").write_text("{}")
    (proj / "kicad" / "board.kicad_pro").write_text("{}")
    (proj / "kicad" / "board.kicad_sch").write_text("")
    work = proj / "work" / "p7"
    work.mkdir(parents=True)
    pcb = work / "board.kicad_pcb"
    shutil.copy(BLINKY2, pcb)
    assert pb.candidates(pcb) == [proj / "kicad" / "board.kicad_sch"]


@pytest.mark.smoke
def test_smoke_board_init_and_backfill_from_the_schematic(tmp_path):
    import kc
    from lib import env
    cli = env.find_kicad_cli()
    net = tmp_path / "blinky2.net"
    assert kc.export_netlist(cli, GOLD / "blinky2.kicad_sch",
                             net)["status"] == "pass"
    comps, _ = board_init.parse_netlist(net)
    want = {(c["ref"], n): v for c in comps for n, v in c["pins"].items()}
    assert any(v[0] for v in want.values())

    # board_init writes the pin names on every pad it nets
    out = tmp_path / "bi"
    rep = tmp_path / "bi.json"
    rc = board_init.main(["--netlist", str(net), "--name", "blinky2",
                          "--out", str(out), "--layers", "2",
                          "--mounting-holes", "0",
                          "--schematic", str(GOLD / "blinky2.kicad_sch"),
                          "--out-report", str(rep)])
    r = json.loads(rep.read_text("utf-8"))
    assert rc == 0 and r["status"] == "pass", r
    assert r["self_check"]["parity_count"] == 0
    got = {k: [p.fn, p.type]
           for k, p in _pads(out / "blinky2.kicad_pcb").items()}
    for key, (fn, ty) in want.items():
        if key in got:
            assert got[key][0] == fn, key
            assert got[key][1] == ty, key

    # the golden (made before #1220) gets the same names from its schematic
    cp = tmp_path / "gold"
    shutil.copytree(GOLD, cp)
    rep2 = tmp_path / "bf.json"
    assert pb.main(["--pcb", str(cp / "blinky2.kicad_pcb"), "--write",
                    "--out-report", str(rep2)]) == 0
    r2 = json.loads(rep2.read_text("utf-8"))
    assert r2["status"] == "written" and r2["agree"] == 1.0, r2
    assert Path(r2["source"]).name == "blinky2.kicad_sch"
    back = {k: p.fn for k, p in _pads(cp / "blinky2.kicad_pcb").items()}
    assert {k: v for k, v in back.items() if k in got} == \
        {k: v[0] for k, v in got.items() if k in back}
