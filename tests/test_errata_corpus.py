"""The real-errata corpus: its manifest, its matcher, and the three
netlist_audit rules its rev-A boards needed (ground_island, pin_net_swap,
sot23_pinout), each on a netlist shaped like the board it came from."""
import json
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
    for b in ec.load_all():
        for d in b["defects"]:
            if d.get("catch"):
                assert d["catch"]["script"] in ec.RUNNERS, d["id"]


def _holdout_manifest(tmp_path, swept=True, blind="missed", verdict="missed",
                      rnd=1, **extra):
    d = {"id": "h-1", "summary": "s", "source_quote": "q", "fault_class": "c",
         "verdict": verdict, "follow_up": "#1", **extra}
    if blind:
        d["blind"] = blind
    b = {"id": "h", "url": "u", "commit": "0" * 40, "licence": "MIT", "source": "s",
         "rev_a": {"name": "A", "sch": "a/x.kicad_sch"},
         "rev_b": {"name": "B", "sch": "b/x.kicad_sch"}, "defects": [d]}
    if swept:
        b["swept"] = {"flow": "1" * 40, "date": "2026-11-14", "round": rnd}
    import yaml
    m = tmp_path / "m.yaml"
    m.write_text(yaml.safe_dump({"boards": [], "holdout": [b]}))
    return m


def test_holdout_needs_the_sweep_and_a_blind_verdict(tmp_path):
    assert ec.load(_holdout_manifest(tmp_path), "holdout")[0]["id"] == "h"
    for bad in ({"swept": False}, {"rnd": None}, {"rnd": 0}, {"blind": None}, {"blind": "caught_with_constraints"},
                {"verdict": "caught", "catch": {"script": "check_silk", "kind": "k"}}):
        try:
            ec.load(_holdout_manifest(tmp_path, **bad), "holdout")
        except SystemExit:
            continue
        raise AssertionError(bad)
    # a later rule may catch it, but must be named; the blind verdict stays
    ok = _holdout_manifest(tmp_path, verdict="caught", caught_by="#9",
                           catch={"script": "check_silk", "kind": "k"})
    assert ec.load(ok, "holdout")[0]["defects"][0]["blind"] == "missed"


def test_the_holdout_has_its_own_blind_count(tmp_path):
    m = _holdout_manifest(tmp_path, verdict="caught", caught_by="#9",
                          catch={"script": "check_silk", "kind": "k"})
    t = ec.table(tmp_path, ec.load(), ec.load(m, "holdout"))
    assert "- Holdout round 1, blind: 0 of 1 caught (1 missed)." in t
    assert "- Holdout round 1, with the rules written since: 1 of 1 caught" in t
    assert t.index("## Training set") < t.index("## Holdout round 1")
    assert "## A new holdout round" in t
    assert "since #9" in t


def _training_manifest(tmp_path, verdicts, **extra):
    import yaml
    ds = [{"id": f"t-{i}", "summary": "s", "source_quote": "q", "fault_class": "c",
           "verdict": v, "follow_up": "#1", **extra} for i, v in enumerate(verdicts)]
    b = {"id": "t", "url": "u", "commit": "0" * 40, "licence": "MIT", "source": "s",
         "rev_a": {"name": "A", "sch": "a/x.kicad_sch"},
         "rev_b": {"name": "B", "sch": "b/x.kicad_sch"}, "defects": ds}
    m = tmp_path / "t.yaml"
    m.write_text(yaml.safe_dump({"boards": [b], "holdout": []}))
    return m


def test_a_not_pinnable_defect_leaves_the_denominator(tmp_path):
    """A defect whose fix is in every commit is out of the count, with the
    count it would have made kept beside the new one (#1498)."""
    m = _training_manifest(tmp_path, ["missed", "missed", "not_pinnable"],
                           note="the fix is present in every commit")
    t = ec.table(tmp_path, ec.load(m), [])
    assert ("- Training: 0 of 2 caught (2 missed; 1 not_pinnable left out). "
            "With the not_pinnable defect counted, as before #1498: 0 of 3.") in t
    # the quiet twin: nothing left out, the line reads as it always did
    t = ec.table(tmp_path, ec.load(_training_manifest(tmp_path, ["missed"] * 3)), [])
    assert "- Training: 0 of 3 caught (3 missed)." in t
    assert "not_pinnable" not in t and "before #1498" not in t


def test_not_pinnable_needs_a_note_and_the_training_set(tmp_path):
    try:
        ec.load(_training_manifest(tmp_path, ["not_pinnable"]))
    except SystemExit as e:
        assert "needs a note" in str(e)
    else:
        raise AssertionError("not_pinnable with no note loaded")
    # a holdout defect keeps its blind count: no leaving the denominator
    try:
        ec.load(_holdout_manifest(tmp_path, verdict="not_pinnable", note="n"), "holdout")
    except SystemExit as e:
        assert "training" in str(e)
    else:
        raise AssertionError("not_pinnable loaded in the holdout")


