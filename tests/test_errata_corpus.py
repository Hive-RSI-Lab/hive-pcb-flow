"""The real-errata corpus: its manifest, its matcher, and the three
netlist_audit rules its rev-A boards needed (ground_island, pin_net_swap,
sot23_pinout), each on a netlist shaped like the board it came from."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import errata_corpus as ec  # noqa: E402
import netlist_audit as na  # noqa: E402


def netlist(tmp_path, nets, comps):
    """nets: {name: [(ref, pin, pintype, pinfunction)]}; comps: {ref: fp}."""
    cs = " ".join(f'(comp (ref "{r}") (value "{r}") (footprint "{fp}"))'
                  for r, fp in comps.items())
    ns = " ".join(
        f'(net (code "{i}") (name "{n}") ' + " ".join(
            f'(node (ref "{r}") (pin "{p}") (pinfunction "{f}") (pintype "{t}"))'
            for r, p, t, f in nodes) + ")"
        for i, (n, nodes) in enumerate(nets.items(), 1))
    path = tmp_path / "b.net"
    path.write_text(f'(export (version "E") (components {cs}) (nets {ns}))')
    return na.parse_netlist(path)


def kinds(parsed):
    return [(v["kind"], v.get("net"), v.get("refs")) for v in na.audit(parsed, {}, None)[0]]


def test_manifest_has_ten_boards_and_every_miss_a_follow_up():
    boards = ec.load()
    assert len(boards) >= 10
    assert len({b["id"] for b in boards}) == len(boards)
    for b in boards:
        assert b["licence"] and b["source"] and len(b["commit"]) == 40
        for d in b["defects"]:
            assert d["source_quote"]


def test_every_miss_cites_a_filed_issue():
    """A miss is only honest with the issue that would close it."""
    for b in ec.load():
        for d in b["defects"]:
            if d["verdict"] == "missed":
                assert re.search(r"#\d+", d["follow_up"]), d["id"]


def test_matcher_needs_kind_and_net_or_ref():
    c = {"script": "netlist_audit", "kind": "sot23_pinout", "refs": ["Q1", "Q3"]}
    assert ec.matches({"kind": "sot23_pinout", "refs": ["Q3"]}, c)
    assert not ec.matches({"kind": "sot23_pinout", "refs": ["Q2"]}, c)
    assert not ec.matches({"kind": "dangling_net", "refs": ["Q1"]}, c)
    n = {"script": "netlist_audit", "kind": "ground_island", "net": "/MIC_GND"}
    assert ec.matches({"kind": "ground_island", "net": "/MIC_GND"}, n)
    assert not ec.matches({"kind": "ground_island", "net": "/AGND"}, n)


def test_every_catch_has_a_runner():
    """A catch with no runner shows "not run here" and is never verified."""
    for b in ec.load():
        for d in b["defects"]:
            if d.get("catch"):
                assert d["catch"]["script"] in ec.RUNNERS, d["id"]


def test_outcome_reads_fixed_and_regressed():
    d = {"verdict": "caught", "catch": {"script": "netlist_audit", "kind": "x"}}
    assert ec.outcome(d, {"ran": True, "rev_a": True, "rev_b": False}).startswith("caught")
    assert "REGRESSED" in ec.outcome(d, {"ran": True, "rev_a": True, "rev_b": True})
    assert "REGRESSED" in ec.outcome(d, {"ran": True, "rev_a": False, "rev_b": False})


def test_ground_island_fires_on_a_local_label_ground(tmp_path):
    # digits v1: MIC_GND joins C3, J8, J9 and nothing else
    nets = {"GND": [("U1", "3", "power_in", "GND"), ("C1", "2", "passive", "")],
            "/MIC_GND": [("C3", "2", "passive", ""), ("J8", "4", "passive", ""),
                         ("J9", "3", "passive", "")],
            "/MIC": [("C3", "1", "passive", ""), ("J8", "3", "passive", "")]}
    assert ("ground_island", "/MIC_GND", ["C3", "J8", "J9"]) in kinds(
        netlist(tmp_path, nets, {}))


def test_ground_island_quiet_when_a_net_tie_or_power_pin_joins_it(tmp_path):
    nets = {"GND": [("U1", "3", "power_in", "GND"), ("R9", "2", "passive", "")],
            "AGND": [("R9", "1", "passive", ""), ("C3", "2", "passive", "")],
            "GND_ISO": [("U2", "5", "power_in", "GND2"), ("C4", "2", "passive", "")],
            "CHASSIS_GND": [("H1", "1", "passive", ""), ("C5", "2", "passive", "")]}
    assert not [k for k in kinds(netlist(tmp_path, nets, {})) if k[0] == "ground_island"]


def test_pin_net_swap_sda_on_scl(tmp_path):
    # pandore rev A0: CTL.SDA lands on the CS4272's SCL pin
    nets = {"/Audio/CODEC/CTL.SDA": [("U24", "7", "bidirectional", "SDA2"),
                                     ("U11", "11", "input", "SCL")],
            "/Audio/CODEC/CTL.SCL": [("U24", "6", "bidirectional", "SCL2"),
                                     ("U11", "12", "bidirectional", "SDA")],
            "I2C_SDA": [("U3", "1", "bidirectional", "SDA")]}
    ks = [k for k in kinds(netlist(tmp_path, nets, {})) if k[0] == "pin_net_swap"]
    assert sorted(k[1] for k in ks) == ["/Audio/CODEC/CTL.SCL", "/Audio/CODEC/CTL.SDA"]


def test_sot23_pinout_gds_on_stock_footprint(tmp_path):
    # oasis v0.2: Q_NMOS_GDS (pinfunction G_1, D_2, S_3) on SOT-23-3
    def q(ref):
        return {f"{ref}g": [(ref, "1", "input", "G_1")],
                f"{ref}d": [(ref, "2", "passive", "D_2")],
                f"{ref}s": [(ref, "3", "passive", "S_3")]}
    nets = {**q("Q1"), **q("Q2"), **q("Q3")}
    comps = {"Q1": "Package_TO_SOT_SMD:SOT-23-3",
             "Q2": "lib:SOT-23-3-pinswapped-mosfet",  # oasis v0.3's fix
             "Q3": "Package_TO_SOT_SMD:SOT-23"}
    ks = [k[2] for k in kinds(netlist(tmp_path, nets, comps)) if k[0] == "sot23_pinout"]
    assert ks == [["Q1"], ["Q3"]]


def test_sot23_pinout_quiet_on_gsd_and_bec(tmp_path):
    nets = {"a": [("Q1", "1", "input", "G"), ("Q2", "1", "input", "B")],
            "b": [("Q1", "2", "passive", "S"), ("Q2", "2", "passive", "E")],
            "c": [("Q1", "3", "passive", "D"), ("Q2", "3", "passive", "C")]}
    comps = {"Q1": "Package_TO_SOT_SMD:SOT-23", "Q2": "Package_TO_SOT_SMD:SOT-23-3"}
    assert not [k for k in kinds(netlist(tmp_path, nets, comps)) if k[0] == "sot23_pinout"]


def test_every_catch_names_a_check_the_corpus_runs():
    # a catch for a script with no runner would read "not run here" forever
    for b in ec.load():
        for d in b["defects"]:
            if d.get("catch"):
                assert d["catch"]["script"] in ec.RUNNERS, d["id"]


def test_run_dispatches_check_straps_catches(tmp_path, monkeypatch):
    """A check_straps catch (minuet-4) runs check_straps on both revisions;
    a script with no runner is reported as not run."""
    calls = []

    def fake(cache, b, rev, script, constraints=None):
        calls.append((script, rev))
        return [{"kind": "reset_pull_offboard", "net": "GPIO6"}] if rev == "rev_a" else []
    monkeypatch.setattr(ec, "audit", fake)
    assert "check_straps" in ec.RUNNERS
    b = {"id": "m", "defects": [
        {"id": "m-1", "catch": {"script": "check_straps", "kind": "reset_pull_offboard",
                                "net": "GPIO6"}},
        {"id": "m-2", "catch": {"script": "no_such_check", "kind": "x"}}]}
    res = ec.run(tmp_path, b)
    assert res["m-1"] == {"ran": True, "rev_a": True, "rev_b": False}
    assert res["m-2"] == {"ran": False}
    assert sorted(calls) == [("check_straps", "rev_a"), ("check_straps", "rev_b")]


def sheeted(tmp_path, nets, comps):
    """comps: {ref: (value, sheet)}; nets as netlist()'s."""
    cs = " ".join(f'(comp (ref "{r}") (value "{v}") (footprint "x") '
                  f'(sheetpath (names "{s}") (tstamps "{s}")))'
                  for r, (v, s) in comps.items())
    ns = " ".join(
        f'(net (code "{i}") (name "{n}") ' + " ".join(
            f'(node (ref "{r}") (pin "{p}") (pinfunction "{f}") (pintype "{t}"))'
            for r, p, t, f in nodes) + ")"
        for i, (n, nodes) in enumerate(nets.items(), 1))
    path = tmp_path / "b.net"
    path.write_text(f'(export (version "E") (components {cs}) (nets {ns}))')
    return na.parse_netlist(path)


