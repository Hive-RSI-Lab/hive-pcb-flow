"""#857: `gate --summary`, the short findings print that replaces pinned
lite-main's tools/drc.sh.

Each planted fault has a quiet twin beside it: the fault makes the summary
wrong in one way, and the twin is the nearby case that must stay as it is.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
GOLDEN = REPO / "tests" / "golden"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))
import env  # noqa: E402
import gate  # noqa: E402

OUTLINE_PCB = """(kicad_pcb (version 20240108)
  (gr_line (start 100 50) (end 140 50) (stroke (width 0.05) (type default))
    (layer "Edge.Cuts"))
  (gr_line (start 140 50) (end 140 80) (layer "Edge.Cuts"))
  (gr_line (start 140 80) (end 100 80) (layer "Edge.Cuts"))
  (gr_line (start 100 80) (end 100 50) (layer "Edge.Cuts"))
  (gr_line (start 90 10) (end 95 12) (layer "F.SilkS"))
)
"""
NO_OUTLINE_PCB = """(kicad_pcb (version 20240108)
  (gr_line (start 90 10) (end 95 12) (layer "F.SilkS"))
)
"""


def _finding(check, pos=(102.5, 53.25), net="+5V", refs=("U1",),
             msgs=("Track [+5V] on F.Cu",), severity="error"):
    return {"check": check, "severity": severity,
            "pos": list(pos) if pos is not None else None,
            "layer": "F.Cu", "net": net, "refs": list(refs),
            "msg": f"{check} finding",
            "items": [{"msg": m, "pos": list(pos) if pos else None,
                       "uuid": None} for m in msgs]}


def _report(violations):
    return {"script": "kc", "status": "pass", "input": "x.kicad_pcb",
            "input_digest": None,
            "counts": {"total": len(violations)}, "violations": violations}


GATE = {"tool": "drc", "phase": "route", "fail_severities": ["error"],
        "max_count": 0}


def _result(violations, waivers=None):
    return gate.evaluate("drc_routed", GATE, _report(violations),
                         waivers=waivers)


def _finding_lines(lines):
    return [ln for ln in lines if ln.startswith("  ") and "|" in ln]


# ----------------------------------------------------------- origin

def test_positions_are_from_the_outline_top_left(tmp_path):
    """Fault: absolute positions. The finding at (102.5, 53.25) on a board
    whose outline starts at (100, 50) prints as (2.50, 3.25)."""
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(OUTLINE_PCB, encoding="utf-8")
    origin, note = gate.summary_origin(pcb)
    assert origin == [100.0, 50.0]
    assert "(100, 50)" in note
    lines = gate.summary_lines(_result([_finding("clearance")]), origin, note)
    assert "at (2.50, 3.25)" in _finding_lines(lines)[0]


def test_twin_no_outline_stays_absolute_and_says_so(tmp_path):
    """Quiet twin: a board with no Edge.Cuts prints absolute positions,
    and the positions line says they are absolute."""
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(NO_OUTLINE_PCB, encoding="utf-8")
    origin, note = gate.summary_origin(pcb)
    assert origin is None and note.startswith("absolute")
    lines = gate.summary_lines(_result([_finding("clearance")]), origin, note)
    assert lines[1] == "positions: absolute (no Edge.Cuts outline)"
    assert "at (102.50, 53.25)" in _finding_lines(lines)[0]


def test_twin_schematic_input_has_no_origin(tmp_path):
    origin, note = gate.summary_origin(tmp_path / "x.kicad_sch")
    assert origin is None and note == "absolute (no board input)"
    assert gate.summary_origin(None)[0] is None


def test_origin_matches_board_init_on_pinned_lite_main():
    """The origin is the corner drc.sh read from board-init.json: on pinned
    lite-main they agree (read only, nothing written)."""
    ws = REPO / "pinned" / "lite-main"
    if not (ws / "reports" / "board-init.json").is_file():
        pytest.skip("pinned lite-main not present")
    want = json.loads((ws / "reports" / "board-init.json").read_text(
        encoding="utf-8"))["outline_origin"]
    origin, _ = gate.summary_origin(ws / "kicad" / "lite-main.kicad_pcb")
    assert origin == pytest.approx(want, abs=1e-3)


# ----------------------------------------------------------- silk

def test_silk_is_counted_but_not_listed():
    """Fault: silk findings listed one per line (drc.sh left them out)."""
    res = _result([_finding("silk_overlap"), _finding("silk_over_copper"),
                   _finding("clearance")])
    lines = gate.summary_lines(res, None, "absolute")
    found = _finding_lines(lines)
    assert len(found) == 1 and found[0].startswith("  clearance")
    assert lines[-1] == "  (2 silk findings not listed)"


def test_count_by_check_includes_silk():
    """Fault: the first line counts only the listed findings."""
    res = _result([_finding("silk_overlap"), _finding("silk_overlap"),
                   _finding("clearance")])
    head = gate.summary_lines(res, None, "absolute")[0]
    assert head == "drc_routed: FAIL, 3 failing (silk_overlap 2, clearance 1)"


def test_twin_non_silk_check_named_like_silk_is_listed():
    """Quiet twin: only checks that START with silk are left out."""
    res = _result([_finding("courtyards_overlap_silk")])
    lines = gate.summary_lines(res, None, "absolute")
    assert len(_finding_lines(lines)) == 1
    assert "not listed" not in "\n".join(lines)


# ----------------------------------------------------------- one finding

def test_posless_finding_prints_pos_unknown():
    """Fault: a finding without pos (unconnected pad lists, rules-file
    findings) crashes the summary or prints (0, 0) as drc.sh did."""
    res = _result([_finding("rules_file_ignored", pos=None, net=None,
                            refs=())])
    ln = _finding_lines(gate.summary_lines(res, [100.0, 50.0], "x"))[0]
    assert "at pos ?" in ln and "net -" in ln and "refs -" in ln


def test_every_item_message_is_shown_and_cut():
    """Fault: only the first item's message, or messages uncut."""
    long = "Pad 2 [/MCO] of U1 on B.Cu " + "x" * 80
    res = _result([_finding("clearance", refs=("U1", "R3"),
                            msgs=("Track [+5V] on F.Cu", long))])
    ln = _finding_lines(gate.summary_lines(res, None, "absolute"))[0]
    parts = ln.split(" | ")
    assert parts[1] == "Track [+5V] on F.Cu"
    assert parts[2] == long[:gate.SUMMARY_MSG_CHARS]
    assert "refs U1,R3" in ln and "net +5V" in ln


