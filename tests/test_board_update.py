"""T8 tests: board_update - incremental netlist-diff surgery on routed boards.

Pure tests (venv only: sexpdata/geom/netconn/placelib) cover diff
classification, orphan connectivity analysis and placement validation against
the committed frozen fixtures (pd_trigger route stage, lumina_carrier routed).
Smoke tests (SWIG bundled python + kicad-cli) cover the three apply modes
end-to-end, the fix-loop-to-DRC-0/0 acceptance, rollback and the carrier
4-layer del+add. Mutant netlists are generated in-test by deterministic
sexpdata surgery on the frozen netlists - nothing extra is committed.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import sexpdata

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import board_update as bu  # noqa: E402
import geom  # noqa: E402
import placelib  # noqa: E402
import statelib  # noqa: E402
from checklib import CheckError  # noqa: E402

PD = REPO / "tests" / "fixtures" / "stages" / "pd_trigger"
PD_PCB = PD / "route" / "pd-trigger.kicad_pcb"
PD_NET = PD / "pd-trigger.net"
PD_LIB = PD / "lib"
# DRC on the routed pd-trigger: J1's own pads sit 0.18 and 0.21 mm from its own
# peg holes (vendor geometry); since #813 the board's .kicad_dru carries the
# footprint waiver (reference/footprint_waivers.yaml), so none remain.
USBC_OWN_HOLES = 0
CARRIER = REPO / "tests" / "fixtures" / "stages" / "lumina_carrier"
CA_PCB = CARRIER / "routed.kicad_pcb"
CA_NET = CARRIER / "lumina-carrier.net"

S = sexpdata.Symbol


# ---------------------------------------------------------------------------
# netlist surgery helpers (deterministic, tree-level)
# ---------------------------------------------------------------------------

def _head(n):
    return n[0].value() if isinstance(n, list) and n \
        and isinstance(n[0], sexpdata.Symbol) else None


def _kidval(n, key):
    for c in n[1:]:
        if _head(c) == key:
            return c[1] if len(c) > 1 else None
    return None


def _load_net(path: Path):
    return sexpdata.loads(path.read_text(encoding="utf-8"))


def _dump_net(tree, path: Path) -> Path:
    path.write_text(sexpdata.dumps(tree), encoding="utf-8")
    return path


def _comps_sec(tree):
    return next(s for s in tree[1:] if _head(s) == "components")


def _nets_sec(tree):
    return next(s for s in tree[1:] if _head(s) == "nets")


def _del_ref(tree, ref: str) -> None:
    sec = _comps_sec(tree)
    sec[:] = [sec[0]] + [c for c in sec[1:]
                         if not (_head(c) == "comp"
                                 and _kidval(c, "ref") == ref)]
    for net in _nets_sec(tree)[1:]:
        if _head(net) != "net":
            continue
        net[:] = [x for x in net
                  if not (_head(x) == "node" and _kidval(x, "ref") == ref)]


def _set_value(tree, ref: str, value: str) -> None:
    for c in _comps_sec(tree)[1:]:
        if _head(c) == "comp" and _kidval(c, "ref") == ref:
            for k in c[1:]:
                if _head(k) == "value":
                    k[1] = value
                    return
    raise AssertionError(f"{ref} not in netlist")


def _set_field(tree, ref: str, name: str, value: str) -> None:
    for c in _comps_sec(tree)[1:]:
        if _head(c) == "comp" and _kidval(c, "ref") == ref:
            for k in c[1:]:
                if _head(k) == "fields":
                    for f in k[1:]:
                        if _head(f) == "field" and _kidval(f, "name") == name:
                            f[-1] = value
                            return
                    k.append([S("field"), [S("name"), name], value])
                    return
    raise AssertionError(f"{ref} not in netlist")


def _set_footprint(tree, ref: str, fpid: str) -> None:
    for c in _comps_sec(tree)[1:]:
        if _head(c) == "comp" and _kidval(c, "ref") == ref:
            for k in c[1:]:
                if _head(k) == "footprint":
                    k[1] = fpid
                    return
    raise AssertionError(f"{ref} not in netlist")


def _remove_field(tree, ref: str, name: str) -> None:
    for c in _comps_sec(tree)[1:]:
        if _head(c) == "comp" and _kidval(c, "ref") == ref:
            for k in c[1:]:
                if _head(k) == "fields":
                    k[:] = [x for x in k
                            if not (_head(x) == "field"
                                    and _kidval(x, "name") == name)]
                    return
    raise AssertionError(f"{ref} not in netlist")


def _add_comp(tree, ref: str, value: str, fpid: str,
              pin_nets: dict[str, str], lcsc: str = "C14663") -> None:
    _comps_sec(tree).append(
        [S("comp"), [S("ref"), ref], [S("value"), value],
         [S("footprint"), fpid],
         [S("fields"), [S("field"), [S("name"), "LCSC"], lcsc]]])
    for net in _nets_sec(tree)[1:]:
        if _head(net) != "net":
            continue
        name = _kidval(net, "name")
        for pin, want in pin_nets.items():
            if name == want:
                net.append([S("node"), [S("ref"), ref], [S("pin"), pin],
                            [S("pintype"), "passive"]])


def _rewire(tree, ref: str, pin: str, to_net: str) -> None:
    """Move REF.PIN to `to_net` (an unsupported-diff generator)."""
    moved = None
    for net in _nets_sec(tree)[1:]:
        if _head(net) != "net":
            continue
        for x in list(net):
            if _head(x) == "node" and _kidval(x, "ref") == ref \
                    and _kidval(x, "pin") == pin:
                net.remove(x)
                moved = x
    assert moved is not None, f"{ref}.{pin} not in netlist"
    for net in _nets_sec(tree)[1:]:
        if _head(net) == "net" and _kidval(net, "name") == to_net:
            net.append(moved)
            return
    raise AssertionError(f"net {to_net} not in netlist")


# ---------------------------------------------------------------------------
# staging helpers
# ---------------------------------------------------------------------------

def _stage(tmp_path: Path, pcb: Path, extra_exts=(".kicad_pro", ".kicad_dru",
                                                  ".kicad_prl")) -> Path:
    dst = tmp_path / pcb.name
    shutil.copy2(pcb, dst)
    for ext in extra_exts:
        sib = pcb.with_suffix(ext)
        if sib.is_file():
            shutil.copy2(sib, tmp_path / sib.name)
    return dst


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(pcb: Path, netlist: Path, *extra: str):
    payload, _ = bu.run(["--pcb", str(pcb), "--netlist", str(netlist),
                         *extra])
    return payload


def _norm_lines(pcb: Path) -> list[str]:
    out = statelib.NORMALIZERS["sexpr_no_uuid"](Path(pcb))
    if isinstance(out, bytes):
        out = out.decode("utf-8", "replace")
    return out.splitlines()


def _normalized_delta(a: Path, b: Path) -> list[str]:
    import difflib
    return [ln for ln in difflib.unified_diff(_norm_lines(a), _norm_lines(b),
                                              lineterm="", n=0)
            if ln[:1] in "+-" and ln[:3] not in ("+++", "---")]


def _fill_change_mm2(a: Path, b: Path) -> float:
    """Area of copper fill in one board and not the other, all zones."""
    from shapely.ops import unary_union
    total = 0.0
    for za, zb in zip(geom.BoardGeom.from_file(a).zones_of(),
                      geom.BoardGeom.from_file(b).zones_of(), strict=True):
        for layer in {*za.fills, *zb.fills}:
            total += unary_union(za.fills.get(layer, [])).symmetric_difference(
                unary_union(zb.fills.get(layer, []))).area
    return total


# ---------------------------------------------------------------------------
# pure: diff classification
# ---------------------------------------------------------------------------

def test_baseline_diff_empty_pd():
    payload = _run(PD_PCB, PD_NET, "--dry-run")
    assert payload["status"] == "pass"
    assert payload["changes"] == 0
    for bucket in ("swap_same_fp", "swap_new_fp", "add", "del",
                   "unsupported"):
        assert payload["plan"][bucket] == []


def test_baseline_diff_empty_carrier_and_board_only_excluded():
    payload = _run(CA_PCB, CA_NET, "--dry-run")
    assert payload["changes"] == 0
    assert payload["plan"]["unsupported"] == []
    note = " ".join(payload["plan"]["notes"])
    for hole in ("H1", "H2", "H3", "H4"):
        assert hole in note  # mounting holes never classified as dels


def test_swap_classification(tmp_path):
    tree = _load_net(PD_NET)
    _set_value(tree, "C5", "2.2uF 25V X5R")
    _set_field(tree, "C5", "LCSC", "C23630")
    net = _dump_net(tree, tmp_path / "m.net")
    plan = _run(PD_PCB, net, "--dry-run")["plan"]
    assert [e["ref"] for e in plan["swap_same_fp"]] == ["C5"]
    entry = plan["swap_same_fp"][0]
    assert entry["value"] == ["1uF 50V X5R", "2.2uF 25V X5R"]
    assert entry["fields"]["LCSC"][1] == "C23630"
    assert plan["add"] == plan["del"] == plan["swap_new_fp"] == []


def test_add_classification(tmp_path):
    tree = _load_net(PD_NET)
    _add_comp(tree, "C99", "100nF 50V X7R", "aiee:C0603",
              {"1": "VBUS", "2": "GND"})
    plan = _run(PD_PCB, _dump_net(tree, tmp_path / "m.net"),
                "--dry-run")["plan"]
    assert [e["ref"] for e in plan["add"]] == ["C99"]
    assert plan["add"][0]["pads"] == {"1": "VBUS", "2": "GND"}
    assert plan["del"] == plan["swap_same_fp"] == []


def test_del_classification(tmp_path):
    tree = _load_net(PD_NET)
    _del_ref(tree, "C2")
    plan = _run(PD_PCB, _dump_net(tree, tmp_path / "m.net"),
                "--dry-run")["plan"]
    assert [e["ref"] for e in plan["del"]] == ["C2"]
    assert plan["del"][0]["nets"] == ["GND", "VBUS"]


def test_new_fp_classification(tmp_path):
    tree = _load_net(PD_NET)
    _set_footprint(tree, "C5", "aiee:C1206")
    plan = _run(PD_PCB, _dump_net(tree, tmp_path / "m.net"),
                "--dry-run")["plan"]
    assert [e["ref"] for e in plan["swap_new_fp"]] == ["C5"]
    entry = plan["swap_new_fp"][0]
    assert entry["fp_old"] == "aiee:C0603"
    assert entry["fp_new"] == "aiee:C1206"
    assert entry["pos"]  # defaults the add leg to the old spot
    assert plan["del"] == plan["add"] == []


def test_unsupported_rewire_refuses_apply(tmp_path):
    tree = _load_net(PD_NET)
    _rewire(tree, "C5", "1", "GND")
    net = _dump_net(tree, tmp_path / "m.net")
    payload = _run(PD_PCB, net, "--dry-run")
    assert payload["status"] == "violations"  # dry-run reports, exit 1
    assert payload["plan"]["unsupported"][0]["ref"] == "C5"
    assert payload["plan"]["unsupported"][0]["kind"] == "pad_net_change"
    assert payload["plan"]["unsupported"][0]["routed_pads"] == ["1"]
    assert payload["plan"]["rewire"] == []
    with pytest.raises(CheckError, match="unsupported"):
        _run(PD_PCB, net)  # apply refuses, nothing mutated


PD_PLACE = PD / "place" / "pd-trigger.kicad_pcb"


def test_rewire_on_unrouted_board_is_planned(tmp_path):
    """Issue #29: a pad-net change on a board with no copper on the changed
    pads is a rewire_pad, not a refusal - nothing is stranded."""
    tree = _load_net(PD_NET)
    _rewire(tree, "C5", "1", "GND")
    payload = _run(PD_PLACE, _dump_net(tree, tmp_path / "m.net"),
                   "--dry-run")
    assert payload["status"] == "pass"
    plan = payload["plan"]
    assert plan["unsupported"] == []
    assert [e["ref"] for e in plan["rewire"]] == ["C5"]
    assert plan["rewire"][0]["pads"] == {"1": "GND"}
    assert payload["changes"] == 1


def test_rewire_with_field_change_plans_both(tmp_path):
    tree = _load_net(PD_NET)
    _rewire(tree, "C5", "1", "GND")
    _set_value(tree, "C5", "2.2uF 25V X5R")
    plan = _run(PD_PLACE, _dump_net(tree, tmp_path / "m.net"),
                "--dry-run")["plan"]
    assert [e["ref"] for e in plan["rewire"]] == ["C5"]
    assert [e["ref"] for e in plan["swap_same_fp"]] == ["C5"]


def test_routed_pads_named():
    bg = geom.load_board(PD_PCB, refresh=True)
    assert bu._routed_pads(bg, "C5", {"1"}) == ["1"]
    bg = geom.load_board(PD_PLACE, refresh=True)
    assert bu._routed_pads(bg, "C5", {"1", "2"}) == []


def test_unconnected_star_normalization():
    assert bu._nets_equal("unconnected-(U1-PB0-Pad10)",
                          "unconnected-(U1-PB0-Pad9)")
    assert bu._nets_equal("", None)
    assert not bu._nets_equal("GND", "VBUS")
    assert not bu._nets_equal("unconnected-(X)", "GND")
    # hierarchical exports DROP NC singleton nets: board unconnected-* vs
    # netlist-absent pin (and the netless-board-pad mirror) are the same
    # no-connect class, never a pad_net_change refusal
    assert bu._nets_equal("unconnected-(U1-Pad7)", "")
    assert bu._nets_equal(None, "unconnected-(U1-Pad7)")


def test_board_only_collision_is_unsupported(tmp_path):
    """A netlist ref colliding with a board_only footprint (mounting-hole
    class) must refuse at PLAN time, not die inside the SWIG worker."""
    tree = _load_net(CA_NET)
    _add_comp(tree, "H1", "M3", "aiee:C0603", {})
    payload = _run(CA_PCB, _dump_net(tree, tmp_path / "m.net"), "--dry-run")
    assert payload["status"] == "violations"
    u = payload["plan"]["unsupported"]
    assert [e["ref"] for e in u] == ["H1"]
    assert u[0]["kind"] == "board_only_collision"
    assert payload["plan"]["add"] == []


def test_case_only_fpid_is_note_not_swap(tmp_path):
    """Case-only fpid drift is electrically null - it must not classify as
    a destructive swap_new_fp (del + orphan rip + re-add)."""
    tree = _load_net(PD_NET)
    _set_footprint(tree, "C5", "AIEE:C0603")
    plan = _run(PD_PCB, _dump_net(tree, tmp_path / "m.net"),
                "--dry-run")["plan"]
    assert plan["swap_new_fp"] == []
    assert any("differs only by case" in n for n in plan["notes"])


def test_field_removed_from_netlist_is_noted(tmp_path):
    """A field REMOVED from the netlist (in the netlist's own field
    vocabulary) must not pass silently - it feeds the BOM stale."""
    tree = _load_net(PD_NET)
    _remove_field(tree, "C5", "LCSC")
    plan = _run(PD_PCB, _dump_net(tree, tmp_path / "m.net"),
                "--dry-run")["plan"]
    assert plan["swap_same_fp"] == []
    assert any("C5" in n and "LCSC" in n and "absent from the netlist" in n
               for n in plan["notes"])
    # board-side-only field names ("LCSC Part") never note: the clean
    # baseline diff (test_baseline_diff_empty_pd) pins that direction


# ---------------------------------------------------------------------------
# pure: orphan connectivity analysis
# ---------------------------------------------------------------------------

def test_orphan_analysis_del_c2():
    """Machine-pinned on the frozen fixture: deleting C2 orphans exactly its
    GND stub + GND via; the VBUS feeder (anchored beyond C2) survives; the
    two VBUS fan-in lobes (T-junction endpoint-in-body, DRC-clean) surface
    as pre-existing unanchored copper and are NOT in the orphan set."""
    bg = geom.BoardGeom.from_file(PD_PCB)
    orph = bu.plan_orphans(bg, ["C2"])
    assert orph["affected_nets"] == ["GND", "VBUS"]
    assert [(t.net, t.layer) for t in orph["tracks"]] == [("GND", "F.Cu")]
    assert [(v.net, v.at) for v in orph["vias"]] == [("GND", (24.3, 48.1301))]
    lobes = {p["uuid"] for p in orph["preexisting"]}
    assert len(lobes) == 2  # the two wide VBUS fan-in lobes
    assert {t.uuid for t in orph["tracks"]}.isdisjoint(lobes)
    assert all(t.uuid for t in orph["tracks"])  # removal needs names


def test_orphan_analysis_empty_without_dels():
    bg = geom.BoardGeom.from_file(PD_PCB)
    orph = bu.plan_orphans(bg, [])
    assert orph["affected_nets"] == []
    assert orph["tracks"] == [] and orph["vias"] == []


def test_orphan_analysis_carrier_plane_fed_cap():
    """4L carrier: C62's legs are plane-fed via dedicated vias - deleting it
    orphans those two vias (GND + V48_RAW) and no tracks; the pours and
    every other net stay untouched."""
    bg = geom.BoardGeom.from_file(CA_PCB)
    orph = bu.plan_orphans(bg, ["C62"])
    assert set(orph["affected_nets"]) == {"GND", "V48_RAW"}
    assert orph["tracks"] == []
    assert sorted((v.net, v.at) for v in orph["vias"]) == \
        [("GND", (35.35, 93.5)), ("V48_RAW", (38.0, 93.5))]


def test_geom_tracks_vias_carry_uuids():
    for pcb in (PD_PCB, CA_PCB):
        bg = geom.BoardGeom.from_file(pcb)
        assert all(t.uuid for t in bg.tracks_of())
        assert all(v.uuid for v in bg.vias_of())


# ---------------------------------------------------------------------------
# pure: placements
# ---------------------------------------------------------------------------

def test_placements_validation_shapes():
    adds = [{"ref": "C99"}]
    with pytest.raises(CheckError, match="region"):
        bu.validate_placements({"C99": {"region": [5, 5, 1, 9]}}, adds)
    with pytest.raises(CheckError, match="front-side"):
        bu.validate_placements(
            {"C99": {"region": [1, 1, 9, 9], "side": "back"}}, adds)
    with pytest.raises(CheckError, match="needs x\\+y or region"):
        bu.validate_placements({"C99": {"deg": 90}}, adds)
    out, unused = bu.validate_placements(
        {"C99": {"x": 1, "y": 2}, "C42": {"x": 0, "y": 0}}, adds)
    assert unused == ["C42"]


def test_region_scan_rejects_fully_occupied_region(tmp_path):
    tree = _load_net(PD_NET)
    _add_comp(tree, "C99", "100nF", "aiee:C0603", {"1": "VBUS", "2": "GND"})
    net = _dump_net(tree, tmp_path / "m.net")
    pl = tmp_path / "p.json"
    # dead center of U1 - nothing legal there
    pl.write_text(json.dumps({"C99": {"region": [24.0, 36.0, 26.0, 38.0]}}))
    with pytest.raises(CheckError, match="no courtyard-legal spot"):
        _run(PD_PCB, net, "--placements", str(pl),
             "--lib", str(PD_LIB))


def test_mod_extents_courtyard():
    mod = bu.find_mod("aiee:C0603", [PD_LIB])
    assert mod is not None
    x0, y0, x1, y1 = bu._mod_extents(mod)
    assert 1.0 < (x1 - x0) < 4.0 and 0.5 < (y1 - y0) < 3.0


def test_add_needs_placement(tmp_path):
    tree = _load_net(PD_NET)
    _add_comp(tree, "C99", "100nF", "aiee:C0603", {"1": "VBUS", "2": "GND"})
    net = _dump_net(tree, tmp_path / "m.net")
    with pytest.raises(CheckError, match="needs a placement"):
        _run(PD_PCB, net, "--lib", str(PD_LIB))


def test_add_unknown_footprint_lib(tmp_path):
    tree = _load_net(PD_NET)
    _add_comp(tree, "C99", "100nF", "nosuchlib:C0603",
              {"1": "VBUS", "2": "GND"})
    net = _dump_net(tree, tmp_path / "m.net")
    pl = tmp_path / "p.json"
    pl.write_text(json.dumps({"C99": {"x": 45.0, "y": 46.0}}))
    with pytest.raises(CheckError, match="not found under"):
        _run(PD_PCB, net, "--placements", str(pl), "--lib", str(PD_LIB))


def test_dry_run_mutates_nothing(tmp_path):
    pcb = _stage(tmp_path, PD_PCB)
    before = _sha(pcb)
    tree = _load_net(PD_NET)
    _del_ref(tree, "C2")
    _run(pcb, _dump_net(tree, tmp_path / "m.net"), "--dry-run")
    assert _sha(pcb) == before


def test_two_region_adds_do_not_stack():
    """Two region adds sharing a region must resolve to NON-overlapping
    spots: each resolved courtyard becomes an obstacle for the next."""
    bg = geom.load_board(PD_PCB, refresh=True)
    model = placelib.PlaceModel(PD_PCB)
    mod = bu.find_mod("aiee:C0603", [PD_LIB])
    shared: list = []
    warnings: list = []
    spec = {"region": [40.0, 40.0, 50.0, 50.0]}
    a = bu.resolve_placement("C98", spec, mod, {"1": "VBUS", "2": "GND"},
                             model, bg, warnings, extra_obstacles=shared)
    b = bu.resolve_placement("C99", spec, mod, {"1": "VBUS", "2": "GND"},
                             model, bg, warnings, extra_obstacles=shared)
    assert (a["x"], a["y"]) != (b["x"], b["y"])
    assert len(shared) == 2
    from shapely.geometry import box as _box
    x0, y0, x1, y1 = bu._mod_extents(mod)
    ca = _box(x0 + a["x"], y0 + a["y"], x1 + a["x"], y1 + a["y"])
    cb = _box(x0 + b["x"], y0 + b["y"], x1 + b["x"], y1 + b["y"])
    assert not ca.intersects(cb)


def test_state_prevalidated_before_mutation(tmp_path):
    """A bad --state path must fail BEFORE any mutation (the caller asked
    for edit recording; silently skipping it would drop the invalidation
    marks)."""
    pcb = _stage(tmp_path, PD_PCB)
    before = _sha(pcb)
    tree = _load_net(PD_NET)
    _del_ref(tree, "C2")
    net = _dump_net(tree, tmp_path / "m.net")
    with pytest.raises(CheckError, match="state file"):
        _run(pcb, net, "--state", str(tmp_path / "nope" / "state.json"))
    assert _sha(pcb) == before


# ---------------------------------------------------------------------------
# smoke: the three modes end-to-end (SWIG worker + kicad-cli)
# ---------------------------------------------------------------------------

@pytest.mark.kicad_recorded
@pytest.mark.smoke
def test_swap_apply_fields_only_bom_cpl_and_state(tmp_path):
    """Acceptance (a): the swap diff touches ONLY fields/BOM/CPL. Geometry
    stability is asserted through the T7 normalizer (sexpr_no_uuid): the
    normalized board delta is exactly the swapped property lines. BOM
    regeneration moves only the swapped group; CPL is byte-identical. The
    edit lands in state.json as swap_part_same_fp."""
    ws = tmp_path / "ws"
    (ws / "kicad").mkdir(parents=True)
    pcb = _stage(ws / "kicad", PD_PCB)
    orig = tmp_path / "orig.kicad_pcb"
    shutil.copy2(pcb, orig)
    tree = _load_net(PD_NET)
    _set_value(tree, "C5", "2.2uF 25V X5R")
    _set_field(tree, "C5", "LCSC", "C23630")
    net = _dump_net(tree, tmp_path / "m.net")

    subprocess.run([sys.executable, str(SCRIPTS / "state.py"), "init",
                    "--workspace", str(ws), "--board", "pd-trigger"],
                   check=True, capture_output=True)
    payload = _run(pcb, net, "--state", str(ws / "state.json"))
    assert payload["status"] == "pass"
    assert payload["applied"]["worker"]["fields_updated"] == ["C5"]
    assert payload["applied"]["drc"]["after"]["total"] == USBC_OWN_HOLES

    # the apply refills zones, and this KiCad tessellates a fill a little
    # differently from the one that saved the fixture: fill vertices aside
    delta = [ln for ln in _normalized_delta(orig, pcb)
             if not ln[1:].lstrip().startswith("(xy ")]
    assert delta, "value change must appear"
    for line in delta:
        assert "(property" in line or "LCSC" in line, \
            f"non-field change leaked into the board: {line}"
    assert _fill_change_mm2(orig, pcb) < 0.1

    # copper + placement inventories identical
    a, b = geom.BoardGeom.from_file(orig), geom.BoardGeom.from_file(pcb)
    assert bu._copper_inventory(a) == bu._copper_inventory(b)
    ma, mb = placelib.PlaceModel(orig), placelib.PlaceModel(pcb)
    assert {r: f.pos for r, f in ma.footprints.items()} \
        == {r: f.pos for r, f in mb.footprints.items()}

    # BOM/CPL: only the swapped group moves
    def fab(board, out):
        out.mkdir()
        subprocess.run([sys.executable, str(SCRIPTS / "bom_cpl.py"),
                        "--pcb", str(board), "--out-dir", str(out)],
                       check=True, capture_output=True)
        return ((out / "BOM.csv").read_text().splitlines(),
                (out / "CPL.csv").read_text())

    bom_a, cpl_a = fab(orig, tmp_path / "fab_a")
    bom_b, cpl_b = fab(pcb, tmp_path / "fab_b")
    assert cpl_a == cpl_b
    changed = set(bom_a).symmetric_difference(bom_b)
    assert changed and all("C5" in row or "2.2uF" in row or "1uF" in row
                           for row in changed)

    st = json.loads((ws / "state.json").read_text())
    assert [(e["class"], e["refs"]) for e in st["edits"]] \
        == [("swap_part_same_fp", ["C5"])]
    assert payload["state"]["human_hold"] == 2
    # the re-run set comes from the invalidation map, not prose
    imap = statelib.load_map()
    assert payload["gates_to_rerun"] \
        == sorted(imap["edit_classes"]["swap_part_same_fp"]["gates"])


@pytest.mark.kicad_recorded
@pytest.mark.smoke
def test_del_apply_rips_orphans_and_silk(tmp_path):
    """Acceptance (c): delete leaves no orphan stubs (connectivity-checked
    AND KiCad-checked: DRC total 0 after) and no silk residue (a functional
    silk label inside C2's bbox goes with it). Pre-existing unanchored
    copper (the VBUS fan-in lobes) is preserved."""
    pcb = _stage(tmp_path, PD_PCB)
    import place_edit
    place_edit.apply_ops(pcb, [{"op": "add_text", "text": "IN CAP",
                                "x": 23.6, "y": 47.2, "layer": "F.SilkS"}])
    tree = _load_net(PD_NET)
    _del_ref(tree, "C2")
    payload = _run(pcb, _dump_net(tree, tmp_path / "m.net"))
    assert payload["status"] == "pass"
    assert payload["applied"]["worker"]["removed_refs"] == ["C2"]
    assert payload["applied"]["worker"]["removed_items"] == 2  # stub + via
    texts = payload["applied"]["worker"]["removed_texts"]
    assert [t["text"] for t in texts] == ["IN CAP"]
    # worker must report the FILE-token layer name or the driver's
    # silk-residue verify is vacuous (review finding: GetLayerName says
    # "F.Silkscreen" while sexpr carries "F.SilkS")
    assert texts[0]["layer"] == "F.SilkS"
    assert payload["applied"]["drc"]["after"]["total"] == USBC_OWN_HOLES
    assert payload["applied"]["drc"]["dangling_after"] == 0
    assert payload["applied"]["refilled"] is True

    b = geom.BoardGeom.from_file(pcb)
    assert not [p for p in b.pads_of() if p.ref == "C2"]
    # the two pre-existing fan-in lobes survive (by geometry - uuids churn)
    keys = {bu._item_key_track(t) for t in b.tracks_of("VBUS")}
    a = geom.BoardGeom.from_file(PD_PCB)
    for uu in ("01de587b", "e0b8be12"):
        t = next(t for t in a.tracks_of("VBUS") if t.uuid.startswith(uu))
        assert bu._item_key_track(t) in keys
    assert "IN CAP" not in pcb.read_text(encoding="utf-8")


@pytest.mark.kicad_recorded
@pytest.mark.smoke
def test_replace_c2_reaches_drc_zero(tmp_path):
    """Acceptance (b): one invocation deletes C2 and adds C99 at the freed
    spot; existing copper is preserved; the single open ratsnest leg closes
    through the STANDARD fix loop (route_edit via + kicad-cli refill) and
    the board reaches DRC 0 errors / 0 warnings."""
    pcb = _stage(tmp_path, PD_PCB)
    tree = _load_net(PD_NET)
    _del_ref(tree, "C2")
    _add_comp(tree, "C99", "100nF 50V X7R", "aiee:C0603",
              {"1": "VBUS", "2": "GND"})
    net = _dump_net(tree, tmp_path / "m.net")
    pl = tmp_path / "p.json"
    pl.write_text(json.dumps({"C99": {"x": 23.6, "y": 47.2}}))

    before = geom.BoardGeom.from_file(pcb)
    payload = _run(pcb, net, "--placements", str(pl), "--lib", str(PD_LIB))
    assert payload["status"] == "pass"
    assert payload["applied"]["drc"]["new_errors"] == 0
    assert payload["applied"]["drc"]["unconnected_after"] == 1  # GND leg
    assert payload["applied"]["drc"]["dangling_after"] == 0

    # existing copper preserved minus exactly the C2 orphans
    after = geom.BoardGeom.from_file(pcb)
    inv_before = bu._copper_inventory(before)
    for t in payload_orphans_tracks(before):
        inv_before[t] -= 1
    assert +inv_before == +bu._copper_inventory(after)

    # standard fix loop: GND via at C99.2, refill, gate
    import route_edit
    route_edit.apply_ops(pcb, [{"op": "add_via", "at": [24.3, 47.2],
                                "size": 0.6, "drill": 0.3, "net": "GND"}])
    import env as env_mod
    import kc
    rep = kc.run_drc(env_mod.find_kicad_cli(), pcb, refill=True,
                     save_board=True)
    # no warnings, and J1's own holes are waived (#813)
    assert rep["violations"] == []
    assert rep["counts"]["total"] == USBC_OWN_HOLES


def payload_orphans_tracks(before_bg) -> list[tuple]:
    orph = bu.plan_orphans(before_bg, ["C2"])
    return [bu._item_key_track(t) for t in orph["tracks"]] + \
           [bu._item_key_via(v) for v in orph["vias"]]


@pytest.mark.smoke
def test_add_region_scan_preserves_copper(tmp_path):
    """Acceptance (b), region form: the scan finds a courtyard-legal,
    clearance-clear spot inside the declared region; every pre-existing
    track/via survives byte-for-byte; the only DRC delta is the new part's
    ratsnest."""
    pcb = _stage(tmp_path, PD_PCB)
    tree = _load_net(PD_NET)
    _add_comp(tree, "C99", "100nF 50V X7R", "aiee:C0603",
              {"1": "VBUS", "2": "GND"})
    net = _dump_net(tree, tmp_path / "m.net")
    pl = tmp_path / "p.json"
    pl.write_text(json.dumps({"C99": {"region": [40.0, 40.0, 50.0, 50.0]}}))

    before_inv = bu._copper_inventory(geom.BoardGeom.from_file(pcb))
    payload = _run(pcb, net, "--placements", str(pl), "--lib", str(PD_LIB))
    got = payload["placements"]["C99"]
    assert 40.0 <= got["x"] <= 50.0 and 40.0 <= got["y"] <= 50.0
    assert payload["applied"]["drc"]["new_errors"] == 0
    assert payload["applied"]["drc"]["unconnected_after"] == 2
    assert payload["applied"]["drc"]["dangling_after"] == 0

    after = geom.BoardGeom.from_file(pcb)
    assert bu._copper_inventory(after) == before_inv  # nothing touched
    fp = placelib.PlaceModel(pcb).footprints["C99"]
    assert {p.number: p.net for p in fp.pads} == {"1": "VBUS", "2": "GND"}


@pytest.mark.smoke
def test_carrier_del_and_add_4layer(tmp_path):
    """Carrier mutants: deleting the plane-fed C62 rips exactly its two
    feed vias on a 4-layer board with pours; a same-invocation region add
    (C99 on +3V3/GND) lands legally; no new dangling, planes intact."""
    pcb = _stage(tmp_path, CA_PCB)
    tree = _load_net(CA_NET)
    _del_ref(tree, "C62")
    _add_comp(tree, "C99", "100nF 50V X7R", "aiee:C0603",
              {"1": "+3V3", "2": "GND"})
    net = _dump_net(tree, tmp_path / "m.net")
    pl = tmp_path / "p.json"
    pl.write_text(json.dumps({"C99": {"region": [60.0, 60.0, 75.0, 75.0]}}))

    payload = _run(pcb, net, "--placements", str(pl), "--lib", str(PD_LIB))
    assert payload["status"] == "pass"
    assert payload["applied"]["worker"]["removed_refs"] == ["C62"]
    assert payload["applied"]["worker"]["removed_items"] == 2  # the 2 vias
    assert payload["applied"]["drc"]["dangling_after"] \
        <= payload["applied"]["drc"]["dangling_before"]
    assert payload["applied"]["drc"]["new_errors"] == 0
    assert payload["applied"]["refilled"] is True

    b = geom.BoardGeom.from_file(pcb)
    assert not [p for p in b.pads_of() if p.ref == "C62"]
    assert placelib.PlaceModel(pcb).footprints["C99"].side == "front"
    # planes still filled after the refill
    assert any(z.fills for z in b.zones_of("GND"))


# ---------------------------------------------------------------------------
# smoke: atomicity
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_rewire_apply_on_unrouted_board(tmp_path):
    """Issue #29 end-to-end: the unrouted board's pad is re-netted, every
    other pad and all geometry stay, and state records rewire_pad."""
    ws = tmp_path / "ws"
    (ws / "kicad").mkdir(parents=True)
    pcb = _stage(ws / "kicad", PD_PLACE)
    orig = tmp_path / "orig.kicad_pcb"
    shutil.copy2(pcb, orig)
    tree = _load_net(PD_NET)
    _rewire(tree, "C5", "1", "GND")
    net = _dump_net(tree, tmp_path / "m.net")
    subprocess.run([sys.executable, str(SCRIPTS / "state.py"), "init",
                    "--workspace", str(ws), "--board", "pd-trigger"],
                   check=True, capture_output=True)
    payload = _run(pcb, net, "--state", str(ws / "state.json"))
    assert payload["status"] == "pass"
    assert payload["applied"]["worker"]["pads_renetted"] == ["C5.1"]
    after = {(p.ref, p.number): p.net
             for p in geom.load_board(pcb, refresh=True).pads_of()}
    before = {(p.ref, p.number): p.net
              for p in geom.load_board(orig, refresh=True).pads_of()}
    assert after[("C5", "1")] == "GND"
    assert {k: v for k, v in after.items() if k != ("C5", "1")} \
        == {k: v for k, v in before.items() if k != ("C5", "1")}
    ma, mb = placelib.PlaceModel(orig), placelib.PlaceModel(pcb)
    assert {r: f.pos for r, f in ma.footprints.items()} \
        == {r: f.pos for r, f in mb.footprints.items()}
    st = json.loads((ws / "state.json").read_text())
    assert [(e["class"], e["refs"]) for e in st["edits"]] \
        == [("rewire_pad", ["C5"])]
    # re-running is a no-op: the board now matches the netlist
    assert _run(pcb, net, "--dry-run")["changes"] == 0


@pytest.mark.smoke
def test_new_fp_swap_apply(tmp_path):
    """swap_part_new_fp applies as del+add of the SAME ref in one worker
    run (the duplicate-ref guard must tolerate a ref that is also being
    removed, and removal must rip the ORIGINAL object, not the
    replacement). C5 C0603 -> C1206 at the old spot: replacement present
    with the new fpid, pads netted, old orphan stubs gone, no new
    dangling."""
    pcb = _stage(tmp_path, PD_PCB)
    tree = _load_net(PD_NET)
    _set_footprint(tree, "C5", "aiee:C1206")
    payload = _run(pcb, _dump_net(tree, tmp_path / "m.net"),
                   "--lib", str(PD_LIB))
    assert payload["status"] == "pass"
    assert payload["applied"]["worker"]["removed_refs"] == ["C5"]
    assert [a["ref"] for a in payload["applied"]["worker"]["added"]] \
        == ["C5"]
    assert payload["applied"]["drc"]["dangling_after"] \
        <= payload["applied"]["drc"]["dangling_before"]
    fp = placelib.PlaceModel(pcb).footprints["C5"]
    assert fp.fpid == "aiee:C1206"
    nets = {p.number: p.net for p in fp.pads if p.number}
    assert set(nets) == {"1", "2"} and all(nets.values())


@pytest.mark.smoke
def test_dangling_gate_rolls_back(tmp_path, monkeypatch):
    """Wiring test for the hard DRC-dangling gate: if the staged board
    reports more dangling than the original, the update must roll back
    byte-identically. (The counts are monkeypatched - the organic
    scenario needs a board KiCad itself half-flags; see LEARNINGS
    [connectivity] 2026-08-07.)"""
    pcb = _stage(tmp_path, PD_PCB)
    before = _sha(pcb)
    tree = _load_net(PD_NET)
    _del_ref(tree, "C2")
    net = _dump_net(tree, tmp_path / "m.net")
    counts = iter([0, 1])  # before -> 0, after -> 1
    monkeypatch.setattr(bu, "_dangling_count", lambda rep: next(counts))
    with pytest.raises(CheckError, match="NEW dangling"):
        _run(pcb, net)
    assert _sha(pcb) == before


CRUMB = ('  (segment (start 24.3 47.2) (end 24.3 47.2) (width 0.25) '
         '(layer "F.Cu") (net "GND") '
         '(uuid "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeee01"))\n')


@pytest.mark.kicad_recorded
@pytest.mark.smoke
def test_zero_length_crumb_inside_deleted_pad_is_removed(tmp_path):
    """Zero-length editing residue under a deleted pad is invisible to
    netconn (no edge) but must still be ripped, or the del rolls back
    forever on the crumb's track_dangling (adversarial-review probe,
    machine-verified). Judged by direct copper touch against dead pads."""
    pcb = _stage(tmp_path, PD_PCB)
    text = pcb.read_text(encoding="utf-8")
    assert text.rstrip().endswith(")")
    i = text.rindex(")")
    pcb.write_text(text[:i] + CRUMB + text[i:], encoding="utf-8")
    bg = geom.load_board(pcb, refresh=True)
    assert any(t.length == 0 for t in bg.tracks_of("GND"))

    tree = _load_net(PD_NET)
    _del_ref(tree, "C2")
    payload = _run(pcb, _dump_net(tree, tmp_path / "m.net"))
    assert payload["status"] == "pass"
    assert payload["applied"]["worker"]["removed_items"] == 3  # stub+via+crumb
    assert payload["applied"]["drc"]["after"]["total"] == USBC_OWN_HOLES
    after = geom.load_board(pcb, refresh=True)
    assert not any(t.length == 0 for t in after.tracks_of())


@pytest.mark.smoke
def test_rollback_on_worker_failure(tmp_path, monkeypatch):
    pcb = _stage(tmp_path, PD_PCB)
    before = _sha(pcb)
    tree = _load_net(PD_NET)
    _del_ref(tree, "C2")
    net = _dump_net(tree, tmp_path / "m.net")

    def boom(*a, **k):
        raise CheckError("synthetic worker failure")
    monkeypatch.setattr(bu.routelib, "run_worker", boom)
    with pytest.raises(CheckError, match="synthetic worker failure"):
        _run(pcb, net)
    assert _sha(pcb) == before


@pytest.mark.smoke
def test_rollback_on_verify_failure(tmp_path, monkeypatch):
    pcb = _stage(tmp_path, PD_PCB)
    before = _sha(pcb)
    tree = _load_net(PD_NET)
    _del_ref(tree, "C2")
    net = _dump_net(tree, tmp_path / "m.net")

    monkeypatch.setattr(bu, "verify_apply",
                        lambda *a, **k: ["synthetic verify problem"])
    with pytest.raises(CheckError, match="synthetic verify problem"):
        _run(pcb, net)
    assert _sha(pcb) == before


def test_noop_apply_short_circuits(tmp_path):
    """A no-diff apply must not stage, run workers, or touch the board -
    pure by construction (no toolchain in this test)."""
    pcb = _stage(tmp_path, PD_PCB)
    before = _sha(pcb)
    payload = _run(pcb, PD_NET)
    assert payload["status"] == "pass"
    assert "nothing to do" in payload["note"]
    assert _sha(pcb) == before


# ---------------------------------------------------------------------------
# copper-keeping footprint swap (U-ECO: swap_fp.py + rip_ep.py on PR #68)
# ---------------------------------------------------------------------------

def _variant_lib(tmp_path: Path, name: str, base: str, extra: str = "",
                 move: dict | None = None) -> Path:
    """PD_LIB copied, plus aiee:<name> = aiee:<base> with `extra` pad text
    and `move` {pad number: "(at ...)" replacement}."""
    lib = tmp_path / "lib"
    if not lib.exists():
        shutil.copytree(PD_LIB, lib)
    text = (lib / "aiee.pretty" / f"{base}.kicad_mod").read_text()
    for num, at in (move or {}).items():
        head = f"(pad {num} "
        i = text.index(head)
        j = text.index("(at ", i)
        k = text.index(")", j)
        text = text[:j] + at + text[k + 1:]
    text = text.rstrip()[:-1] + extra + "\n)\n"
    (lib / "aiee.pretty" / f"{name}.kicad_mod").write_text(text)
    return lib


def _add_node(tree, ref: str, pin: str, net_name: str) -> None:
    for net in _nets_sec(tree)[1:]:
        if _head(net) == "net" and _kidval(net, "name") == net_name:
            net.append([S("node"), [S("ref"), ref], [S("pin"), pin],
                        [S("pintype"), "passive"]])
            return
    raise AssertionError(f"net {net_name} not in netlist")


def _clash_pad_local(bg, ref: str, net: str) -> tuple[float, float]:
    """Footprint-local spot (front part) of the nearest point of a `net`
    track to REF's origin: a new pad there lands on that track."""
    from shapely.geometry import Point
    from shapely.ops import nearest_points
    fp = placelib.PlaceModel(PD_PCB).footprints[ref]
    c = Point(fp.pos)
    t = min(bg.tracks_of(net=net), key=lambda t: t.shape.distance(c))
    q = nearest_points(t.shape, c)[0]
    return geom._rot(q.x - fp.pos[0], q.y - fp.pos[1], fp.angle)