def test_only_a_fix_in_every_commit_is_not_pinnable():
    """a03cb's ruling (hwde-gaps-rerank): ambient-1 alone; the other
    not_static training defects stay honest misses."""
    left = [d for b in ec.load() for d in b["defects"] if d["verdict"] == "not_pinnable"]
    assert [d["id"] for d in left] == ["ambient-1"]
    assert "every commit" in left[0]["note"]
    ids = {d["id"]: d["verdict"] for b in ec.load() for d in b["defects"]}
    assert ids["bedlift-3"] == "not_static" and ids["minuet-7"] == "missed"


def test_each_holdout_round_is_counted_apart(tmp_path):
    import yaml
    one = yaml.safe_load(_holdout_manifest(tmp_path).read_text())["holdout"][0]
    two = yaml.safe_load(_holdout_manifest(tmp_path, blind="caught", verdict="caught",
                                           rnd=2, catch={"script": "check_silk",
                                                         "kind": "k"}
                                           ).read_text())["holdout"][0]
    two["id"], two["defects"][0]["id"] = "h2", "h2-1"
    m = tmp_path / "two.yaml"
    m.write_text(yaml.safe_dump({"boards": [], "holdout": [one, two]}))
    t = ec.table(tmp_path, [], ec.load(m, "holdout"))
    assert "- Holdout round 1, blind: 0 of 1 caught (1 missed)." in t
    assert "- Holdout round 2, blind: 1 of 1 caught (1 caught)." in t
    assert t.index("## Holdout round 1") < t.index("## Holdout round 2")