def pin_parts(parsed, specs=None):
    return [v for v in na.audit(parsed, {}, None, specs)[0]
            if v["kind"] == "pin_part_missing"]


def mcf(fb_caps, sw_bk="unconnected-(U5-SW_BK-Pad5)"):
    """minuet's fan sheet, cut to the MCF8316D's buck pins: fb_caps
    {ref: (value, sheet)} across +3.3V/GND."""
    nets = {"+3.3V": [("U5", "3", "power_in", "FB_BK")]
            + [(c, "1", "passive", "") for c in fb_caps],
            "GND": [("U5", "4", "power_in", "GND_BK"), ("U1", "1", "power_in", "GND")]
            + [(c, "2", "passive", "") for c in fb_caps],
            sw_bk: [("U5", "5", "power_out", "SW_BK")]}
    return nets, {"U5": ("MCF8316D", "/fan/"), "U1": ("AMS1117", "/"), **fb_caps}


def test_pin_part_missing_on_minuet_v3_caps_drawn_elsewhere(tmp_path):
    # v3: +3.3V carries C5 47u, C10 100n, all on other sheets than U5's
    nets, comps = mcf({"C5": ("47u", "/"), "C10": ("100n", "/core/")})
    vs = pin_parts(sheeted(tmp_path, nets, comps))
    assert [(v["severity"], v["part"], v["net"], v["refs"]) for v in vs] == [
        ("warning", "CBK", "+3.3V", ["U5"])]
    assert vs[0]["drawn_elsewhere"] == ["C10", "C5"]