def test_placed_pads_match_the_board():
    """placed_pads reproduces every pd-trigger part's pads from its library
    footprint at the board's own spot - what a swap is judged against."""
    bg = geom.BoardGeom.from_file(PD_PCB)
    model = placelib.PlaceModel(PD_PCB)
    for ref, fp in model.footprints.items():
        mod = bu.find_mod(fp.fpid, [PD_LIB])
        got = bu.placed_pads(bg, mod, ref, {"x": fp.pos[0], "y": fp.pos[1],
                                            "deg": fp.angle,
                                            "side": fp.side}, {})
        real = bg.pads_of(ref=ref)
        assert len(got) == len(real), ref
        for p in got:
            assert any(r.number == p.number and r.layers == p.layers
                       and p.poly.symmetric_difference(r.poly).area < 1e-5
                       for r in real), (ref, p.number)


@pytest.mark.smoke
def test_placed_pads_match_kicad_back_side(tmp_path):
    """The back-side flip convention against KiCad itself: pcbnew places
    library parts flipped TOP_BOTTOM at several angles (update_swig's
    set_side), and placed_pads must land on the same copper."""
    import env
    cli = env.find_kicad_cli()
    bp = env.find_kicad_python(cli) if cli else None
    if bp is None:
        pytest.skip("KiCad bundled python not installed")
    pcb = _stage(tmp_path, PD_PCB)
    mods = ["SOT-363-6_L2.2-W1.3-P0.65-LS2.1-BL", "USB-C-SMD_MC-311D",
            "HDR-TH_2P-P2.54-V-M-3", "ESSOP-10_L4.9-W3.9-P1.0-LS6.0-TL-EP"]
    cases = [{"ref": f"ZZ{i}", "mod": m, "x": 100.0 + 12 * i, "y": 100.0,
              "deg": d, "side": s}
             for i, (m, d, s) in enumerate(
                 (m, d, s) for m in mods
                 for d, s in ((30, "front"), (0, "back"), (30, "back"),
                              (-90, "back")))]
    script = tmp_path / "place.py"
    script.write_text(
        "import json, sys, pcbnew\n"
        "b = pcbnew.LoadBoard(sys.argv[1])\n"
        "flip = getattr(pcbnew, 'FLIP_DIRECTION_TOP_BOTTOM', True)\n"
        "for c in json.loads(sys.argv[3]):\n"
        "    fp = pcbnew.FootprintLoad(sys.argv[2], c['mod'])\n"
        "    fp.SetReference(c['ref']); b.Add(fp)\n"
        "    fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(c['x']),"
        " pcbnew.FromMM(c['y'])))\n"
        "    if c['side'] == 'back': fp.Flip(fp.GetPosition(), flip)\n"
        "    fp.SetOrientationDegrees(c['deg'])\n"
        "b.Save(sys.argv[1])\n")
    subprocess.run([str(bp), str(script), str(pcb),
                    str(PD_LIB / "aiee.pretty"), json.dumps(cases)],
                   check=True, capture_output=True, timeout=300)
    bg = geom.BoardGeom.from_file(pcb)
    for c in cases:
        got = bu.placed_pads(bg, PD_LIB / "aiee.pretty" / f"{c['mod']}"
                             ".kicad_mod", c["ref"], c, {})
        real = bg.pads_of(ref=c["ref"])
        assert len(got) == len(real), c
        for p in got:
            assert any(r.number == p.number and r.layers == p.layers
                       and p.poly.symmetric_difference(r.poly).area < 1e-5
                       for r in real), (c, p.number)


