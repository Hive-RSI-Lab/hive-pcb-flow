"""T3 - library and authoring hygiene.

Two halves:
  fpfix / lib_pull   the footprint sanitiser that now runs at pull time
  schem_refdes       deterministic schematic refdes/value placement

The footprint fixtures in tests/fixtures/lib/pristine are UNTOUCHED easyeda2kicad
pulls (C14663 0603 cap, C2286 red LED, C7421520 3-position DIP switch, C5184243
GCT USB-C), captured live on 2026-08-06 with only their 3D-model paths made
portable. Every expectation below is the recipe that was applied BY HAND on the
three shipped boards and recorded in boards/*/lib/EDITS.md.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
PRISTINE = ROOT / "tests" / "fixtures" / "lib" / "pristine"
S7 = ROOT / "tests" / "s7_regen"

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import fpfix  # noqa: E402
import lib_pull  # noqa: E402
import schem_refdes as sr  # noqa: E402
import schlib  # noqa: E402

C0603 = "C0603"
LED = "LED-SMD_L1.6-W0.8-R-RD"
SWITCH = "SW-SMD_6P-L7.6-W6.0-P2.54-LS9.3-BL"
USBC = "USB-C-SMD_MC-311D"


@pytest.fixture
def lib(tmp_path) -> Path:
    """A writable copy of the pristine pull."""
    d = tmp_path / "aiee.pretty"
    shutil.copytree(PRISTINE, d)
    return d


def _actions(rep: dict, name: str) -> list[dict]:
    for r in rep["results"]:
        if r["footprint"] == name:
            return r["actions"]
    raise AssertionError(f"{name} not in report")


def _report(rep: dict, name: str) -> dict:
    for r in rep["results"]:
        if r["footprint"] == name:
            return r
    raise AssertionError(f"{name} not in report")


# ----------------------------------------------------------------- fpfix rules

def test_pristine_fixtures_carry_the_four_defect_classes(lib):
    """Guard the fixtures themselves: if a re-pull ever lands clean, say so."""
    c = fpfix.analyze((lib / f"{C0603}.kicad_mod").read_text(encoding="utf-8"))
    assert c["unprintable_silk"] == 1 and c["worst_gap_mm"] < 0      # silk on pad 1
    assert c["under_gap_silk"] >= 8                                  # outline under the bar
    u = fpfix.analyze((lib / f"{USBC}.kicad_mod").read_text(encoding="utf-8"))
    assert u["plated_pegs"] == 2                                     # zero annular ring
    sw = (lib / f"{SWITCH}.kicad_mod").read_text(encoding="utf-8")
    assert sw.count("(fp_text user") == 6                            # 5 legend + %R


def test_rule_a_deletes_silk_on_pad_and_promotes_silk_that_is_clear(lib):
    rep = fpfix.fix_lib(lib, dry_run=True)
    # the 0.06 artifact dot inside pad 1 cannot print and sits on copper -> gone
    dele = [a for a in _actions(rep, C0603) if a["action"] == "delete_unprintable_silk"]
    assert len(dele) == 1 and dele[0]["width_mm"] == 0.06 and dele[0]["gap_mm"] < 0
    # the switch's pin-1 dot is 0.6 mm clear of copper: kept, and made printable
    prom = [a for a in _actions(rep, SWITCH) if a["action"] == "promote_silk_width"]
    assert len(prom) == 1
    assert prom[0]["from_mm"] == 0.06 and prom[0]["to_mm"] == fpfix.MIN_LINE_WIDTH


def test_rule_a_leaves_filled_graphics_alone(lib):
    """A solid fp_poly prints from its fill; widening its stroke would grow it."""
    before = (lib / f"{SWITCH}.kicad_mod").read_text(encoding="utf-8")
    fpfix.fix_lib(lib, names=[SWITCH])
    after = (lib / f"{SWITCH}.kicad_mod").read_text(encoding="utf-8")
    for line in before.splitlines():
        if "fp_poly" in line and "solid" in line:
            assert line in after.splitlines(), "a filled slider indicator changed"
    assert before.count("(fp_poly") == after.count("(fp_poly")


def test_rule_b_narrows_stroke_and_never_moves_a_coordinate(lib):
    rep = fpfix.fix_lib(lib, dry_run=True)
    nar = [a for a in _actions(rep, C0603) if a["action"] == "narrow_silk_stroke"]
    # EDITS.md measured 0.25 -> 0.20 buying 0.025 mm per edge: 0.135 -> 0.160
    assert nar and all(a["from_mm"] == 0.25 and a["to_mm"] == 0.20 for a in nar)
    assert all(a["new_gap_mm"] >= fpfix.MIN_GAP for a in nar)
    # violators sharing an original width narrow together (one uniform outline)
    led = [a for a in _actions(rep, LED) if a["action"] == "narrow_silk_stroke"]
    assert led and {a["to_mm"] for a in led} == {0.15}

    before = (lib / f"{C0603}.kicad_mod").read_text(encoding="utf-8")
    fpfix.fix_lib(lib, names=[C0603])
    after = (lib / f"{C0603}.kicad_mod").read_text(encoding="utf-8")
    # silk only: rule E rebuilds this courtyard (it misses the pads) on purpose
    coords = lambda t: [l.split("(layer")[0] for l in t.splitlines()
                        if "fp_line" in l and "SilkS" in l]
    assert coords(before) and coords(before) == coords(after), "a coordinate moved"


def test_rule_c_converts_plated_pegs_keeping_position_and_drill(lib):
    rep = fpfix.fix_lib(lib, names=[USBC])
    pegs = [a for a in _actions(rep, USBC) if a["action"] == "peg_to_npth"]
    assert len(pegs) == 2
    assert {tuple(p["at"]) for p in pegs} == {(2.89, -1.3), (-2.89, -1.3)}
    assert all(p["drill_mm"] == 0.65 for p in pegs)
    text = (lib / f"{USBC}.kicad_mod").read_text(encoding="utf-8")
    assert text.count("np_thru_hole") == 2
    assert '(at 2.89 -1.30) (size 0.65 0.65) (drill 0.65)' in text
    assert _report(rep, USBC)["after"]["plated_pegs"] == 0


def test_rule_d_deletes_legend_text_hidden_under_the_body(lib):
    rep = fpfix.fix_lib(lib, names=[SWITCH])
    gone = {a["text"] for a in _actions(rep, SWITCH)
            if a["action"] == "delete_body_legend_text"}
    assert gone == {"ON", "1", "2", "3", "KE"}
    text = (lib / f"{SWITCH}.kicad_mod").read_text(encoding="utf-8")
    assert "(fp_text user %R" in text, "the %R placeholder must survive"
    assert text.count("(fp_text user") == 1


def test_every_fixture_ends_clean_and_the_fix_is_idempotent(lib):
    first = fpfix.fix_lib(lib)
    assert first["status"] == "pass" and first["residue"] == 0
    assert first["changed"] == 4
    for r in first["results"]:
        assert r["after"]["under_gap_silk"] == 0
        assert r["after"]["plated_pegs"] == 0
        assert r["after"]["worst_gap_mm"] is None or r["after"]["worst_gap_mm"] >= fpfix.MIN_GAP
    second = fpfix.fix_lib(lib)
    assert second["changed"] == 0 and second["actions"] == 0


def test_footprint_names_containing_dots_are_matched_whole():
    """Path().stem would strip '.3-BL' from '...-LS9.3-BL' and skip the part."""
    assert fpfix._fp_stem(f"{SWITCH}.kicad_mod") == SWITCH
    assert fpfix._fp_stem(SWITCH) == SWITCH


def test_residue_is_reported_when_no_rule_can_repair(tmp_path):
    """Silk whose CENTERLINE is inside pad copper cannot be narrowed clear."""
    d = tmp_path / "x.pretty"
    d.mkdir()
    (d / "bad.kicad_mod").write_text(
        '(module bad (layer F.Cu)\n'
        '  (pad 1 smd rect (at 0 0) (size 2 2) (layers F.Cu F.Paste F.Mask))\n'
        '  (fp_line (start -0.5 0) (end 0.5 0) (layer F.SilkS) (width 0.2))\n'
        ')\n', encoding="utf-8")
    rep = fpfix.fix_lib(d)
    assert rep["status"] == "residue" and rep["residue"] == 1
    assert _actions(rep, "bad")[0]["action"] == "residue_silk_gap"


def test_cli_contract(tmp_path, lib):
    out = tmp_path / "r.json"
    rc = fpfix.main(["--lib", str(lib), "--dry-run", "--out", str(out)])
    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["script"] == "fpfix" and payload["status"] == "pass"
    assert payload["footprints"] == 4 and payload["changed"] == 4
    assert fpfix.main(["--lib", str(tmp_path / "nope.pretty")]) == 2


# ------------------------------------------------- real DRC (the only oracle)

@pytest.mark.kicad_recorded
@pytest.mark.smoke
def test_scratch_drc_pristine_then_fixed(lib):
    """18 -> 4 violations, measured the way EDITS.md measured them. The 4 left
    are the USB-C's own pads against its own peg holes (0.18 and 0.21 mm against
    KiCad's 0.25 mm hole clearance): vendor geometry that fpfix doesn't move."""
    before = fpfix.scratch_drc(lib)
    assert before["counts"]["total"] == 18
    assert before["by_check"] == {"annular_width": 2, "clearance": 2, "hole_clearance": 2,
                                  "padstack": 2, "silk_overlap": 8, "silk_over_copper": 2}
    fpfix.fix_lib(lib)
    after = fpfix.scratch_drc(lib)
    assert after["by_check"] == {"hole_clearance": 4}


@pytest.mark.smoke
def test_fixed_footprints_still_load_in_kicad(lib):
    fpfix.fix_lib(lib)
    assert lib_pull._verify_load(lib)["ok"] is True


# ------------------------------------------------------- lib_pull integration

def test_lib_pull_post_pull_hygiene_runs_both_repairs(lib):
    names = [C0603, LED, SWITCH, USBC]
    fixed = lib_pull._autofix(lib, names, dry_run=False)
    assert fixed["changed"] == 4 and fixed["residue"] == 0
    norm = lib_pull._refdes_norm(lib, dry_run=False)
    assert norm["changed"] == 4 and norm["skipped"] == 0
    # a refdes must end up derived from the part, not the blanket -4.0 mm
    text = (lib / f"{C0603}.kicad_mod").read_text(encoding="utf-8")
    assert "(at 0 -4)" not in text and "(at 0 -4.0" not in text
    # order matters: refdes normalisation measures against the silk that SURVIVES
    assert fpfix.fix_lib(lib)["changed"] == 0


def test_lib_pull_declares_the_new_flags():
    src = (SCRIPTS / "lib_pull.py").read_text(encoding="utf-8")
    for flag in ("--no-autofix", "--no-refdes-norm", "--verify-drc"):
        assert flag in src
    assert "out_dir.resolve()" in src, "relative --out-dir bakes dead 3D model paths"


# ------------------------------------------------- schlib.apply_native_dnp

def _instance(ref, value="10n", dnp="no", extra=""):
    """One saved symbol instance, in the order kicad-sch-api 0.5.6 emits it:
    `(dnp no)` then fields_autoplaced/uuid then the Reference property."""
    return (f'\t(symbol\n\t\t(lib_id "Device:C")\n\t\t(at 100 100 0)\n'
            f'\t\t(in_bom yes)\n\t\t(on_board yes)\n\t\t(dnp {dnp})\n'
            f'\t\t(fields_autoplaced no)\n\t\t(uuid "u-{ref}")\n'
            f'\t\t(property "Reference" "{ref}"\n\t\t\t(at 1 2 0)\n\t\t)\n'
            f'\t\t(property "Value" "{value}"\n\t\t\t(at 1 4 0)\n\t\t)\n'
            f'{extra}\t)\n')


def _sch(tmp_path, *blocks, name="dnp.kicad_sch"):
    p = tmp_path / name
    p.write_text("(kicad_sch\n" + "".join(blocks) + ")\n", encoding="utf-8")
    return p


def test_apply_native_dnp_flips_only_the_named_instance(tmp_path):
    """"the nearest preceding `(dnp no)` before that property belongs
    unambiguously to this instance" - so C2's own flag must be untouched, and
    in_bom/on_board/custom fields survive."""
    sch = _sch(tmp_path, _instance("C1"),
               _instance("C2", extra='\t\t(property "Variant" "DNF"\n\t\t)\n'))
    schlib.apply_native_dnp(["C2"], sch)
    text = sch.read_text(encoding="utf-8")
    # the flag sits BEFORE its own Reference property, so slice at the instance
    # boundary, not at the property
    _, c1, c2 = text.split("\t(symbol\n")
    assert '"C1"' in c1 and '"C2"' in c2
    assert "(dnp no)" in c1 and "(dnp yes)" not in c1      # C1 untouched
    assert "(dnp yes)" in c2 and "(dnp no)" not in c2      # C2 flipped
    assert text.count("(in_bom yes)") == 2                 # left alone
    assert text.count("(on_board yes)") == 2
    assert '(property "Variant" "DNF"' in c2               # custom field survives


def test_apply_native_dnp_flips_several_and_is_order_independent(tmp_path):
    a = _sch(tmp_path, *(_instance(r) for r in ("C1", "C2", "C3")), name="a.kicad_sch")
    b = _sch(tmp_path, *(_instance(r) for r in ("C1", "C2", "C3")), name="b.kicad_sch")
    schlib.apply_native_dnp(["C1", "C3"], a)
    schlib.apply_native_dnp(["C3", "C1"], b)
    assert a.read_text(encoding="utf-8") == b.read_text(encoding="utf-8")
    _, c1, c2, c3 = a.read_text(encoding="utf-8").split("\t(symbol\n")
    assert "(dnp yes)" in c1 and "(dnp yes)" in c3
    assert "(dnp no)" in c2 and "(dnp yes)" not in c2   # the one not named


def test_apply_native_dnp_refuses_absent_and_duplicate_refs(tmp_path):
    """A ref that is missing, or that appears twice, has no unambiguous
    `(dnp no)` of its own - raise rather than flip a neighbour's."""
    sch = _sch(tmp_path, _instance("C1"), _instance("C2"))
    before = sch.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="not found"):
        schlib.apply_native_dnp(["C9"], sch)
    assert sch.read_text(encoding="utf-8") == before   # nothing written

    dup = _sch(tmp_path, _instance("C1"), _instance("C1"), name="dup.kicad_sch")
    with pytest.raises(ValueError, match="not unique"):
        schlib.apply_native_dnp(["C1"], dup)

    orphan = _sch(tmp_path, _instance("C1").replace("(dnp no)\n\t\t", ""),
                  name="orphan.kicad_sch")
    with pytest.raises(ValueError, match="no preceding"):
        schlib.apply_native_dnp(["C1"], orphan)