def test_twin_itemless_finding_shows_its_own_message():
    f = _finding("clearance")
    f["items"] = []
    ln = _finding_lines(gate.summary_lines(_result([f]), None, "a"))[0]
    assert ln.endswith("| clearance finding")


# ----------------------------------------------------------- waivers

def test_waived_findings_are_counted_not_listed():
    """Fault: a waived finding is listed as failing, or vanishes unsaid."""
    w = [{"check": "clearance", "net": "+5V", "reason": "ok by review",
          "approved": "a492e 2026-11-16"}]
    res = _result([_finding("clearance"), _finding("track_width",
                                                    net="GND")], waivers=w)
    lines = gate.summary_lines(res, None, "absolute")
    found = _finding_lines(lines)
    assert len(found) == 1 and found[0].startswith("  track_width")
    assert lines[0] == "drc_routed: FAIL, 1 failing (track_width 1)"
    assert lines[-1] == "  (1 waived)"


def test_twin_pass_is_two_lines():
    lines = gate.summary_lines(_result([]), None, "absolute")
    assert lines == ["drc_routed: PASS, 0 failing", "positions: absolute"]


# ----------------------------------------------------------- the CLI

@pytest.fixture
def run_main(monkeypatch, tmp_path, capsys):
    """gate.main with the kc report replaced by a canned one."""
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(OUTLINE_PCB, encoding="utf-8")
    gates = tmp_path / "gates.yaml"
    gates.write_text("gates:\n  drc_routed:\n    tool: drc\n"
                     "    phase: route\n    fail_severities: [error]\n"
                     "    max_count: 0\n",
                     encoding="utf-8")

    def go(violations, *extra):
        monkeypatch.setattr(gate, "run_report_for_gate",
                            lambda g, p: _report(violations))
        rc = gate.main(["--gate", "drc_routed", str(pcb), "--gates",
                        str(gates), "--no-record", *extra])
        out = capsys.readouterr()
        return rc, out.out, out.err
    return go, tmp_path