def _swap_c5(tmp_path, extra="", move=None, pin3=None):
    lib = _variant_lib(tmp_path, "C0603_X", "C0603", extra, move)
    tree = _load_net(PD_NET)
    _set_footprint(tree, "C5", "aiee:C0603_X")
    if pin3:
        _add_node(tree, "C5", "3", pin3)
    return lib, _dump_net(tree, tmp_path / "m.net")


def test_swap_keeps_copper_decisions(tmp_path):
    bg = geom.BoardGeom.from_file(PD_PCB)
    spot = {"x": 28.3, "y": 31.2, "deg": 0.0, "side": "front"}
    nets = {"1": "/VDD", "2": "GND"}
    lib, _ = _swap_c5(tmp_path)
    same = bu.placed_pads(bg, lib / "aiee.pretty" / "C0603_X.kicad_mod",
                          "C5", spot, nets)
    assert bu.swap_keeps_copper(bg, "C5", same, nets) is None
    assert "moves from net" in bu.swap_keeps_copper(
        bg, "C5", same, {"1": "/VDD", "2": "/CFG2"})
    lib, _ = _swap_c5(tmp_path, move={"2": "(at 3.70 0.00 0.00)"})
    moved = bu.placed_pads(bg, lib / "aiee.pretty" / "C0603_X.kicad_mod",
                           "C5", spot, nets)
    assert "misses the new pad 2" in bu.swap_keeps_copper(
        bg, "C5", moved, nets)
    assert "has no such pad" in bu.swap_keeps_copper(
        bg, "C5", [p for p in same if p.number != "1"], nets)


