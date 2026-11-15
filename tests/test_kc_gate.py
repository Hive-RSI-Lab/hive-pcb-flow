"""S2 acceptance tests: kicad-cli wrappers (kc.py) + gate infrastructure.

Plan S2 accept criteria:
  - wrappers produce normalized JSON for all three golden boards
  - a seeded DRC violation fails gate.py with exit 1 and correct coordinates

Pure tests (no toolchain) exercise the normalizer and gate logic on captured
kicad-cli JSON. `smoke`-marked tests drive the real kicad-cli (10.0.3 via
env.py) so `pytest -m "not smoke"` still runs the parser/logic checks.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
GOLDEN = REPO / "tests" / "golden"
GATES_YAML = SCRIPTS.parent / "reference" / "gates.yaml"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))
import checklib  # noqa: E402
import env  # noqa: E402
import kc  # noqa: E402
import gate  # noqa: E402

BOARDS = ["blinky2", "usbbuck4", "rf4"]


# ------------------------------------------------------------ captured JSON

# A representative raw kicad-cli DRC report exercising every section and the
# layer/net/refs parsing paths (shapes captured from real 10.0.3 output).
RAW_DRC = {
    "coordinate_units": "mm",
    "kicad_version": "10.0.3",
    "violations": [
        {"type": "track_width", "severity": "error",
         "description": "Track width (min width 0.1270 mm; actual 0.0500 mm)",
         "items": [{"description": "Track [+5V] on F.Cu, length 7.5000 mm",
                    "pos": {"x": 102.8, "y": 103.5}, "uuid": "u1"}]},
        {"type": "silk_over_copper", "severity": "warning",
         "description": "Silkscreen clipped by solder mask",
         "items": [{"description": "PCB text 'TP1' on F.Silkscreen",
                    "pos": {"x": 132.44, "y": 129.5}, "uuid": "u2"},
                   {"description": "Pad 1 [GND] of D1 on F.Cu",
                    "pos": {"x": 132.4375, "y": 129.5}, "uuid": "u3"}]},
    ],
    "unconnected_items": [
        {"type": "unconnected_items", "severity": "error",
         "description": "Missing connection",
         "items": [{"description": "Pad 2 [/MCO] of U1 on B.Cu",
                    "pos": {"x": 141.0, "y": 123.0}, "uuid": "u4"}]},
    ],
    "schematic_parity": [
        {"type": "footprint", "severity": "error",
         "description": "Footprint D1 pad nets differ from schematic",
         "items": [{"description": "Pad 1 [GND] of D1 on F.Cu",
                    "pos": {"x": 132.4, "y": 129.5}, "uuid": "u5"}]},
    ],
}

RAW_ERC = {
    "coordinate_units": "mm",
    "sheets": [{"path": "/", "uuid_path": "/",
                "violations": [
                    {"type": "pin_not_connected", "severity": "error",
                     "description": "Input pin not driven",
                     "items": [{"description": "Symbol U1 Pin 5 [PB0]",
                                "pos": {"x": 50.0, "y": 60.0}}]}]}],
}


# ---------------------------------------------------------- normalizer (pure)

def test_normalized_violation_has_schema_keys():
    for v in kc.parse_drc_data(RAW_DRC):
        for key in kc.SCHEMA_KEYS:
            assert key in v, f"missing {key} in {v}"
        assert v["source"] in ("drc", "unconnected", "parity")


def test_parse_drc_merges_all_sections():
    vs = kc.parse_drc_data(RAW_DRC)
    assert len(vs) == 4
    by_source = {}
    for v in vs:
        by_source.setdefault(v["source"], 0)
        by_source[v["source"]] += 1
    assert by_source == {"drc": 2, "unconnected": 1, "parity": 1}


def test_track_width_fields_parsed():
    tw = next(v for v in kc.parse_drc_data(RAW_DRC) if v["check"] == "track_width")
    assert tw["severity"] == "error"
    assert tw["pos"] == [102.8, 103.5]
    assert tw["layer"] == "F.Cu"
    assert tw["net"] == "+5V"
    assert tw["refs"] == []          # a bare track has no refdes
    assert "Track width" in tw["msg"]


def test_multi_item_aggregation():
    silk = next(v for v in kc.parse_drc_data(RAW_DRC)
                if v["check"] == "silk_over_copper")
    # net comes from the second item (first has none); refs union both items
    assert silk["net"] == "GND"
    assert silk["layer"] == "F.Silkscreen"
    assert silk["refs"] == ["D1", "TP1"]
    assert len(silk["items"]) == 2
    assert silk["pos"] == [132.44, 129.5]     # first item's position


def test_unconnected_net_and_refs():
    u = next(v for v in kc.parse_drc_data(RAW_DRC) if v["source"] == "unconnected")
    assert u["net"] == "/MCO"
    assert u["refs"] == ["U1"]
    assert u["layer"] == "B.Cu"


def test_parse_erc_nesting_and_source():
    vs = kc.parse_erc_data(RAW_ERC)
    assert len(vs) == 1
    v = vs[0]
    assert v["source"] == "erc"
    assert v["sheet"] == "/"
    assert v["refs"] == ["U1"]
    assert v["net"] == "PB0"


def test_summarize_counts():
    c = kc.summarize(kc.parse_drc_data(RAW_DRC))
    assert c["total"] == 4
    assert c["by_severity"] == {"error": 3, "warning": 1}


# ------------------------------------------------------------- gate logic (pure)

def _report(violations):
    return {"input": "x.kicad_pcb", "violations": violations,
            "counts": kc.summarize(violations)}


def test_gate_evaluate_fails_on_error():
    report = _report(kc.parse_drc_data(RAW_DRC))
    g = {"phase": "P6", "tool": "drc", "fail_severities": ["error"], "max_count": 0}
    res = gate.evaluate("drc", g, report)
    assert res["status"] == "fail"
    assert res["failing_count"] == 3          # three error-severity violations
    assert all(v["severity"] == "error" for v in res["failing"])


def test_gate_evaluate_clean_gate_counts_warnings():
    report = _report(kc.parse_drc_data(RAW_DRC))
    g = {"tool": "drc", "fail_severities": ["error", "warning"], "max_count": 0}
    res = gate.evaluate("clean", g, report)
    assert res["failing_count"] == 4          # warning now counts too


def test_gate_result_carries_warnings_below_the_bar():
    """A pass/fail result shows what it didn't fail on (issue #30): the
    verify gate's merged report is gone by the time the caller reads it."""
    report = _report(kc.parse_drc_data(RAW_DRC))
    g = {"tool": "drc", "fail_severities": ["error"], "max_count": 5}
    res = gate.evaluate("drc", g, report)
    assert res["status"] == "pass"
    assert res["warnings_count"] == 1
    assert [v["severity"] for v in res["warnings"]] == ["warning"]
    assert "warnings_truncated" not in res
    res = gate.evaluate("clean", {"tool": "drc",
                                  "fail_severities": ["error", "warning"]},
                        report)
    assert res["warnings_count"] == 0 and res["warnings"] == []