def test_pin_part_quiet_on_minuet_v4_c20_with_the_ic(tmp_path):
    nets, comps = mcf({"C5": ("47u", "/"), "C20": ("1u", "/fan/")})
    assert not pin_parts(sheeted(tmp_path, nets, comps))


def test_pin_part_missing_is_an_error_when_nothing_spans(tmp_path):
    nets, comps = mcf({})
    vs = pin_parts(sheeted(tmp_path, nets, comps))
    assert [(v["severity"], v["part"]) for v in vs] == [("error", "CBK")]


def test_pin_part_buck_inductor_only_when_sw_bk_is_used(tmp_path):
    # SW_BK on a real net and no inductor/resistor to FB_BK: LBK missing
    nets, comps = mcf({"C20": ("1u", "/fan/")}, sw_bk="/fan/SW")
    nets["/fan/SW"].append(("TP1", "1", "passive", ""))
    comps["TP1"] = ("TestPoint", "/fan/")
    assert [v["part"] for v in pin_parts(sheeted(tmp_path, nets, comps))] == ["LBK"]


def test_pin_part_value_window(tmp_path):
    # CDVDD 1u: a 100n across DVDD/DGND is off-value, a 2.2u is in
    spec = [{"mpn": "X", "match": "^X1$", "requires": [
        {"id": "CDVDD", "pin": "DVDD", "to": "DGND", "kinds": ["C"], "value": "1u"}]}]
    def board(val):
        nets = {"DV": [("U1", "1", "power_out", "DVDD"), ("C1", "1", "passive", "")],
                "GND": [("U1", "2", "power_in", "DGND"), ("C1", "2", "passive", "")]}
        return sheeted(tmp_path, nets, {"U1": ("X1", "/"), "C1": (val, "/")})
    vs = pin_parts(board("100n"), spec)
    assert [(v["severity"], v["off_value"]) for v in vs] == [("error", ["C1"])]
    assert not pin_parts(board("2.2u"), spec)


def test_pin_requirements_table_rows_are_well_formed():
    for part in na.load_pin_requirements():
        assert part["mpn"] and part["match"] and part["requires"]
        for r in part["requires"]:
            assert r["pin"] and r["to"] and r["source"]
            assert set(r["kinds"]) <= {"C", "L", "R"}
            assert r.get("when") in (None, "connected")