def test_swap_dry_run_names_the_clash(tmp_path):
    """A new pad landing on another net's track: the swap keeps C5's
    routing and plans to rip that track (rip_ep.py's job)."""
    bg = geom.BoardGeom.from_file(PD_PCB)
    lx, ly = _clash_pad_local(bg, "C5", "/CFG2")
    lib, net = _swap_c5(tmp_path, extra=(
        f"\n\t(pad 3 smd rect (at {lx:.3f} {ly:.3f}) (size 0.3 0.3) "
        "(layers F.Cu F.Paste F.Mask))"), pin3="GND")
    pcb = _stage(tmp_path, PD_PCB)
    payload = _run(pcb, net, "--lib", str(lib), "--dry-run")
    entry = payload["plan"]["swap_new_fp"][0]
    assert entry["ref"] == "C5" and entry["keep_copper"] is True
    # the track it lands on, and any other net within its clearance
    assert "/CFG2" in {c["net"] for c in entry["clashes"]}
    assert not {c["net"] for c in entry["clashes"]} & {"GND", "/VDD"}
    # each says how close and why (#1146); only the new pad 3 is new copper
    for c in entry["clashes"]:
        assert {"clearance_mm", "gap_mm", "pad", "rule"} <= set(c), c
        assert c["gap_mm"] < c["clearance_mm"] and c["pad"] == "3", c
    assert min(c["gap_mm"] for c in entry["clashes"]
               if c["net"] == "/CFG2") == 0.0
    payload = _run(pcb, net, "--lib", str(lib), "--dry-run",
                   "--no-keep-copper")
    assert payload["plan"]["swap_new_fp"][0]["keep_copper"] is False
    assert any("--no-keep-copper" in n for n in payload["plan"]["notes"])


