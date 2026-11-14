"""T6 Batch B acceptance tests: constraints_lint.py (T6-P1-4) + the seeded
reference/interfaces fragments (T6-P1-2).

The lint converts the 'keys you emit wrong are keys the pipeline silently
ignores' trap class into a deterministic exit-1. Contract under test:
  - misspelled keys (close match to a documented key) are ERRORS, at entry
    level (max_skew -> max_skew_mm) and top level (voltage -> voltages);
  - unknown keys with no close match are WARNINGS (advisory, exit 0) - the
    power.json thermal_constraints notes/role class;
  - '_'-prefixed keys are the comment convention, allowed at every level;
  - every committed board fragment/constraints file passes (exit 0);
  - the script's key-set is pinned to reference/constraints_schema.md so the
    two cannot drift apart.

Pure tests - no toolchain, no marker.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
REFERENCE = REPO / "reference"
PYTHON = sys.executable
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))
import constraints_lint  # noqa: E402
from _boards import BOARDS, SHIPPED_CHECKS, need_shipped_checks  # noqa: E402


def lint_doc(tmp_path: Path, doc, name: str = "frag.json"):
    p = tmp_path / name
    p.write_text(json.dumps(doc), encoding="utf-8")
    return constraints_lint.lint_file(p)


def errors(violations):
    return [v for v in violations if v["severity"] == "error"]


def warnings(violations):
    return [v for v in violations if v["severity"] == "warning"]


# ================================================== schema drift pin

def test_schema_md_declares_exactly_the_script_keys():
    """The md stays human-authoritative; the script's key-set is pinned to
    it. A key added to either alone fails here."""
    md = (REFERENCE / "constraints_schema.md").read_text(encoding="utf-8")
    block = re.search(r"```jsonc\n(.*?)```", md, re.S).group(1)
    declared = set(re.findall(r'^  "([a-z_]+)":', block, re.M))
    assert declared == constraints_lint.SCHEMA_TOP_KEYS


def test_entry_keys_cover_schema_md_documented_entry_keys():
    """Spot-pin the entry key sets the schema md documents (T2 additions
    included) so a schema extension cannot silently outrun the lint."""
    dp = constraints_lint.SECTIONS["diff_pairs"]
    assert "term_pair_mm" in dp["optional"]
    pw = constraints_lint.SECTIONS["power"]
    assert {"plane_fed", "pdn", "overrides", "min_width_mm",
            "switch_node"} <= set(pw["optional"])
    th = constraints_lint.SECTIONS["thermal"]
    assert {"airflow_lfm", "airflow_m_s", "rating",
            "theta_ja_c_w", "theta_source"} <= set(th["optional"])
    assert constraints_lint.COATINGS == {"none", "soldermask", "conformal"}


# ================================================== error paths (exit 1)

def test_misspelled_entry_key_is_error(tmp_path):
    vs, _ = lint_doc(tmp_path, {
        "diff_pairs": [{"p": "/A", "n": "/B", "max_skew": 5.0}]})
    errs = errors(vs)
    assert len(errs) == 1
    assert errs[0]["kind"] == "misspelled_key"
    assert "max_skew" in errs[0]["msg"] and "max_skew_mm" in errs[0]["msg"]


def test_misspelled_top_level_key_is_error(tmp_path):
    vs, _ = lint_doc(tmp_path, {"voltage": [{"net": "X", "voltage": 48}]})
    errs = errors(vs)
    assert [e["kind"] for e in errs] == ["misspelled_key"]
    assert "voltages" in errs[0]["msg"]


def test_missing_required_key_is_error(tmp_path):
    vs, _ = lint_doc(tmp_path, {"power": [{"net": "+3V3"}]})
    assert [e["kind"] for e in errors(vs)] == ["missing_key"]
    assert "current_a" in errors(vs)[0]["msg"]


def test_wrong_value_type_is_error(tmp_path):
    vs, _ = lint_doc(tmp_path, {"voltages": [{"net": "X", "voltage": "48"}]})
    assert [e["kind"] for e in errors(vs)] == ["bad_type"]


def test_pin_modes_shape(tmp_path):
    vs, _ = lint_doc(tmp_path, {"pin_modes": {"U1": {"PB11": "analog"}}})
    assert vs == []
    vs, _ = lint_doc(tmp_path, {"pin_modes": {"U1": "analog"}})
    assert [e["kind"] for e in errors(vs)] == ["bad_type"]


def test_io_supply_shape(tmp_path):
    vs, _ = lint_doc(tmp_path, {"io_supply": {"U101": "VDDD", "U3": "23"}})
    assert vs == []
    for bad in ({"U101": {"pin": "VDDD"}}, {"U101": ""}, ["U101", "VDDD"]):
        vs, _ = lint_doc(tmp_path, {"io_supply": bad})
        assert [e["kind"] for e in errors(vs)] == ["bad_type"], bad


def test_bool_is_not_a_number(tmp_path):
    vs, _ = lint_doc(tmp_path, {"voltages": [{"net": "X", "voltage": True}]})
    assert [e["kind"] for e in errors(vs)] == ["bad_type"]


def test_coating_enum_enforced(tmp_path):
    vs, _ = lint_doc(tmp_path, {"coating": "parylene"})
    assert [e["kind"] for e in errors(vs)] == ["bad_enum"]
    vs, _ = lint_doc(tmp_path, {"coating": "soldermask"}, "ok.json")
    assert not errors(vs)


def test_placement_edge_enum_and_keepout_shape(tmp_path):
    vs, _ = lint_doc(tmp_path, {"placement": {
        "edges": [{"ref": "J1", "edge": "north"}],
        "keepouts": [{"side": "front", "reason": "antenna"}]}})
    kinds = sorted(e["kind"] for e in errors(vs))
    assert kinds == ["bad_type", "missing_key"]  # edge enum + rect-or-poly


def test_placement_mates_shape(tmp_path):
    ok = {"ref": "J2", "mate": "onboard_flex",
          "corridor": [[0, 0, 5, 5], [5, 0, 9, 9]], "mate_refs": ["DS1"],
          "reason": "LCD tail"}
    vs, _ = lint_doc(tmp_path, {"placement": {"mates": [ok]}})
    assert not errors(vs) and not warnings(vs)
    vs, _ = lint_doc(tmp_path, {"placement": {"mates": [
        {**ok, "corridor": [0, 0, 5, 5]}]}}, "bad.json")
    assert [e["kind"] for e in errors(vs)] == ["bad_type"]


def test_voltage_pairs_shape(tmp_path):
    vs, _ = lint_doc(tmp_path, {"voltage_pairs": [
        {"a": "/POE_TAP_A1", "b": "/POE_TAP_A2", "voltage": 114}]})
    assert not errors(vs) and not warnings(vs)
    vs, _ = lint_doc(tmp_path, {"voltage_pairs": [
        {"a": "/HV_BULK", "b": "/DRAIN", "voltage": 219,
         "why": "RCD snubber clamps DRAIN"}]}, "why.json")
    assert not errors(vs) and not warnings(vs)
    vs, _ = lint_doc(tmp_path, {"voltage_pairs": [{"a": "/X", "voltage": 114}]},
                     "bad.json")
    assert [e["kind"] for e in errors(vs)] == ["missing_key"]


def test_power_overrides_validated(tmp_path):
    vs, _ = lint_doc(tmp_path, {"power": [{
        "net": "VBUS", "current_a": 5.0,
        "overrides": [{"near": [1, 2], "radius_mm": 3}]}]})
    assert [e["kind"] for e in errors(vs)] == ["missing_key"]


def test_envelope_alias_sections_validated(tmp_path):
    """power.json's power_constraints validates as the power shape."""
    vs, _ = lint_doc(tmp_path, {"power_constraints": [{"net": "+5V"}]})
    assert [e["kind"] for e in errors(vs)] == ["missing_key"]


