"""Design-document generator acceptance tests (report_gen.py + env.find_pdflatex,
report_gen.find_lualatex).

Criteria -> tests:
  - latex_escape is a total function: specials, backslash handled first,
    each mapped non-ASCII char, CJK fallback to '?', pure-ASCII invariant
                                     -> test_escape_* (hermetic)
  - markdown-lite ceiling: headings, nested bullets, inline marks, md tables
    and fences pass through as escaped tt blocks, snake_case never emphasized
                                     -> test_md_* (hermetic)
  - sections conditional on phase (pending stubs, exit 0)
                                     -> test_sections_pending_at_p4
  - due-but-absent core artifact -> missing[] + exit 1
                                     -> test_missing_core_exits_1
  - payload key contract             -> test_payload_keys
  - absent workspace / corrupt state.json -> exit 2
                                     -> test_absent_workspace_exit2,
                                        test_corrupt_state_exit2
  - --tex-only is full success (pdf null, ASCII .tex)
                                     -> test_tex_only_pass
  - lualatex or the house style not installed -> auto tex-only, warning,
    exit 1                           -> test_no_lualatex_degrades,
                                        test_no_house_style_degrades
  - set-but-invalid HWDE_LUALATEX -> EnvError -> exit 2, .tex still written
                                     -> test_bad_pin_exit2
  - [ ] * brace-wrapped: a line-initial [ after the line-join is a fatal
    "Missing number", item labels swallow [x] content (adversarial F1/F2)
                                     -> test_escape_bracket_star_contexts,
                                        test_bracket_content_never_breaks_latex
  - compile failure/timeout explains itself in warnings (adversarial F5)
                                     -> test_compile_failure_adds_warning
  - env.find_pdflatex override ladder (check_env's) -> test_find_pdflatex_*
  - report_gen.find_lualatex ladder, house style by path
                                     -> test_find_lualatex_*, test_house_style_*
  - check_env lualatex + house style check (warn-level, loud on bad pin)
                                     -> test_check_env_lualatex_unit
  - real boards produce a real PDF (both render conventions), >= 8 pages via
    pypdf, sections included, zero residue outside reports/design_doc/,
    second run overwrites cleanly    -> test_smoke_* (smoke marker)

Hermetic tests never invoke lualatex (all synthetic runs are --tex-only or
have discovery monkeypatched/failed), so they pass on TeX-less machines.
"""
from __future__ import annotations

import json
import os
import re
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import check_env  # noqa: E402
import report_gen  # noqa: E402
from lib import env  # noqa: E402
from _boards import copy_board  # noqa: E402

TS = "2026-07-28T10:00:00"


# ------------------------------------------------------------------ helpers

def run_main(argv, tmp_path, capsys, name="out"):
    """Run report_gen.main() in-process; return (exit_code, payload_dict)."""
    out = tmp_path / f"{name}.json"
    code = report_gen.main([*argv, "--out", str(out)])
    cap = capsys.readouterr()
    if out.exists():
        payload = json.loads(out.read_text(encoding="utf-8"))
    else:
        payload = json.loads(cap.out) if cap.out.strip() else None
    return code, payload


def write_png(path: Path) -> None:
    """A minimal valid 1x1 RGB PNG (no imaging lib needed)."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\xff\x00\x00")
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
                     + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


def gate_entry(phase: str) -> dict:
    last = {"ts": TS, "status": "pass", "failing_count": 0, "total": 0}
    return {"phase": phase, "status": "pass", "attempts": 1,
            "last": dict(last), "history": [dict(last)]}


BRIEF_MD = """# Brief: synth