def test_check_current_catch_runs_on_the_board():
    assert ec.RUNNERS["check_current"]
    caught = [d for b in ec.load() for d in b["defects"]
              if (d.get("catch") or {}).get("script") == "check_current"]
    assert any(d["id"] == "digits-1" for d in caught)


# ------------------------------------------- dfm on a revision's shipped CPL

OASIS = ROOT / "tests" / "fixtures" / "stages" / "oasis_cpl"


def _cpl_board(tmp_path, rot_b: str):
    """A corpus board whose two revisions are the pinned oasis v0.2 copper
    with the CPL it shipped (MISTER1 at 119, 180 off) and the same CPL with
    MISTER1 at `rot_b`; each run dir is seeded with the pin's LCSC models
    and fetching is off, so the verdict is the fixture's alone."""
    import shutil
    b = {"id": "cplplant", "commit": "0" * 40,
         "rev_a": {"name": "a", "pcb": "a/oasis.kicad_pcb", "cpl": "a/positions.csv"},
         "rev_b": {"name": "b", "pcb": "b/oasis.kicad_pcb", "cpl": "b/positions.csv"},
         "defects": [{"id": "cplplant-1", "verdict": "caught",
                      "catch": {"script": "dfm", "kind": "cpl_rotation",
                                "refs": ["mister1"]}}]}
    cache = tmp_path / "cache"
    repo = ec.repo_dir(cache, b)
    shipped = (OASIS / "gerbers" / "CPL.csv").read_text(encoding="utf-8-sig")
    assert "MISTER1,56.277091,-124.18828,119.0,top" in shipped
    for rev, cpl in (("a", shipped), ("b", shipped.replace(
            "MISTER1,56.277091,-124.18828,119.0,top",
            f"MISTER1,56.277091,-124.18828,{rot_b},top"))):
        (repo / rev).mkdir(parents=True)
        shutil.copyfile(OASIS / "oasis.kicad_pcb", repo / rev / "oasis.kicad_pcb")
        (repo / rev / "positions.csv").write_text(cpl, encoding="utf-8")
        shutil.copytree(OASIS / "gerbers" / "easyeda",
                        cache / "runs" / b["id"] / f"rev_{rev}-cpl" / "easyeda")
    return cache, b


def test_dfm_catch_finds_a_planted_cpl_rotation_and_sees_the_fix(tmp_path, monkeypatch):
    monkeypatch.setenv("HWDE_EASYEDA_OFFLINE", "1")
    cache, b = _cpl_board(tmp_path, "299.0")
    assert ec.run(cache, b)["cplplant-1"] == {"ran": True, "rev_a": True, "rev_b": False}


def test_dfm_catch_reads_the_cpl_not_the_board(tmp_path, monkeypatch):
    """Same copper on both revisions: only the CPL row differs, so a rev B
    left 180 off is still found there (the outcome reads it as regressed)."""
    monkeypatch.setenv("HWDE_EASYEDA_OFFLINE", "1")
    cache, b = _cpl_board(tmp_path, "119.0")
    r = ec.run(cache, b)["cplplant-1"]
    assert r == {"ran": True, "rev_a": True, "rev_b": True}
    assert ec.outcome(b["defects"][0], r).startswith("REGRESSED")


def test_dfm_catch_needs_the_revisions_cpl(tmp_path):
    import pytest
    b = {"id": "x", "rev_a": {"pcb": "p.kicad_pcb"}}
    with pytest.raises(SystemExit, match="cpl"):
        ec._dfm(b, "rev_a", tmp_path, tmp_path / "constraints.json")


def test_fetch_checks_out_the_cpl_dirs(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(ec.subprocess, "run", lambda cmd, **k: calls.append(cmd))
    b = {"id": "o", "url": "u", "commit": "c" * 40,
         "rev_a": {"sch": "v2/kicad/p.kicad_sch", "cpl": "v2/positions.csv"},
         "rev_b": {"sch": "v3/kicad/p.kicad_sch"}}
    ec.fetch(tmp_path, b)
    sparse = next(c for c in calls if "sparse-checkout" in c)
    assert sparse[-3:] == ["v2", "v2/kicad", "v3/kicad"]