def test_apply_native_dnp_empty_list_is_a_noop(tmp_path):
    sch = _sch(tmp_path, _instance("C1"))
    before = sch.read_text(encoding="utf-8")
    schlib.apply_native_dnp([], sch)
    assert sch.read_text(encoding="utf-8") == before


# ------------------------------------ schem_refdes.strip_private_properties

_PRIVATE = (
    '\t\t(property "private" "KLC_S3.3" The rectangle is not (a) quoted string\n'
    '\t\t\t(at 0 0 0)\n'
    '\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t)\n\t\t\t)\n'
    '\t\t)\n')
_PRIVATE_UNBALANCED_QUOTE = (
    '\t\t(property "private" "KLC_S4.1" note with a "quoted ) paren" in it\n'
    '\t\t\t(at 0 0 0)\n\t\t)\n')
_KEEP = ('\t\t(property "Value" "Crystal_GND24"\n\t\t\t(at 0 -3.81 0)\n\t\t)\n')
_KEEP_REF = ('\t\t(property "Reference" "Y"\n\t\t\t(at 0 3.81 0)\n\t\t)\n')


def _lib(tmp_path, body, name="lib.kicad_sch"):
    p = tmp_path / name
    p.write_text('(kicad_sch\n\t(lib_symbols\n\t\t(symbol "Device:Crystal_GND24"\n'
                 + body + "\t\t)\n\t)\n)\n", encoding="utf-8")
    return p


