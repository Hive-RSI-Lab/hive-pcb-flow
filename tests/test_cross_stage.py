"""U9 - cross-stage rails: P3 layout-implication screens + budgeted backward
spawns (hwde-v3-plan.md U9, design decision 4).

Known answers (LEARNINGS 2026-08-09): the SO-8EP (AP64350) exposed pad holds
at most 12 thermal vias and no 4x4; a DB128L terminal's wire entry is local
+Y, so the left edge wants rot 270 and the right edge rot 90. The sbuck
fixture is that board's P3 state (min_vias 16, J1 rot 0 / J2 rot 180).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import layoutimpl as li  # noqa: E402
import state as state_mod  # noqa: E402
import statelib  # noqa: E402
from checklib import CheckError  # noqa: E402

SBUCK = REPO / "tests" / "fixtures" / "u9_sbuck"
AP64350 = SBUCK / "parts" / "C2071691.json"
PASS = {"status": "pass", "failing_count": 0, "counts": {"total": 0}}
IMAP = statelib.load_map()


def _ws(tmp_path: Path) -> Path:
    ws = tmp_path / "sbuck"
    shutil.copytree(SBUCK, ws)
    return ws


def _cli(script: str, *args: str) -> tuple[int, dict]:
    p = subprocess.run([sys.executable, str(SCRIPTS / script), *args],
                       capture_output=True, text=True)
    return p.returncode, json.loads(p.stdout)


# ---- P3 screens --------------------------------------------------------------

def test_so8ep_via_capacity_known_answer():
    cap = li.fab_row("2layer_1oz")
    tv = li.via_capacity(3.502, 2.613, cap)
    assert (tv["nx"], tv["ny"], tv["max_vias"]) == (4, 3, 12)
    assert tv["pitch_floor_mm"] == 0.8          # 0.3 drill + 0.5 hole-to-hole
    assert tv["square_4x4_fits"] is False
    assert tv["max_square"] == "3x3"
    # a square pad that is big enough does hold 4x4 - the screen is not
    # a constant "no"
    assert li.via_capacity(3.502, 3.502, cap)["square_4x4_fits"] is True


def test_db128l_wire_entry_rotations():
    assert li.wire_entry("DB128L-5.08-2P-GN-S", None) == "+Y"
    assert li.wire_entry("KF128-2.54-3P", None) == "+Y"
    assert li.wire_entry("AP64350SP-13", None) is None
    assert li.edge_rotations("+Y") == {"left": 270, "right": 90,
                                       "top": 180, "bottom": 0}
    # the extraction's own field outranks the family table
    ext = {"land_pattern": {"pad_count": 2, "wire_entry_local": "-X"}}
    assert li.wire_entry("DB128L-5.08-2P-GN-S", ext) == "-X"
    assert li.edge_rotations("-X")["left"] == 0


def test_part_refs_from_role():
    assert li.part_refs({"role": "C5-C8, input ceramic bank"}) \
        == ["C5", "C6", "C7", "C8"]
    assert li.part_refs({"role": "J1 (LEFT edge, DC in) and J2 (RIGHT)"}) \
        == ["J1", "J2"]
    # a ref of another prefix later in the prose is not this part's
    assert li.part_refs({"role": "R1, Q1 gate-to-GND bleed"}) == ["R1"]
    assert li.part_refs({"role": "One part covers BOTH C1 and C9"}) == []
    assert li.part_refs({"refs": ["U7"], "role": "U1 buck"}) == ["U7"]


def test_screen_on_sbuck_emits_both_known_answers(tmp_path):
    """Accept: P3 on the sbuck workspace emits the SO-8EP known answer (and
    the DB128L one), as conflicts against its P3 constraints."""
    ws = _ws(tmp_path)
    code, out = _cli("datasheet_extract.py", "--screen", str(ws),
                     "--board-mm", "50x40")
    assert code == 1 and out["status"] == "violations"
    kinds = {(c["kind"], c.get("ref")) for c in out["conflicts"]}
    assert kinds == {("thermal_vias_over_capacity", "U1"),
                     ("wire_entry_faces_inward", "J1"),
                     ("wire_entry_faces_inward", "J2")}
    tv = next(c for c in out["conflicts"] if c["ref"] == "U1")
    assert (tv["wanted"], tv["max"]) == (16, 12)
    rots = {c["ref"]: c["want_rot"] for c in out["conflicts"]
            if c["kind"] == "wire_entry_faces_inward"}
    assert rots == {"J1": 270, "J2": 90}
    u1 = next(p for p in out["parts"] if p["refs"] == ["U1"])
    assert u1["thermal_vias"]["max_vias"] == 12
    assert u1["routing"]["track_between_pads"] is True
    assert out["board_budget"]["verdict"] == "ok"
    written = json.loads((ws / "parts" / "layout_implications.json")
                         .read_text())
    assert written["conflicts"] == out["conflicts"]


def test_screen_is_clean_once_constraints_carry_the_answers(tmp_path):
    """The suppressed case: the P6-corrected constraints (12 vias, rot
    270/90) raise no conflict, so the screen is not a constant fail."""
    ws = _ws(tmp_path)
    cj = ws / "architecture" / "constraints.json"
    c = json.loads(cj.read_text())
    c["thermal"][0]["min_vias"] = 12
    c["placement"]["edges"][0]["rot"] = 270
    c["placement"]["edges"][1]["rot"] = 90
    cj.write_text(json.dumps(c))
    code, out = _cli("datasheet_extract.py", "--screen", str(ws),
                     "--board-mm", "50x40")
    assert code == 0 and out["conflicts"] == []


def test_screen_missing_ep_size_is_a_gap_not_a_pass(tmp_path):
    ws = _ws(tmp_path)
    ext = json.loads(AP64350.read_text())
    del ext["exposed_pad"]["size_mm"]
    (ws / "parts" / "C2071691.json").write_text(json.dumps(ext))
    res = li.screen(ws, (50, 40))
    u1 = next(p for p in res["parts"] if p["refs"] == ["U1"])
    assert "thermal_vias" not in u1
    assert any("size_mm" in g for g in u1["gaps"])
    assert not any(c["kind"] == "thermal_vias_over_capacity"
                   for c in res["conflicts"])


def test_screen_reads_an_mpn_keyed_extraction(tmp_path):
    """#1119: a US-sourced parts.json line with no lcsc finds its
    extraction at parts/<mpn>.json; with neither file it has none."""
    ws = _ws(tmp_path)
    pj = ws / "parts" / "parts.json"
    data = json.loads(pj.read_text())
    line = next(p for p in data["parts"] if p.get("lcsc") == "C2071691")
    line["lcsc"] = ""
    pj.write_text(json.dumps(data))
    ext = ws / "parts" / "C2071691.json"
    ext.rename(ws / "parts" / f"{line['mpn']}.json")
    res = li.screen(ws, (50, 40))
    u1 = next(p for p in res["parts"] if p["refs"] == ["U1"])
    assert u1["extraction"] == f"{line['mpn']}.json"
    assert u1["thermal_vias"]["max_vias"] == 12
    (ws / "parts" / f"{line['mpn']}.json").unlink()
    u1 = next(p for p in li.screen(ws, (50, 40))["parts"]
              if p["refs"] == ["U1"])
    assert u1["extraction"] is None and "thermal_vias" not in u1


def test_screen_courtyard_budget(tmp_path):
    ws = _ws(tmp_path)
    # fixture courtyards: SO-8EP 32.4 + four 1206 at 10.58 = 74.7 mm2
    over = li.screen(ws, (10, 10))
    assert over["board_budget"]["courtyard_sum_mm2"] == 74.7
    assert over["board_budget"]["verdict"] == "over"
    assert any(c["kind"] == "courtyard_over_budget"
               for c in over["conflicts"])
    tight = li.screen(ws, (12, 10))             # 62 %: warn, no conflict
    assert tight["board_budget"]["verdict"] == "tight"
    assert not any(c["kind"].startswith("courtyard")
                   for c in tight["conflicts"])
    roomy = li.screen(ws, (50, 40))
    assert roomy["board_budget"]["verdict"] == "ok"
    assert not any(c["kind"].startswith("courtyard")
                   for c in roomy["conflicts"])


def test_schema_accepts_the_new_fields():
    code, out = _cli("datasheet_extract.py", "--validate", str(AP64350))
    assert code == 0, out


def test_part_sourcer_scoring_ranks_by_needs(tmp_path):
    """A candidate whose pad can hold the needed vias outranks the SO-8EP."""
    big = json.loads(AP64350.read_text())
    big["mpn"] = "BIG-EP"
    big["exposed_pad"]["size_mm"] = [3.502, 3.502]
    (tmp_path / "big.json").write_text(json.dumps(big))
    code, out = _cli("datasheet_extract.py", "--implications",
                     str(AP64350), str(tmp_path / "big.json"),
                     "--needs", json.dumps({"min_thermal_vias": 16}))
    assert code == 0
    ranked = [(c["mpn"], c["fails"]) for c in out["candidates"]]
    assert ranked[0] == ("BIG-EP", [])
    assert ranked[1][0] == "AP64350SP-13" and ranked[1][1] \
        == ["thermal_vias 12 < 16"]
    # with no need stated, neither candidate is penalised for it
    assert li.score(li.implications(json.loads(AP64350.read_text())),
                    {})["fails"] == []


# ---- backward spawns -----------------------------------------------------------

def test_cross_spawn_past_its_count_spawns_and_says_so(tmp_path):
    ws = tmp_path / "ws"
    state_mod.State.init(ws, "b", "P6")
    sp = str(ws / "state.json")
    for n in (1, 0):
        code, out = _cli("state.py", "cross-spawn", "--state", sp,
                         "--stage", "P6", "--role", "part-sourcer",
                         "--model", "sonnet", "--brief", "find a part")
        assert code == 0 and out["status"] == "spawned"
        assert out["remaining"] == n
    code, out = _cli("state.py", "cross-spawn", "--state", sp,
                     "--stage", "P6", "--role", "part-sourcer",
                     "--model", "sonnet", "--brief", "third")
    assert code == 0 and out["status"] == "spawned"
    assert out["over"] is True and out["remaining"] == -1
    assert "upstream" in out["warning"]
    st = json.loads((ws / "state.json").read_text())
    assert len([s for s in st["spawns"] if s.get("cross_stage")]) == 3
    assert st["open_issues"][-1]["status"] == "fixing"
    assert st["history"][-1]["event"] == "cross_spawn_over"
    # another stage's budget is its own
    code, out = _cli("state.py", "cross-spawn", "--state", sp,
                     "--stage", "P7", "--role", "research-component-scout",
                     "--model", "sonnet", "--brief", "router dead end")
    assert code == 0 and out["remaining"] == 1


def test_cross_spawn_refuses_writer_roles(tmp_path):
    st = state_mod.State.init(tmp_path / "ws", "b", "P6")
    with pytest.raises(CheckError, match="may not be spawned backward"):
        st.cross_spawn("P6", "schematic-block", "opus", "rewire it")
    assert st.data["spawns"] == [] and st.data["open_issues"] == []


def test_cross_spawn_on_a_pre_u9_state_installs_the_budget(tmp_path):
    st = state_mod.State.init(tmp_path / "ws", "b", "P6")
    del st.data["budgets"]["cross_stage_spawns"]
    out = st.cross_spawn("P6", "part-sourcer", "sonnet", "brief")
    assert out["status"] == "spawned" and out["remaining"] == 1


def test_round_trip_issue_spawn_edit_stale_set(tmp_path):
    """Placement dead end -> issue + tagged spawn -> the answer applied as a
    declared edit class -> exactly the mapped recorded gates go stale ->
    issue closed. (Toolchain-free leg; the board_update leg is below.)"""
    ws = tmp_path / "ws"
    st = state_mod.State.init(ws, "b", "P6")
    for g in ("erc", "place"):
        st.record_gate(g, PASS)
    st.save()
    sp = str(ws / "state.json")
    code, out = _cli("state.py", "cross-spawn", "--state", sp, "--stage",
                     "P6", "--role", "part-sourcer", "--model", "sonnet",
                     "--brief", "U1: SO-8EP holds 12 vias, need 16 - find a "
                     "pin-compatible part with a bigger EP",
                     "--kinds", "thermal_vias_over_capacity")
    assert code == 0
    iid = out["issue"]["id"]
    assert out["spawn"]["cross_stage"] is True and out["spawn"]["issue"] == iid
    assert out["issue"]["kinds"] == ["cross_stage",
                                     "thermal_vias_over_capacity"]
    code, out = _cli("state.py", "edit", "--state", sp, "--class",
                     "swap_part_new_fp", "--refs", "U1")
    assert code == 0
    ec = IMAP["edit_classes"]["swap_part_new_fp"]
    assert out["edit"]["gates_marked"] == [g for g in ec["gates"]
                                           if g in ("erc", "place")]
    fr = state_mod.State.load(ws / "state.json").freshness()
    assert set(fr["summary"]["stale"]) == {"erc", "place"}
    code, out = _cli("state.py", "issue", "--state", sp, "--id", str(iid),
                     "--status", "fixed")
    assert code == 0 and out["issue"]["status"] == "fixed"


@pytest.mark.smoke
def test_round_trip_through_board_update(tmp_path):
    """Same round trip with the real writer: the scout's answer (a new LCSC
    part on C5) lands through board_update --state, which declares the edit
    class itself; the marked set is the map's gates that had results."""
    import test_board_update as tbu
    ws = tmp_path / "ws"
    (ws / "kicad").mkdir(parents=True)
    pcb = tbu._stage(ws / "kicad", tbu.PD_PCB)
    st = state_mod.State.init(ws, "pd-trigger", "P6")
    for g in ("erc", "place"):
        st.record_gate(g, PASS)
    st.save()
    out = st.cross_spawn("P6", "part-sourcer", "sonnet",
                         "C5: find a 2.2uF 25V X5R in the same 0603")
    st.save()
    tree = tbu._load_net(tbu.PD_NET)
    tbu._set_value(tree, "C5", "2.2uF 25V X5R")
    tbu._set_field(tree, "C5", "LCSC", "C23630")
    net = tbu._dump_net(tree, tmp_path / "m.net")
    payload = tbu._run(pcb, net, "--state", str(ws / "state.json"))
    assert payload["status"] == "pass"
    after = json.loads((ws / "state.json").read_text())
    edit = after["edits"][-1]
    assert (edit["class"], edit["refs"]) == ("swap_part_same_fp", ["C5"])
    assert edit["gates_marked"] == ["erc"]      # place is not in the map
    assert payload["gates_to_rerun"] \
        == sorted(IMAP["edit_classes"]["swap_part_same_fp"]["gates"])
    spawn = after["spawns"][-1]
    assert spawn["cross_stage"] and spawn["issue"] == out["issue"]["id"]
