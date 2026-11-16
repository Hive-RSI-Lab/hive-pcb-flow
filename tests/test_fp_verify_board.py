"""fp_verify --pcb (#1285): the pad-numbering checks on each footprint placed
in a .kicad_pcb. tests/fixtures/fp_board/placed.kicad_pcb was placed and
flipped by KiCad itself (its place.py), so a bottom copy is unflipped
against KiCad's transform, not this one's own idea of it."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import check_fp_geometry as fpg  # noqa: E402
import fp_verify  # noqa: E402
import fplib  # noqa: E402

BOARD = REPO / "tests" / "fixtures" / "fp_board"


def _run(tmp_path, *argv):
    out = tmp_path / "r.json"
    code = fp_verify.main([*argv, "--out", str(out)])
    return code, json.loads(out.read_text(encoding="utf-8"))


def test_library_copy_matches_the_library_footprint(tmp_path):
    import sexpdata
    tree = sexpdata.loads((BOARD / "placed.kicad_pcb").read_text(encoding="utf-8"))
    nodes = fpg._kids(tree, "footprint")
    assert {str(fpg._t(fpg._kid(n, "layer")[1])) for n in nodes} == {"F.Cu", "B.Cu"}
    for node in nodes:
        name = str(fpg._t(node[1])).split(":")[-1]
        at = fpg._nums(fpg._kid(node, "at"))
        bottom = str(fpg._t(fpg._kid(node, "layer")[1])).startswith("B.")
        f = tmp_path / "x.kicad_mod"
        f.write_text(sexpdata.dumps(fp_verify.library_copy(
            node, at[2] if len(at) > 2 else 0.0, bottom)), encoding="utf-8")
        got = {p.number: p.at for p in fplib.parse_footprint(f).pads}
        want = {p.number: p.at for p in fplib.parse_footprint(
            BOARD / "lib.pretty" / f"{name}.kicad_mod").pads}
        assert set(got) == set(want)
        for k, w in want.items():
            assert got[k][:2] == pytest.approx(w[:2], abs=1e-5), (name, k)
            assert (got[k][2] - w[2]) % 360 == pytest.approx(0, abs=1e-6)


def test_board_mode_judges_each_copy_on_either_side(tmp_path):
    code, rep = _run(tmp_path, "--pcb", str(BOARD / "placed.kicad_pcb"),
                     "--parts", str(BOARD / "parts"))
    assert code == 1
    by = {p["ref"]: p for p in rep["parts"]}
    # the top-view footprints fit wherever they sit, the mirrored ones don't
    for ref in ("U1", "U2", "U3", "U4", "U7", "U8"):
        assert by[ref]["pad_numbering"]["fit"] is True, ref
    assert by["U8"]["pad_numbering"]["turn_deg"] == 0  # bottom, turned 90
    assert by["U7"]["record"].endswith("C999008.json")
    bad = {v["refs"][0]: v for v in rep["violations"]}
    assert set(bad) == {"U5", "U6", "U9", "U10"}
    assert {v["hint"] for v in bad.values()} == {"bottom_view_numbering"}
    assert bad["U6"]["side"] == "bottom" and bad["U6"]["pos"] == [95.0, 20.0]
    assert rep["judged"] == {"ball_names": 6, "pads": 4}
    assert "nothing_to_judge" not in rep


def test_board_mode_ref_filter_and_no_record(tmp_path):
    code, rep = _run(tmp_path, "--pcb", str(BOARD / "placed.kicad_pcb"),
                     "--ref", "U9", "--ref", "U1")
    # without --parts the SO8 has no record: not judged, and said so
    assert code == 0 and rep["placed"] == 2
    by = {p["ref"]: p for p in rep["parts"]}
    assert by["U9"]["pad_numbering"] is None
    assert "no record" in by["U9"]["not_judged"][0]
    assert by["U1"]["pad_numbering"] == {"from": "ball_names", "fit": True}


def test_board_mode_nothing_to_judge(tmp_path):
    code, rep = _run(tmp_path, "--pcb", str(
        REPO / "tests" / "golden" / "blinky2" / "blinky2.kicad_pcb"))
    assert code == 0 and rep["placed"] == 17 and rep["judged"] == {}
    assert "pad numbering" in rep["nothing_to_judge"]


def test_golden_mutant_and_its_quiet_twin(tmp_path):
    """The planted fault (lqfp-numbered-clockwise: U1 numbered from the
    bottom view) is caught at U1, and its quiet twin, the same record on
    blinky2's own U1, judges U1 and passes."""
    golden = REPO / "tests" / "golden"
    mut = golden / "mutants" / "lqfp-numbered-clockwise"
    code, rep = _run(tmp_path, "--pcb", str(mut / "blinky2.kicad_pcb"),
                     "--parts", str(mut / "parts"))
    assert code == 1 and rep["judged"] == {"numbering": 1}
    [v] = rep["violations"]
    assert (v["kind"], v["refs"], v["hint"], v["pos"]) == (
        "pad_numbering", ["U1"], "bottom_view_numbering", [128.0, 118.0])

    sys.path.insert(0, str(golden / "mutations"))
    import lqfp_numbered_clockwise as m
    text = (golden / "blinky2" / "blinky2.kicad_pcb").read_text(encoding="utf-8")
    s, e = m.mutlib.footprint_block(text, "U1")
    twin = text[:s] + text[s:e].replace(
        "(at 128 118)", f'(at 128 118)\n\t\t(property "LCSC" "{m.LCSC}" '
        '(at 0 0 0) (layer "F.Fab") (hide yes))', 1) + text[e:]
    (tmp_path / "parts").mkdir()
    (tmp_path / "parts" / f"{m.LCSC}.json").write_text(
        json.dumps(m.EXTRACTION), encoding="utf-8")
    (tmp_path / "twin.kicad_pcb").write_text(twin, encoding="utf-8")
    code, rep = _run(tmp_path, "--pcb", str(tmp_path / "twin.kicad_pcb"),
                     "--parts", str(tmp_path / "parts"))
    assert code == 0 and rep["violations"] == []
    assert rep["judged"] == {"numbering": 1}