def test_gate_result_caps_inline_warnings(monkeypatch):
    monkeypatch.setattr(gate, "WARNINGS_CAP", 2)
    viols = [{"severity": "warning", "source": "drc", "description": str(i)}
             for i in range(5)]
    res = gate.evaluate("drc", {"tool": "drc", "fail_severities": ["error"]},
                        _report(viols))
    assert res["warnings_count"] == 5
    assert len(res["warnings"]) == 2 and res["warnings_truncated"] is True


def test_gate_evaluate_passes_when_clean():
    res = gate.evaluate("drc", {"tool": "drc", "fail_severities": ["error"]},
                        _report([]))
    assert res["status"] == "pass"
    assert res["failing"] == []


def test_gate_evaluate_max_count_tolerance():
    report = _report(kc.parse_drc_data(RAW_DRC))
    g = {"tool": "drc", "fail_severities": ["error"], "max_count": 5}
    assert gate.evaluate("drc", g, report)["status"] == "pass"


# --------------------------------------------------------------- gates.yaml

def test_gates_yaml_valid():
    data = yaml.safe_load(GATES_YAML.read_text(encoding="utf-8"))
    gates = data["gates"]
    assert {"erc", "drc", "drc_routed"} <= set(gates)
    for name, g in gates.items():
        # S5 added the "verify" tool (verify_all.py aggregate); S9 added
        # "place" (place_metrics.py legality); S12 added "dfm" (dfm_check.py
        # on the exported gerbers); post-v1 added "sim" (sim_run.py testbench
        # bounds) - gate.py wires all four.
        assert g["tool"] in ("erc", "drc", "verify", "place", "dfm", "sim"), \
            f"{name}: bad tool {g['tool']}"
        for sev in g.get("fail_severities", ["error"]):
            assert sev in ("error", "warning", "exclusion")
    # T6 (ladder row 122): the P6 interim drc gate runs schematic parity so
    # a symbol/footprint mismatch surfaces BEFORE routing, not at drc_routed
    assert gates["drc"]["drc_options"]["parity"] is True
    # T6 (ladder row 128): drc_routed refuses stale fills instead of grading
    # phantom zone clearance errors
    assert gates["drc_routed"]["drc_options"]["require_fresh_fills"] is True