def test_strip_private_properties_counts_and_keeps_the_others(tmp_path):
    """Returns the number removed; every non-private property survives whole."""
    sch = _lib(tmp_path, _KEEP_REF + _PRIVATE + _KEEP + _PRIVATE_UNBALANCED_QUOTE)
    assert sr.strip_private_properties(sch) == 2
    text = sch.read_text(encoding="utf-8")
    assert '"private"' not in text
    assert "KLC_S3.3" not in text and "KLC_S4.1" not in text
    assert _KEEP.strip() in text and _KEEP_REF.strip() in text
    # the balanced-block walk must not eat past its own closing paren
    assert text.count("(property") == 2
    assert text.rstrip().endswith(")")


def test_strip_private_properties_removes_a_balanced_block_exactly(tmp_path):
    """Removing the block leaves the file byte-identical to one written
    without it - no stray blank line, no swallowed neighbour."""
    with_ = _lib(tmp_path, _KEEP_REF + _PRIVATE + _KEEP, name="with.kicad_sch")
    without = _lib(tmp_path, _KEEP_REF + _KEEP, name="without.kicad_sch")
    assert sr.strip_private_properties(with_) == 1
    assert with_.read_text(encoding="utf-8") == without.read_text(encoding="utf-8")


def test_strip_private_properties_leaves_a_clean_file_alone(tmp_path):
    sch = _lib(tmp_path, _KEEP_REF + _KEEP)
    before = sch.read_text(encoding="utf-8")
    assert sr.strip_private_properties(sch) == 0
    assert sch.read_text(encoding="utf-8") == before