def _dru_board(tmp_path: Path, name: str, *rules) -> Path:
    """PD_PCB staged in tmp_path/name, its .kicad_dru given clearance rules
    (rule name, min mm, condition[, layer])."""
    (tmp_path / name).mkdir()
    pcb = _stage(tmp_path / name, PD_PCB)
    dru = pcb.with_suffix(".kicad_dru")
    dru.write_text(dru.read_text(encoding="utf-8") + "".join(
        f'(rule "{r[0]}"\n'
        + (f"\t(layer {r[3]})\n" if len(r) > 3 else "")
        + f"\t(constraint clearance (min {r[1]}mm))\n"
        f'\t(condition "{r[2]}")\n)\n' for r in rules), encoding="utf-8")
    return pcb


def _clash_nets(found) -> set:
    return {it.net for it, _ in found}


def test_swap_clash_weighs_footprint_rules_per_pair(tmp_path):
    """A .kicad_dru clearance rule held to a footprint counts only where it
    may hold (#1146): the voice board's package ratings (0.19 mm on J801's
    pads) ripped copper at every swap on the board. C5's nearest other-net
    copper is a /CFG2 track 0.19 mm off, clear of the 0.1524 mm floor."""
    bg = geom.BoardGeom.from_file(PD_PCB)
    pads = bg.pads_of(ref="C5")
    pcb = _dru_board(tmp_path, "far", (
        "d3_court", 0.5,
        "A.intersectsCourtyard('D3') && B.intersectsCourtyard('D3')"), (
        "j1_member", 0.5,
        "A.memberOfFootprint('J1') && B.memberOfFootprint('J1')"))
    assert bu.swap_clash_details(bg, "C5", pads, pcb) == []
    pcb = _dru_board(tmp_path, "r4", (
        "r4_court", 0.5,
        "A.intersectsCourtyard('R4') && B.intersectsCourtyard('R4')"))
    assert bu.swap_clash_details(bg, "C5", pads, pcb) == []
    # R4 swapped in the same update: its new courtyard is not known yet
    assert _clash_nets(bu.swap_clash_details(
        bg, "C5", pads, pcb, pending={"R4"})) == {"/CFG2"}
    pcb = _dru_board(tmp_path, "c5", (
        "c5_court", 0.5,
        "A.intersectsCourtyard('C5') || B.intersectsCourtyard('C5')"))
    found = bu.swap_clash_details(bg, "C5", pads, pcb)
    assert _clash_nets(found) == {"/CFG2"}
    assert all(info["clearance_mm"] == 0.5 and info["rule"]
               == "rule c5_court" and info["gap_mm"] < 0.5
               for _, info in found)
    # the same rule held to B.Cu leaves C5's front pads alone
    pcb = _dru_board(tmp_path, "c5b", (
        "c5_back", 0.5,
        "A.intersectsCourtyard('C5') || B.intersectsCourtyard('C5')",
        "B.Cu"))
    assert bu.swap_clash_details(bg, "C5", pads, pcb) == []