def test_sweep_keeps_what_the_fix_removed(tmp_path, monkeypatch):
    found = {"rev_a": [{"kind": "k", "net": "/A", "refs": ["R1"]},
                       {"kind": "k", "net": "/B", "refs": ["R2"]}],
             "rev_b": [{"kind": "k", "net": "/B", "refs": ["R2"]}]}
    monkeypatch.setattr(ec, "audit", lambda c, b, rev, s: found[rev])
    b = {"id": "h", "rev_a": {}, "rev_b": {}}
    r = ec.sweep(tmp_path, b)
    assert "dfm" not in r  # no CPL, no placement check
    assert r["check_silk"]["gone_on_rev_b"] == [found["rev_a"][0]]
    assert (tmp_path / "runs" / "h" / "sweep.json").exists()


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
    spec = [{"mpn": "X", "match": "^X1$", "verified": True, "requires": [
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
            assert r.get("when") in (None, "connected", "undriven")


def test_check_current_catch_runs_on_the_board():
    assert ec.RUNNERS["check_current"]
    caught = [d for b in ec.load() for d in b["defects"]
              if (d.get("catch") or {}).get("script") == "check_current"]
    assert any(d["id"] == "digits-1" for d in caught)


def test_check_ratings_reads_the_library_not_the_foreign_parts(tmp_path):
    """check_ratings on a foreign board: its parts/ is not read (the overvolt
    fixture's U1.10 error comes only from its own extractions, which hwde's
    library lacks), the run does not crash on the missing parts folder or
    power tree, and no ledger.json lands in the cached repo even when it
    looks like a workspace."""
    import shutil
    fix = ROOT / "tests" / "fixtures" / "ratings" / "overvolt"
    b = {"id": "ratplant", "commit": "0" * 40,
         "rev_a": {"name": "a", "pcb": "kicad/board.kicad_pcb"},
         "rev_b": {"name": "b", "pcb": "kicad/board.kicad_pcb"},
         "defects": []}
    cache = tmp_path / "cache"
    repo = ec.repo_dir(cache, b)
    shutil.copytree(fix, repo)
    (repo / "state.json").write_text("{}")
    assert not (ROOT / "reference" / "extractions" / "C6186.json").exists()
    viols = ec.audit(cache, b, "rev_a", "check_ratings")
    assert "U1.10" not in [v.get("pin") for v in viols
                           if v.get("severity") == "error"]
    assert not (repo / "ledger.json").exists()
    rep = cache / "runs" / "ratplant" / "rev_a-check_ratings.json"
    assert rep.is_file()
    assert isinstance(json.loads(rep.read_text())["rated_from"], dict)


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


def test_a_revision_kept_only_in_history_gets_its_own_checkout(tmp_path):
    b = {"id": "h", "commit": "b" * 40, "rev_a": {"commit": "a" * 40}, "rev_b": {}}
    assert ec.repo_dir(tmp_path, b, "rev_a").name == "h@" + "a" * 12
    assert ec.repo_dir(tmp_path, b, "rev_b") == ec.repo_dir(tmp_path, b)


def test_a_kicad8_board_is_read_as_it_is(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "x.kicad_pcb").write_text("(kicad_pcb (version 20240108))")
    b = {"id": "x", "rev_a": {"pcb": "x.kicad_pcb"}}
    empty = tmp_path / "run" / "constraints.json"
    empty.parent.mkdir()
    assert ec._pcb(b, "rev_a", repo, empty) == repo / "x.kicad_pcb"


def test_a_pre_kicad6_board_is_upgraded_once_into_the_run_dir(tmp_path, monkeypatch):
    # sizif rev C, ambient, cmm2-jlc, smart-lcc, lofence: `module`-era files
    # geom saw no pads on until they were upgraded
    import env
    repo = tmp_path / "repo"
    repo.mkdir()
    src = repo / "old.kicad_pcb"
    src.write_text("(kicad_pcb (version 20171130) (module x))")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        Path(cmd[-1]).write_text("(kicad_pcb (version 20240108) (footprint x))")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    import subprocess
    monkeypatch.setattr(env, "find_kicad_cli", lambda: "kicad-cli")
    monkeypatch.setattr(ec.subprocess, "run", fake_run)
    b = {"id": "old", "rev_a": {"pcb": "old.kicad_pcb"}}
    empty = tmp_path / "run" / "constraints.json"
    empty.parent.mkdir()
    got = ec._pins_pcb(b, "rev_a", repo, empty)
    assert got == empty.parent / "rev_a-upgraded.kicad_pcb"
    assert calls and calls[0][1:3] == ["pcb", "upgrade"]
    assert "footprint" in got.read_text() and "module" in src.read_text()
    assert ec._pins_pcb(b, "rev_a", repo, empty) == got and len(calls) == 1
    src.write_text("(kicad_pcb (version 20171130) (module y))")
    ec._pins_pcb(b, "rev_a", repo, empty)
    assert len(calls) == 2                       # source changed: redone
    # mdbt-micro-50q, zxdiag (#1123): a KiCad 6/7 board's references are
    # fp_text, which placelib reads none from, so it is upgraded too
    src.write_text("(kicad_pcb (version 20211014) (footprint y))")
    ec._pins_pcb(b, "rev_a", repo, empty)
    assert len(calls) == 3


def _pins_repo(tmp_path, monkeypatch, renet=None):
    """blinky2 (no pin names on its pads) as a checkout, with a schematic
    whose netlist names U2's pins; `renet` moves pads to other nets, as a
    schematic that is not this board's would."""
    import shutil
    from test_pin_backfill import AMS1117, BLINKY2, _netlist
    repo = tmp_path / "repo"
    repo.mkdir()
    shutil.copyfile(BLINKY2, repo / "b.kicad_pcb")
    (repo / "b.kicad_sch").write_text("(kicad_sch)")
    net = tmp_path / "b.net"
    net.write_text(_netlist(BLINKY2, {"U2": AMS1117}, renet=renet))
    calls = []
    monkeypatch.setattr(ec, "_netlist", lambda *a: calls.append(a) or net)
    b = {"id": "x", "rev_a": {"pcb": "b.kicad_pcb", "sch": "b.kicad_sch"}}
    empty = tmp_path / "run" / "constraints.json"
    empty.parent.mkdir()
    return repo, b, empty, calls


def test_the_sweep_reads_a_board_with_its_schematics_pin_names(tmp_path,
                                                               monkeypatch):
    # hwde-holdout-3: 11 of 25 blind boards carry no pin names, so every
    # check that finds a pin by name judged them from constraints alone
    import pin_backfill as pb
    repo, b, empty, calls = _pins_repo(tmp_path, monkeypatch)
    before = (repo / "b.kicad_pcb").read_bytes()
    got = ec._pins_pcb(b, "rev_a", repo, empty)
    assert got == empty.parent / "rev_a-pins.kicad_pcb"
    u2 = {p["num"]: p["fn"] for fp in pb.footprints(got.read_text("utf-8"))
          if fp["ref"] == "U2" for p in fp["pads"]}
    assert u2["2"] == "VOUT" and u2["3"] == "VIN"
    assert (repo / "b.kicad_pcb").read_bytes() == before   # checkout untouched
    rep = json.loads((empty.parent / "rev_a-pins.json").read_text())
    assert rep["status"] == "written"
    assert ec._pins_pcb(b, "rev_a", repo, empty) == got and len(calls) == 1
    (repo / "b.kicad_sch").write_text("(kicad_sch (changed))")
    assert ec._pins_pcb(b, "rev_a", repo, empty) == got and len(calls) == 2


def test_a_schematic_that_is_not_the_boards_names_nothing(tmp_path,
                                                          monkeypatch):
    import pin_backfill as pb
    from test_pin_backfill import BLINKY2
    fps = pb.footprints(BLINKY2.read_text("utf-8"))
    renet = {f'{fp["ref"]}.{p["num"]}': "OTHER" for fp in fps
             for p in fp["pads"]}
    repo, b, empty, calls = _pins_repo(tmp_path, monkeypatch, renet)
    assert ec._pins_pcb(b, "rev_a", repo, empty) == repo / "b.kicad_pcb"
    assert not (empty.parent / "rev_a-pins.kicad_pcb").exists()
    rep = json.loads((empty.parent / "rev_a-pins.json").read_text())
    assert rep["status"] == "mismatch"
    assert ec._pins_pcb(b, "rev_a", repo, empty) == repo / "b.kicad_pcb"
    assert len(calls) == 1                       # the verdict is kept too


def test_a_board_whose_netlist_will_not_export_is_read_as_it_is(tmp_path,
                                                                monkeypatch):
    repo, b, empty, _ = _pins_repo(tmp_path, monkeypatch)

    def fail(*a):
        raise SystemExit("export failed")
    monkeypatch.setattr(ec, "_netlist", fail)
    assert ec._pins_pcb(b, "rev_a", repo, empty) == repo / "b.kicad_pcb"
    rep = json.loads((empty.parent / "rev_a-pins.json").read_text())
    assert rep["status"] == "no_netlist"


def test_a_check_that_writes_no_report_keeps_its_stderr(tmp_path, monkeypatch):
    # #1189: the sweep scored a crashed check as "no finding"
    def crash(c, b, rev, s):
        if s == "check_current":
            raise ec.CheckCrashed(f"h {rev}: {s} wrote no report", 1,
                                  "".join(f"line {i}\n" for i in range(30))
                                  + "KeyError: 'value'\n")
        return []
    monkeypatch.setattr(ec, "audit", crash)
    r = ec.sweep(tmp_path, {"id": "h", "rev_a": {}, "rev_b": {}})
    e = r["check_current"]
    assert e["error"] == "h rev_a: check_current wrote no report (rc 1)"
    assert e["rc"] == 1 and len(e["stderr_tail"]) == 20
    assert e["stderr_tail"][-1] == "KeyError: 'value'"
    assert "error" not in r["check_silk"]


def test_audit_raises_with_the_check_stderr(tmp_path, monkeypatch):
    b = {"id": "h", "rev_a": {}, "rev_b": {}}
    monkeypatch.setitem(ec.RUNNERS, "check_current",
                        lambda b, rev, d, c: ["nope.py"])
    monkeypatch.setattr(ec, "repo_dir", lambda cache, b, rev: tmp_path)
    monkeypatch.setattr(ec, "SCRIPTS", tmp_path)
    (tmp_path / "nope.py").write_text("import sys; sys.exit('boom here')")
    try:
        ec.audit(tmp_path, b, "rev_a", "check_current")
    except ec.CheckCrashed as e:
        assert e.rc == 1 and e.tail == ["boom here"]
        assert "boom here" in str(e)
    else:
        raise AssertionError("no CheckCrashed")


def _fake_check(tmp_path, monkeypatch, body):
    monkeypatch.setitem(ec.RUNNERS, "check_current",
                        lambda b, rev, d, c: ["fake.py"])
    monkeypatch.setattr(ec, "repo_dir", lambda cache, b, rev: tmp_path)
    monkeypatch.setattr(ec, "SCRIPTS", tmp_path)
    (tmp_path / "fake.py").write_text(body)
    try:
        ec.audit(tmp_path, {"id": "h"}, "rev_a", "check_current")
    except ec.CheckCrashed as e:
        return e
    raise AssertionError("no CheckCrashed")


def test_an_error_json_on_stdout_is_kept(tmp_path, monkeypatch):
    # checklib.cli_wrap's contract: error JSON on stdout, exit 2, no stderr
    e = _fake_check(tmp_path, monkeypatch, (
        "import json, sys\n"
        "print(json.dumps({'script': 'check_current', 'status': 'error',"
        " 'error': 'StaleFillError: 1 zone(s) unfilled'}))\n"
        "sys.exit(2)\n"))
    assert e.rc == 2 and e.check_error == "StaleFillError: 1 zone(s) unfilled"
    assert "StaleFillError" in str(e).splitlines()[0]


def test_a_report_with_status_error_is_a_crash_not_a_pass(tmp_path, monkeypatch):
    e = _fake_check(tmp_path, monkeypatch, (
        "import json, sys\n"
        "out = sys.argv[sys.argv.index('--out') + 1]\n"
        "open(out, 'w').write(json.dumps({'status': 'error', 'error': 'x'}))\n"
        "sys.exit(2)\n"))
    assert e.check_error == "x" and "reported an error" in str(e)


def _planted(tmp_path, monkeypatch, body):
    """A check script `planted` whose source is `body`, run by the real
    audit() through its runner, as a corpus check would be."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "planted.py").write_text(body)
    monkeypatch.setattr(ec, "SCRIPTS", scripts)
    monkeypatch.setattr(ec, "repo_dir", lambda c, b, rev: tmp_path)
    monkeypatch.setattr(ec, "RUNNERS", {"planted": lambda b, rev, repo, c: ["planted.py"]})
    return tmp_path / "cache"


CRASH = "import sys\nprint('boom: planted crash', file=sys.stderr)\nsys.exit(3)\n"


def test_a_crashed_catch_reads_crashed_in_the_table(tmp_path, monkeypatch):
    cache = _planted(tmp_path, monkeypatch, CRASH)
    d = {"id": "p-1", "verdict": "caught", "catch": {"script": "planted", "kind": "x"}}
    b = {"id": "p", "rev_a": {}, "rev_b": {}, "defects": [d]}
    res = ec.run(cache, b)
    assert res["p-1"]["crashed"] == "planted" and res["p-1"]["rc"] == 3
    assert ec.outcome(d, res["p-1"]) == "crashed: planted"
    assert ec.outcome({**d, "caught_by": "#1"}, res["p-1"]).startswith("crashed: planted")


def test_a_stale_report_does_not_stand_in_for_a_crash(tmp_path, monkeypatch):
    cache = _planted(tmp_path, monkeypatch, CRASH)
    stale = cache / "runs" / "p" / "rev_a-planted.json"
    stale.parent.mkdir(parents=True)
    stale.write_text('{"violations": [{"kind": "old"}]}')
    assert "error" in ec.sweep(cache, {"id": "p", "rev_a": {}, "rev_b": {}})["planted"]


def test_sweep_lists_coverage_notes_apart(tmp_path, monkeypatch):
    note = {"kind": "rating_unrated", "net": None, "refs": ["D2"]}
    fault = {"kind": "rating_over_voltage", "net": "/5V", "refs": ["C1"]}
    found = {"rev_a": [note, fault], "rev_b": []}
    monkeypatch.setattr(ec, "audit", lambda c, b, rev, s: found[rev])
    r = ec.sweep(tmp_path, {"id": "h", "rev_a": {}, "rev_b": {}})
    assert r["check_ratings"]["gone_on_rev_b"] == [fault]
    assert r["check_ratings"]["coverage_gone_on_rev_b"] == [note]
    assert r["check_ratings"]["rev_a"] == 2
    # a check with no coverage kinds keeps every row
    assert r["check_silk"]["gone_on_rev_b"] == [note, fault]
    assert "coverage_gone_on_rev_b" not in r["check_silk"]


def test_ratings_not_judged_kinds_are_coverage(tmp_path, monkeypatch):
    """Every ratings kind that only says a limit wasn't judged is coverage,
    whatever its severity; a finding of a limit broken stays in triage."""
    notes = [{"kind": k, "net": None, "refs": ["U1"]} for k in (
        "rating_datasheet_unassessed", "rating_regulator_output_unjudged",
        "rating_no_attributes")]
    fault = {"kind": "rating_regulator_output", "net": "/3V3", "refs": ["U1"]}
    found = {"rev_a": notes + [fault], "rev_b": []}
    monkeypatch.setattr(ec, "audit", lambda c, b, rev, s: found[rev])
    r = ec.sweep(tmp_path, {"id": "h", "rev_a": {}, "rev_b": {}})["check_ratings"]
    assert r["gone_on_rev_b"] == [fault]
    assert r["coverage_gone_on_rev_b"] == notes


def test_sweep_output_labels_the_crash(tmp_path, monkeypatch, capsys):
    cache = _planted(tmp_path, monkeypatch, CRASH)
    monkeypatch.setattr(ec, "fetch", lambda c, b: None)
    monkeypatch.setattr(ec, "_raw", lambda m, w: [{"id": "p", "rev_a": {}, "rev_b": {}}])
    ec.main(["sweep", "p", "--cache", str(cache)])
    out = capsys.readouterr().out
    assert "p planted crashed: planted" in out and "1 errored" in out


def test_run_cli_counts_a_crash_as_bad(tmp_path, monkeypatch):
    cache = _planted(tmp_path, monkeypatch, CRASH)
    d = {"id": "p-1", "verdict": "caught", "catch": {"script": "planted", "kind": "x"}}
    monkeypatch.setattr(ec, "load_all", lambda m: [{"id": "p", "rev_a": {}, "rev_b": {},
                                                    "defects": [d]}])
    assert ec.main(["run", "p", "--cache", str(cache)]) == 1


def test_check_current_reads_a_shipped_constraints_json(tmp_path):
    """check_current takes the board's own constraints.json where the project
    ships one, the same lookup on every board; with none, the empty file."""
    repo = tmp_path / "repo"
    (repo / "hw").mkdir(parents=True)
    pcb = repo / "hw" / "b.kicad_pcb"
    pcb.write_text("(kicad_pcb (version 20240108))")
    b = {"id": "x", "rev_a": {"pcb": "hw/b.kicad_pcb"}}
    empty = tmp_path / "constraints.json"
    empty.write_text("{}")
    args = ec._check_current(b, "rev_a", repo, empty)
    assert args[args.index("--constraints") + 1] == str(empty)
    (repo / "constraints.json").write_text('{"power": []}')
    args = ec._check_current(b, "rev_a", repo, empty)
    assert args[args.index("--constraints") + 1] == str(repo / "constraints.json")
    (repo / "hw" / "constraints.json").write_text('{"power": []}')
    args = ec._check_current(b, "rev_a", repo, empty)
    assert args[args.index("--constraints") + 1] == str(repo / "hw" / "constraints.json")
    # a catch's own constraints win over the board's
    stated = tmp_path / "constraints-c1.json"
    stated.write_text('{"power": [{"net": "/5V"}]}')
    args = ec._check_current(b, "rev_a", repo, stated)
    assert args[args.index("--constraints") + 1] == str(stated)


def test_coverage_reads_judged_nothing_and_not_judged():
    assert ec.coverage({"status": "pass", "nothing_to_judge": "no MCU"}) == {
        "status": "nothing_to_judge", "reason": "no MCU", "not_judged": 0}
    assert ec.coverage({"status": "pass", "not_judged": ["a", "b"]}) == {
        "status": "judged", "not_judged": 2}
    assert ec.coverage({"not_judged": {"r1": ["C1", "C2"], "r2": 3}}) == {
        "status": "judged", "not_judged": 5}
    assert ec.coverage({"status": "pass"}) == {"status": "judged", "not_judged": 0}


NOTHING = ("import json, sys\n"
           "out = sys.argv[sys.argv.index('--out') + 1]\n"
           "json.dump({'status': 'pass', 'violations': [],"
           " 'nothing_to_judge': 'no part of its kind'}, open(out, 'w'))\n")


def test_sweep_prints_a_coverage_line_per_check(tmp_path, monkeypatch, capsys):
    cache = _planted(tmp_path, monkeypatch, NOTHING)
    monkeypatch.setattr(ec, "fetch", lambda c, b: None)
    monkeypatch.setattr(ec, "_raw", lambda m, w: [
        {"id": "p", "rev_a": {}, "rev_b": {}}, {"id": "q", "rev_a": {}, "rev_b": {}}])
    ec.main(["sweep", "p", "q", "--cache", str(cache)])
    out = capsys.readouterr().out
    assert ("coverage planted: judged 0, nothing_to_judge 2, judged nothing "
            "(unsaid) 0, with not_judged items 0, crashed 0") in out
    r = ec.sweep(cache, {"id": "p", "rev_a": {}, "rev_b": {}})["planted"]
    assert r["coverage"]["status"] == "nothing_to_judge"


def test_coverage_line_counts_a_crash():
    rows = [("a", "c", {"error": "boom"}),
            ("b", "c", {"coverage": {"status": "judged", "not_judged": 1}})]
    assert ec.coverage_line(rows) == [
        "coverage c: judged 1, nothing_to_judge 0, judged nothing (unsaid) 0, "
        "with not_judged items 1, crashed 1"]


def test_coverage_counts_judged_only_on_evidence():
    """A check that says nothing_to_judge only when it finds no part of its
    kind is judged only when its report shows it judged something."""
    assert ec.coverage({"checked": []}, "check_current")["status"] == "judged_nothing"
    assert ec.coverage({"checked": [{"net": "/5V"}]}, "check_current")["status"] == "judged"
    # a finding is a judgement; a can't-judge note is not
    assert ec.coverage({"violations": [{"kind": "rating_unrated"}],
                        "pins_checked": 0}, "check_ratings")["status"] == "judged_nothing"
    assert ec.coverage({"violations": [{"kind": "rating_over"}],
                        "pins_checked": 0}, "check_ratings")["status"] == "judged"
    # a part rated with no extraction (listing, part-number code) is judged
    assert ec.coverage({"pins_checked": 0, "parts_rated_unextracted": ["C1"]},
                       "check_ratings")["status"] == "judged"
    assert ec.EVIDENCE["check_ratings"]({"parts_rated_unextracted": ["C1", "C2"]}) == 2
    assert ec.coverage({"violations": [{"kind": "mcu_unknown"}],
                        "checked": [{"mcus": []}]}, "check_straps")["status"] == "judged_nothing"
    assert ec.coverage({"checked": [{"mcus": ["U1"]}]}, "check_straps")["status"] == "judged"
    assert ec.coverage({"coverage": {"ran": []}}, "dfm")["status"] == "judged_nothing"
    assert ec.coverage({"pins_connected": 0, "i2c_buses_checked": 1},
                       "netlist_audit")["status"] == "judged"
    # nothing_to_judge still wins, and a check with no evidence rule keeps the default
    assert ec.coverage({"nothing_to_judge": "x", "checked": []},
                       "check_current")["status"] == "nothing_to_judge"
    assert ec.coverage({}, "planted")["status"] == "judged"


def test_coverage_counts_footprint_parts():
    geo = ec.coverage({"judged": {"stock": 5, "model": 2},
                       "not_judged": {"why": ["R1", "R2"]}}, "check_fp_geometry")
    assert (geo["status"], geo["parts_judged"], geo["parts_not_judged"]) == ("judged", 7, 2)
    pkg = ec.coverage({"judged": 0, "not_judged": {"a": 3, "b": 1}}, "check_fp_package")
    assert (pkg["status"], pkg["parts_judged"], pkg["parts_not_judged"]) == (
        "judged_nothing", 0, 4)
    rows = [("a", "check_fp_package", {"coverage": pkg}),
            ("b", "check_fp_package", {"coverage": {**pkg, "status": "judged",
                                                    "parts_judged": 6}})]
    assert ec.coverage_line(rows) == [
        "coverage check_fp_package: judged 1, nothing_to_judge 0, judged nothing "
        "(unsaid) 1, with not_judged items 2, crashed 0; parts judged 6, "
        "parts not_judged 8"]


def test_blocker_label_names_nothing_on_the_board():
    lab = ec.blocker_label
    assert lab("reference/reset_states.yaml has no row for U3 (STM32G071KBUx)") \
        == "reference/reset_states.yaml has no row"
    assert lab("no standard header on J4") == "no standard header"
    assert lab("4 connector(s) on the board, none with an extraction") \
        == "connector on the board, none with an extraction"
    assert lab("package 'SMD-4P,1.6x0.9mm' not read") == "package not read"
    assert lab("the part's package name is not read") == "the part's package name is not read"
    assert lab("") == "unlabelled"
    # items: a kind by its kind, `REFS (part) ...: why` by its why
    assert ec._item_label("rail_over on +3V3: U2 (AMS1117-3.3) is rated 800 mA") == "rail_over"
    assert ec._item_label("U2 (LMR62014) SW on Net-(C8-Pad1): a switch node whose part "
                          "isn't in switch_nodes.yaml") \
        == "a switch node whose part isn't in switch_nodes.yaml"
    assert ec._item_label("J3: no extraction") == "no extraction"
    assert ec._item_label({"ref": "U1", "reason": "no row"}) == "no row"


def test_blockers_say_why_a_board_was_not_judged():
    cov = ec.coverage({"nothing_to_judge": "no MCU on the board"}, "check_straps")
    assert ec.blockers({"nothing_to_judge": "no MCU on the board"}, cov) == {
        "board": "no MCU on the board", "items": {}}
    # a check that judged nothing and gave no reason (#1295) is counted so
    cov = ec.coverage({"pins_checked": 0}, "check_ratings")
    assert ec.blockers({"pins_checked": 0}, cov)["board"] == "no reason given"
    # judged, with items it couldn't judge, by reason
    rep = {"judged": 2, "not_judged": {"no part number on the footprint": ["R1", "R2"],
                                       "package 'X-1' not read": 3}}
    bl = ec.blockers(rep, ec.coverage(rep, "check_fp_package"))
    assert bl == {"board": None, "items": {"no part number on the footprint": 2,
                                           "package not read": 3}}
    # a crash or a timeout, and the pin names the harness couldn't give
    assert ec.blockers(None, None, {"error": "b rev_a: x timed out after 900 s"},
                       "no_netlist") == {"board": "timeout", "items": {},
                                         "inputs": ["pin names: no_netlist"]}
    assert ec.blockers(None, None, {"error": "boom"})["board"] == "crashed"
    # an input note only on a board the check didn't judge
    assert "inputs" not in ec.blockers({}, {"status": "judged"}, pins="no_netlist")


def test_blocker_lines_count_by_reason_per_check():
    rows = [("a", "c", {"blockers": {"board": "no reason given", "items": {}}}),
            ("b", "c", {"blockers": {"board": "no MCU", "items": {"x": 2}}}),
            ("d", "c", {"blockers": {"board": "no MCU", "items": {"x": 1},
                                     "inputs": ["pin names: no_netlist"]}}),
            ("e", "c", {"blockers": {"board": None, "items": {}}})]
    assert ec.blocker_lines(rows) == [
        "blockers c: boards not judged: no MCU 2; no reason given 1 | inputs "
        "missing: pin names: no_netlist 1 | items not judged: x 3 on 2 board(s)"]


def test_sweep_prints_blockers_and_stores_them(tmp_path, monkeypatch, capsys):
    cache = _planted(tmp_path, monkeypatch, NOTHING)
    monkeypatch.setattr(ec, "fetch", lambda c, b: None)
    monkeypatch.setattr(ec, "_raw", lambda m, w: [{"id": "p", "rev_a": {}, "rev_b": {}}])
    ec.main(["sweep", "p", "--cache", str(cache)])
    out = capsys.readouterr().out
    assert ("blockers planted: boards not judged: no part of its kind 1 | "
            "items not judged: none") in out
    (tmp_path / "c").mkdir()
    cache2 = _planted(tmp_path / "c", monkeypatch, CRASH)
    r = ec.sweep(cache2, {"id": "p", "rev_a": {}, "rev_b": {}})["planted"]
    assert r["blockers"]["board"] == "crashed"


def test_errata_ratings_adds_only_each_rated_parts_source(tmp_path, monkeypatch):
    """errata_ratings.py gives check_ratings' own report, unchanged, plus
    rated_from: one source per part it rated (verified, listed or failed)."""
    import errata_ratings
    import check_ratings
    import checklib
    monkeypatch.setenv("HWDE_LEDGER_WRITE", "0")
    # the two runs may stamp different seconds (#1352): make every stamp
    # differ, so a compare that reads the clock fails here every time
    stamp, tick = checklib.stamp, iter(range(10 ** 6))

    def ticking(payload, input_path):
        out = stamp(payload, input_path)
        out["generated_at"] = f"tick {next(tick)}"
        return out
    monkeypatch.setattr(checklib, "stamp", ticking)
    pcb = ROOT / "tests" / "fixtures" / "ratings" / "overvolt" / "kicad" / "board.kicad_pcb"
    mine, _ = errata_ratings.run(["--pcb", str(pcb)])
    theirs, _ = check_ratings.run(["--pcb", str(pcb)])
    rated = mine.pop("rated_from")
    assert mine.pop("generated_at") != theirs.pop("generated_at")
    assert json.loads(json.dumps(mine)) == json.loads(json.dumps(theirs))
    counts = theirs["ledger_counts"]["rating"]
    assert len(rated) == counts["verified"] + counts["listed"] + counts["failed"] > 0
    assert set(rated.values()) <= {"datasheet", "distributor", "part_number_code",
                                   "value", "package"}
    assert "datasheet" in rated.values() and "distributor" in rated.values()
    assert check_ratings.ledger_facts is not None and \
        check_ratings.ledger_facts.__name__ == "ledger_facts"


def test_errata_ratings_tells_a_value_field_from_a_part_number_code():
    from errata_ratings import source_of
    f = {"kind": "rating", "status": "listed", "rated_from": "part_code"}
    assert source_of({**f, "source": "Value field '100n 16V' states 16V"}) == "value"
    assert source_of({**f, "source": "part-number voltage code Q of X"}) == "part_number_code"
    assert source_of({**f, "rated_from": "package"}) == "package"
    assert source_of({**f, "status": "unknown"}) is None
    assert source_of({**f, "kind": "pinout"}) is None
    assert source_of({"kind": "rating", "status": "verified"}) is None  # held


def test_coverage_counts_ratings_parts_by_source():
    rep = {"pins_checked": 3, "rated_from": {"U1": "datasheet", "C1": "value",
                                             "C2": "part_number_code",
                                             "C3": "part_number_code"}}
    cov = ec.coverage(rep, "check_ratings")
    assert (cov["status"], cov["parts_judged"]) == ("judged", 4)
    assert cov["parts_by_source"] == {"datasheet": 1, "part_number_code": 2, "value": 1}
    assert "parts_judged" not in ec.coverage({"pins_checked": 0}, "check_ratings")
    rows = [("a", "check_ratings", {"coverage": cov}),
            ("b", "check_ratings", {"coverage": {
                "status": "judged", "not_judged": 0, "parts_judged": 2,
                "parts_by_source": {"distributor": 2}}})]
    assert ec.coverage_line(rows) == [
        "coverage check_ratings: judged 2, nothing_to_judge 0, judged nothing "
        "(unsaid) 0, with not_judged items 0, crashed 0; parts judged 6 "
        "(distributor 2, part_number_code 2, datasheet 1, value 1)"]


def test_the_sweep_gives_footprints_their_boms_part_numbers(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    pcb = repo / "ctrl.kicad_pcb"
    pcb.write_text(
        '(kicad_pcb (version 20240108)\n'
        '  (footprint "R_0603" (layer "F.Cu")\n'
        '    (property "Reference" "R1" (at 0 0 0) (layer "F.SilkS"))\n'
        '    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))))\n')
    (repo / "ctrl-bom.csv").write_text("Designator,LCSC\nR1,C25804\n")
    before = {p.name: p.read_bytes() for p in repo.iterdir()}
    b = {"id": "x", "rev_a": {"pcb": "ctrl.kicad_pcb"}}
    empty = tmp_path / "run" / "constraints.json"
    empty.parent.mkdir()
    got = ec._pcb(b, "rev_a", repo, empty)
    assert got == empty.parent / "rev_a-partnum.kicad_pcb"
    assert '(property "LCSC" "C25804"' in got.read_text()
    rep = json.loads((empty.parent / "rev_a-partnum.json").read_text())
    assert rep["counts"]["added_from"]["bom"] == 1
    assert {p.name: p.read_bytes() for p in repo.iterdir()} == before
    mtime = got.stat().st_mtime_ns
    assert ec._pcb(b, "rev_a", repo, empty) == got
    assert got.stat().st_mtime_ns == mtime     # reused, not rewritten


def _corpus_sheet(path, lib):
    """A one-symbol schematic whose MCU has a bidirectional SPAN_SCS and a
    VCC typed bidirectional, its symbol named "LIB:MCU"."""
    pins = "\n".join(
        f'      (pin {t} line (at 0 0 0) (length 2.54) (name "{n}" (effects)) '
        f'(number "{i}" (effects)))'
        for i, (n, t) in enumerate([("SPAN_SCS", "bidirectional"),
                                    ("VCC", "bidirectional")], 1))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '(kicad_sch (version 20231120) (generator eeschema)\n'
        ' (lib_symbols\n'
        f'  (symbol "{lib}:MCU" (property "Reference" "U" (at 0 0 0))\n'
        '    (symbol "MCU_1_1"\n' + pins + ')))\n'
        f' (symbol (lib_id "{lib}:MCU") (at 50 50 0)\n'
        '  (property "Reference" "U1" (at 0 0 0))))\n', encoding="utf-8")
    return path


def test_an_imported_revision_is_read_retyped(tmp_path):
    # planted (#1494): an Eagle-imported sheet types every pin from Eagle's
    # `io`; the corpus reads it as the flow does, retyped by lib_pin_types
    b = {"id": "imp", "commit": "a" * 40, "rev_a": {"sch": "hw/top.kicad_sch"}}
    repo = ec.repo_dir(tmp_path, b, "rev_a")
    sch = _corpus_sheet(repo / "hw" / "top.kicad_sch", "top-eagle-import")
    (repo / "hw" / "top.kicad_pcb").write_text("(kicad_pcb)")
    typed = ec.typed_repo(tmp_path, b, "rev_a")
    assert typed != repo and (typed / "hw" / "top.kicad_pcb").is_file()
    text = (typed / "hw" / "top.kicad_sch").read_text()
    assert "(pin passive line" in text and "(pin power_in line" in text
    assert "bidirectional" in sch.read_text()   # the checkout is untouched
    assert ec.typed_repo(tmp_path, b, "rev_a") == typed


def test_a_kicad_drawn_revision_is_read_as_it_is(tmp_path):
    # twin: the same sheet drawn in KiCad (bedlift SPAN_SCS) is not copied
    b = {"id": "kc", "commit": "b" * 40, "rev_a": {"sch": "hw/top.kicad_sch"}}
    repo = ec.repo_dir(tmp_path, b, "rev_a")
    _corpus_sheet(repo / "hw" / "top.kicad_sch", "bedlift")
    assert ec.typed_repo(tmp_path, b, "rev_a") == repo