def test_strip_private_properties_does_not_match_a_private_named_field(tmp_path):
    """The pattern is the bare `private` FLAG that kicad-sch-api mis-reads as a
    name. A field whose value happens to say 'private' is ordinary data."""
    keep = '\t\t(property "Note" "private" \n\t\t\t(at 0 0 0)\n\t\t)\n'
    sch = _lib(tmp_path, _KEEP_REF + keep)
    assert sr.strip_private_properties(sch) == 0
    assert '"Note" "private"' in sch.read_text(encoding="utf-8")


# ------------------------------------------------------------- schem_refdes

@pytest.fixture
def sheets(tmp_path) -> list[Path]:
    dst = tmp_path / "s7"
    shutil.copytree(S7, dst)
    return [dst / "blinky2" / "kicad" / "blinky2.kicad_sch",
            dst / "hierdemo" / "kicad" / "hierdemo.kicad_sch",
            dst / "hierdemo" / "kicad" / "power.kicad_sch",
            dst / "hierdemo" / "kicad" / "load.kicad_sch"]


def test_library_to_page_transform_matches_a_wired_pin():
    """Device:C pin 1 is at library (0, +3.81); blinky2 wires it at page y-3.81."""
    sheet = sr.Sheet(S7 / "blinky2" / "kicad" / "blinky2.kicad_sch")
    c1 = next(s for s in sheet.symbols if s["ref"] == "C1")
    assert c1["at"][:2] == (152.4, 240.03)
    x, y = sr.to_page(0.0, 3.81, c1["at"], None)
    assert (round(x, 2), round(y, 2)) == (152.4, 236.22)
    x2, y2 = sr.to_page(0.0, 3.81, (152.4, 240.03, 180.0), None)
    assert (round(x2, 2), round(y2, 2)) == (152.4, 243.84)


def test_text_box_honours_justification():
    left = sr.text_box("U1", (10.0, 10.0), (1.27, 1.27), 0.15, "left")
    right = sr.text_box("U1", (10.0, 10.0), (1.27, 1.27), 0.15, "right")
    centre = sr.text_box("U1", (10.0, 10.0), (1.27, 1.27), 0.15, None)
    assert left.bounds[0] == 10.0 and right.bounds[2] == 10.0
    assert round(centre.centroid.x, 6) == 10.0
    assert round(left.bounds[3] - left.bounds[1], 4) == round(1.27 + 0.15, 4)


def test_audit_finds_the_overlaps_the_generator_leaves():
    """kicad-sch-api's y-sign puts fields on the wrong side; this is the baseline."""
    sheet = sr.Sheet(S7 / "blinky2" / "kicad" / "blinky2.kicad_sch")
    overlaps = sr.audit_sheet(sheet)
    assert len(overlaps) > 0
    assert any(o["field"].startswith("U2.") for o in overlaps)


@pytest.mark.kicad_recorded
def test_placement_clears_every_field_and_is_stable(sheets):
    payload, _ = sr.run(["--sch"] + [str(p) for p in sheets])
    # What stays is labels on parts: the audit reports them, a field placer
    # can't move them, and these frozen sheets have 19 of them.
    assert payload["status"] == "residue"
    assert payload["counts"]["residue"] == 19
    for row in payload["sheets"]:
        assert row["overlaps_before"], f"{row['sheet']} had nothing to fix"
        assert row["residue"] == []
        assert not [o for o in row["overlaps_after"]
                    if o["field"].endswith((".Reference", ".Value"))]
        assert row["write"]["applied"] > 0
    # re-running must be a no-op: same inputs, same candidate ladder
    again, _ = sr.run(["--sch"] + [str(p) for p in sheets])
    assert again["counts"]["moved"] == 0
    assert again["counts"]["residue"] == 19


def test_offsets_are_consistent_within_a_class(sheets):
    payload, _ = sr.run(["--sch", str(sheets[0]), "--dry-run"])
    placed = payload["sheets"][0]["placements"]
    caps = {p["ref"]: p for p in placed
            if p["field"] == "Reference" and p["ref"].startswith("C")}
    assert len(caps) >= 4
    sheet = sr.Sheet(sheets[0])
    at = {s["ref"]: s["at"] for s in sheet.symbols}
    offsets = {(round(p["to"][0] - at[r][0], 3), round(p["to"][1] - at[r][1], 3))
               for r, p in caps.items()}
    assert len(offsets) == 1, f"same class, different offsets: {offsets}"