def test_drc_gate_parity_flows_to_kc(monkeypatch, tmp_path):
    """The P6 drc gate's parity option must reach kc.run_drc."""
    data = yaml.safe_load(GATES_YAML.read_text(encoding="utf-8"))
    seen = {}

    def fake_run_drc(cli, pcb, *, parity=False, all_track_errors=False,
                     refill=False, save_board=False):
        seen.update(parity=parity, all_track_errors=all_track_errors)
        return {"input": str(pcb), "violations": [], "counts": {"total": 0}}

    monkeypatch.setattr(kc, "resolve_cli", lambda: Path("kicad-cli"))
    monkeypatch.setattr(kc, "run_drc", fake_run_drc)
    board = tmp_path / "b.kicad_pcb"
    board.write_text("(kicad_pcb)", encoding="utf-8")
    gate.run_report_for_gate(data["gates"]["drc"], board)
    assert seen == {"parity": True, "all_track_errors": False}


def test_run_drc_fails_a_board_whose_rules_kicad_drops(monkeypatch, tmp_path):
    """#1273: one value KiCad's rule parser rejects drops the whole
    .kicad_dru, and kicad-cli then checks none of its rules and reports
    nothing; run_drc puts an error row first, so the gate fails."""
    monkeypatch.setattr(kc, "_run_json_report", lambda cli, args, pcb: {
        "coordinate_units": "mm", "violations": []})
    board = tmp_path / "b.kicad_pcb"
    board.write_text("(kicad_pcb)\n", encoding="utf-8")
    assert kc.run_drc(Path("kicad-cli"), board)["status"] == "pass"
    dru = tmp_path / "b.kicad_dru"
    dru.write_text('(version 1)\n(rule "hv" (constraint clearance '
                   '(min 0.5MM))\n  (condition "A.NetName == \'HV\'"))\n',
                   encoding="utf-8")
    rep = kc.run_drc(Path("kicad-cli"), board)
    assert rep["status"] == "violations"
    row = rep["violations"][0]
    assert all(key in row for key in kc.SCHEMA_KEYS)
    assert row["check"] == "rules_file_ignored" and row["severity"] == "error"
    assert "b.kicad_dru" in row["msg"] and "0.5MM" in row["msg"]
    g = {"tool": "drc", "fail_severities": ["error"], "max_count": 0}
    assert gate.evaluate("drc", g, rep)["status"] == "fail"
    dru.write_text('(version 1)\n(rule "hv" (constraint clearance '
                   '(min 20mil)))\n', encoding="utf-8")
    assert kc.run_drc(Path("kicad-cli"), board)["status"] == "pass"


def test_drc_routed_gate_refuses_stale_fills(tmp_path):
    """A board with an unfilled zone exits 2 with a stale-fill message
    instead of being graded (offline: the preflight runs before kicad-cli
    resolution)."""
    board = tmp_path / "stale.kicad_pcb"
    board.write_text("""(kicad_pcb (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user)) (setup)
  (gr_rect (start 0 0) (end 20 10) (stroke (width 0.1)) (fill no)
    (layer "Edge.Cuts"))
  (zone (net "GND") (layer "B.Cu")
    (polygon (pts (xy 0 0) (xy 20 0) (xy 20 10) (xy 0 10)))))
""", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "gate.py"), "--gate", "drc_routed",
         str(board)], capture_output=True, text=True, encoding="utf-8",
        errors="replace")
    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert payload["status"] == "error"
    assert "stale" in payload["error"]
    assert "--refill" in payload["error"]


# ------------------------------------------------------------- waivers (T6)

def _wv(check=None, kind=None, net=None, refs=None,
        reason="pkg-internal gap", approved="H4 2026-08-06"):
    w = {"reason": reason, "approved": approved}
    if check:
        w["check"] = check
    if kind:
        w["kind"] = kind
    if net:
        w["net"] = net
    if refs:
        w["refs"] = refs
    return w