def _class_board(tmp_path: Path, name: str, **ns) -> Path:
    """PD_PCB staged in tmp_path/name, its .kicad_pro given an HV class
    (0.5 mm) and the net_settings keys in ns."""
    (tmp_path / name).mkdir()
    pcb = _stage(tmp_path / name, PD_PCB)
    pro = pcb.with_suffix(".kicad_pro")
    proj = json.loads(pro.read_text(encoding="utf-8"))
    proj["net_settings"]["classes"].append(
        {"name": "HV", "clearance": 0.5, "priority": 2})
    proj["net_settings"].update(ns)
    pro.write_text(json.dumps(proj), encoding="utf-8")
    return pcb


def test_swap_clash_reads_netclass_assignments(tmp_path):
    """#1274: a net the board's netclass_assignments puts in a class keeps
    that class's clearance at a swap: C5's /CFG2 track, 0.19 mm off, clashes
    once /CFG2 is assigned to a 0.5 mm class. Net settings that can't be
    read hold every net to the largest clearance a class sets, never to
    the floor ("no clearances")."""
    bg = geom.BoardGeom.from_file(PD_PCB)
    pads = bg.pads_of(ref="C5")
    pcb = _class_board(tmp_path, "none")
    assert bu.swap_clash_details(bg, "C5", pads, pcb) == []
    for name, ns in (("str", {"netclass_assignments": {"/CFG2": "HV"}}),
                     ("list", {"netclass_assignments": {"/CFG2": ["HV"]}})):
        found = bu.swap_clash_details(
            bg, "C5", pads, _class_board(tmp_path, name, **ns))
        assert _clash_nets(found) == {"/CFG2"}, name
        assert all(info["clearance_mm"] == 0.5 for _, info in found)
    pcb = _class_board(tmp_path, "unread", netclass_patterns=[
        {"netclass": "Power", "pattern": "VBUS"}, "/CFG2 -> HV"])
    found = bu.swap_clash_details(bg, "C5", pads, pcb)
    assert "/CFG2" in _clash_nets(found)
    assert all(info["clearance_mm"] == 0.5 for _, info in found
               if info["gap_mm"] > 0.1524)