def test_write_preserves_lib_symbols(sheets):
    text = sheets[0].read_text(encoding="utf-8")
    before = sr._lib_symbol_names(text)
    assert len(before) >= 10
    sr.run(["--sch", str(sheets[0])])
    placed = sheets[0].read_text(encoding="utf-8")
    assert placed != text
    assert sr._lib_symbol_names(placed) == before
    # byte for byte: moving a field must not take a part's pins from the
    # installed library (Crystal_GND24 changed its pin 2 in KiCad 10.0.6)
    a, b = sr._lib_symbols_span(text), sr._lib_symbols_span(placed)
    assert placed[b[0]:b[1]] == text[a[0]:a[1]]


def test_cli_exit_codes(tmp_path, sheets):
    out = tmp_path / "p.json"
    rc = sr.main(["--sch", str(sheets[0]), "--dry-run", "--out", str(out)])
    assert rc == 0
    assert json.loads(out.read_text(encoding="utf-8"))["mode"] == "dry-run"
    assert sr.main(["--sch", str(tmp_path / "missing.kicad_sch")]) == 2


# ------------------------------------------------- V19 rotation/mirror oracle
#
# tests/fixtures/sch/rotmirror: 7 Device:R instances (rot 0/90/180/270,
# mirror x, mirror y, rot90+mirror x), every pin wired stub+label, captured
# ONLY after kicad-cli 10.0.3 ERC reported 0/0 AND the exported netlist put
# every pin on its designed net.  What the build falsified (T6): the V19
# "inward stubs" were ksa's pin POSITIONS mirrored at 90/270 (fixed in
# schlib.pin_pos), NOT stub_dir's sign; and to_page originally composed
# mirror BEFORE rotation - KiCad rotates first, which SWAPS the pins of a
# rotated+mirrored part (caught by the netlist leg, invisible to ERC).

ROTMIRROR = ROOT / "tests" / "fixtures" / "sch" / "rotmirror" / "rotmirror.kicad_sch"

# the fixture's design table: ref -> (rotation, mirror, {pin: net})
ROTMIRROR_DESIGN = {
    "R1": (0.0, None, {"1": "N1", "2": "N7"}),
    "R2": (90.0, None, {"1": "N2", "2": "N1"}),
    "R3": (180.0, None, {"1": "N3", "2": "N2"}),
    "R4": (270.0, None, {"1": "N4", "2": "N3"}),
    "R5": (0.0, "x", {"1": "N5", "2": "N4"}),
    "R6": (0.0, "y", {"1": "N6", "2": "N5"}),
    "R7": (90.0, "x", {"1": "N7", "2": "N6"}),
}


def _numbered_lib_pins(sheet, lib_id):
    """[(number, (lx, ly), lib_rotation, length)] of an embedded symbol."""
    block = sr._kid(sheet.root, "lib_symbols")
    for sym in sr._kids(block, "symbol"):
        if str(sr._tok(sym[1])) != lib_id:
            continue
        out = []
        for sub in [sym] + sr._kids(sym, "symbol"):
            for g in sub[1:]:
                if isinstance(g, list) and g and sr._tok(g[0]) == "pin":
                    a = sr._nums(sr._kid(g, "at"))
                    ln = sr._nums(sr._kid(g, "length"))
                    num = str(sr._tok(sr._kid(g, "number")[1]))
                    out.append((num, (a[0], a[1]), a[2], ln[0]))
        return out
    raise AssertionError(f"{lib_id} not embedded in the fixture")


def _wires_and_labels(sheet):
    wires = [sr._pts(w) for w in sr._kids(sheet.root, "wire")]
    labels = []
    for lb in sr._kids(sheet.root, "label"):
        a = sr._nums(sr._kid(lb, "at"))
        labels.append((str(sr._tok(lb[1])), (a[0], a[1])))
    return wires, labels


def _close(a, b, tol=0.01):
    return abs(a[0] - b[0]) < tol and abs(a[1] - b[1]) < tol


def test_rotmirror_covers_all_configs_and_to_page_hits_wire_endpoints():
    """to_page(pin) must BE a wire endpoint for every rot/mirror config."""
    sheet = sr.Sheet(ROTMIRROR)
    got = {s["ref"]: (s["at"][2], s["mirror"]) for s in sheet.symbols}
    assert got == {r: (rot, mir)
                   for r, (rot, mir, _n) in ROTMIRROR_DESIGN.items()}
    wires, _ = _wires_and_labels(sheet)
    endpoints = [p for w in wires for p in (w[0], w[-1])]
    for sym in sheet.symbols:
        for _num, lp, _rot, _ln in _numbered_lib_pins(sheet, sym["lib_id"]):
            page = sr.to_page(lp[0], lp[1], sym["at"], sym["mirror"])
            assert any(_close(page, e) for e in endpoints), \
                f"{sym['ref']} pin at {page}: no wire endpoint there"


def test_rotmirror_per_pin_net_assignment():
    """Each pin's wire must end at the label of ITS designed net - pins
    swapped by a wrong transform order pass ERC but fail here (this is the
    hermetic twin of the netlist oracle the fixture was captured against)."""
    sheet = sr.Sheet(ROTMIRROR)
    wires, labels = _wires_and_labels(sheet)
    for sym in sheet.symbols:
        _rot, _mir, nets = ROTMIRROR_DESIGN[sym["ref"]]
        for num, lp, _lr, _ln in _numbered_lib_pins(sheet, sym["lib_id"]):
            page = sr.to_page(lp[0], lp[1], sym["at"], sym["mirror"])
            far = next((w[-1] if _close(page, w[0]) else w[0]
                        for w in wires
                        if _close(page, w[0]) or _close(page, w[-1])), None)
            assert far is not None, f"{sym['ref']}.{num}: no wire at {page}"
            hit = next((n for n, at in labels if _close(far, at)), None)
            assert hit == nets[num], (
                f"{sym['ref']}.{num}: wired to '{hit}', designed "
                f"'{nets[num]}'")