def test_invalid_json_is_error(tmp_path):
    p = tmp_path / "broken.json"
    p.write_text("{not json", encoding="utf-8")
    vs, _ = constraints_lint.lint_file(p)
    assert [e["kind"] for e in errors(vs)] == ["invalid_json"]


# ================================================== warning paths (exit 0)

def test_unknown_entry_key_without_close_match_warns(tmp_path):
    """The shipped carrier power.json class: thermal entries carrying
    prose keys stay advisory. 'role' warns; 'notes' is prose the schema
    page allows on any entry, so it does not."""
    vs, _ = lint_doc(tmp_path, {"thermal_constraints": [
        {"ref": "U2", "power_w": 0.8, "role": "PoE switcher",
         "notes": "datasheet 8.2"}]})
    assert not errors(vs)
    ws = warnings(vs)
    assert [w["kind"] for w in ws] == ["unknown_key"]
    assert "key 'role'" in ws[0]["msg"]


def test_underscore_keys_allowed_everywhere(tmp_path):
    vs, _ = lint_doc(tmp_path, {
        "_comment": "x",
        "voltages": [{"net": "X", "voltage": 48, "_why": "y"}],
        "placement": {"_note": "z", "groups": [
            {"name": "g", "anchor": "Y1", "members": ["C1"], "_p8": "w"}]}})
    assert not errors(vs) and not warnings(vs)