def test_swap_clash_leaves_copper_the_old_pad_held(tmp_path):
    """Copper the board already held as close to the old pad is not the
    swap's to rip (#1146); a new pad's own clash still is."""
    from shapely.geometry import Point
    from shapely.ops import nearest_points
    bg = geom.BoardGeom.from_file(PD_PCB)
    old = bg.pads_of(ref="C5")
    pcb = _dru_board(tmp_path, "m", (
        "c5_member", 0.5,
        "A.memberOfFootprint('C5') || B.memberOfFootprint('C5')"))
    assert _clash_nets(bu.swap_clash_details(bg, "C5", old, pcb)) \
        == {"/CFG2"}
    assert bu.swap_clash_details(bg, "C5", old, pcb, old_pads=old) == []
    smaller = [dataclasses.replace(p, size=(p.size[0] - 0.2,
                                            p.size[1] - 0.2)) for p in old]
    assert bu.swap_clash_details(bg, "C5", smaller, pcb,
                                 old_pads=old) == []
    # a third (GND) pad on the nearest /CFG2 track
    c = Point(placelib.PlaceModel(PD_PCB).footprints["C5"].pos)
    t = min(bg.tracks_of(net="/CFG2"), key=lambda t: t.shape.distance(c))
    q = nearest_points(t.shape, c)[0]
    pad3 = dataclasses.replace(old[0], number="3", net="GND", shape="rect",
                               size=(0.3, 0.3), center=(q.x, q.y),
                               angle=0.0, rratio=0.0)
    found = bu.swap_clash_details(bg, "C5", old + [pad3], pcb, old_pads=old)
    assert found and {info["pad"] for _, info in found} == {"3"}
    assert min(info["gap_mm"] for it, info in found
               if it.net == "/CFG2") == 0.0
    # swap_clashes: the same items, without the detail
    assert bu.swap_clashes(bg, "C5", old + [pad3], pcb, old_pads=old) \
        == [it for it, _ in found]


def _c5_spot() -> dict:
    fp = placelib.PlaceModel(PD_PCB).footprints["C5"]
    return {"x": fp.pos[0], "y": fp.pos[1], "deg": fp.angle, "side": fp.side}


def _c0603_court(tmp_path: Path, name: str, grow: bool = False,
                 extra: str = "") -> Path:
    """The .kicad_mod of aiee:<name>: C0603 with `extra` text, its F.CrtYd
    outline (+-0.8 x +-0.4 mm) grown to +-1.5 x +-0.75 mm when grow."""
    lib = _variant_lib(tmp_path, name, "C0603", extra)
    mod = lib / "aiee.pretty" / f"{name}.kicad_mod"
    if grow:
        mod.write_text("".join(
            ln.replace("0.80", "1.50").replace("0.40", "0.75")
            if "F.CrtYd" in ln else ln
            for ln in mod.read_text().splitlines(True)))
    return mod


def _dru_text_board(tmp_path: Path, name: str, text: str) -> Path:
    """PD_PCB staged in tmp_path/name, `text` added to its .kicad_dru."""
    (tmp_path / name).mkdir()
    pcb = _stage(tmp_path / name, PD_PCB)
    dru = pcb.with_suffix(".kicad_dru")
    dru.write_text(dru.read_text(encoding="utf-8") + text, encoding="utf-8")
    return pcb


def _grown(pads: list) -> list:
    """C5's pads at 0.83 x 0.93 mm: pad 2 comes 0.1683 mm from the /CFG2
    track the 0.8 x 0.9 mm pad clears by 0.1895 mm."""
    return [dataclasses.replace(p, size=(0.83, 0.93)) for p in pads]


def test_swap_clash_reads_the_new_footprint(tmp_path):
    """A footprint test reads the swapped part as the update leaves it
    (#1163): its new library id and courtyard. The same pads in a library
    a 0.5 mm rule names, or under a courtyard grown over the /CFG2 tracks,
    need copper the old pad never did; a courtyard that cannot be read may
    hold. KiCad's DRC agrees on each board (two /CFG2 tracks, 0.233 and
    0.1895 mm off)."""
    bg = geom.BoardGeom.from_file(PD_PCB)
    old = bg.pads_of(ref="C5")
    spot = _c5_spot()
    c0603 = PD_LIB / "aiee.pretty" / "C0603.kicad_mod"

    def clash(pcb, fpid, mod):
        return bu.swap_clash_details(
            bg, "C5", old, pcb, old_pads=old,
            new_fps={"C5": bu._placed_footprint("C5", fpid, mod, spot)})

    pcb = _dru_board(tmp_path, "hv", (
        "hv_member", 0.5, "A.memberOfFootprint('HV:*')"))
    found = clash(pcb, "HV:C0603", c0603)
    assert len(found) == 2 and _clash_nets(found) == {"/CFG2"}
    assert {info["rule"] for _, info in found} == {"rule hv_member"}
    assert clash(pcb, "aiee:C0603", c0603) == []
    pcb = _dru_board(tmp_path, "court", (
        "c5_court", 0.5,
        "A.intersectsCourtyard('C5') && B.intersectsCourtyard('C5') "
        "&& A.Type != 'Zone' && B.Type != 'Zone'"))
    assert clash(pcb, "aiee:C0603", c0603) == []
    grown = _c0603_court(tmp_path, "C0603_G", grow=True)
    found = clash(pcb, "aiee:C0603_G", grown)
    assert len(found) == 2 and _clash_nets(found) == {"/CFG2"}
    # a courtyard line that does not close into the outline
    odd = _c0603_court(tmp_path, "C0603_O", extra=(
        "\n\t(fp_line (start 0.80 0.40) (end 1.20 0.80) (layer F.CrtYd) "
        "(width 0.05))"))
    assert _clash_nets(clash(pcb, "aiee:C0603_O", odd)) == {"/CFG2"}


def test_swap_clash_reads_far_side_courtyards(tmp_path):
    """A courtyard test reads both sides' courtyards, as KiCad does
    (#1163): R4, a front part, carries a back courtyard over the /CFG2
    track by C5's pad 2, so a 0.18 mm rule held to R4's courtyard holds
    there, and not once the update deletes R4."""
    pcb = _dru_board(tmp_path, "far", (
        "r4_court", 0.18, "A.intersectsCourtyard('R4')"))
    bg = geom.BoardGeom.from_file(pcb)
    old = bg.pads_of(ref="C5")
    assert bu.swap_clash_details(bg, "C5", _grown(old), pcb,
                                 old_pads=old) == []
    s = pcb.read_text(encoding="utf-8")
    j = s.index("\n\t)\n", s.index('"Reference" "R4"')) + 1
    pcb.write_text(s[:j] + (
        '\t\t(fp_rect (start -3.2 -2.1) (end -2.6 -1.4) (stroke (width '
        '0.05) (type solid)) (fill no) (layer "B.CrtYd"))\n') + s[j:],
        encoding="utf-8")
    found = bu.swap_clash_details(bg, "C5", _grown(old), pcb, old_pads=old)
    assert [(it.net, info["pad"], info["clearance_mm"], info["gap_mm"],
             info["rule"]) for it, info in found] \
        == [("/CFG2", "2", 0.18, 0.1683, "rule r4_court")]
    assert bu.swap_clash_details(bg, "C5", _grown(old), pcb, old_pads=old,
                                 gone={"R4"}) == []


def test_swap_clash_reads_one_line_rules(tmp_path):
    """A rule written on one line is read as itself (#1163): one with no
    condition raises the board floor, and one held to J1 neither lends its
    condition to the rule after it nor sets the floor."""
    bg = geom.BoardGeom.from_file(PD_PCB)
    old = bg.pads_of(ref="C5")
    for name, text in (
            ("floor_first",
             '(rule "oneline_floor" (constraint clearance (min 0.18mm)))\n'
             '(rule "j1_keepout"\n\t(constraint clearance (min 0.16mm))\n'
             "\t(condition \"A.memberOfFootprint('J1')\")\n)\n"),
            ("j1_first",
             "(rule \"j1_hv\" (condition \"A.memberOfFootprint('J1')\") "
             "(constraint clearance (min 0.5mm)))\n"
             '(rule "board_floor_018"\n'
             "\t(constraint clearance (min 0.18mm))\n)\n")):
        pcb = _dru_text_board(tmp_path, name, text)
        assert bu.swap_clash_details(bg, "C5", old, pcb, old_pads=old) == []
        found = bu.swap_clash_details(bg, "C5", _grown(old), pcb,
                                      old_pads=old)
        assert [(it.net, info["pad"], info["clearance_mm"], info["rule"])
                for it, info in found] \
            == [("/CFG2", "2", 0.18, "board floor")], name


def test_swap_clash_parts_without_a_known_footprint(tmp_path):
    """A courtyard test of a part the update adds or swaps with no footprint
    given, or of a reference two footprints carry, cannot be answered and
    counts as holding (#1163); one given its footprint is read."""
    bg = geom.BoardGeom.from_file(PD_PCB)
    old = bg.pads_of(ref="C5")
    r4 = placelib.PlaceModel(PD_PCB).footprints["R4"]
    pcb = _dru_board(tmp_path, "two", (
        "r4_court", 0.5,
        "A.intersectsCourtyard('R4') && B.intersectsCourtyard('R4')"), (
        "d3_court", 0.5,
        "A.intersectsCourtyard('D3') && B.intersectsCourtyard('D3')"))
    assert bu.swap_clash_details(bg, "C5", old, pcb, pending={"R4"},
                                 new_fps={"R4": r4}) == []
    found = bu.swap_clash_details(bg, "C5", old, pcb, pending={"R4", "D3"},
                                  new_fps={"R4": r4})
    assert _clash_nets(found) == {"/CFG2"}
    assert {info["rule"] for _, info in found} == {"rule d3_court"}
    # R3 renamed R4: KiCad's test reads both footprints
    s = pcb.read_text(encoding="utf-8")
    assert s.count('"Reference" "R3"') == 1
    pcb.write_text(s.replace('"Reference" "R3"', '"Reference" "R4"'),
                   encoding="utf-8")
    found = bu.swap_clash_details(bg, "C5", old, pcb)
    assert _clash_nets(found) == {"/CFG2"}
    assert {info["rule"] for _, info in found} == {"rule r4_court"}


