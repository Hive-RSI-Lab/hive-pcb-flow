"""S13 tests: state.json helpers, fixer dispatch wiring, and the scripted
P4->P8 orchestrator dry-run (smoke) with mid-pipeline mutations + kill/resume.

Hermetic tests exercise state.py / fix_dispatch.py / cluster_violations in
process (no toolchain). The smoke test runs tests/orchestrator/dryrun.py twice
as subprocesses: once stopping right after the P7 gate (simulated kill), once
resuming to completion - the plan's S13 acceptance.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import cluster_violations  # noqa: E402
import fix_dispatch  # noqa: E402
import state as state_mod  # noqa: E402
from checklib import CheckError  # noqa: E402

DRYRUN = REPO / "tests" / "orchestrator" / "dryrun.py"
GOLDEN = REPO / "tests" / "golden" / "blinky2"


# ---------------------------------------------------------------------------
# state.py (pure)
# ---------------------------------------------------------------------------
def make_state(tmp_path: Path) -> state_mod.State:
    return state_mod.State.init(tmp_path / "ws", "demo", "P4")


def test_state_init_show_and_force(tmp_path):
    st = make_state(tmp_path)
    assert st.path.exists()
    data = state_mod.State.load(st.path).data
    assert data["board"] == "demo" and data["phase"] == "P4"
    assert data["history"][0]["event"] == "init"
    with pytest.raises(CheckError):
        state_mod.State.init(tmp_path / "ws", "demo", "P4")
    state_mod.State.init(tmp_path / "ws", "demo2", "P0", force=True)
    assert state_mod.State.load(st.path).data["board"] == "demo2"


def test_state_init_creates_standard_subdirs(tmp_path):
    """T6 state-scaffold: init owns the workspace scaffold (the docs/playbook.md
    run-start dir list moves down-ladder); pre-existing content survives."""
    ws = tmp_path / "ws"
    (ws / "brief").mkdir(parents=True)
    (ws / "brief" / "brief.md").write_text("the brief", encoding="utf-8")
    state_mod.State.init(ws, "demo")
    for d in state_mod.SUBDIRS:
        assert (ws / d).is_dir(), d
    assert (ws / "brief" / "brief.md").read_text(encoding="utf-8") \
        == "the brief"


def test_set_phase_digest_discipline(tmp_path):
    """T6 XC-4: leaving a phase without a <=15-line digest yields a warn-only
    digest_discipline finding; a compliant digest stays silent.

    require_gates=False throughout: this is the digest tooth, not U16's
    gate-coverage tooth (which has its own tests)."""
    st = make_state(tmp_path)                      # phase P4
    ws = tmp_path / "ws"
    warns = st.set_phase("P5", require_gates=False)  # no log/P4-digest.md
    assert [w["kind"] for w in warns] == ["gate_coverage", "digest_discipline"]
    assert "P4-digest.md missing" in warns[1]["msg"]
    (ws / "log" / "P5-digest.md").write_text(
        "\n".join(f"line {i}" for i in range(12)), encoding="utf-8")
    warns = st.set_phase("P6", require_gates=False)   # 12 lines: compliant
    assert [w["kind"] for w in warns] == ["gate_coverage"]
    (ws / "log" / "P6-digest.md").write_text(
        "\n".join(f"line {i}" for i in range(20)), encoding="utf-8")
    warns = st.set_phase("P7", require_gates=False)
    assert [w["kind"] for w in warns] == ["gate_coverage", "digest_discipline"]
    assert "20 lines" in warns[1]["msg"]
    assert st.set_phase("P7") == []                # no-op: nothing was left


def test_state_phase_validation(tmp_path):
    st = make_state(tmp_path)
    st.set_phase("P7", require_gates=False)
    assert st.data["phase"] == "P7"
    with pytest.raises(CheckError):
        st.set_phase("P99")
    with pytest.raises(CheckError):
        state_mod.State.init(tmp_path / "ws2", "x", "nope")


def test_state_record_gate_attempts_and_history(tmp_path):
    st = make_state(tmp_path)
    fail = {"status": "fail", "failing_count": 3, "counts": {"total": 5},
            "phase": "P7"}
    ok = {"status": "pass", "failing_count": 0, "counts": {"total": 0}}
    g = st.record_gate("drc_routed", fail)
    assert g["attempts"] == 1 and g["status"] == "fail"
    g = st.record_gate("drc_routed", ok, phase="P7")
    assert g["attempts"] == 2 and g["status"] == "pass"
    assert [h["status"] for h in g["history"]] == ["fail", "pass"]
    assert g["phase"] == "P7"
    events = [e["event"] for e in st.data["history"]]
    assert events.count("gate") == 2
    with pytest.raises(CheckError):
        st.record_gate("x", {"status": "error"})


def test_state_artifact_decision_human(tmp_path):
    st = make_state(tmp_path)
    st.set_artifact("pcb", "kicad\\b.kicad_pcb")
    # v2 (T7): registry entries are typed objects; the file is absent here so
    # the normalized hash records null
    entry = st.data["artifacts"]["pcb"]
    assert entry["path"] == "kicad/b.kicad_pcb"
    assert entry["kind"] == "pcb" and entry["sha256"] is None
    st.add_decision("4-layer", "USB + buck")
    assert st.data["decisions"][0]["phase"] == "P4"
    st.record_human("2", "approved", note="ok")
    assert st.data["human"]["2"]["status"] == "approved"
    with pytest.raises(CheckError):
        st.record_human("9", "approved")
    with pytest.raises(CheckError):
        st.record_human("2", "maybe")


def test_state_issue_lifecycle(tmp_path):
    st = make_state(tmp_path)
    rec = st.open_issue({"gate": "verify", "fixer": "router",
                         "kinds": ["undersized_track"]})
    assert rec["id"] == 1 and rec["status"] == "open"
    assert st.data["next_issue_id"] == 2
    st.update_issue(1, status="fixing", agent="fixer-1", bump=True)
    rec = st.update_issue(1, status="fixed")
    assert rec["closed"] is not None and rec["attempts"] == 1
    with pytest.raises(CheckError):
        st.update_issue(99, status="fixed")
    with pytest.raises(CheckError):
        st.update_issue(1, status="bogus")


def test_state_budget(tmp_path):
    st = make_state(tmp_path)
    assert st.budget("fix_loops.verify") == 3
    assert st.budget("fix_loops.verify", consume=True) == 2
    st.data["budgets"]["fix_loops"]["verify"] = 0
    # a counter, not a wall: past zero it counts below and says so
    assert st.budget("fix_loops.verify", consume=True) == -1
    assert st.data["history"][-1]["event"] == "budget_over"
    with pytest.raises(CheckError):
        st.budget("nope.nope")


def test_state_snapshot_restore(tmp_path):
    st = make_state(tmp_path)
    ws = Path(st.data["workspace"])
    f = ws / "kicad" / "b.txt"
    f.parent.mkdir(parents=True, exist_ok=True)  # init already scaffolds it
    f.write_text("original", encoding="utf-8")
    st.set_artifact("pcb", "kicad/b.txt")
    snap = st.snapshot("pre-fix-1")
    assert snap["files"][0]["path"] == "kicad/b.txt"
    f.write_text("mutated", encoding="utf-8")
    res = st.restore("pre-fix-1")
    assert res["restored"] == ["kicad/b.txt"]
    assert f.read_text(encoding="utf-8") == "original"
    with pytest.raises(CheckError):
        st.restore("no-such-label")


def test_state_resume_summary_progression(tmp_path):
    st = make_state(tmp_path)
    s = st.resume_summary()
    assert s["next_gate"] == {"phase": "P4", "gate": "erc"}
    assert s["gates_passed"] == []
    st.record_gate("erc", {"status": "pass", "failing_count": 0,
                           "counts": {"total": 0}})
    st.set_phase("P7", require_gates=False)        # place not run yet (U16)
    s = st.resume_summary()
    assert s["gates_passed"] == ["erc"]
    assert s["next_gate"] == {"phase": "P6", "gate": "place"}
    # phase P7 is past the P2/P4/P6 checkpoints and none is recorded
    assert s["pending_human"] == ["1", "2", "3"]
    st.record_human("1", "approved")
    st.record_human("2", "approved")
    st.record_human("3", "skipped")  # optional checkpoint, explicitly skipped
    assert state_mod.State.load(st.path).path  # file still parses
    st.save()
    assert st.resume_summary()["pending_human"] == []


def test_state_cli_contract(tmp_path, capsys):
    ws = tmp_path / "ws"
    rc = state_mod.main(["init", "--workspace", str(ws), "--board", "b"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["cmd"] == "init"
    assert out["subdirs"] == list(state_mod.SUBDIRS)
    # digest warning surfaces in the set-phase result JSON, exit stays 0
    rc = state_mod.main(["set-phase", "--workspace", str(ws),
                         "--phase", "P1"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert any(w["kind"] == "digest_discipline"
               for w in out.get("warnings", []))
    rc = state_mod.main(["show", "--workspace", str(ws)])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["board"] == "b"
    rc = state_mod.main(["show", "--workspace", str(tmp_path / "nope")])
    assert rc == 2
    err = json.loads(capsys.readouterr().out)
    assert err["status"] == "error"
    rc = state_mod.main(["log", "--workspace", str(ws), "--event", "x",
                         "--data", "[1]"])  # not an object
    assert rc == 2


# ---------------------------------------------------------------------------
# cluster_violations wiring (kind fallback + hints coverage)
# ---------------------------------------------------------------------------
PIPELINE_KINDS = [
    # S4/S5 checks
    "corridor_void", "no_reference_plane", "missing_return_via",
    "missing_stitch_cap", "insufficient_transition_vias", "undersized_track",
    "pour_neckdown", "decoupler_distance", "decoupler_loop", "gnd_stub_long",
    "metadata_mismatch", "diffpair_skew", "diffpair_uncoupled",
    "diffpair_via_asymmetry", "diffpair_missing_net", "creepage",
    "thermal_area", "thermal_vias", "silk_over_pad", "silk_illegible",
    "silk_thin", "pdn_undecoupled", "pdn_no_bulk",
    # S6 fp_verify
    "pad_count", "pin1_missing", "pad_pitch", "pad_size", "no_courtyard",
    "stroked_pad_primitive",
    # S7 netlist_audit
    "missing_net", "diffpair_naming", "diffpair_unpaired",
    "power_no_consumers", "power_undeclared", "dangling_net", "netlist_diff",
    # S9 placement
    "courtyard_overlap", "outside_outline", "edge_violation",
    "keepout_violation", "courtyard_missing", "seed_unplaced",
    "mating_faces_inward", "mating_mouth_inset", "mating_zone_blocked",
    "mating_direction_unknown", "mating_model_reversed",
    "mating_mate_misaligned", "mating_mate_invalid", "mating_mouth_disagrees",
    # S11 routing
    "critical_route_failed", "critical_missing_net", "zone_unfilled",
    "stitch_impossible", "plane_split", "plane_split_unrepairable",
    "cleanup_regression",
    # S12 DFM
    "dfm_trace_width", "dfm_clearance", "dfm_copper_to_edge", "dfm_hole_size",
    "dfm_hole_to_hole", "dfm_hole_to_edge", "dfm_annular_ring",
    "dfm_silk_width", "dfm_silk_over_pad", "dfm_mask_dam",
    "dfm_missing_layer", "dfm_no_drill", "dfm_bom_incomplete", "cpl_polarity",
    "pad_net_mismatch",
]


def test_fixer_hints_cover_every_pipeline_kind():
    missing = [k for k in PIPELINE_KINDS
               if k not in cluster_violations.FIXER_HINTS]
    assert not missing, f"FIXER_HINTS lacks: {missing}"


def test_fixer_hint_domains_all_have_dispatch_tables():
    domains = set(cluster_violations.FIXER_HINTS.values()) | {"review"}
    missing = [d for d in domains if d not in fix_dispatch.DOMAINS]
    assert not missing, f"fix_dispatch.DOMAINS lacks: {missing}"


def drc_violation(check="track_width", pos=(10.0, 10.0), net="+3V3",
                  uuid="u-1", sev="error", source="drc"):
    return {"check": check, "severity": sev, "pos": list(pos), "layer": "F.Cu",
            "net": net, "refs": [], "msg": f"{check} violation",
            "source": source,
            "items": [{"msg": "x", "pos": list(pos), "uuid": uuid}]}


def test_cluster_kind_fallback_for_drc_reports():
    vs = [drc_violation(uuid="u-1"), drc_violation(uuid="u-2", pos=(11, 10))]
    clusters = cluster_violations.cluster(vs, 5.0)
    assert len(clusters) == 1
    c = clusters[0]
    assert c["kinds"] == ["track_width"]
    assert c["fixer"] == "router"
    # far apart -> spatial split still applies
    vs = [drc_violation(uuid="u-1"), drc_violation(uuid="u-2", pos=(90, 90))]
    assert len(cluster_violations.cluster(vs, 5.0)) == 2


# ---------------------------------------------------------------------------
# fix_dispatch.py (pure)
# ---------------------------------------------------------------------------
def gate_result(failing, gate="drc_routed", phase="P7"):
    return {"script": "gate", "gate": gate, "phase": phase, "status": "fail",
            "failing_count": len(failing),
            "counts": {"total": len(failing)}, "failing": failing}


def make_board(tmp_path: Path) -> Path:
    kicad = tmp_path / "kicad"
    kicad.mkdir(exist_ok=True)
    board = kicad / "b.kicad_pcb"
    board.write_text("(kicad_pcb)", encoding="utf-8")
    (kicad / "constraints.json").write_text("{}", encoding="utf-8")
    return board


def run_dispatch(tmp_path, failing, state_path=None, gate="drc_routed"):
    board = make_board(tmp_path)
    inp = tmp_path / "gate.json"
    inp.write_text(json.dumps(gate_result(failing, gate=gate)),
                   encoding="utf-8")
    argv = ["--input", str(inp), "--board", str(board)]
    if state_path:
        argv += ["--state", str(state_path)]
    return fix_dispatch.run(argv)


def test_dispatch_writes_actionable_work_orders(tmp_path):
    failing = [
        drc_violation(uuid="u-1"),                        # router (DRC kind)
        {"check": "check_current", "severity": "error", "pos": [50, 50],
         "layer": "F.Cu", "net": "+5V", "refs": [], "msg": "undersized",
         "source": "check_current", "kind": "undersized_track",
         "required_mm": 0.3, "segment": {"start": [49, 50], "end": [51, 50]},
         "items": [{"msg": "x", "pos": [50, 50]}]},       # router (kind)
        {"check": "pin_not_connected", "severity": "error", "pos": None,
         "layer": None, "net": None, "refs": ["U1"], "msg": "pin",
         "source": "erc", "items": [{"msg": "x", "pos": None}]},  # schematic
    ]
    payload, _ = run_dispatch(tmp_path, failing)
    assert payload["status"] == "violations"
    # T6 (XC-2): the two single-violation router clusters batch into ONE
    # work order (same domain, small clusters); schematic stays separate
    assert payload["counts"]["orders"] == 2
    assert payload["counts"]["by_domain"] == {"router": 1, "schematic": 1}
    for o in payload["orders"]:
        wo = json.loads(Path(o["work_order"]).read_text(encoding="utf-8"))
        assert wo["allowed_scripts"] and wo["guidance"] and wo["scope"]
        assert wo["cluster"]["violations"]
        assert wo["artifacts"]["board"].endswith("b.kicad_pcb")
        assert wo["artifacts"]["constraints"].endswith("constraints.json")
        assert wo["role_prompt"].endswith("agents/fixer.md")
    # DRC uuid travels into the work order for remove-by-uuid fixes; the
    # merged order keeps every violation's coordinates
    router = [o for o in payload["orders"] if o["fixer"] == "router"][0]
    wo = json.loads(Path(router["work_order"]).read_text(encoding="utf-8"))
    assert len(wo["cluster"]["violations"]) == 2
    uuids = [v["items"][0].get("uuid") for v in wo["cluster"]["violations"]]
    assert "u-1" in uuids


def test_dispatch_parallel_groups_disjoint_regions(tmp_path):
    failing = [drc_violation(uuid="u-1", pos=(10, 10)),
               drc_violation(uuid="u-2", pos=(90, 90), net="GND"),
               drc_violation(uuid="u-3", pos=(11, 11), net="GND")]
    payload, _ = run_dispatch(tmp_path, failing)
    groups = payload["parallel_groups"]
    assert sum(len(g) for g in groups) == payload["counts"]["orders"]
    # the two clusters near (10,10) overlap spatially -> different groups
    by_id = {o["id"]: o for o in payload["orders"]}
    for g in groups:
        centers = [(by_id[i]["net"], i) for i in g]
        assert len(centers) == len(set(centers))
    if len(groups) > 1:
        assert any(len(g) > 1 for g in groups) or len(groups) == 3


def test_dispatch_records_open_issues_in_state(tmp_path):
    st = state_mod.State.init(tmp_path / "ws", "b", "P7")
    payload, _ = run_dispatch(tmp_path, [drc_violation()], state_path=st.path)
    st2 = state_mod.State.load(st.path)
    assert len(st2.data["open_issues"]) == 1
    issue = st2.data["open_issues"][0]
    assert issue["fixer"] == "router" and issue["status"] == "open"
    assert issue["gate"] == "drc_routed"
    assert Path(issue["work_order"]).exists()
    assert payload["orders"][0]["id"] == issue["id"]
    # orders land in the workspace log dir when --state is given
    assert "log" in payload["out_dir"].replace("\\", "/").split("/")


def test_dispatch_nothing_to_do(tmp_path):
    payload, _ = run_dispatch(tmp_path, [])
    assert payload["status"] == "pass" and payload["counts"]["orders"] == 0


def test_dispatch_accepts_plain_reports_and_cluster_payloads(tmp_path):
    board = make_board(tmp_path)
    rep = tmp_path / "rep.json"
    rep.write_text(json.dumps({"violations": [drc_violation()]}),
                   encoding="utf-8")
    payload, _ = fix_dispatch.run(["--input", str(rep), "--board", str(board)])
    assert payload["counts"]["orders"] == 1
    clus = tmp_path / "clusters.json"
    clus.write_text(json.dumps(
        {"clusters": [{"violations": [drc_violation()]}]}), encoding="utf-8")
    payload, _ = fix_dispatch.run(["--input", str(clus), "--board",
                                   str(board)])
    assert payload["counts"]["orders"] == 1
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    with pytest.raises(CheckError):
        fix_dispatch.run(["--input", str(bad), "--board", str(board)])


def test_dispatch_missing_board_errors(tmp_path):
    inp = tmp_path / "gate.json"
    inp.write_text(json.dumps(gate_result([drc_violation()])),
                   encoding="utf-8")
    with pytest.raises(CheckError):
        fix_dispatch.run(["--input", str(inp), "--board",
                          str(tmp_path / "missing.kicad_pcb")])


def test_dispatch_erc_fallback_to_schematic(tmp_path):
    v = drc_violation(check="totally_new_erc_type", source="erc", uuid=None)
    payload, _ = run_dispatch(tmp_path, [v], gate="erc")
    assert payload["orders"][0]["fixer"] == "schematic"
    # unknown kind from a non-erc source stays review (human triage)
    v = drc_violation(check="totally_new_drc_type", source="drc")
    payload, _ = run_dispatch(tmp_path, [v])
    assert payload["orders"][0]["fixer"] == "review"


# ---------------------------------------------------------------------------
# the S13 acceptance dry-run (smoke: live toolchain, ~1 min)
# ---------------------------------------------------------------------------
def run_dryrun(ws: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(DRYRUN), "--workspace", str(ws), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(REPO), timeout=900)


def events(state: dict, name: str) -> list[dict]:
    return [e for e in state["history"] if e["event"] == name]


@pytest.mark.smoke
def test_dryrun_p4_p8_with_kill_and_resume(tmp_path):
    ws = tmp_path / "ws"

    # ---- session 1: run to the P7 gate, then die -------------------------
    p = run_dryrun(ws, "--reset", "--stop-after", "drc_routed")
    assert p.returncode == 0, p.stdout + p.stderr
    st = json.loads((ws / "state.json").read_text(encoding="utf-8"))
    assert st["gates"]["erc"]["status"] == "pass"
    assert st["gates"]["place"]["status"] == "pass"
    g = st["gates"]["drc_routed"]
    assert g["status"] == "pass" and g["attempts"] == 2  # fail -> fix -> pass
    assert [h["status"] for h in g["history"]] == ["fail", "pass"]
    assert "verify" not in st["gates"]
    assert st["phase"] == "P8"
    assert events(st, "mutation_injected")[0]["name"] == "A"
    assert st["budgets"]["fix_loops"]["drc_routed"] == 2  # one loop consumed
    # U13: the recipe's P3-exit coverage report was emitted + logged
    cov = json.loads((ws / "log" / "coverage-P3.json").read_text(
        encoding="utf-8"))
    assert cov["script"] == "knowledge" and cov["phase"] == "P3"
    assert {"slots", "covered", "provisional", "gap"} <= set(cov["summary"])
    assert events(st, "coverage_reported")[0]["summary"] == cov["summary"]
    issues = st["open_issues"]
    assert len(issues) == 1 and issues[0]["status"] == "fixed"
    assert issues[0]["fixer"] == "router"
    assert Path(issues[0]["work_order"]).exists()
    assert (ws / "state_snapshots" / "pre-fix-1" / "manifest.json").exists()

    # ---- session 2: resume from state.json, finish P8 --------------------
    p = run_dryrun(ws)
    assert p.returncode == 0, p.stdout + p.stderr
    st = json.loads((ws / "state.json").read_text(encoding="utf-8"))
    assert events(st, "resumed"), "resume must be recorded in history"
    # the resumed session did NOT redo earlier gates
    assert st["gates"]["erc"]["attempts"] == 1
    assert st["gates"]["place"]["attempts"] == 1
    v = st["gates"]["verify"]
    assert v["status"] == "pass" and v["attempts"] == 2
    assert st["gates"]["drc_routed"]["attempts"] == 3  # + post-fix regate
    assert st["gates"]["drc_routed"]["status"] == "pass"
    names = [e["name"] for e in events(st, "mutation_injected")]
    assert names == ["A", "B"]
    assert len(st["open_issues"]) == 2
    assert all(i["status"] == "fixed" for i in st["open_issues"])
    assert st["budgets"]["fix_loops"]["verify"] == 2
    assert st["phase"] == "P9"
    assert events(st, "dryrun_complete")
    # history is ordered: init < resumed < complete
    ev = [e["event"] for e in st["history"]]
    assert ev.index("init") < ev.index("resumed") < ev.index("dryrun_complete")

    # U16: the run recorded itself. Every gate carries its input hashes (the
    # freshness key), the whole walk was recorded rather than narrated, and
    # resume names the TRUE next gate - dfm, the one phase P9 has not run.
    for name, entry in st["gates"].items():
        inputs = entry["last"]["inputs"]
        # every mapped kind is present as a key; a None value is honest (the
        # golden ships no .kicad_dru) and still binds - it matches only while
        # the file stays absent
        assert inputs and any(v for v in inputs.values()), name
    assert not events(st, "phase_ungated")
    summary = state_mod.State.load(ws / "state.json").resume_summary()
    assert summary["gates_passed"] == ["erc", "place", "drc_routed", "verify"]
    # place ran before the injected mutations + their fixes, so its recorded
    # pcb hash no longer matches - recorded evidence going stale on its own is
    # the point of recording it
    assert summary["gates_passed_fresh"] == ["erc", "drc_routed", "verify"]
    assert summary["gates_stale"] == ["place"]
    assert summary["next_gate"] == {"phase": "P9", "gate": "dfm"}

    # ---- the fixes restored the golden geometry (not just gate-quiet) ----
    import geom
    fixed = geom.BoardGeom.from_file(ws / "kicad" / "blinky2.kicad_pcb")
    golden = geom.BoardGeom.from_file(GOLDEN / "blinky2.kicad_pcb")
    a = fixed.net_area("+3V3", "F.Cu")
    b = golden.net_area("+3V3", "F.Cu")
    assert abs(a - b) / b < 1e-6, (a, b)

    # ---- session 3 on a COMPLETE run: resume logs, nothing re-runs -------
    st1 = st
    p = run_dryrun(ws)
    assert p.returncode == 0, p.stdout + p.stderr
    st = json.loads((ws / "state.json").read_text(encoding="utf-8"))
    assert st["gates"] == st1["gates"]
    assert len(st["open_issues"]) == len(st1["open_issues"])
    assert len(events(st, "resumed")) == len(events(st1, "resumed")) + 1