def test_scout_candidates_over_contract_warns(tmp_path):
    vs, _ = lint_doc(tmp_path, {
        "block": "mcu",
        "candidates": [{"mpn": f"P{i}", "lcsc": f"C{i}"} for i in range(7)]})
    assert not errors(vs)
    assert [w["kind"] for w in warnings(vs)] == ["scout_candidates_over"]
    vs, _ = lint_doc(tmp_path, {
        "block": "mcu",
        "candidates": [{"mpn": "P", "lcsc": "C"}] * 6}, "six.json")
    assert not warnings(vs)


def test_non_object_root_warns_only(tmp_path):
    vs, _ = lint_doc(tmp_path, [1, 2, 3])
    assert not errors(vs)
    assert [w["kind"] for w in warnings(vs)] == ["not_object"]


def test_explicit_empty_diff_pairs_is_clean(tmp_path):
    """The pd-trigger DP/DM-short trap resolution must lint clean."""
    vs, _ = lint_doc(tmp_path, {"diff_pairs": []})
    assert not errors(vs) and not warnings(vs)


# ================================================== shipped artifacts

# Opt-in (_boards.SHIPPED_CHECKS): nothing globs BOARDS unless asked.
SHIPPED = sorted(
    list(BOARDS.glob("*/research/*.json"))
    + list(BOARDS.glob("*/architecture/constraints.json"))
    + list(BOARDS.glob("*/kicad/constraints.json"))) if SHIPPED_CHECKS else []


@pytest.mark.parametrize("path", SHIPPED or [None], ids=lambda p: (
    "not-opted-in" if p is None
    else "boards/" + p.relative_to(BOARDS).as_posix()))
def test_shipped_artifacts_have_no_errors(path):
    need_shipped_checks()
    if path is None:
        pytest.skip(f"needs the boards repo ({BOARDS}; set HWDE_BOARDS_ROOT)"
                    " - no shipped constraints found there")
    vs, _ = constraints_lint.lint_file(path)
    errs = errors(vs)
    assert not errs, [e["msg"] for e in errs]


# ================================================== seeded reference fragments

SEEDS = sorted((REFERENCE / "interfaces").glob("*.json"))


def test_three_interface_seeds_exist():
    names = {p.stem for p in SEEDS}
    assert {"usb-fs", "usbc-pd-sink", "ethernet-10-100"} <= names


@pytest.mark.parametrize("path", SEEDS, ids=lambda p: p.stem)
def test_interface_seed_lints_totally_clean(path):
    """Seeds are the standing conformance example - zero errors AND zero
    warnings, and each carries provenance + a companion md."""
    vs, _ = constraints_lint.lint_file(path)
    assert not vs, [v["msg"] for v in vs]
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert "_provenance" in doc and doc.get("interface") == path.stem
    assert path.with_suffix(".md").is_file()


def test_seed_stackup_dependent_gap_dropped():
    """gap_mm is stackup-dependent; the seeds must not freeze it."""
    for name in ("usb-fs", "ethernet-10-100"):
        doc = json.loads((REFERENCE / "interfaces" / f"{name}.json")
                         .read_text(encoding="utf-8"))
        for pair in doc.get("diff_pairs", []):
            assert "gap_mm" not in pair, name