def _creep(net="V48_RTN", refs=("U22",), sev="error"):
    return {"check": "check_creepage", "kind": "creepage", "severity": sev,
            "net": net, "refs": list(refs), "pos": [1.0, 1.0],
            "msg": "x", "source": "check_creepage"}


def test_waiver_matching_passes_gate():
    g = {"tool": "verify", "fail_severities": ["error"], "max_count": 0}
    report = _report([_creep()])
    res = gate.evaluate("verify", g, report,
                        waivers=[_wv(kind="creepage", net="V48_RTN")])
    assert res["status"] == "pass"
    assert res["failing_count"] == 0
    assert res["waived_count"] == 1
    assert res["waived"][0]["net"] == "V48_RTN"


def test_waiver_nonmatching_still_fails():
    g = {"tool": "verify", "fail_severities": ["error"], "max_count": 0}
    report = _report([_creep(), _creep(net="+48V_SW")])
    res = gate.evaluate("verify", g, report,
                        waivers=[_wv(kind="creepage", net="V48_RTN")])
    assert res["status"] == "fail"               # +48V_SW is not waived
    assert res["failing_count"] == 1 and res["waived_count"] == 1
    assert res["failing"][0]["net"] == "+48V_SW"


def test_waiver_refs_subset_matching():
    w = _wv(kind="creepage", net="V48_RTN", refs=["U22", "U1"])
    assert gate.waiver_matches(w, _creep(refs=("U22",)))
    assert gate.waiver_matches(w, _creep(refs=("U22", "U1")))
    assert not gate.waiver_matches(w, _creep(refs=("U22", "U9")))
    # check-keyed waivers match on check OR source
    w2 = _wv(check="check_creepage", net="V48_RTN")
    assert gate.waiver_matches(w2, _creep())
    assert not gate.waiver_matches(_wv(kind="corridor_void", net="V48_RTN"),
                                   _creep())


def test_waiver_without_reason_is_refused(tmp_path):
    """load_waivers passes the entry through; evaluate refuses it by index."""
    f = tmp_path / "verify-waivers.json"
    report = {"violations": [_creep()], "counts": {"total": 1}}
    gdef = {"fail_severities": ["error"], "max_count": 0}
    for entry, says in (
            ({"kind": "creepage", "net": "X", "reason": "", "approved": "H4"},
             "reason"),
            ({"net": "X", "reason": "r", "approved": "H4"}, "check")):
        f.write_text(json.dumps({"waivers": [entry]}), encoding="utf-8")
        res = gate.evaluate("g", gdef, report, waivers=gate.load_waivers(f))
        assert [b["index"] for b in res["waivers_invalid"]] == [0]
        assert says in res["waivers_invalid"][0]["problems"][0]