def test_rotmirror_stub_dir_agrees_with_transform():
    """schlib.stub_dir vs the to_page-derived outward pin axis: they agree
    at every rotation (V19's sign bug was ksa's positions, not stub_dir)."""
    sheet = sr.Sheet(ROTMIRROR)
    for sym in sheet.symbols:
        if sym["mirror"] is not None:
            continue                      # stub_dir has no mirror input
        for num, lp, librot, ln in _numbered_lib_pins(sheet, sym["lib_id"]):
            conn = sr.to_page(lp[0], lp[1], sym["at"], None)
            import math
            body = sr.to_page(lp[0] + ln * math.cos(math.radians(librot)),
                              lp[1] + ln * math.sin(math.radians(librot)),
                              sym["at"], None)
            d = (conn[0] - body[0], conn[1] - body[1])
            n = max(abs(d[0]), abs(d[1]))
            outward = (round(d[0] / n), round(d[1] / n))
            assert schlib.stub_dir(librot, sym["at"][2]) == outward, \
                f"{sym['ref']}.{num}"


def test_rotmirror_place_sheet_clears_every_field():
    res = sr.place_sheet(sr.Sheet(ROTMIRROR))
    assert res["residue"] == []


@pytest.mark.smoke
def test_rotmirror_erc_oracle():
    """The machine oracle itself: kicad-cli ERC must stay 0/0."""
    erc = subprocess.run([sys.executable, str(SCRIPTS / "kc.py"), "erc",
                          str(ROTMIRROR)], capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    payload = json.loads(erc.stdout)
    assert payload["counts"]["total"] == 0, payload["violations"]


@pytest.mark.kicad_recorded
@pytest.mark.smoke
def test_placement_is_electrically_inert(sheets):
    """Fields carry no connectivity: ERC stays clean, netlist stays identical."""
    sr.run(["--sch"] + [str(p) for p in sheets])
    erc = subprocess.run([sys.executable, str(SCRIPTS / "kc.py"), "erc",
                          str(sheets[0])], capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    assert json.loads(erc.stdout)["counts"]["total"] == 0
    cmp_ = subprocess.run([sys.executable, str(SCRIPTS / "netlist_audit.py"),
                           "--sch", str(sheets[0]), "--compare",
                           str(S7 / "blinky2" / "golden.net")],
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    assert cmp_.returncode == 0, cmp_.stdout[-400:]


# ------------------------------------- rule F and the 3D-model flags (lite-main)

FLAGS = ROOT / "tests" / "fixtures" / "lib" / "flags"
HTSSOP = "HTSSOP-32_L11.0-W6.1-P0.65-LS8.1-BL-EP"   # TPA3118D2, C46497
RCA = "CONN-TH_XDJK-0271-1650"                     # stacked RCA, C7527522


def _box(x1, y1, x2, y2, z1, z2) -> str:
    """One VRML box, from footprint mm (y down) to KiCad's 0.1 in, y up."""
    u = 2.54
    pts = [(x, -y, z) for z in (z1, z2) for y in (y1, y2) for x in (x1, x2)]
    faces = [(0, 1, 3, 2), (4, 5, 7, 6), (0, 1, 5, 4), (2, 3, 7, 6),
             (0, 2, 6, 4), (1, 3, 7, 5)]
    return ("Shape { geometry IndexedFaceSet { coord Coordinate { point [ "
            + ", ".join(f"{x / u:.4f} {y / u:.4f} {z / u:.4f}" for x, y, z in pts)
            + " ] } coordIndex [ "
            + ", ".join(", ".join(map(str, f)) + ", -1" for f in faces)
            + " ] } }\n")


def _rca_model(lib: Path, dy: float = 0.0) -> None:
    """The RCA jack's model, as easyeda2kicad's has it: a body, three pins
    into the board at the pads, two locating pegs where the footprint has
    no hole. dy shifts the whole model (a model off its footprint)."""
    shapes = _box(-8.85, -2.25 + dy, 11.97, 16.85 + dy, 0, 10)
    for x, y in ((-4.0, -1.95), (0.0, 1.95), (4.0, -1.15)):
        shapes += _box(x - 0.5, y - 0.15 + dy, x + 0.5, y + 0.15 + dy, -4.5, 0)
    for x in (-7.66, 7.64):
        shapes += _box(x - 0.65, 0.3 + dy, x + 0.65, 2.3 + dy, -2.0, 0)
    d = lib.parent / "aiee.3dshapes"
    d.mkdir(exist_ok=True)
    (d / f"{RCA}.wrl").write_text("#VRML V2.0 utf8\n" + shapes)


@pytest.fixture
def flagged(tmp_path) -> Path:
    d = tmp_path / "lib" / "aiee.pretty"
    shutil.copytree(FLAGS, d)
    return d


def test_rule_f_numbers_ep_vias_and_drops_their_paste(flagged):
    rep = fpfix.fix_lib(flagged, names=[HTSSOP])
    vias = [a for a in _actions(rep, HTSSOP) if a["action"] == "number_ep_via"]
    assert len(vias) == 9 and {a["pad"] for a in vias} == {"33"}
    assert all(a["paste_dropped"] for a in vias)
    text = (flagged / f"{HTSSOP}.kicad_mod").read_text(encoding="utf-8")
    assert '(pad ""' not in text
    vias = [l for l in text.splitlines() if "thru_hole" in l]
    assert len(vias) == 9
    assert all(l.lstrip().startswith("(pad 33 thru_hole") and "Paste" not in l
               and "(zone_connect 2)" in l for l in vias)
    # the EP itself keeps its paste; a re-run changes nothing
    assert "(pad 33 smd rect (at 0.00 0.00 0.00) (size 3.810 3.710) (layers F.Cu F.Paste F.Mask))" in text
    assert fpfix.fix_lib(flagged, names=[HTSSOP])["changed"] == 0


def test_rule_f_leaves_pegs_and_free_holes_alone(lib):
    """The USB-C's unnamed holes are zero-ring pegs outside any SMD pad:
    rule C's, not F's."""
    rep = fpfix.fix_lib(lib, dry_run=True, names=[USBC])
    assert not [a for a in _actions(rep, USBC) if a["rule"] == "F"]


def test_flags_a_peg_with_no_hole_and_a_body_outside_the_courtyard(flagged):
    _rca_model(flagged)
    rep = fpfix.fix_lib(flagged, names=[RCA])
    flags = _report(rep, RCA)["flags"]
    pegs = [f for f in flags if f["flag"] == "peg_without_hole"]
    assert sorted(round(f["at"][0]) for f in pegs) == [-8, 8]
    assert all(abs(f["at"][1] - 1.3) < 0.01 for f in pegs)
    body = [f for f in flags if f["flag"] == "body_outside_courtyard"]
    assert len(body) == 1 and body[0]["overhang_mm"]["right"] > 3.5
    assert rep["flags"] == 3 and rep["status"] == "pass"     # flags never fail


def test_flags_a_model_off_its_footprint_and_nothing_else(flagged):
    _rca_model(flagged, dy=2.15)
    flags = _report(fpfix.fix_lib(flagged, dry_run=True, names=[RCA]), RCA)["flags"]
    assert [f["flag"] for f in flags] == ["model_off_footprint"]
    assert flags[0]["pins_met"] == 0 and flags[0]["pins"] == 3


def test_no_model_no_flags(flagged):
    assert _report(fpfix.fix_lib(flagged, dry_run=True, names=[RCA]), RCA)["flags"] == []


def test_lib_pull_autofix_reports_the_flags(flagged):
    _rca_model(flagged)
    out = lib_pull._autofix(flagged, [RCA, HTSSOP], dry_run=True)
    assert {f["flag"] for f in out["flags"]} == {"peg_without_hole", "body_outside_courtyard"}
    assert {f["footprint"] for f in out["flags"]} == {RCA}


# ------------------------------------------- schem_refdes multi-unit symbols

_DUAL = """(kicad_sch
  (lib_symbols
    (symbol "Amp:Dual"
      (symbol "Dual_0_1"
        (polyline (pts (xy 0 0) (xy 0 1))))
      (symbol "Dual_1_1"
        (rectangle (start -5 -5) (end 5 5))
        (pin input line (at -7.54 0 0) (length 2.54)))
      (symbol "Dual_2_1"
        (rectangle (start 20 -5) (end 30 5))
        (pin input line (at 17.46 0 0) (length 2.54)))
      (symbol "Dual_2_2"
        (rectangle (start 60 -5) (end 70 5)))))
  (symbol (lib_id "Amp:Dual") (at 100 100 0) (unit 2)
    (property "Reference" "U1" (at 100 90 0)))
  (symbol (lib_id "Amp:Dual") (at 100 200 0)
    (property "Reference" "U2" (at 100 190 0))))
"""


def test_multi_unit_symbol_body_is_only_its_own_unit(tmp_path):
    """A placed unit draws its own sub-symbol plus the unit-0 common one, body
    style 1 only. The union of every unit overstated U1 (unit 2) by the
    whole of unit 1 and the De Morgan body, which pushed fields off it."""
    p = tmp_path / "dual.kicad_sch"
    p.write_text(_DUAL, encoding="utf-8")
    sheet = sr.Sheet(p)
    lib = sheet.libs["Amp:Dual"]
    assert set(lib["units"]) == {1, 2}
    by_ref = {s["ref"]: s for s in sheet.symbols}
    assert by_ref["U1"]["unit"] == 2 and by_ref["U2"]["unit"] == 1
    x0, _, x1, _ = sheet.body(by_ref["U1"]).bounds
    assert (x0, x1) == pytest.approx((100.0, 130.0))   # pin end .. rect, unit 2
    x0, _, x1, _ = sheet.body(by_ref["U2"]).bounds
    assert (x0, x1) == pytest.approx((92.46, 105.0))   # unit 1 only
    # a single-unit symbol keeps its whole geometry
    assert sr.lib_unit({"graphics": [], "pins": []}, 3) == {"graphics": [], "pins": []}


# ------------------------------------------------------ schlib.apply_hier_shapes

def test_apply_hier_shapes_puts_back_each_declared_shape(tmp_path):
    """kicad-sch-api saves every hierarchical label as `(shape input)`; the
    shapes the sheet's hier_pins declared must come back, others untouched."""
    sch = tmp_path / "child.kicad_sch"
    sch.write_text('(kicad_sch\n'
                   '\t(hierarchical_label "SDA"\n\t\t(shape input)\n\t\t(at 1 2 0)\n\t)\n'
                   '\t(hierarchical_label "VOUT" (shape input) (at 3 4 0))\n'
                   '\t(hierarchical_label "EN" (shape input) (at 5 6 0))\n)\n',
                   encoding="utf-8")
    schlib.apply_hier_shapes({"SDA": "bidirectional", "VOUT": "output"}, sch)
    text = sch.read_text(encoding="utf-8")
    assert '(hierarchical_label "SDA"\n\t\t(shape bidirectional)' in text
    assert '(hierarchical_label "VOUT" (shape output)' in text
    assert '(hierarchical_label "EN" (shape input)' in text


# ---------------------------------------------------------------- rule H (#587)
BD10 = ROOT / "pinned" / "core" / "lib" / "aiee.pretty" / "CAP-TH_BD10.0-P5.00-D1.0-FD.kicad_mod"
PADS_ONLY = "".join(
    f"\t(fp_line (start {a}) (end {b}) (layer F.CrtYd) (width 0.05))\n"
    for a, b in (("-3.60 -1.05", "3.60 -1.05"), ("3.60 -1.05", "3.60 1.05"),
                 ("3.60 1.05", "-3.60 1.05"), ("-3.60 1.05", "-3.60 -1.05")))
CIRCLE_CY = "\t(fp_circle (center 0 0) (end 5.3 0) (layer F.CrtYd) (width 0.05))\n"
SILK_CAN = "\t(fp_circle (center 0.00 0.00) (end 5.00 0.00) (layer F.SilkS) (width 0.25))\n"


def _bd10(courtyard: str) -> str:
    text = BD10.read_text()
    assert CIRCLE_CY in text and SILK_CAN in text
    return text.replace(CIRCLE_CY, courtyard)


def _eh(actions):
    return [a for a in actions if a["rule"] in ("E", "H")]


def test_rule_h_rebuilds_a_pads_only_courtyard_round_the_drawn_can():
    # EasyEDA's pull (hc-power, #587): the courtyard is the pads, the can a
    # 10 mm circle on the silk; placement packed parts under it
    new, acts = fpfix.sanitize(_bd10(PADS_ONLY))
    h = _eh(acts)
    assert [(a["rule"], a["action"]) for a in h] == [("H", "rebuild_courtyard_body")]
    assert h[0]["to_circle"] == [0.0, 0.0, 5.25] and h[0]["body_outside_mm2"] > 60
    cy = [ln.strip() for ln in new.splitlines() if "CrtYd" in ln]
    assert cy == ["(fp_circle (center 0.00 0.00) (end 5.25 0.00) (layer F.CrtYd) (width 0.05))"]
    again, acts2 = fpfix.sanitize(new)
    assert again == new and _eh(acts2) == []


def test_rule_h_twin_a_courtyard_round_the_can_is_left_alone():
    text = BD10.read_text()
    new, acts = fpfix.sanitize(text)
    assert _eh(acts) == [] and CIRCLE_CY in new


def test_rule_h_takes_a_round_model_only_and_never_guesses():
    # no body drawn: the footprint's own model, when it is a round can, is
    # the body; with no model either the courtyard is left as it is
    bare = _bd10(PADS_ONLY).replace(SILK_CAN, "")
    _, acts = fpfix.sanitize(bare)
    assert _eh(acts) == []
    body = fpfix._model_body(BD10.read_text(), BD10)
    assert body is not None and abs(body[2] - 5.0) < 0.05
    _, acts = fpfix.sanitize(bare, model_body=body)
    assert [a["action"] for a in _eh(acts)] == ["rebuild_courtyard_body"]
    # a square body is not a can
    assert fpfix.body_circle([], (0, 0, 5)) is None


def test_rule_h_keeps_an_old_courtyard_that_reaches_past_the_can():
    wide = _bd10(PADS_ONLY.replace("-3.60", "-7.00"))
    _, acts = fpfix.sanitize(wide)
    h = _eh(acts)
    assert [(a["rule"], a["action"]) for a in h] == [("H", "rebuild_courtyard")]
    assert h[0]["to_bbox"] == [-7.0, -5.25, 5.25, 5.25]


def test_rule_h_on_a_placed_board_footprint(tmp_path):
    fp = ('\t(footprint "aiee:CAP" (layer "F.Cu") (at 50 50 90)\n'
          '\t\t(attr through_hole)\n'
          '\t\t(property "Reference" "C1" (at 0 0 0) (layer "F.SilkS"))\n'
          '\t\t(pad "1" thru_hole circle (at -2.54 0 90) (size 1.6 1.6) (drill 1) (layers "*.Cu" "*.Mask"))\n'
          '\t\t(pad "2" thru_hole circle (at 2.54 0 90) (size 1.6 1.6) (drill 1) (layers "*.Cu" "*.Mask"))\n'
          '\t\t(fp_circle (center 0 0) (end 5 0) (stroke (width 0.25) (type solid)) (fill none) (layer "F.SilkS"))\n'
          '\t\t(fp_rect (start -3.6 -1.05) (end 3.6 1.05) (stroke (width 0.05) (type solid)) (fill none) (layer "F.CrtYd"))\n'
          '\t)\n')
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text("(kicad_pcb\n\t(version 20240108)\n" + fp + ")\n")
    out = fpfix.fix_board(pcb)
    assert [a["action"] for r in out["results"] for a in r["actions"]
            if a["rule"] == "H"] == ["rebuild_courtyard_body"]
    assert '(fp_circle (center 0.00 0.00) (end 5.25 0.00) (stroke (width 0.05) (type solid)) (fill none) (layer "F.CrtYd"))' in pcb.read_text()
    assert fpfix.fix_board(pcb)["changed"] == 0