def test_topology_seed_exists():
    assert (REFERENCE / "topologies" / "buck.md").is_file()


# ================================================== CLI contract (SPEC 6)

def run_cli(*args):
    return subprocess.run(
        [PYTHON, str(SCRIPTS / "constraints_lint.py"), *args],
        capture_output=True, text=True, timeout=60)


def test_cli_pass_warn_error_exit_codes(tmp_path):
    clean = tmp_path / "clean.json"
    clean.write_text(json.dumps(
        {"voltages": [{"net": "X", "voltage": 48}]}), encoding="utf-8")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(
        {"diff_pairs": [{"p": "/A", "n": "/B", "max_skew": 5}]}),
        encoding="utf-8")

    r = run_cli("--file", str(clean))
    assert r.returncode == 0
    payload = json.loads(r.stdout)
    assert payload["status"] == "pass"
    assert payload["schema_keys"] == sorted(constraints_lint.SCHEMA_TOP_KEYS)

    r = run_cli("--file", str(clean), str(bad))  # nargs: multi-file
    assert r.returncode == 1
    payload = json.loads(r.stdout)
    assert payload["counts"]["by_severity"]["error"] == 1
    assert len(payload["files"]) == 2

    r = run_cli("--file", str(tmp_path / "nope.json"))
    assert r.returncode == 2  # cannot run

    r = run_cli("--file", str(tmp_path / "*.json"))  # glob expansion
    assert r.returncode == 1  # bad.json's error found via the glob


# ================================================== blocks (U4)

def test_blocks_section_lints(tmp_path):
    """U4: the P2 block list - {topology} required, labels optional."""
    vs, _ = lint_doc(tmp_path, {"blocks": [
        {"topology": "buck", "block": "B3", "name": "U1 AP64350 class"},
        {"topology": "ldo"}]})
    assert errors(vs) == []

    vs, _ = lint_doc(tmp_path, {"blocks": [{"block": "B3"}]})
    errs = errors(vs)
    assert len(errs) == 1 and "topology" in errs[0]["msg"]

    vs, _ = lint_doc(tmp_path, {"blocks": [{"topolgy": "buck"}]})
    assert any(v["kind"] == "misspelled_key" for v in errors(vs))


def test_note_beside_a_documented_key_is_not_a_misspelling(tmp_path):
    """linux-computer writes length_groups_note next to length_groups: prose
    about the section, an unknown-key warning, never misspelled_key."""
    p = tmp_path / "constraints.json"
    p.write_text(json.dumps({
        "length_groups": [{"nets": ["DRAM_D*"], "tol_mm": 0.635}],
        "length_groups_note": "Table 22 rules not expressible yet"}),
        encoding="utf-8")
    payload, _ = constraints_lint.run(["--file", str(p)])
    kinds = {v["kind"] for v in payload["violations"]}
    assert "misspelled_key" not in kinds and "unknown_key" in kinds


def test_note_and_notes_are_prose_on_entries_and_objects(tmp_path):
    """"note" and "notes" pass on any entry or object section, with any
    value, without a warning."""
    vs, _ = lint_doc(tmp_path, {
        "power": [{"net": "+5V", "current_a": 1, "note": "fan header",
                   "notes": ["two", "lines"]}],
        "esd": {"connectors": ["J1"], "notes": {"why": "front panel"}},
        "placement": {"note": "edge parts first"}})
    assert vs == [], vs


def test_strict_sections_take_no_prose_keys(tmp_path):
    """planes[] and teardrops stop their consumer on a key it does not
    know, so neither takes note or notes; length_groups[] takes "note"
    (its own key) and reads "notes" as a misspelling of it."""
    plane = {"layer": "In1.Cu", "net": "GND"}
    for doc, consumer in (
            ({"planes": [dict(plane, note="x")]}, "planes_gen"),
            ({"planes": [dict(plane, notes="x")]}, "planes_gen"),
            ({"teardrops": {"enabled": True, "note": "x"}}, "teardroplib"),
            ({"teardrops": {"enabled": True, "notes": "x"}}, "teardroplib"),
            ({"length_groups": [{"nets": ["D*"], "notes": "x"}]},
             "check_length_groups")):
        vs, _ = lint_doc(tmp_path, doc)
        assert any(consumer in v["msg"] for v in vs), (doc, vs)
    vs, _ = lint_doc(tmp_path, {"length_groups": [
        {"nets": ["D*"], "note": "byte lane 0"}]})
    assert vs == [], vs
    vs, _ = lint_doc(tmp_path, {"length_groups": [
        {"nets": ["D*"], "notes": "byte lane 0"}]})
    assert [v["kind"] for v in errors(vs)] == ["misspelled_key"]


