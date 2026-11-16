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
    # without --parts the SO8 has no record, but it is a perimeter package,
    # so its direction is read (#1285): SO8_BOT is numbered from the bottom
    # view, a warning since no record declares the numbering
    assert code == 0 and rep["placed"] == 2
    by = {p["ref"]: p for p in rep["parts"]}
    assert by["U9"]["pad_numbering"]["from"] == "direction"
    assert by["U9"]["pad_numbering"]["fit"] is False
    assert by["U9"]["pad_numbering"]["hint"] == "bottom_view_numbering"
    [w] = [x for x in rep["violations"] if x["refs"] == ["U9"]]
    assert (w["kind"], w["severity"]) == ("pad_numbering_direction", "warning")
    assert by["U1"]["pad_numbering"] == {"from": "ball_names", "fit": True}


def test_board_mode_nothing_to_judge(tmp_path):
    code, rep = _run(tmp_path, "--pcb", str(
        REPO / "tests" / "golden" / "blinky2" / "blinky2.kicad_pcb"))
    # no record on blinky2 states its numbering, but U1's LQFP-48 is read
    # by its direction since #1285: counter-clockwise, as a top view runs
    assert code == 0 and rep["placed"] == 17
    assert rep["judged"] == {"direction": 1} and not rep["violations"]
    by = {p["ref"]: p for p in rep["parts"]}
    assert by["U1"]["pad_numbering"]["direction"] == "ccw"


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


def _value_keyed(tmp_path, value: str):
    """placed.kicad_pcb with the three C999008 parts (U7 fits; U9 and U10
    are numbered from the bottom view) given no LCSC number, only the Value
    `value` (#1406)."""
    text = (BOARD / "placed.kicad_pcb").read_text(encoding="utf-8")
    head, *fps = text.split("\n\t(footprint ")
    out = [head]
    for fp in fps:
        if '(property "LCSC" "C999008"' in fp:
            fp = fp.replace('(property "LCSC" "C999008"', '(property "Note" "x"')
            fp = fp.replace('(property "Value" ""', f'(property "Value" "{value}"')
        out.append(fp)
    pcb = tmp_path / "v.kicad_pcb"
    pcb.write_text("\n\t(footprint ".join(out), encoding="utf-8")
    return pcb


def test_board_mode_finds_a_record_by_value_alone(tmp_path):
    """#1406 planted fault and quiet twin: the record reached only through
    the Value catches U9 and U10 numbered from the bottom view, and U7, the
    same part placed right, fits."""
    pcb = _value_keyed(tmp_path, "SO8X")
    code, rep = _run(tmp_path, "--pcb", str(pcb), "--parts", str(BOARD / "parts"))
    assert code == 1
    by = {p["ref"]: p for p in rep["parts"]}
    for ref in ("U7", "U9", "U10"):
        assert by[ref]["found_by"] == "value", ref
        assert by[ref]["record"].endswith("C999008.json"), ref
    assert by["U7"]["pad_numbering"]["fit"] is True
    assert "found_by" not in by["U1"]
    bad = {v["refs"][0]: v["hint"] for v in rep["violations"]}
    assert bad == {"U5": "bottom_view_numbering", "U6": "bottom_view_numbering",
                   "U9": "bottom_view_numbering", "U10": "bottom_view_numbering"}


def test_board_mode_value_lookup_is_exact_and_unique(tmp_path):
    """#1406: a Value two records carry, or one no record carries exactly
    (no prefix match), or one that is a component value, judges nothing,
    and says why."""
    parts = tmp_path / "parts"
    parts.mkdir()
    rec = (BOARD / "parts" / "C999008.json").read_text(encoding="utf-8")
    (parts / "C999008.json").write_text(rec, encoding="utf-8")
    (parts / "SO8X.json").write_text(rec, encoding="utf-8")   # a second claim

    code, rep = _run(tmp_path, "--pcb", str(_value_keyed(tmp_path, "so8x")),
                     "--parts", str(parts))
    by = {p["ref"]: p for p in rep["parts"]}
    assert {v["refs"][0] for v in rep["violations"]} == {"U5", "U6"}
    for ref in ("U7", "U9", "U10"):
        assert by[ref]["pad_numbering"] is None and by[ref]["record"] is None
        [why] = by[ref]["not_judged"]
        assert "names 2 records (C999008.json, SO8X.json)" in why

    # a record whose number only starts with the Value is not a match
    longer = tmp_path / "longer"
    longer.mkdir()
    doc = json.loads(rec)
    doc["mpn"] = "SO8XTR"
    (longer / "SO8XTR.json").write_text(json.dumps(doc), encoding="utf-8")
    for value, where, said in (
            ("SO8X", longer, "no record named by its Value SO8X"),
            ("SO8XA", BOARD / "parts", "no record named by its Value SO8XA"),
            ("100nF", BOARD / "parts", "no record found by its LCSC number or MPN")):
        code, rep = _run(tmp_path, "--pcb", str(_value_keyed(tmp_path, value)),
                         "--parts", str(where))
        by = {p["ref"]: p for p in rep["parts"]}
        assert not {"U7", "U9", "U10"} & {v["refs"][0] for v in rep["violations"]}
        assert said in by["U9"]["not_judged"][0], value


def test_board_mode_value_never_overrides_a_part_number(tmp_path):
    """#1406: a part with an MPN keeps the MPN lookup; its Value, even one
    naming a record, is not read."""
    pcb = _value_keyed(tmp_path, "SO8X")
    text = pcb.read_text(encoding="utf-8").replace(
        '(property "Note" "x"', '(property "MPN" "NOPE123"')
    pcb.write_text(text, encoding="utf-8")
    code, rep = _run(tmp_path, "--pcb", str(pcb), "--parts", str(BOARD / "parts"))
    by = {p["ref"]: p for p in rep["parts"]}
    assert code == 1    # U5 and U6 still caught through their own records
    assert by["U9"]["record"] is None and "found_by" not in by["U9"]
    assert "LCSC number or MPN" in by["U9"]["not_judged"][0]


def test_board_mode_finds_a_record_by_its_lcsc_field(tmp_path):
    """#1406: a record landed from a Value is named by its MPN (SO8X.json)
    with the LCSC number only inside it; a board that gives just the LCSC
    number still reaches it, so U9 and U10 are caught and U7 fits."""
    parts = tmp_path / "parts"
    parts.mkdir()
    doc = json.loads((BOARD / "parts" / "C999008.json").read_text(encoding="utf-8"))
    doc["lcsc"] = "C999008"
    (parts / "SO8X.json").write_text(json.dumps(doc), encoding="utf-8")
    code, rep = _run(tmp_path, "--pcb", str(BOARD / "placed.kicad_pcb"),
                     "--parts", str(parts))
    by = {p["ref"]: p for p in rep["parts"]}
    for ref in ("U7", "U9", "U10"):
        assert by[ref]["record"].endswith("SO8X.json"), ref
        assert "found_by" not in by[ref]
    assert by["U7"]["pad_numbering"]["fit"] is True
    assert {"U9", "U10"} <= {v["refs"][0] for v in rep["violations"]}