def test_cli_summary_prints_text_and_still_writes_out(run_main):
    """Fault: --summary drops the --out JSON, or prints the JSON too."""
    go, tmp = run_main
    out_json = tmp / "reports" / "drc_routed-gate.json"
    rc, out, err = go([_finding("clearance")], "--summary", "--out",
                      str(out_json))
    assert rc == 1
    assert out.splitlines()[0] == "drc_routed: FAIL, 1 failing (clearance 1)"
    assert "at (2.50, 3.25)" in out
    assert not out.lstrip().startswith("{")
    assert json.loads(out_json.read_text(encoding="utf-8"))["status"] == "fail"
    assert "gate drc_routed: FAIL" in err  # the stderr line is unchanged


def test_twin_cli_without_summary_prints_json(run_main):
    """Quiet twin: without --summary stdout is still the JSON, and a pass
    still exits 0."""
    go, _ = run_main
    rc, out, _ = go([])
    assert rc == 0 and json.loads(out)["status"] == "pass"
    rc, out, _ = go([], "--summary")
    assert rc == 0 and out.splitlines()[0] == "drc_routed: PASS, 0 failing"
    assert "{" not in out  # the summary only, no JSON after it


# ----------------------------------------------------------- real kicad-cli

SEG_RE = re.compile(
    r"\(segment\s+\(start (-?[\d.]+) (-?[\d.]+)\)\s+"
    r"\(end (-?[\d.]+) (-?[\d.]+)\)\s+\(width ([\d.]+)")


@pytest.mark.smoke
def test_seeded_track_width_summary_on_real_drc(tmp_path):
    """A track narrowed to 0.05 mm on golden blinky2: the summary names
    track_width, its net, and a position on the narrowed segment measured
    from the outline's top-left."""
    if env.find_kicad_cli() is None:
        pytest.skip("kicad-cli not installed")
    src = GOLDEN / "blinky2"
    pcb = tmp_path / "blinky2.kicad_pcb"
    shutil.copy(src / "blinky2.kicad_pro", tmp_path / "blinky2.kicad_pro")
    text = (src / "blinky2.kicad_pcb").read_text(encoding="utf-8")
    m = SEG_RE.search(text)
    sx, sy, ex, ey = (float(m.group(i)) for i in range(1, 5))
    pcb.write_text(text[:m.start(5)] + "0.05" + text[m.end(5):],
                   encoding="utf-8")
    origin, _ = gate.summary_origin(pcb)
    assert origin is not None
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "gate.py"), "--gate", "drc", str(pcb),
         "--no-record", "--summary"],
        capture_output=True, text=True, cwd=REPO)
    assert proc.returncode == 1, proc.stderr
    lines = proc.stdout.splitlines()
    assert lines[0].startswith("drc: FAIL") and "track_width 1" in lines[0]
    tw = [ln for ln in lines if ln.startswith("  track_width")]
    assert len(tw) == 1
    u, v = (float(n) for n in
            re.search(r"at \((-?[\d.]+), (-?[\d.]+)\)", tw[0]).groups())
    tol = 0.02
    assert min(sx, ex) - origin[0] - tol <= u <= max(sx, ex) - origin[0] + tol
    assert min(sy, ey) - origin[1] - tol <= v <= max(sy, ey) - origin[1] + tol