def test_swap_clash_loads_footprints_only_for_a_rule_that_reads_them(
        tmp_path):
    """model and new_fps may be given as functions, each called once and
    only when a rule's footprint test first needs a part (#1163): with no
    such rule, part_repair loads no board model."""
    bg = geom.BoardGeom.from_file(PD_PCB)
    old = bg.pads_of(ref="C5")
    r4 = placelib.PlaceModel(PD_PCB).footprints["R4"]
    calls: list = []

    def model():
        calls.append("model")
        return placelib.PlaceModel(PD_PCB)

    def new_fps():
        calls.append("new_fps")
        return {"R4": r4}

    kw = {"model": model, "pending": {"R4", "D3"}, "new_fps": new_fps}
    assert bu.swap_clash_details(bg, "C5", old, PD_PCB, **kw) == []
    assert calls == []
    pcb = _dru_board(tmp_path, "two", (
        "r4_court", 0.5,
        "A.intersectsCourtyard('R4') && B.intersectsCourtyard('R4')"), (
        "d3_court", 0.5,
        "A.intersectsCourtyard('D3') && B.intersectsCourtyard('D3')"))
    found = bu.swap_clash_details(bg, "C5", old, pcb, **kw)
    assert sorted(calls) == ["model", "new_fps"]
    # R4 read from new_fps: only D3, with no footprint, may hold
    assert {info["rule"] for _, info in found} == {"rule d3_court"}


def test_swap_clash_reads_net_tests_per_pair(tmp_path):
    """A NetName test is read per pair as KiCad reads it (#1163): case
    aside, and inside an || with a footprint test. A NetClass rule raises
    the floor; copper the old pad held as close stays, and is reported in
    kept."""
    bg = geom.BoardGeom.from_file(PD_PCB)
    old = bg.pads_of(ref="C5")
    for name, cond in (("or", "A.NetName == 'VBUS' || "
                              "A.memberOfFootprint('C5')"),
                       ("case", "A.NetName == '/cfg2'")):
        pcb = _dru_board(tmp_path, name, (name, 0.5, cond))
        found = bu.swap_clash_details(bg, "C5", old, pcb)
        assert _clash_nets(found) == {"/CFG2"}, name
        assert {info["rule"] for _, info in found} == {f"rule {name}"}, name
    pcb = _dru_board(tmp_path, "class", (
        "default_class", 0.5, "A.NetClass == 'Default'"))
    kept: list = []
    assert bu.swap_clash_details(bg, "C5", old, pcb, old_pads=old,
                                 kept=kept) == []
    assert _clash_nets(kept) == {"/CFG2"}
    assert {(info["rule"], info["clearance_mm"]) for _, info in kept} \
        == {("board floor", 0.5)}
    assert _clash_nets(bu.swap_clash_details(bg, "C5", old, pcb)) \
        == {"/CFG2"}


def test_swap_clash_old_pad_on_an_open_net(tmp_path):
    """An open pad's net name churns (_nets_equal): the old pad on its
    unconnected-* net and the new one with no net are one net, so copper
    the old pad held as close stays (#1163), unless a rule that reads the
    name answers otherwise for the two."""
    bg = geom.BoardGeom.from_file(PD_PCB)
    pads = bg.pads_of(ref="C5")
    old = [dataclasses.replace(p, net="unconnected-(C5-Pad1)")
           if p.number == "1" else p for p in pads]
    new = [dataclasses.replace(p, net=None) if p.number == "1" else p
           for p in pads]
    kept: list = []
    assert bu.swap_clash_details(bg, "C5", new, PD_PCB, old_pads=old,
                                 kept=kept) == []
    assert _clash_nets(kept) == {"/VDD"}
    pcb = _dru_board(tmp_path, "open", (
        "open_c5", 0.5, "A.NetName == 'unconnected-(C5-Pad1)'"))
    found = bu.swap_clash_details(bg, "C5", new, pcb, old_pads=old)
    assert _clash_nets(found) == {"/VDD"}
    assert {info["rule"] for _, info in found} == {"board floor"}


def test_swap_dry_run_notes_kept_copper(tmp_path):
    """The dry run says which other-net copper a kept swap leaves closer
    than the clearance because the old pad already came as close."""
    lib, net = _swap_c5(tmp_path)
    pcb = _dru_board(tmp_path, "k", (
        "default_class", 0.5, "A.NetClass == 'Default'"))
    payload = _run(pcb, net, "--lib", str(lib), "--dry-run")
    entry = payload["plan"]["swap_new_fp"][0]
    assert entry["ref"] == "C5" and entry["keep_copper"] is True
    assert entry["clashes"] == []
    assert any(n.startswith("C5: kept 2 track(s)/via(s) of other nets "
                            "(/CFG2)") for n in payload["plan"]["notes"])


def test_rule_layer_ok():
    ok = bu._rule_layer_ok
    assert ok(None, "In2.Cu") and ok("outer", "B.Cu")
    assert not ok("outer", "In1.Cu")
    assert ok("inner", "In12.Cu") and not ok("inner", "F.Cu")
    assert ok("F.Cu", "F.Cu") and not ok("F.Cu", "B.Cu")
    assert not ok("In1.Cu", "In2.Cu")
    # a name it does not read takes in every layer: the stricter reading
    assert ok("F.Silkscreen", "F.Cu") and ok("Edge.Cuts", "In1.Cu")


@pytest.mark.smoke
def test_pin_compatible_swap_keeps_routing_and_rips_clash(tmp_path):
    """C5 C0603 -> a C0603 with a third (GND) pad on top of a /CFG2 track:
    C5's own copper is kept to the byte, the /CFG2 track and the stub it
    alone held up go, no new dangling copper, and the swapped footprint
    keeps C5's schematic link and reference text."""
    bg = geom.BoardGeom.from_file(PD_PCB)
    lx, ly = _clash_pad_local(bg, "C5", "/CFG2")
    lib, net = _swap_c5(tmp_path, extra=(
        f"\n\t(pad 3 smd rect (at {lx:.3f} {ly:.3f}) (size 0.3 0.3) "
        "(layers F.Cu F.Paste F.Mask))"), pin3="GND")
    pcb = _stage(tmp_path, PD_PCB)
    payload = _run(pcb, net, "--lib", str(lib))
    assert payload["status"] == "pass"
    entry = payload["plan"]["swap_new_fp"][0]
    assert entry["keep_copper"] is True
    drc = payload["applied"]["drc"]
    assert drc["dangling_after"] <= drc["dangling_before"]
    after = geom.BoardGeom.from_file(pcb)
    keep = lambda b, n: sorted(bu._item_key_track(t)  # noqa: E731
                               for t in b.tracks_of(net=n))
    for n in ("/VDD", "GND"):  # C5's own nets: every track kept
        assert keep(after, n) == keep(bg, n)
    assert len(after.tracks_of(net="/CFG2")) < len(bg.tracks_of(net="/CFG2"))
    assert not [t for t in after.tracks_of(net="/CFG2")
                for p in after.pads_of(ref="C5") if p.number == "3"
                if t.poly.intersects(p.poly)]
    fp = placelib.PlaceModel(pcb).footprints["C5"]
    assert fp.fpid == "aiee:C0603_X"
    assert {p.number: p.net for p in fp.pads if p.number} \
        == {"1": "/VDD", "2": "GND", "3": "GND"}
    assert _ref_text(pcb, "C5") == _ref_text(PD_PCB, "C5")


def _ref_text(pcb: Path, ref: str):
    """(at ...) and hide flag of REF's Reference field."""
    for fp in sexpdata.loads(pcb.read_text())[1:]:
        if _head(fp) != "footprint":
            continue
        for k in fp[1:]:
            if _head(k) == "property" and k[1] == "Reference" \
                    and k[2] == ref:
                at = next(x for x in k[3:] if _head(x) == "at")
                return ([round(float(v), 3) for v in at[1:]],
                        "hide" in sexpdata.dumps(k))
    raise AssertionError(f"{ref} not on {pcb}")


@pytest.mark.smoke
def test_netlist_dnp_reaches_the_board(tmp_path):
    """A DNP set in the schematic (the netlist's dnp property) lands on the
    board's footprint, and clearing it there clears it again."""
    import place_edit
    pcb = _stage(tmp_path, PD_PCB)
    tree = _load_net(PD_NET)
    for c in _comps_sec(tree)[1:]:
        if _head(c) == "comp" and _kidval(c, "ref") == "R9":
            c.append([S("property"), [S("name"), "dnp"]])
    net = _dump_net(tree, tmp_path / "dnp.net")
    plan = _run(pcb, net, "--dry-run")["plan"]
    assert [(e["ref"], e["dnp"]) for e in plan["swap_same_fp"]] \
        == [("R9", [False, True])]
    assert _run(pcb, net)["status"] == "pass"
    assert "R9" in place_edit._dnp_refs(pcb)
    assert _run(pcb, net, "--dry-run")["changes"] == 0
    _run(pcb, PD_NET)
    assert "R9" not in place_edit._dnp_refs(pcb)