def test_word_notes_key_warns_and_is_no_misspelling(tmp_path):
    """exempt_notes beside esd's exempt_nets is prose about it: an unknown-
    key warning, not a misspelled_key error."""
    vs, _ = lint_doc(tmp_path, {"esd": {"exempt_nets": ["/SENSE"],
                                        "exempt_notes": "inside the box"}})
    assert not errors(vs)
    assert [w["kind"] for w in warnings(vs)] == ["unknown_key"]


def test_null_top_object_means_none(tmp_path):
    """A top object read with .get(key) or {} may be null; enclosure may
    not, because check_thermal indexes one it finds."""
    vs, _ = lint_doc(tmp_path, {"pd_source": None, "esd": None})
    assert vs == [], vs
    vs, _ = lint_doc(tmp_path, {"enclosure": None})
    assert [v["kind"] for v in errors(vs)] == ["bad_type"]


def test_documented_top_keys_without_a_branch_are_not_unknown(tmp_path):
    """si, antennas and pcb_antennas are in SCHEMA_TOP_KEYS but had no branch
    of their own, so the lint called each one unknown and suggested itself
    ("unknown key 'si' - did you mean 'si'?")."""
    for key, val in (("si", {"nets": []}), ("antennas", []),
                     ("pcb_antennas", [])):
        violations, _ = lint_doc(tmp_path, {key: val})
        assert not [v for v in violations if "unknown key" in v["msg"]], key


def test_package_ratings_accept_the_layer_rules_gen_reads(tmp_path):
    """rules_gen reads package_ratings[].layer (the part's own copper side);
    the lint must not warn that it is an unknown key."""
    violations, _ = lint_doc(tmp_path, {"package_ratings": [
        {"ref": "U2", "clearance_mm": 0.1, "why": "0.4 mm WLCSP",
         "layer": "F.Cu"}]})
    assert not [v for v in violations if "layer" in v["msg"]], violations


def test_switch_node_entry_shape(tmp_path):
    """#924: a switch_node object takes only ref/layer/keep_away/keep_away_mm;
    a misspelled key inside it is a bad_type error."""
    ok = {"net": "/LX", "current_a": 0.6, "min_width_mm": 0.5,
          "switch_node": {"keep_away": ["MK1"], "keep_away_mm": 2.0}}
    vs, _ = lint_doc(tmp_path, {"power": [ok, dict(ok, net="/SW",
                                                   switch_node=True)]})
    assert errors(vs) == []
    vs, _ = lint_doc(tmp_path, {"power": [dict(ok, switch_node={
        "keepaway": ["MK1"]})]})
    assert any(v["kind"] == "bad_type" and "switch_node" in v["msg"]
               for v in errors(vs))


def test_sense_pads_entry_shape(tmp_path):
    """#936: power[].sense_pads maps the sense line's end pad to the pad it
    must tap, both as REF.PAD; anything else is a bad_type error."""
    ok = {"net": "+5V", "current_a": 1, "sense_pads": {"R113.1": "C114.1"}}
    vs, _ = lint_doc(tmp_path, {"power": [ok]})
    assert errors(vs) == [] and not [v for v in vs if "sense_pads" in v["msg"]]
    for bad in ("C114.1", {"R113.1": "C114"}, {"R113.1": 1}, {}):
        vs, _ = lint_doc(tmp_path, {"power": [dict(ok, sense_pads=bad)]})
        assert any(v["kind"] == "bad_type" and "sense_pads" in v["msg"]
                   for v in errors(vs)), bad