Overview paragraph with **bold**, *stars*, _under_, `code_span` and a
[link](https://example.com/x_y). Tolerance \u00b110%, load 4.7\u03a9, cap 1\u00b5F.

- top bullet, rated -40\u2103~+85\u2103
  - nested bullet \u0394T 10\u00b0
- second top bullet

| col_a | col_b |
|-------|-------|
| 1     | 2     |

```
raw & unparsed_stuff ~ ^ 100%
```
"""


def make_workspace(tmp_path: Path, phase: str = "P10",
                   schematic: bool = True) -> Path:
    """Synthetic workspace matching the real state.json schema + artifact
    conventions closely enough to drive every section builder."""
    ws = tmp_path / "synth"
    board = "synth"
    gates = {"erc": gate_entry("P4")}
    if phase in ("P6", "P7", "P8", "P9", "P10", "done"):
        gates.update({"place": gate_entry("P6"), "drc_routed": gate_entry("P7"),
                      "verify": gate_entry("P8"), "dfm": gate_entry("P9")})
    state = {
        "version": 1, "board": board,
        "workspace": str(ws).replace("\\", "/"),
        "created": TS, "updated": TS, "phase": phase,
        "gates": gates,
        "human": {"1": {"status": "approved", "ts": TS,
                        "note": "note & ~caveat_with 100% margin"}},
        "artifacts": {"order": "fab/order.json"},
        "open_issues": [{
            "id": 1, "gate": None, "phase": "P8", "fixer": "review",
            "net": "GND", "kinds": ["current-return", "cu_neck"],
            "severity": "error", "count": 1, "region": None,
            "work_order": None, "status": "fixed", "agent": None,
            "attempts": 0, "opened": TS, "closed": TS}],
        "next_issue_id": 2,
        "budgets": {"fix_loops": {"erc": 3}},
        "decisions": [{"what": "2oz copper & pour_fanin for the 5A path",
                       "why": "IPC-2152 at dT=10; 1oz needs >3.5mm",
                       "phase": "P2", "ts": TS}],
        "history": [{"ts": TS, "event": "init", "board": board, "phase": "P0"}],
    }
    ws.mkdir()
    (ws / "state.json").write_text(json.dumps(state, indent=1), encoding="utf-8")

    (ws / "brief").mkdir()
    (ws / "brief" / "brief.md").write_text(BRIEF_MD, encoding="utf-8")
    (ws / "requirements.md").write_text(
        "# Requirements: synth\n\n## 1. Function\n\nDoes a thing at 5A "
        "\u00b110% with `check_current.required_width_mm` honored.\n",
        encoding="utf-8")
    arch = ws / "architecture"
    arch.mkdir()
    (arch / "stackup.md").write_text(
        "# synth - stackup\n\n## Chosen: `SYN2313_1.6_2oz` (2-layer, 2 oz)\n",
        encoding="utf-8")
    (arch / "sheets.md").write_text(
        "# synth - sheet plan\n\n| Sheet | File |\n|---|---|\n| root | x |\n",
        encoding="utf-8")
    for n in ("blocks.md", "decisions.md", "power_tree.md"):
        (arch / n).write_text(f"# {n}\n\ncontent\n", encoding="utf-8")

    log = ws / "log"
    log.mkdir()
    top = {"P4": 4, "P10": 10}.get(phase, 10)
    for i in range(0, top + 1):
        (log / f"P{i}-digest.md").write_text(
            f"# P{i} digest (synth)\nDid work with \u00b12% margins & "
            "100% coverage_of the plan.\n", encoding="utf-8")

    rep = ws / "reports"
    rep.mkdir()
    (rep / "erc-waivers.md").write_text(
        "# Schematic review waivers (synth)\n\nW1 range: -40\u2103~+85\u2103 "
        "accepted; see notes_x.\n", encoding="utf-8")
    if schematic:
        (rep / "schematic.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    rf = rep / "render_final"
    rf.mkdir()
    write_png(rf / "top.png")
    write_png(rf / "bottom.png")
    (rep / "review-board.md").write_text(
        "# Board review (synth)\n\nVerdict: fine.\n\n### E1 - a finding\n\n"
        "Detail with `check_current.required_width_mm` and 50% margin.\n",
        encoding="utf-8")
    (rep / "verify_all.json").write_text(json.dumps({
        "script": "verify_all", "board": "synth.kicad_pcb", "status": "pass",
        "counts": {"total": 0},
        "checks": {
            "check_silk": {"status": "pass", "counts": {"total": 0},
                           "report": None, "reason": None},
            "check_current": {"status": "skipped", "counts": {"total": 0},
                              "report": None, "reason": "no constraints"}},
        "violations": []}), encoding="utf-8")
    (rep / "bom_cpl.json").write_text(json.dumps({
        "script": "bom_cpl", "status": "pass", "bom_rows": [
            {"Comment": "10uF \u00b110%", "Designator": "C1A,C1B",
             "Footprint": "ESSOP-10_L4.9-W3.9-P1.0-LS6.0-TL-EP",
             "LCSC": "C13585"},
            {"Comment": "-40\u2103~+85\u2103 \u6df1\u5733",
             "Designator": "U1",
             "Footprint": "F.0603.00011/P2-0603R4TS2-06T-001",
             "LCSC": "C970725"}],
        "n_rotation_corrections": 2, "missing_lcsc": []}), encoding="utf-8")
    (rep / "fab_export.json").write_text(json.dumps({
        "script": "fab_export", "status": "pass",
        "layers_exported": ["F.Cu", "B.Cu", "Edge.Cuts"]}), encoding="utf-8")
    (rep / "gate-dfm.json").write_text(json.dumps({
        "script": "gate", "gate": "dfm", "status": "pass",
        "counts": {"total": 3, "by_severity": {"warning": 3}}}),
        encoding="utf-8")

    fab = ws / "fab"
    fab.mkdir()
    (fab / "order.json").write_text(json.dumps({
        "script": "order_submit", "status": "ready_for_human",
        "board": "synth.kicad_pcb",
        "spec_snapshot": {"layers": 2, "width_mm": 30.0, "height_mm": 20.0,
                          "qty": 10, "surface_finish": "HASL",
                          "solder_mask_color": "green", "assembly": True},
        "quote": {"selected": {"qty": 10, "total": 12.34, "unit_cost": 1.23},
                  "estimated": True,
                  "source": "boards\\synth\\fab\\quote.json"},
        "artifacts": {"gerber_zip": {
            "path": "boards\\synth\\fab\\synth_gerbers.zip",
            "sha256": "abcd" * 16, "bytes": 1}},
        "human_steps": [
            "Select 2oz copper ~ mandatory (\u00b10 tolerance on this)",
            "Upload boards\\synth\\fab\\synth_gerbers.zip to jlcdfm.com"]}),
        encoding="utf-8")
    return ws


def sections_by_name(payload: dict) -> dict:
    return {s["name"]: s["status"] for s in payload["sections"]}


# ------------------------------------------------------------------ latex_escape

def test_escape_specials():
    esc = report_gen.latex_escape
    assert esc("&") == r"\&" and esc("%") == r"\%" and esc("$") == r"\$"
    assert esc("#") == r"\#" and esc("_") == r"\_"
    assert esc("{") == r"\{" and esc("}") == r"\}"
    assert esc("~") == r"\textasciitilde{}"
    assert esc("^") == r"\textasciicircum{}"
    # OT1 text mode renders raw < > | as inverted punctuation / em-dash
    assert esc("->") == r"-\textgreater{}"
    assert esc("<") == r"\textless{}"
    assert esc("|") == r"\textbar{}"


def test_escape_backslash_first():
    esc = report_gen.latex_escape
    assert esc("\\") == r"\textbackslash{}"
    # the braces introduced FOR the backslash must not get re-escaped
    assert esc("a\\b") == r"a\textbackslash{}b"
    assert esc("\\&") == r"\textbackslash{}\&"
    assert esc("boards\\pd-trigger\\fab\\x.zip") == \
        r"boards\textbackslash{}pd-trigger\textbackslash{}fab\textbackslash{}x.zip"


def test_escape_unicode_map():
    esc = report_gen.latex_escape
    assert esc("\u00b1") == r"\(\pm\)"
    assert esc("\u03a9") == r"\(\Omega\)"
    assert esc("\u00b5") == r"\(\mu\)"      # micro sign U+00B5
    assert esc("\u03bc") == r"\(\mu\)"      # greek mu U+03BC
    assert esc("\u2103") == r"\(^{\circ}\)C"  # single-glyph degree C
    assert esc("\u00b0") == r"\(^{\circ}\)"
    assert esc("\u2022") == r"\textbullet{}"
    assert esc("\u0394") == r"\(\Delta\)"
    assert esc("\u00d8") == "dia. "


def test_escape_cjk_fallback():
    assert report_gen.latex_escape("\u6df1\u5733\u5e02") == "???"


def test_escape_corpus_torture():
    esc = report_gen.latex_escape
    assert esc("unconnected-(U1-PC15-OSC32_OUT-Pad4)") == \
        r"unconnected-(U1-PC15-OSC32\_OUT-Pad4)"
    assert esc("ESSOP-10_L4.9-W3.9-P1.0-LS6.0-TL-EP") == \
        r"ESSOP-10\_L4.9-W3.9-P1.0-LS6.0-TL-EP"
    # non-ASCII glyph + active ~ in ONE attribute string
    assert esc("-40\u2103~+85\u2103") == \
        r"-40\(^{\circ}\)C\textasciitilde{}+85\(^{\circ}\)C"
    assert esc("F.0603.00011/P2-0603R4TS2-06T-001") == \
        "F.0603.00011/P2-0603R4TS2-06T-001"


def test_escape_bracket_star_contexts():
    """F1/F2: [ ] * are context-sensitive after \\\\ (optional arg / no-break
    star) and inside \\item (label); brace-wrapping is literal everywhere."""
    esc = report_gen.latex_escape
    assert esc("[ok] verified") == "{[}ok{]} verified"
    assert esc("[NRND] 22uF") == "{[}NRND{]} 22uF"
    assert esc("a[5mm]b") == "a{[}5mm{]}b"
    assert esc("*caveat*") == "{*}caveat{*}"


def test_escape_ascii_invariant():
    probe = ("".join(chr(i) for i in range(1, 0x300))
             + "\u2103\u2126\u6df1\u5733 mixed & _ ~ \\ text")
    out = report_gen.latex_escape(probe)
    assert all(ord(c) < 128 for c in out)
    assert report_gen.latex_escape(None) == ""
    assert report_gen.latex_escape(42) == "42"


# ------------------------------------------------------------------ markdown-lite

def test_chosen_stackup_both_heading_shapes():
    """Bare `## Chosen stackup` heading must not crash (buck-5v3a P2).

    order_submit.derive_copper_oz's scan window pushes architects to keep the
    heading bare and put the id on the line below; the old split(":", 1)[1]
    raised IndexError on exactly that shape.
    """
    inline = "# t\n\n## Chosen stackup: JLC04162H-7628A\n\nprose\n"
    assert report_gen._chosen_stackup(inline) == "JLC04162H-7628A"
    below = "# t\n\n## Chosen stackup\n\n**`JLC04162H-7628A`** - 4 layer\n"
    assert report_gen._chosen_stackup(below).startswith("**`JLC04162H")
    # value-less heading, and no heading at all -> None, still no crash
    assert report_gen._chosen_stackup("## Chosen stackup\n\n### next\n") is None
    assert report_gen._chosen_stackup("# t\n\nno stackup here\n") is None


def test_md_headings():
    tex = report_gen.md_to_latex("# A\n\n## B\n\n### C\n")
    assert r"\subsection*{A}" in tex
    assert r"\subsubsection*{B}" in tex
    assert r"\paragraph*{C}" in tex


def test_md_bullets_nested():
    tex = report_gen.md_to_latex("- a\n  - b\n- c\n\npara\n")
    assert tex.count(r"\begin{itemize}") == 2
    assert tex.count(r"\end{itemize}") == 2
    assert tex.count(r"\item") == 3
    # lists closed before the trailing paragraph
    assert tex.rindex(r"\end{itemize}") < tex.rindex("para")


def test_md_inline():
    tex = report_gen.md_to_latex(
        "**bold** *star* _under_ `code_x` [txt](http://u/p_q)")
    assert r"\textbf{bold}" in tex
    assert r"\emph{star}" in tex
    assert r"\emph{under}" in tex
    assert r"\texttt{code\_x}" in tex
    assert r"txt (\texttt{http://u/p\_q})" in tex


def test_md_snake_case_not_emphasized():
    tex = report_gen.md_to_latex(
        "state_snapshots and check_current.required_width_mm stay plain")
    assert r"\emph" not in tex
    assert r"state\_snapshots" in tex


def test_md_table_passthrough():
    tex = report_gen.md_to_latex("| col_a | col_b |\n|---|---|\n| 1 | 2 |\n")
    assert r"\ttfamily" in tex          # rendered as tt block
    assert r"\begin{tabular}" not in tex  # never parsed into a table
    assert r"col\_a" in tex             # content escaped


def test_md_fence_passthrough():
    tex = report_gen.md_to_latex("```\nraw & stuff_here ~\n```\n")
    assert r"\ttfamily" in tex
    assert r"raw \& stuff\_here \textasciitilde{}" in tex


# ------------------------------------------------------------------ synthetic runs

def test_tex_only_pass(tmp_path, capsys):
    ws = make_workspace(tmp_path)
    code, payload = run_main(["--workspace", str(ws), "--tex-only"],
                             tmp_path, capsys)
    assert code == 0, payload
    assert payload["status"] == "pass"
    assert payload["pdf"] is None and payload["pages"] is None
    assert payload["compile"] is None
    assert payload["missing"] == []
    tex = ws / payload["tex"]
    assert tex.is_file()
    text = tex.read_text(encoding="utf-8")
    assert all(ord(c) < 128 for c in text)          # hard ASCII invariant
    assert r"\(\pm\)" in text                       # torture survived escaped
    assert "??" in text                             # CJK fell back
    assert "skipped - no constraints" in text       # honest skip reason
    assert r"ESSOP-10\_L4.9-W3.9-P1.0-LS6.0-TL-EP" in text
    assert r"\includepdf[pages=-]{reports/schematic.pdf}" in text
    assert "reports/render_final/top.png" in text
    assert "SYN2313" in text                        # stackup Chosen line
    assert sections_by_name(payload) == {
        s: "included" for s in ("title", "overview", "requirements",
                                "architecture", "schematic", "layout",
                                "verification", "dfm_fab", "run_record",
                                "artifact_index")}


def test_sections_pending_at_p4(tmp_path, capsys):
    ws = make_workspace(tmp_path, phase="P4", schematic=False)
    code, payload = run_main(["--workspace", str(ws), "--tex-only"],
                             tmp_path, capsys)
    assert code == 0, payload         # nothing hard-due yet -> pass
    assert payload["missing"] == []
    by_name = sections_by_name(payload)
    assert by_name["layout"] == "pending"
    assert by_name["verification"] == "pending"
    assert by_name["dfm_fab"] == "pending"
    assert by_name["requirements"] == "included"
    assert by_name["architecture"] == "included"
    text = (ws / payload["tex"]).read_text(encoding="utf-8")
    assert "Pending \\textemdash{} produced at P6" in text


def test_missing_core_exits_1(tmp_path, capsys):
    ws = make_workspace(tmp_path, phase="P10", schematic=False)
    code, payload = run_main(["--workspace", str(ws), "--tex-only"],
                             tmp_path, capsys)
    assert code == 1
    assert payload["status"] == "violations"
    assert "reports/schematic.pdf" in payload["missing"]
    assert sections_by_name(payload)["schematic"] == "missing"
    assert (ws / payload["tex"]).is_file()   # doc still produced


def test_payload_keys(tmp_path, capsys):
    ws = make_workspace(tmp_path)
    _, payload = run_main(["--workspace", str(ws), "--tex-only"],
                          tmp_path, capsys)
    assert list(payload) == ["script", "status", "board", "workspace", "tex",
                             "pdf", "pages", "sections", "missing", "warnings",
                             "compile", "filed", "unchanged", "attached", "kind"]
    assert payload["script"] == "report_gen"
    assert payload["board"] == "synth"
    assert all(set(s) == {"name", "status", "source"}
               for s in payload["sections"])


def test_absent_workspace_exit2(tmp_path, capsys):
    code, payload = run_main(["--workspace", str(tmp_path / "nope"),
                              "--tex-only"], tmp_path, capsys)
    assert code == 2
    assert payload["status"] == "error"
    assert "state.json" in payload["error"]


def test_corrupt_state_exit2(tmp_path, capsys):
    ws = tmp_path / "corrupt"
    ws.mkdir()
    (ws / "state.json").write_text("{not json", encoding="utf-8")
    code, payload = run_main(["--workspace", str(ws), "--tex-only"],
                             tmp_path, capsys)
    assert code == 2
    assert payload["status"] == "error"


def test_no_lualatex_degrades(tmp_path, capsys, monkeypatch):
    ws = make_workspace(tmp_path)
    monkeypatch.setattr(report_gen, "find_lualatex", lambda: None)
    code, payload = run_main(["--workspace", str(ws)], tmp_path, capsys)
    assert code == 1
    assert payload["status"] == "violations"
    assert payload["pdf"] is None and payload["compile"] is None
    assert any("lualatex not installed" in w for w in payload["warnings"])
    assert (ws / payload["tex"]).is_file()   # tex still written


def test_no_house_style_degrades(tmp_path, capsys, monkeypatch):
    ws = make_workspace(tmp_path)
    fake = tmp_path / "lualatex"
    fake.write_bytes(b"x")
    monkeypatch.setattr(report_gen, "find_lualatex", lambda: fake)
    monkeypatch.setenv("HWDE_HOUSE_STYLE", str(tmp_path / "nowhere"))
    monkeypatch.setattr(report_gen, "compile_pdf",
                        lambda *a: pytest.fail("compiled without a style"))
    code, payload = run_main(["--workspace", str(ws)], tmp_path, capsys)
    assert code == 1 and payload["compile"] is None
    assert any("house style not found" in w for w in payload["warnings"])
    assert (ws / payload["tex"]).is_file()


def test_tex_uses_house_style(tmp_path, capsys):
    ws = make_workspace(tmp_path)
    code, payload = run_main(["--workspace", str(ws), "--tex-only"],
                             tmp_path, capsys)
    assert code == 0, payload
    text = (ws / payload["tex"]).read_text(encoding="utf-8")
    assert r"\usepackage[nodiagramkit]{housestyle}" in text
    assert r"\hstitleblock{" in text and r"\hsslug{" in text
    assert "geometry" not in text and r"\toprule" not in text


def test_bad_pin_exit2(tmp_path):
    ws = make_workspace(tmp_path)
    e = dict(os.environ)
    e["HWDE_LUALATEX"] = r"C:\nonexistent\lualatex.exe"
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "report_gen.py"),
         "--workspace", str(ws)],
        capture_output=True, text=True, env=e, timeout=120, cwd=str(REPO))
    assert r.returncode == 2, r.stdout + r.stderr
    payload = json.loads(r.stdout)
    assert payload["status"] == "error"
    assert "HWDE_LUALATEX" in payload["error"]
    # F4: the loud exit must not discard the already-built document
    assert (ws / "reports" / "design_doc" / "synth-design-doc.tex").is_file()


def test_bracket_content_never_breaks_latex(tmp_path, capsys):
    """F1/F2 regression: [-initial digest lines, [NRND] BOM comments,
    task-list bullets and [optional] human_steps survive brace-wrapped."""
    ws = make_workspace(tmp_path)
    (ws / "log" / "P8-digest.md").write_text(
        "# P8 digest (synth)\n[ok] verified all rails\n*starred caveat*\n",
        encoding="utf-8")
    bom = json.loads((ws / "reports" / "bom_cpl.json")
                     .read_text(encoding="utf-8"))
    bom["bom_rows"][0]["Comment"] = "[NRND] 22uF low-ESR"
    (ws / "reports" / "bom_cpl.json").write_text(json.dumps(bom),
                                                 encoding="utf-8")
    order = json.loads((ws / "fab" / "order.json").read_text(encoding="utf-8"))
    order["human_steps"].insert(0, "[optional] flag the assembly side")
    (ws / "fab" / "order.json").write_text(json.dumps(order), encoding="utf-8")
    (ws / "brief" / "brief.md").write_text(
        "# Brief: synth\n\n- [x] shipped\n- [ ] pending\n", encoding="utf-8")
    code, payload = run_main(["--workspace", str(ws), "--tex-only"],
                             tmp_path, capsys)
    assert code == 0, payload
    text = (ws / payload["tex"]).read_text(encoding="utf-8")
    assert "{[}ok{]} verified all rails" in text
    assert "{[}NRND{]} 22uF low-ESR" in text
    assert r"\item {[}optional{]}" in text
    assert r"\item {[}x{]} shipped" in text
    # the fatal/swallowing raw forms must not exist anywhere
    assert "\\\\\n[" not in text and "\\\\\n*" not in text
    assert "\\item [" not in text


def test_requirements_fallback_architecture(tmp_path, capsys):
    """T6 reportgen-fallback: an off-root requirements.md (the shipped
    carrier escape) is included via the architecture/ fallback with a
    visible warning, never a 'not found' stub."""
    ws = make_workspace(tmp_path)
    body = (ws / "requirements.md").read_text(encoding="utf-8")
    (ws / "requirements.md").unlink()
    (ws / "architecture" / "requirements.md").write_text(body,
                                                         encoding="utf-8")
    code, payload = run_main(["--workspace", str(ws), "--tex-only"],
                             tmp_path, capsys)
    assert code == 0, payload
    by_name = {s["name"]: s for s in payload["sections"]}
    assert by_name["requirements"]["status"] == "included"
    assert by_name["requirements"]["source"] == "architecture/requirements.md"
    assert any("not workspace root" in w for w in payload["warnings"])
    text = (ws / payload["tex"]).read_text(encoding="utf-8")
    assert "Does a thing at 5A" in text          # the real body, not a stub
    assert "requirements.md not found" not in text


def test_requirements_registry_path_wins(tmp_path, capsys):
    """The artifacts registry outranks the path ladder; a registry entry at
    the root stays warning-free."""
    ws = make_workspace(tmp_path)
    (ws / "docs").mkdir()
    (ws / "requirements.md").rename(ws / "docs" / "req.md")
    state = json.loads((ws / "state.json").read_text(encoding="utf-8"))
    state["artifacts"]["requirements"] = "docs/req.md"
    (ws / "state.json").write_text(json.dumps(state, indent=1),
                                   encoding="utf-8")
    code, payload = run_main(["--workspace", str(ws), "--tex-only"],
                             tmp_path, capsys)
    assert code == 0, payload
    by_name = {s["name"]: s for s in payload["sections"]}
    assert by_name["requirements"]["source"] == "docs/req.md"
    assert any("not workspace root" in w for w in payload["warnings"])
    # registry pointing at the root -> no warning (root is the contract)
    state["artifacts"]["requirements"] = "requirements.md"
    (ws / "state.json").write_text(json.dumps(state, indent=1),
                                   encoding="utf-8")
    (ws / "docs" / "req.md").rename(ws / "requirements.md")
    code, payload = run_main(["--workspace", str(ws), "--tex-only"],
                             tmp_path, capsys, name="out2")
    assert code == 0, payload
    assert not any("not workspace root" in w for w in payload["warnings"])


def test_compile_failure_adds_warning(tmp_path, capsys, monkeypatch):
    """F5: a failed or timed-out compile must explain itself in warnings."""
    ws = make_workspace(tmp_path)
    fake = tmp_path / "lualatex.exe"
    fake.write_bytes(b"x")
    monkeypatch.setattr(report_gen, "find_lualatex", lambda: fake)
    monkeypatch.setattr(report_gen, "house_style_dir", lambda: tmp_path)
    monkeypatch.setattr(
        report_gen, "compile_pdf",
        lambda p, w, n, *_: ({"engine": str(p), "rc": 1, "passes": 1,
                          "seconds": 0.1, "latex_log_tail": "boom"}, None))
    code, payload = run_main(["--workspace", str(ws)], tmp_path, capsys)
    assert code == 1 and payload["status"] == "violations"
    assert any("lualatex failed" in w for w in payload["warnings"])

    monkeypatch.setattr(
        report_gen, "compile_pdf",
        lambda p, w, n, *_: ({"engine": str(p), "rc": 124, "passes": 1,
                          "seconds": 0.1, "timed_out": True}, None))
    code2, payload2 = run_main(["--workspace", str(ws)], tmp_path, capsys,
                               name="out2")
    assert code2 == 1
    assert any("timed out" in w for w in payload2["warnings"])


# ------------------------------------------------------------------ find_lualatex

def test_find_lualatex_pin(monkeypatch, tmp_path):
    exe = tmp_path / "lualatex"
    exe.write_bytes(b"x")
    monkeypatch.setenv("HWDE_LUALATEX", str(exe))
    assert report_gen.find_lualatex() == exe


def test_find_lualatex_pin_invalid(monkeypatch):
    monkeypatch.setenv("HWDE_LUALATEX", r"C:\nonexistent\lualatex.exe")
    with pytest.raises(env.EnvError, match="HWDE_LUALATEX does not exist"):
        report_gen.find_lualatex()


def test_find_lualatex_path_or_none(monkeypatch, tmp_path):
    monkeypatch.delenv("HWDE_LUALATEX", raising=False)
    monkeypatch.delenv("AIEE_LUALATEX", raising=False)
    exe = tmp_path / "lualatex"
    monkeypatch.setattr(report_gen.shutil, "which",
                        lambda n: str(exe) if n == "lualatex" else None)
    assert report_gen.find_lualatex() == exe
    monkeypatch.setattr(report_gen.shutil, "which", lambda n: None)
    assert report_gen.find_lualatex() is None


def test_house_style_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("HWDE_HOUSE_STYLE", str(tmp_path))
    assert report_gen.house_style_dir() is None
    (tmp_path / "housestyle.sty").write_text("%", encoding="utf-8")
    assert report_gen.house_style_dir() == tmp_path


# ------------------------------------------------------------------ find_pdflatex

def test_find_pdflatex_pin(monkeypatch, tmp_path):
    exe = tmp_path / "pdflatex.exe"
    exe.write_bytes(b"x")
    monkeypatch.setenv("HWDE_PDFLATEX", str(exe))
    assert env.find_pdflatex() == exe


def test_find_pdflatex_pin_invalid(monkeypatch):
    monkeypatch.setenv("HWDE_PDFLATEX", r"C:\nonexistent\pdflatex.exe")
    with pytest.raises(env.EnvError, match="HWDE_PDFLATEX does not exist"):
        env.find_pdflatex()


def test_find_pdflatex_path(monkeypatch, tmp_path):
    exe = tmp_path / "pdflatex.EXE"        # PATH hits may be uppercase
    exe.write_bytes(b"x")
    monkeypatch.delenv("HWDE_PDFLATEX", raising=False)
    monkeypatch.setattr(env.shutil, "which", lambda name: str(exe))
    assert env.find_pdflatex() == exe


def test_find_pdflatex_none(monkeypatch, tmp_path):
    monkeypatch.delenv("HWDE_PDFLATEX", raising=False)
    monkeypatch.setattr(env.shutil, "which", lambda name: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))   # no MiKTeX inside
    assert env.find_pdflatex() is None


@pytest.mark.skipif(sys.platform != "win32", reason="windows default location")
def test_find_pdflatex_miktex_default(monkeypatch, tmp_path):
    exe = tmp_path / "Programs" / "MiKTeX" / "miktex" / "bin" / "x64" / "pdflatex.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"x")
    monkeypatch.delenv("HWDE_PDFLATEX", raising=False)
    monkeypatch.setattr(env.shutil, "which", lambda name: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert env.find_pdflatex() == exe


def test_check_env_lualatex_unit(monkeypatch, tmp_path):
    # F3: hermetic against an ambient HWDE_LUALATEX pin in the caller's env
    monkeypatch.delenv("HWDE_LUALATEX", raising=False)
    c = check_env.check_lualatex({})
    assert c["name"] == "lualatex"
    assert c["status"] in ("pass", "warn")     # never a hard fail when absent

    monkeypatch.setattr(report_gen, "find_lualatex", lambda: None)
    c_absent = check_env.check_lualatex({})
    assert c_absent["status"] == "warn"        # absent branch, deterministic
    assert "HWDE_LUALATEX" in c_absent["remediation"]

    fake = tmp_path / "lualatex"
    monkeypatch.setattr(report_gen, "find_lualatex", lambda: fake)
    monkeypatch.setattr(report_gen, "house_style_dir", lambda: None)
    c_nostyle = check_env.check_lualatex({})
    assert c_nostyle["status"] == "warn"       # engine without its style
    assert "house style not found" in c_nostyle["detail"]
    monkeypatch.setattr(report_gen, "house_style_dir", lambda: tmp_path)
    assert check_env.check_lualatex({})["status"] == "pass"

    def raiser():
        raise env.EnvError("HWDE_LUALATEX does not exist: bad")
    monkeypatch.setattr(report_gen, "find_lualatex", raiser)
    c2 = check_env.check_lualatex({})
    assert c2["status"] == "fail"              # bad pin fails loudly
    assert "HWDE_LUALATEX" in c2["detail"]


# ------------------------------------------------------------------ smoke

@pytest.fixture(scope="session")
def pdflatex_bin():
    """The report engine (lualatex + the house style); named for history."""
    try:
        p = report_gen.find_lualatex()
    except env.EnvError:
        p = None
    if p is None or report_gen.house_style_dir() is None:
        pytest.skip("lualatex or the pdf-material-builder house style absent")
    return p


def tree_state(ws: Path) -> dict[str, tuple[int, int]]:
    """Every file under the workspace copy with its size and mtime."""
    return {p.relative_to(ws).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in ws.rglob("*") if p.is_file()}


def assert_no_residue_outside_design_doc(ws: Path, before: dict) -> None:
    """The run may only touch `<workspace>/reports/design_doc/`.

    report_gen writes into the workspace with no output-dir override, so the
    smoke runs on a private copy of the board (the boards repo is never
    written) and compares that copy's tree before and after: a file added,
    removed or rewritten outside design_doc/ fails, which is the invariant
    worth having. A re-run overwriting design_doc/ files is expected.
    """
    after = tree_state(ws)
    changed = {k for k in before.keys() | after.keys()
               if before.get(k) != after.get(k)}
    assert all(k.startswith("reports/design_doc/") for k in changed), \
        sorted(changed)


def run_cli(ws: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "report_gen.py"),
         "--workspace", str(ws)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=280, cwd=str(REPO))


def assert_real_run(r: subprocess.CompletedProcess, ws: Path) -> dict:
    assert r.returncode == 0, (r.stdout or "")[-2500:] + (r.stderr or "")[-500:]
    payload = json.loads(r.stdout)
    assert payload["status"] == "pass"
    assert payload["compile"]["rc"] == 0 and payload["compile"]["passes"] == 2
    pdf = ws / payload["pdf"]
    assert pdf.is_file() and pdf.stat().st_size > 0
    assert payload["pages"] >= 8
    from pypdf import PdfReader                      # independent recount
    assert len(PdfReader(str(pdf)).pages) == payload["pages"]
    by_name = {s["name"]: s for s in payload["sections"]}
    for sec in ("schematic", "layout", "verification", "dfm_fab"):
        assert by_name[sec]["status"] == "included", by_name[sec]
    return payload


@pytest.mark.smoke
def test_smoke_pd_trigger_with_residue_and_rerun(pdflatex_bin, tmp_path):
    """render_final/{top,bottom}.png convention + full pipeline data."""
    ws = copy_board("pd-trigger", tmp_path)
    before = tree_state(ws)
    payload = assert_real_run(run_cli(ws), ws)
    assert "render_final/top.png" in json.dumps(payload["sections"])
    # second run must overwrite cleanly
    assert_real_run(run_cli(ws), ws)
    # residue: nothing new outside reports/design_doc/
    assert_no_residue_outside_design_doc(ws, before)


@pytest.mark.smoke
def test_smoke_stm32_blinky(pdflatex_bin, tmp_path):
    """renders/<board>_{top,bottom,iso}.png convention + labeled + layers."""
    ws = copy_board("stm32-blinky", tmp_path)
    before = tree_state(ws)
    payload = assert_real_run(run_cli(ws), ws)
    src = json.dumps(payload["sections"])
    assert "renders/stm32-blinky_top.png" in src
    assert "render_labeled/stm32-blinky_top.png" in src
    assert_no_residue_outside_design_doc(ws, before)


def test_layout_finds_renders_named_after_numbered_project(tmp_path, capsys):
    """A numbered board's KiCad project is <PN>_<name> (statelib.project_stem),
    and render.py names its output after that stem, not state.json's board -
    the renders/ ladder rung must try the stem first."""
    ws = make_workspace(tmp_path)
    rf = ws / "reports" / "render_final"
    (rf / "top.png").unlink()
    (rf / "bottom.png").unlink()
    kicad = ws / "kicad"
    kicad.mkdir()
    (kicad / "PCB-0007-C_synth.kicad_pro").write_text("{}", encoding="utf-8")
    renders = ws / "reports" / "renders"
    renders.mkdir()
    write_png(renders / "PCB-0007-C_synth_top.png")

    code, payload = run_main(["--workspace", str(ws), "--tex-only"],
                             tmp_path, capsys, name="numbered")
    assert code == 0, payload
    assert sections_by_name(payload)["layout"] == "included"
    src = json.dumps(payload["sections"])
    assert "renders/PCB-0007-C_synth_top.png" in src


def test_generation_cost_section(tmp_path, capsys):
    """The Run Record prints cost.json's total and per-step lines; without
    cost.json it says 'Not recorded' and warns, never estimating."""
    ws = make_workspace(tmp_path)
    code, payload = run_main(["--workspace", str(ws), "--tex-only"],
                             tmp_path, capsys, name="nocost")
    text = (ws / payload["tex"]).read_text(encoding="utf-8")
    assert r"\subsection*{Generation cost}" in text
    assert "Not recorded (no reports/cost.json)" in text
    assert any("cost.json" in w for w in payload["warnings"])

    (ws / "reports" / "cost.json").write_text(json.dumps({
        "total_usd": 12.3456, "loop_logged_usd": 5.0,
        "by_step": [{"step": "P4", "label": "Schematic", "usd": 4.0},
                    {"step": "P7", "label": "Routing", "usd": 8.3456}],
        "breakdown": "partial", "breakdown_reason": "one round left no steps",
        "shared": [{"label": "pre-buy", "shared_with": "a skill feature",
                    "usd": 1.5}],
        "notes": ["a note"],
        "rounds": [{"loop": {"unlogged_iterations": 1}},
                   {"shared_with": "x", "loop": {"unlogged_iterations": 5}}]}),
        encoding="utf-8")
    code, payload = run_main(["--workspace", str(ws), "--tex-only"],
                             tmp_path, capsys, name="cost")
    assert code == 0, payload
    text = (ws / payload["tex"]).read_text(encoding="utf-8")
    assert r"\textbf{\$12.35}" in text
    assert r"Schematic & \$4.00" in text and r"Routing & \$8.35" in text
    assert "one round left no steps" in text
    assert r"shared with a skill feature, cost \$1.50" in text
    assert (r"The worker loop logged \$5.00. 1 of its iterations timed out"
            in text)                                # shared round not counted
    assert not any("cost.json" in w for w in payload["warnings"])
    assert "reports/cost.json" in sections_by_name_source(payload)


def sections_by_name_source(payload: dict) -> str:
    return next(s["source"] for s in payload["sections"]
                if s["name"] == "run_record")


def test_cc_docs_args_name_part_number_and_step_costs(tmp_path):
    """The design-doc filing passes --describes PCB-NNNN-R from the register
    beside the workspace, and one --cost step=usd per step of cost.json; an
    unlisted board and a board with no cost.json pass neither."""
    ws = make_workspace(tmp_path)
    pdf = ws / "x.pdf"
    args = report_gen.cc_docs_args(ws, "synth", pdf)
    assert "--describes" not in args and "--cost" not in args
    assert args[:2] == ["file", str(pdf)]

    (tmp_path / "register.yaml").write_text(
        "products:\n  PCB-0007:\n    title: t\n    revs:\n"
        "      A: {dir: other}\n      B: {dir: synth}\n", encoding="utf-8")
    (ws / "reports" / "cost.json").write_text(json.dumps({
        "by_step": [{"step": "P4", "usd": 4.0}, {"step": "P7", "usd": 8.3456},
                    {"step": "P9", "usd": None}]}), encoding="utf-8")
    args = report_gen.cc_docs_args(ws, "synth", pdf)
    assert args[args.index("--describes") + 1] == "PCB-0007-B"
    costs = [args[i + 1] for i, a in enumerate(args) if a == "--cost"]
    assert costs == ["P4=4.00", "P7=8.35"]


def test_part_number_row_in_metadata(tmp_path, capsys):
    ws = make_workspace(tmp_path)
    code, payload = run_main(["--workspace", str(ws), "--tex-only"],
                             tmp_path, capsys, name="nopn")
    text = (ws / payload["tex"]).read_text(encoding="utf-8")
    assert "part number & none (not in the boards register)" in text
    assert "\\hsslug{no part number" in text
    (tmp_path / "register.yaml").write_text(
        "products:\n  PCB-0007:\n    revs:\n      C: {dir: synth}\n",
        encoding="utf-8")
    code, payload = run_main(["--workspace", str(ws), "--tex-only"],
                             tmp_path, capsys, name="pn")
    text = (ws / payload["tex"]).read_text(encoding="utf-8")
    assert "part number & PCB-0007-C" in text
    # ...and on the title page and in every page's running head (owner, #ai-ee:
    # "in the library the PDFs should have the PNs on them").
    assert "\\hstitleblock{PCB-0007-C \\textperiodcentered" in text
    assert "\\hsslug{PCB-0007-C \\textperiodcentered" in text


def test_design_spend_row_in_metadata(tmp_path, capsys):
    """Owner, edit m1 on the esp32c3-node full doc: 'how much did it cost to
    design this board' - the metadata table answers from design_spend.json."""
    ws = make_workspace(tmp_path)
    code, payload = run_main(["--workspace", str(ws), "--tex-only"],
                             tmp_path, capsys, name="nospend")
    text = (ws / payload["tex"]).read_text(encoding="utf-8")
    assert "design spend & not recorded" in text

    (ws / "reports" / "design_spend.json").write_text(json.dumps({
        "actual_usd": 27.1934, "cap_usd": 48, "source": "loop runs",
        "as_of": "2026-10-01"}), encoding="utf-8")
    code, payload = run_main(["--workspace", str(ws), "--tex-only"],
                             tmp_path, capsys, name="spend")
    text = (ws / payload["tex"]).read_text(encoding="utf-8")
    assert "design spend & USD 27.19 of a USD 48.00 cap, as of 2026-10-01" in text
    assert "not recorded" not in text


def test_design_spend_text_edges(tmp_path):
    ws = make_workspace(tmp_path)
    f = ws / "reports" / "design_spend.json"
    f.write_text("{broken", encoding="utf-8")
    assert report_gen.design_spend(ws) == "not recorded"
    f.write_text(json.dumps({"cap_usd": 48}), encoding="utf-8")
    assert report_gen.design_spend(ws) == "not recorded"
    f.write_text(json.dumps({"actual_usd": 3}), encoding="utf-8")
    assert report_gen.design_spend(ws) == "USD 3.00 (no cap recorded)"


# ------------------------------------------------------------- filing opt-in

class _Builder:
    def __init__(self):
        self.warnings = []

    def warn(self, m):
        self.warnings.append(m)


def _fake_cc_docs(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    exe = bindir / "cc-docs"
    exe.write_text(f'#!/bin/sh\necho "$@" >> {log}\n')
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return log


def test_no_filing_without_opt_in_even_with_cc_docs_on_path(tmp_path, monkeypatch):
    log = _fake_cc_docs(tmp_path, monkeypatch)
    report_gen.file_in_register(tmp_path / "x.pdf", "b", _Builder())
    assert not log.exists()


def test_filing_with_flag_or_doc_project(tmp_path, monkeypatch):
    log = _fake_cc_docs(tmp_path, monkeypatch)
    report_gen.file_in_register(tmp_path / "x.pdf", "b", _Builder(), True)
    assert "--project Boards --title" in log.read_text()
    log.unlink()
    monkeypatch.setenv("DOC_PROJECT", "Other")
    report_gen.file_in_register(tmp_path / "x.pdf", "b", _Builder())
    assert "--project Other" in log.read_text()


def test_board_project_is_the_boards_project(tmp_path, monkeypatch):
    """A new board document files into "Boards" (002), registered or not.
    The per-board "Boards/<PN> <name>" is refused by cc-docs while 002 is a
    project outside any group named "Boards" (checked with `cc-docs number`,
    which reserves nothing); DOC_PROJECT still overrides."""
    root = tmp_path / "boards"
    numbered = root / "PCB-0018-A_bldc-motor-driver"
    stray = root / "stray"           # not in the register
    for d in (numbered, stray):
        d.mkdir(parents=True)
    (root / "register.yaml").write_text(
        "products:\n  PCB-0018:\n    revs:\n"
        "      A: {dir: PCB-0018-A_bldc-motor-driver}\n", encoding="utf-8")
    for ws in (numbered, stray, None):
        assert report_gen.board_project(ws) == "Boards"
    args = report_gen.cc_docs_args(numbered, "bldc", numbered / "x.pdf")
    assert args[args.index("--project") + 1] == "Boards"
    monkeypatch.setenv("DOC_PROJECT", "Other")
    assert report_gen.board_project(numbered) == "Other"
    assert report_gen.board_project(stray) == "Other"


def test_filing_goes_to_the_boards_project(tmp_path, monkeypatch):
    log = _fake_cc_docs(tmp_path, monkeypatch)
    ws = tmp_path / "PCB-0018-A_bldc"
    ws.mkdir()
    (tmp_path / "register.yaml").write_text(
        "products:\n  PCB-0018:\n    revs:\n      A: {dir: PCB-0018-A_bldc}\n",
        encoding="utf-8")
    report_gen.file_in_register(ws / "x.pdf", "b", _Builder(), True, ws=ws)
    assert "--project Boards --title PCB-0018-A b design doc " in log.read_text()


def _docs_register(tmp_path, monkeypatch, documents):
    """A scratch cc-docs register (CC_DOCS_ROOT), never the owner's."""
    lib = tmp_path / "docs-lib"
    lib.mkdir(exist_ok=True)
    (lib / "register.json").write_text(json.dumps(
        {"version": 1, "projects": {"002": {"name": "Boards", "kind": "work"}},
         "documents": documents, "retired": {}}), encoding="utf-8")
    monkeypatch.setenv("CC_DOCS_ROOT", str(lib))


def _doc(number, title, source, describes=None, at="2026-10-01T00:00:00Z"):
    return {"number": number, "project": "002", "title": title,
            "source": source,
            "revisions": [{"rev": "A", "describes": describes,
                           "filed_at": at}]}


def test_rebuild_reuses_the_filed_documents_title(tmp_path, monkeypatch):
    """PCB-0024-A's rebuilt docs were titled "... highlight doc" / "... design
    doc" from a new worktree, matching neither the source path nor the title
    the library had, so cc-docs made 002-0048/0049. A rebuild now files
    under the existing document's own project and exact title."""
    log = _fake_cc_docs(tmp_path, monkeypatch)
    root = tmp_path / "boards"
    ws = root / "PCB-0023-A_gan-rf-inverter"
    ws.mkdir(parents=True)
    (root / "register.yaml").write_text(
        "products:\n  PCB-0023:\n    revs:\n"
        "      A: {dir: PCB-0023-A_gan-rf-inverter}\n", encoding="utf-8")
    old = "/wt/gan/PCB-0023-A_gan-rf-inverter/reports"
    _docs_register(tmp_path, monkeypatch, {
        "002-0045": _doc("002-0045", "PCB-0023-A gan-rf-inverter highlights",
                         f"{old}/highlight/gan-rf-inverter-highlight.tex",
                         "PCB-0023-A"),
        "002-0046": _doc("002-0046", "PCB-0023-A gan-rf-inverter full design doc",
                         f"{old}/design_full/gan-rf-inverter-design-full.tex",
                         "PCB-0023-A"),
        # another board's highlights, and this board's guide: neither is it
        "002-0042": _doc("002-0042", "PCB-0024-A rf-load-50ohm highlights",
                         "/wt/rf/PCB-0024-A_rf-load-50ohm/reports/highlight/x.tex",
                         "PCB-0024-A"),
        "002-0050": _doc("002-0050", "PCB-0023-A guide",
                         f"{old}/guide/gan-rf-inverter-guide.tex", "PCB-0023-A"),
    })
    pdf = ws / "reports" / "highlight" / "gan-rf-inverter-highlight.pdf"
    report_gen.file_in_register(pdf, "gan-rf-inverter", _Builder(), True,
                                ws=ws, kind="highlight")
    line = log.read_text()
    assert "--project 002 --title PCB-0023-A gan-rf-inverter highlights " in line
    assert "--describes PCB-0023-A" in line
    args = report_gen.cc_docs_args(ws, "gan-rf-inverter", pdf, kind="full")
    assert args[args.index("--title") + 1] == \
        "PCB-0023-A gan-rf-inverter full design doc"
    # no design doc filed yet: the stable convention, into Boards
    assert report_gen.doc_target(ws, "gan-rf-inverter", "design") == \
        ("Boards", "PCB-0023-A gan-rf-inverter design doc")


def test_filed_document_matching_edges(tmp_path, monkeypatch):
    """Which filed document is the board's: the part number's directory or
    describes, never another revision's; the last filed of several; an
    unregistered board by its directory name; nothing on a bad register."""
    root = tmp_path / "boards"
    a, b = root / "PCB-0022-A_nfc-card", root / "PCB-0022-B_nfc-card"
    stray = root / "stray"
    for d in (a, b, stray):
        d.mkdir(parents=True)
    (root / "register.yaml").write_text(
        "products:\n  PCB-0022:\n    revs:\n"
        "      A: {dir: PCB-0022-A_nfc-card}\n"
        "      B: {dir: PCB-0022-B_nfc-card}\n", encoding="utf-8")
    _docs_register(tmp_path, monkeypatch, {
        "002-0031": _doc("002-0031", "nfc-card design doc",
                         "/wt/nfc-card/PCB-0022-A_nfc-card/reports/design_doc/x.pdf",
                         "PCB-0022-A", "2026-09-30T00:00:00Z"),
        # refiled by hand later, from a directory that doesn't name the
        # board, but describing it: the last filed wins
        "002-0060": _doc("002-0060", "PCB-0022-A nfc-card design doc",
                         "C:\\tmp\\clone\\reports\\design_doc\\x.pdf",
                         "PCB-0022-A", "2026-10-02T00:00:00Z"),
        # in a directory named like A's but describing B: never A's
        "002-0061": _doc("002-0061", "B's", "/x/PCB-0022-A_nfc-card/reports/"
                         "design_doc/x.pdf", "PCB-0022-B", "2026-10-05T00:00:00Z"),
        "002-0070": _doc("002-0070", "stray design doc",
                         "/wt/stray/reports/design_doc/stray-design-doc.pdf"),
    })
    got = report_gen.filed_document(a, "design", "PCB-0022-A")
    assert got == {"number": "002-0060", "project": "002",
                   "title": "PCB-0022-A nfc-card design doc"}
    assert report_gen.filed_document(b, "design", "PCB-0022-B")["number"] \
        == "002-0061"
    assert report_gen.filed_document(a, "highlight", "PCB-0022-A") is None
    assert report_gen.filed_document(stray, "design")["number"] == "002-0070"
    assert report_gen.filed_document(None) is None
    (tmp_path / "docs-lib" / "register.json").write_text("{bad", encoding="utf-8")
    assert report_gen.filed_document(a, "design", "PCB-0022-A") is None
    assert report_gen.doc_target(a, "nfc-card") == \
        ("Boards", "PCB-0022-A nfc-card design doc")


def test_second_revision_never_lands_on_the_firsts_docs(tmp_path, monkeypatch):
    """PCB-0022-B's docs once filed as new revisions of PCB-0022-A's because
    the title was "<board> <kind>" with no part number and cc-docs matched on
    it. The title now leads with the part number, and each revision has its
    own folder, so a register keyed on either never gives B one of A's ids."""
    monkeypatch.delenv("DOC_PROJECT", raising=False)
    root = tmp_path / "boards"
    a, b = root / "PCB-0022-A_nfc-card", root / "PCB-0022-B_nfc-card"
    for d in (a, b):
        d.mkdir(parents=True)
    (root / "register.yaml").write_text(
        "products:\n  PCB-0022:\n    revs:\n"
        "      A: {dir: PCB-0022-A_nfc-card}\n"
        "      B: {dir: PCB-0022-B_nfc-card}\n", encoding="utf-8")
    ids: dict = {}

    def file(ws, kind):
        args = report_gen.cc_docs_args(ws, "nfc-card", ws / "x.pdf", kind=kind)
        title = args[args.index("--title") + 1]
        project = args[args.index("--project") + 1]
        # the old matching (title alone) and the folder-aware one
        return (ids.setdefault(("t", title), len(ids)),
                ids.setdefault(("pt", project, title), len(ids)), title)

    for kind in report_gen.KINDS:
        a_by_title, a_by_folder, a_title = file(a, kind)
        b_by_title, b_by_folder, b_title = file(b, kind)
        assert a_title == f"PCB-0022-A nfc-card {report_gen.KINDS[kind][3]}"
        assert b_title == f"PCB-0022-B nfc-card {report_gen.KINDS[kind][3]}"
        assert a_by_title != b_by_title and a_by_folder != b_by_folder
        # a rebuild of A still lands on A's own documents
        assert file(a, kind)[:2] == (a_by_title, a_by_folder)
    # a board name that already carries the part number is not doubled
    args = report_gen.cc_docs_args(a, "PCB-0022-A_nfc-card", a / "x.pdf")
    assert args[args.index("--title") + 1] == "PCB-0022-A_nfc-card design doc"
    # with no part number the title stays as it was
    stray = root / "stray"
    stray.mkdir()
    args = report_gen.cc_docs_args(stray, "stray", stray / "x.pdf")
    assert args[args.index("--title") + 1] == "stray design doc"


def _stub_compile(tmp_path, monkeypatch):
    """A compile that writes a PDF whose bytes differ every build, like lualatex."""
    fake = tmp_path / "lualatex.exe"
    fake.write_bytes(b"x")
    monkeypatch.setattr(report_gen, "find_lualatex", lambda: fake)
    monkeypatch.setattr(report_gen, "house_style_dir", lambda: tmp_path)

    def compile_pdf(p, ws, name, subdir="design_doc", style=None):
        pdf = ws / "reports" / subdir / f"{name}.pdf"
        pdf.write_bytes(b"%PDF-1.4 " + os.urandom(8))
        return {"engine": str(p), "rc": 0, "passes": 1, "seconds": 0.1}, pdf
    monkeypatch.setattr(report_gen, "compile_pdf", compile_pdf)


def test_smoke_build_files_nothing_with_cc_docs_on_path(tmp_path, capsys, monkeypatch):
    log = _fake_cc_docs(tmp_path, monkeypatch)
    _stub_compile(tmp_path, monkeypatch)
    ws = make_workspace(tmp_path)
    code, payload = run_main(["--workspace", str(ws)], tmp_path, capsys)
    assert payload["pdf"] is not None
    assert not log.exists()


def test_unchanged_rebuild_files_once(tmp_path, capsys, monkeypatch):
    log = _fake_cc_docs(tmp_path, monkeypatch)
    _stub_compile(tmp_path, monkeypatch)
    monkeypatch.setenv("DOC_PROJECT", "Boards")
    ws = make_workspace(tmp_path)
    run_main(["--workspace", str(ws)], tmp_path, capsys, name="a")
    run_main(["--workspace", str(ws)], tmp_path, capsys, name="b")
    assert len(log.read_text().splitlines()) == 1
    # a changed document files again
    state = json.loads((ws / "state.json").read_text(encoding="utf-8"))
    state["board"] = state["board"] + "-rev2"
    (ws / "state.json").write_text(json.dumps(state), encoding="utf-8")
    run_main(["--workspace", str(ws), "--name", "renamed"], tmp_path, capsys, name="c")
    assert len(log.read_text().splitlines()) == 2


def test_rebuild_a_minute_later_is_unchanged(tmp_path, capsys, monkeypatch):
    """The title block's build time is no change to the document: a rebuild
    whose clock reads the next minute files nothing (#1255, the flaky
    test_file_with_note_passes_it_to_cc_docs when CI crossed a minute)."""
    log = _fake_cc_docs(tmp_path, monkeypatch)
    _stub_compile(tmp_path, monkeypatch)
    monkeypatch.setenv("DOC_PROJECT", "Boards")
    ws = make_workspace(tmp_path)
    real, base = report_gen.time.strftime, report_gen.time.time()
    for i, name in enumerate(("a", "b")):
        monkeypatch.setattr(report_gen.time, "strftime",
                            lambda f, t=None, i=i: real(
                                f, t or report_gen.time.localtime(base + 60 * i)))
        _, p = run_main(["--workspace", str(ws)], tmp_path, capsys, name=name)
    tex = (ws / p["tex"]).read_text(encoding="utf-8")
    assert re.search(r"generated \d{4}-\d\d-\d\d \d\d:\d\d\b", tex)
    assert p["unchanged"] is True
    assert len(log.read_text().splitlines()) == 1


def test_failed_filing_leaves_no_stamp(tmp_path, capsys, monkeypatch):
    log = _fake_cc_docs(tmp_path, monkeypatch)
    (tmp_path / "bin" / "cc-docs").write_text(f'#!/bin/sh\necho "$@" >> {log}\nexit 3\n')
    _stub_compile(tmp_path, monkeypatch)
    ws = make_workspace(tmp_path)
    run_main(["--workspace", str(ws), "--file"], tmp_path, capsys, name="a")
    run_main(["--workspace", str(ws), "--file"], tmp_path, capsys, name="b")
    assert len(log.read_text().splitlines()) == 2
    assert not (ws / "reports" / "design_doc" / report_gen.FILED_STAMP).exists()


def test_stamp_is_keyed_on_the_library(tmp_path, capsys, monkeypatch):
    """A rehearsal filed into a scratch CC_DOCS_ROOT must not stop the live
    run from filing; a repeat against the same library reports unchanged."""
    log = _fake_cc_docs(tmp_path, monkeypatch)
    _stub_compile(tmp_path, monkeypatch)
    ws = make_workspace(tmp_path)
    monkeypatch.setenv("CC_DOCS_ROOT", str(tmp_path / "scratch-lib"))
    _, p = run_main(["--workspace", str(ws), "--file"], tmp_path, capsys, name="a")
    assert p["filed"] is not None and p["unchanged"] is False
    monkeypatch.delenv("CC_DOCS_ROOT")
    _, p = run_main(["--workspace", str(ws), "--file"], tmp_path, capsys, name="b")
    assert p["filed"] is not None and p["unchanged"] is False
    assert len(log.read_text().splitlines()) == 2
    _, p = run_main(["--workspace", str(ws), "--file"], tmp_path, capsys, name="c")
    assert p["filed"] is None and p["unchanged"] is True
    assert len(log.read_text().splitlines()) == 2


def test_missing_cc_docs_warns(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    b = _Builder()
    report_gen.file_in_register(tmp_path / "x.pdf", "b", b, True)
    assert any("not on PATH" in w for w in b.warnings)
    # not asked to file: no warning
    b = _Builder()
    report_gen.file_in_register(tmp_path / "x.pdf", "b", b)
    assert b.warnings == []


# ------------------------------------------------- accepted verify waivers

FULL_WAIVER = {
    "check": "check_return_path", "kind": "corridor_void", "net": "/USB_DP",
    "refs": ["U4", "J1"], "pos": [24.45, 36.46],
    "reason": "One 0.13 mm2 void under the pair; USB full speed\n does not see it.",
    "approved": "owner 2026-10-08, #ai-ee: 'ok it's fine but flag it in the documentation'",
    "expires": "next revision"}
BARE_WAIVER = {"check": "check_silk", "reason": "silk ring clears the pad",
               "approved": "Ihsan S. 2026-08-08"}


def _waivers(ws: Path, waivers) -> None:
    (ws / "reports" / "verify-waivers.json").write_text(
        json.dumps({"waivers": waivers}), encoding="utf-8")


def _tex(tmp_path, capsys, ws, *extra, name="out"):
    code, payload = run_main(["--workspace", str(ws), "--tex-only", *extra],
                             tmp_path, capsys, name=name)
    return code, payload, (ws / payload["tex"]).read_text(encoding="utf-8")


def _item(text: str, marker: str) -> str:
    """The waiver \\item of the tex that contains marker."""
    return next(i for i in text.split(r"\item ")[1:] if marker in i).split(
        r"\end{itemize}")[0]


def test_design_doc_lists_each_accepted_verify_waiver(tmp_path, capsys):
    ws = make_workspace(tmp_path)
    _waivers(ws, [FULL_WAIVER, BARE_WAIVER])
    code, payload, text = _tex(tmp_path, capsys, ws)
    assert code == 0, payload
    assert all(ord(c) < 128 for c in text)
    verification = text.split(r"\section{Verification}")[1].split(r"\section{")[0]
    assert r"\subsection*{Accepted verify waivers}" in verification
    assert "2 verify findings were accepted rather than fixed" in verification
    full = _item(verification, "corridor")
    assert (r"\textbf{check\_return\_path / corridor\_void} on net /USB\_DP, "
            "refs U4, J1, at (24.45, 36.46) mm.") in full
    assert "Reason: One 0.13 mm2 void under the pair; USB full speed does not" in full
    assert "Accepted: owner 2026-10-08, \\#ai-ee:" in full
    assert "Expires: next revision" in full
    bare = _item(verification, "check\\_silk")
    assert bare.startswith(r"\textbf{check\_silk}.")       # no net/refs/pos invented
    assert "Accepted: Ihsan S. 2026-08-08" in bare
    assert "Expires" not in bare and " on " not in bare.split(".")[0]
    source = next(x["source"] for x in payload["sections"]
                  if x["name"] == "verification")
    assert "reports/verify-waivers.json" in source
    assert not any("waiver" in w for w in payload["warnings"])


@pytest.mark.parametrize("content", [None, {"waivers": []}])
def test_no_or_empty_verify_waivers_render_as_before(tmp_path, capsys, content):
    ws = make_workspace(tmp_path)
    _, base, before = _tex(tmp_path, capsys, ws, name="a")
    if content is not None:
        (ws / "reports" / "verify-waivers.json").write_text(
            json.dumps(content), encoding="utf-8")
    _, payload, after = _tex(tmp_path, capsys, ws, name="b")
    assert "verify waivers" not in after
    strip = lambda t: re.sub(r"\d{4}-\d\d-\d\dT[\d:]+", "", t)  # noqa: E731
    if content is not None:   # an empty file is still listed as an artifact
        cut = lambda t: t.split(r"\section{Artifact Index}")[0]  # noqa: E731
        before, after = cut(before), cut(after)
    assert strip(after) == strip(before)
    assert payload["warnings"] == base["warnings"]
    assert payload["sections"] == base["sections"]


def test_verify_waivers_the_gate_refuses_are_a_warning(tmp_path, capsys):
    ws = make_workspace(tmp_path)
    _waivers(ws, [{"check": "check_silk", "reason": "no approver"}])
    code, payload, text = _tex(tmp_path, capsys, ws)
    assert code == 0
    assert any("reports/verify-waivers.json could not be read" in w
               and "lacks reason/approved" in w for w in payload["warnings"])
    assert r"\emph{reports/verify-waivers.json could not be read" in text
    (ws / "reports" / "verify-waivers.json").write_text("{broken", encoding="utf-8")
    _, payload, _ = _tex(tmp_path, capsys, ws, name="b")
    assert any("JSONDecodeError" in w for w in payload["warnings"])


def test_verify_waiver_entry_not_an_object_is_a_warning(tmp_path, capsys):
    """#731: the gate grades this entry instead of refusing the file; the
    report still says the file can't be read rather than raising."""
    ws = make_workspace(tmp_path)
    _waivers(ws, ["x"])
    code, payload, text = _tex(tmp_path, capsys, ws)
    assert code == 0
    assert any("reports/verify-waivers.json could not be read" in w
               and "waiver 0" in w for w in payload["warnings"])
    assert r"\emph{reports/verify-waivers.json could not be read" in text


def test_highlight_flags_verify_waivers_with_a_cut_reason(tmp_path, capsys):
    ws = make_workspace(tmp_path)
    long = dict(FULL_WAIVER, reason="word " * 100)
    _waivers(ws, [long])
    code, payload, text = _tex(tmp_path, capsys, ws, "--kind", "highlight")
    assert code == 0, payload
    checks = text.split(r"\section{Checks and Cost}")[1]
    assert "1 verify finding was accepted rather than fixed" in checks
    item = _item(checks, "corridor")
    reason = item.split("Reason: ")[1].split(r" \par")[0]
    assert len(reason) == report_gen.HL_REASON_CAP and reason.endswith("...")
    assert "Accepted: owner 2026-10-08" in item
    assert "The design doc gives each reason in full." in checks
    # a reason under the cap is whole and needs no pointer
    _waivers(ws, [FULL_WAIVER])
    _, _, text = _tex(tmp_path, capsys, ws, "--kind", "highlight", name="b")
    assert "does not see it." in text and "gives each reason in full" not in text


def test_waiver_item_odd_fields():
    item = report_gen.waiver_item({"kind": "creepage", "refs": "Q1",
                                   "pos": "near U2", "reason": "", "approved": "x"})
    assert item.startswith(r"\item \textbf{creepage} on refs Q1, at near U2.")
    assert "Reason:" not in item
    item = report_gen.waiver_item({"check": "c", "net": None, "pos": [1, "y"],
                                   "approved": "x"})
    assert "on at {[}1, 'y'{]}." in item


def test_cc_docs_args_note(tmp_path):
    pdf = tmp_path / "x.pdf"
    args = report_gen.cc_docs_args(None, "b", pdf, "Boards", "design", "t",
                                   note="waiver flagged")
    assert args[args.index("--note") + 1] == "waiver flagged"
    assert "--note" not in report_gen.cc_docs_args(None, "b", pdf, "Boards",
                                                   "design", "t")


def test_file_with_note_passes_it_to_cc_docs(tmp_path, capsys, monkeypatch):
    log = _fake_cc_docs(tmp_path, monkeypatch)
    _stub_compile(tmp_path, monkeypatch)
    ws = make_workspace(tmp_path)
    _, p = run_main(["--workspace", str(ws), "--file", "--note",
                     "Accepted waiver flagged"], tmp_path, capsys, name="a")
    assert p["filed"] is not None
    assert "--note Accepted waiver flagged" in log.read_text()
    # unchanged: nothing filed, so the note is dropped with a warning
    _, p = run_main(["--workspace", str(ws), "--file", "--note", "again"],
                    tmp_path, capsys, name="b")
    assert p["unchanged"] is True
    assert any("--note not filed" in w for w in p["warnings"])
    assert len(log.read_text().splitlines()) == 1
    # without --note the call carries none and nothing warns
    (ws / "reports" / "design_doc" / report_gen.FILED_STAMP).unlink()
    _, p = run_main(["--workspace", str(ws), "--file"], tmp_path, capsys, name="c")
    assert "--note" not in log.read_text().splitlines()[-1]
    assert not any("note" in w for w in p["warnings"])


def test_note_without_file_is_refused(tmp_path, capsys, monkeypatch):
    ws = make_workspace(tmp_path)
    monkeypatch.delenv("DOC_PROJECT", raising=False)
    with pytest.raises(SystemExit) as exc:
        report_gen.main(["--workspace", str(ws), "--tex-only", "--note", "x"])
    assert exc.value.code == 2
    assert "--note" in capsys.readouterr().err
    # DOC_PROJECT files too, so it accepts the note
    monkeypatch.setenv("DOC_PROJECT", "Boards")
    assert report_gen.main(["--workspace", str(ws), "--tex-only", "--note", "x",
                            "--out", str(tmp_path / "o.json")]) == 0