def test_waivers_cli_exit_codes(tmp_path):
    """--report + --waivers end to end: waived residual -> exit 0; a waiver
    missing `approved` waives nothing -> exit 1; a file that isn't JSON ->
    exit 2. The report must be a VALID stamped
    verify_all report since U2 (gate.py --report validation, codex C4)."""
    board = tmp_path / "x.kicad_pcb"
    board.write_text("(kicad_pcb)", encoding="utf-8")
    report = tmp_path / "r.json"
    report.write_text(json.dumps(checklib.stamp(
        {"script": "verify_all", "board": board.name, "status": "violations",
         "checks": {}, "violations": [_creep()],
         "counts": {"total": 1}}, board)), encoding="utf-8")
    wv = tmp_path / "w.json"
    wv.write_text(json.dumps({"waivers": [
        {"kind": "creepage", "net": "V48_RTN", "reason": "TI SOIC-8 land",
         "approved": "H4 2026-08-06"}]}), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "gate.py"), "--gate", "verify",
         "--report", str(report), "--waivers", str(wv)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert proc.returncode == 0, proc.stdout
    assert json.loads(proc.stdout)["waived_count"] == 1
    wv.write_text(json.dumps({"waivers": [
        {"kind": "creepage", "net": "V48_RTN", "reason": "r"}]}),
        encoding="utf-8")
    proc2 = subprocess.run(
        [sys.executable, str(SCRIPTS / "gate.py"), "--gate", "verify",
         "--report", str(report), "--waivers", str(wv)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert proc2.returncode == 1
    res = json.loads(proc2.stdout)
    assert res["waived_count"] == 0
    assert [b["index"] for b in res["waivers_invalid"]] == [0]
    wv.write_text("{broken", encoding="utf-8")
    proc3 = subprocess.run(
        [sys.executable, str(SCRIPTS / "gate.py"), "--gate", "verify",
         "--report", str(report), "--waivers", str(wv)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert proc3.returncode == 2
    assert "Traceback" not in proc3.stdout + proc3.stderr


# ------------------------------------------------------- git-commit helper

def _tmp_repo(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()

    def git(*a):
        return subprocess.run(["git", *a], cwd=repo, capture_output=True,
                              text=True)
    assert git("init").returncode == 0
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    return repo, git


def test_git_commit_on_pass(tmp_path):
    """U2 (codex C4): a commit needs a boards/<name>/ scope - unscoped and
    non-boards invocations refuse; a scoped one commits."""
    repo, git = _tmp_repo(tmp_path)
    (repo / "a.txt").write_text("hi", encoding="utf-8")
    res = gate.git_commit_on_pass("gate erc pass", repo)     # no input at all
    assert res["committed"] is False and res["ok"] is False
    assert "boards/<name>" in res["reason"]
    ws = repo / "boards" / "foo"
    ws.mkdir(parents=True)
    board = ws / "foo.kicad_pcb"
    board.write_text("(kicad_pcb)", encoding="utf-8")
    res = gate.git_commit_on_pass("gate erc pass", repo, input_file=board)
    assert res["committed"] is True and res["commit"] and res["ok"] is True
    # nothing left to commit INSIDE the scope -> clean skip, not an error
    res2 = gate.git_commit_on_pass("noop", repo, input_file=board)
    assert res2["committed"] is False and res2["ok"] is True
    assert "nothing to commit" in res2["reason"]


def test_git_commit_scoped_to_workspace(tmp_path):
    """T6 (ladder row 59): a boards/<name>/ input scopes staging to that
    workspace; a parallel session's dirty file stays OUT of the gate commit
    and is reported, not swept."""
    repo, git = _tmp_repo(tmp_path)
    ws = repo / "boards" / "foo" / "kicad"
    ws.mkdir(parents=True)
    board = ws / "foo.kicad_pcb"
    board.write_text("(kicad_pcb)", encoding="utf-8")
    (ws / "constraints.json").write_text("{}", encoding="utf-8")
    (repo / "scratch.txt").write_text("parallel-session WIP", encoding="utf-8")

    res = gate.git_commit_on_pass("gate verify pass", repo, input_file=board)
    assert res["committed"] is True
    assert res["scope"] == "boards/foo"
    assert res["excluded_dirty"] == ["scratch.txt"]
    shown = git("show", "--name-only", "--format=", "HEAD").stdout
    assert "boards/foo/kicad/foo.kicad_pcb" in shown
    assert "boards/foo/kicad/constraints.json" in shown
    assert "scratch.txt" not in shown
    # the WIP file is still dirty, untouched
    assert "scratch.txt" in git("status", "--porcelain").stdout


def test_git_commit_non_boards_input_always_refused(tmp_path):
    """U2 (codex C4): the -A fallback is GONE. A non-boards input refuses
    the commit even on an otherwise clean tree - a gate commit always needs
    an explicit boards/<name>/ scope."""
    repo, git = _tmp_repo(tmp_path)
    ws = repo / "ws"
    ws.mkdir()
    board = ws / "b.kicad_pcb"
    board.write_text("(kicad_pcb)", encoding="utf-8")

    res = gate.git_commit_on_pass("gate pass", repo, input_file=board)
    assert res["committed"] is False and res["ok"] is False
    assert "boards/<name>" in res["reason"]
    # nothing was staged or committed
    assert git("log", "--oneline").returncode != 0 \
        or not git("log", "--oneline").stdout.strip()


def test_git_commit_workspace_only_outside_dirty(tmp_path):
    """Nothing dirty INSIDE the workspace -> clean skip with the outside
    paths reported."""
    repo, git = _tmp_repo(tmp_path)
    ws = repo / "boards" / "bar"
    ws.mkdir(parents=True)
    board = ws / "bar.kicad_pcb"
    board.write_text("(kicad_pcb)", encoding="utf-8")
    res = gate.git_commit_on_pass("first", repo, input_file=board)
    assert res["committed"] is True
    (repo / "stray.txt").write_text("x", encoding="utf-8")
    res2 = gate.git_commit_on_pass("noop", repo, input_file=board)
    assert res2["committed"] is False
    assert "inside the workspace" in res2["reason"]
    assert res2["excluded_dirty"] == ["stray.txt"]


# ------------------------------------------------------------------ fixtures

@pytest.fixture(scope="session")
def cli():
    c = env.find_kicad_cli()
    if c is None:
        pytest.skip("kicad-cli not installed")
    return c


SEG_RE = re.compile(
    r"\(segment\s+\(start (-?[\d.]+) (-?[\d.]+)\)\s+"
    r"\(end (-?[\d.]+) (-?[\d.]+)\)\s+\(width ([\d.]+)")


@pytest.fixture
def seeded_drc_board(tmp_path):
    """Copy golden blinky2, narrow its first track segment below the 0.127 mm
    floor -> a deterministic track_width DRC error. Returns (pcb_path, bbox)
    where bbox is the narrowed segment's extent for coordinate verification."""
    src = GOLDEN / "blinky2"
    pcb = tmp_path / "blinky2.kicad_pcb"
    shutil.copy(src / "blinky2.kicad_pcb", pcb)
    shutil.copy(src / "blinky2.kicad_pro", tmp_path / "blinky2.kicad_pro")
    text = pcb.read_text(encoding="utf-8")
    m = SEG_RE.search(text)
    assert m, "no track segment found in golden"
    sx, sy, ex, ey = (float(m.group(i)) for i in range(1, 5))
    patched = text[:m.start(5)] + "0.05" + text[m.end(5):]
    pcb.write_text(patched, encoding="utf-8")
    bbox = (min(sx, ex), min(sy, ey), max(sx, ex), max(sy, ey))
    return pcb, bbox


# ----------------------------------------------------------- smoke: wrappers

@pytest.mark.smoke
@pytest.mark.parametrize("board", BOARDS)
def test_kc_erc_clean_on_goldens(cli, board):
    r = kc.run_erc(cli, GOLDEN / board / f"{board}.kicad_sch")
    assert r["status"] == "pass", r["violations"]
    assert r["counts"]["total"] == 0


@pytest.mark.smoke
@pytest.mark.parametrize("board", BOARDS)
def test_kc_drc_clean_on_goldens(cli, board):
    r = kc.run_drc(cli, GOLDEN / board / f"{board}.kicad_pcb", parity=True,
                   all_track_errors=True)
    assert r["status"] == "pass", r["violations"]
    assert r["counts"]["total"] == 0
    # normalized schema present even when empty
    assert r["units"] == "mm"


# --------------------------------------------------- smoke: gates on goldens

@pytest.mark.smoke
@pytest.mark.parametrize("board", BOARDS)
def test_gate_erc_passes_goldens(cli, board):
    gates = gate.load_gates(GATES_YAML)
    report = kc.run_erc(cli, GOLDEN / board / f"{board}.kicad_sch")
    assert gate.evaluate("erc", gates["erc"], report)["status"] == "pass"


@pytest.mark.smoke
@pytest.mark.parametrize("board", BOARDS)
def test_gate_drc_routed_passes_goldens(cli, board):
    gates = gate.load_gates(GATES_YAML)
    report = gate.run_report_for_gate(gates["drc_routed"],
                                      GOLDEN / board / f"{board}.kicad_pcb")
    assert gate.evaluate("drc_routed", gates["drc_routed"], report)["status"] == "pass"


# ------------------------------------- smoke: THE acceptance - seeded failure

@pytest.mark.smoke
def test_seeded_drc_violation_fails_gate_with_coords(cli, seeded_drc_board):
    pcb, (x0, y0, x1, y1) = seeded_drc_board
    gates = gate.load_gates(GATES_YAML)
    report = gate.run_report_for_gate(gates["drc"], pcb)
    res = gate.evaluate("drc", gates["drc"], report)
    assert res["status"] == "fail"
    tw = [v for v in res["failing"] if v["check"] == "track_width"]
    assert len(tw) == 1, res["failing"]
    px, py = tw[0]["pos"]
    tol = 0.01
    assert x0 - tol <= px <= x1 + tol and y0 - tol <= py <= y1 + tol, (
        f"violation pos {px},{py} not on narrowed segment bbox {x0,y0,x1,y1}")


@pytest.mark.smoke
def test_gate_cli_exit_codes(cli, seeded_drc_board):
    """The literal acceptance: gate.py exits 1 on the seeded violation, 0 on
    the clean golden schematic."""
    pcb, _ = seeded_drc_board
    gate_py = SCRIPTS / "gate.py"

    fail = subprocess.run(
        [sys.executable, str(gate_py), "--gate", "drc", str(pcb)],
        capture_output=True, text=True, cwd=REPO)
    assert fail.returncode == 1, fail.stderr
    assert json.loads(fail.stdout)["status"] == "fail"

    ok = subprocess.run(
        [sys.executable, str(gate_py), "--gate", "erc",
         str(GOLDEN / "blinky2" / "blinky2.kicad_sch")],
        capture_output=True, text=True, cwd=REPO)
    assert ok.returncode == 0, ok.stderr


# ---------------------------------------------------- smoke: export wrappers

@pytest.mark.smoke
def test_kc_export_wrappers(cli, tmp_path):
    pcb = GOLDEN / "blinky2" / "blinky2.kicad_pcb"
    sch = GOLDEN / "blinky2" / "blinky2.kicad_sch"

    net = kc.export_netlist(cli, sch, tmp_path / "nl.net")
    assert net["status"] == "pass" and net["outputs"]

    pos = kc.export_pos(cli, pcb, tmp_path / "pos.csv", units="mm")
    assert pos["status"] == "pass"
    # confirm mm not inches: a board-space coordinate is tens of mm, ~1 in inches
    rows = (tmp_path / "pos.csv").read_text(encoding="utf-8").splitlines()[1:]
    xs = [abs(float(r.split(",")[3].strip('"'))) for r in rows if r]
    assert max(xs) > 25, "pos coordinates look like inches, not mm"


@pytest.mark.smoke
def test_render_wrapper_multiview(cli, tmp_path):
    import render
    pcb = GOLDEN / "blinky2" / "blinky2.kicad_pcb"
    r = render.render_views(pcb, ["top", "iso"], tmp_path, width=400,
                            height=300, quality="basic")
    assert r["status"] == "pass"
    assert {o["view"] for o in r["outputs"]} == {"top", "iso"}
    for o in r["outputs"]:
        assert Path(o["path"]).exists()


def _fake_loaders(root: Path) -> Path:
    d = root / "usr" / "lib" / "x86_64-linux-gnu" / "kicad" / "plugins" / "3d"
    d.mkdir(parents=True)
    (d / "libs3d_plugin_vrml.so").write_bytes(b"")
    return d


def test_render_env_sets_appdir_for_user_prefix(tmp_path, monkeypatch):
    # KiCad unpacked into a user prefix: loaders live under <prefix>/usr/lib,
    # which kicad-cli only searches with APPDIR=<prefix>.
    monkeypatch.setattr(kc.sys, "platform", "linux")
    monkeypatch.setattr(kc, "_SYS_ROOT", tmp_path / "sysroot")
    monkeypatch.delenv("APPDIR", raising=False)
    prefix = tmp_path / "kicad10"
    _fake_loaders(prefix)
    (prefix / "bin").mkdir()
    env_, err = kc.render_env(prefix / "bin" / "kicad-cli")
    assert err == "" and env_["APPDIR"] == str(prefix)


def test_render_env_inherits_when_system_has_loaders(tmp_path, monkeypatch):
    monkeypatch.setattr(kc.sys, "platform", "linux")
    monkeypatch.setattr(kc, "_SYS_ROOT", tmp_path / "sysroot")
    monkeypatch.delenv("APPDIR", raising=False)
    _fake_loaders(tmp_path / "sysroot")
    assert kc.render_env(Path("/usr/bin/kicad-cli")) == (None, "")


def test_render_env_errors_without_loaders(tmp_path, monkeypatch):
    # no loaders anywhere: the render would be bare, so render_png refuses
    monkeypatch.setattr(kc.sys, "platform", "linux")
    monkeypatch.setattr(kc, "_SYS_ROOT", tmp_path / "sysroot")
    monkeypatch.delenv("APPDIR", raising=False)
    cli = tmp_path / "bare" / "bin" / "kicad-cli"
    env_, err = kc.render_env(cli)
    assert env_ is None and "bare board" in err
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text("(kicad_pcb)", encoding="utf-8")
    r = kc.render_png(cli, pcb, tmp_path / "out" / "t.png")
    assert r["status"] == "error" and "bare board" in r["stderr_tail"]


def test_model_audit_lists_only_unresolved(tmp_path):
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "R.wrl").write_text("", encoding="utf-8")
    (tmp_path / "stock").mkdir()
    (tmp_path / "stock" / "H.step").write_text("", encoding="utf-8")
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(
        '(kicad_pcb (footprint "R" (model "${KIPRJMOD}/lib/R.wrl"))\n'
        ' (footprint "R" (model "lib/R.wrl"))\n'
        ' (footprint "H" (model "${KICAD10_3DMODEL_DIR}/H.step"))\n'
        ' (footprint "U" (model "/nowhere/U.wrl"))\n'
        ' (footprint "C" (model "${UNSET_VAR}/C.wrl")))', encoding="utf-8")
    a = kc.model_audit(pcb, {"KICAD10_3DMODEL_DIR": str(tmp_path / "stock")})
    assert a["referenced"] == 5
    assert a["missing"] == ["${UNSET_VAR}/C.wrl", "/nowhere/U.wrl"]


def _moved_models_ws(tmp_path: Path) -> Path:
    """boards/<b>/kicad/b.kicad_pcb whose R model points into a checkout
    that is gone, while boards/<b>/lib/aiee.3dshapes still has it."""
    ws = tmp_path / "ws"
    lib = ws / "lib" / "aiee.3dshapes"
    lib.mkdir(parents=True)
    for n in ("R0603.wrl", "C0603.wrl"):
        (lib / n).write_text("", encoding="utf-8")
    (ws / "kicad").mkdir()
    pcb = ws / "kicad" / "b.kicad_pcb"
    pcb.write_text(
        '(kicad_pcb (footprint "R" (model "/gone/wt/lib/aiee.3dshapes/R0603.wrl"))\n'
        ' (footprint "C" (model "${KIPRJMOD}/../lib/aiee.3dshapes/C0603.wrl"))\n'
        ' (footprint "U" (model "/nowhere/U.wrl")))', encoding="utf-8")
    return pcb


def test_model_relink_points_moved_models_at_workspace_lib(tmp_path):
    pcb = _moved_models_ws(tmp_path)
    before = pcb.read_bytes()
    text, relinked = kc.model_relink(pcb, {})
    lib = (tmp_path / "ws" / "lib" / "aiee.3dshapes").resolve()
    assert relinked == {"/gone/wt/lib/aiee.3dshapes/R0603.wrl":
                        str(lib / "R0603.wrl")}
    # every resolvable path goes absolute, so the copy renders from any dir;
    # an unresolvable one stays as written
    assert f'(model "{lib / "R0603.wrl"}")' in text
    assert f'(model "{lib / "C0603.wrl"}")' in text
    assert '(model "/nowhere/U.wrl")' in text
    assert pcb.read_bytes() == before
    (tmp_path / "ok.kicad_pcb").write_text(
        '(kicad_pcb (footprint "U" (model "/nowhere/U.wrl")))', encoding="utf-8")
    assert kc.model_relink(tmp_path / "ok.kicad_pcb", {}) == (None, {})


def test_render_png_renders_a_relinked_copy(tmp_path, monkeypatch):
    # the board file is never written; kicad-cli gets a temp copy that is
    # gone after the render
    pcb = _moved_models_ws(tmp_path)
    before = pcb.read_bytes()
    seen = {}

    def fake_cli(cli, args, timeout=0, env=None):
        src = Path(args[-1])
        seen["src"], seen["text"] = src, src.read_text(encoding="utf-8")
        Path(args[args.index("-o") + 1]).write_bytes(b"png")
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(kc, "render_env", lambda cli: (None, ""))
    monkeypatch.setattr(kc, "run_cli", fake_cli)
    out = tmp_path / "out" / "b_top.png"
    r = kc.render_png(Path("kicad-cli"), pcb, out)
    assert r["status"] == "pass" and r["input"] == str(pcb)
    assert seen["src"] != pcb and not seen["src"].exists()
    assert "/gone/" not in seen["text"]
    assert r["models_relinked"] == ["/gone/wt/lib/aiee.3dshapes/R0603.wrl"]
    assert r["models_missing"] == ["/nowhere/U.wrl"]
    assert pcb.read_bytes() == before
    assert [p.name for p in out.parent.iterdir()] == ["b_top.png"]
