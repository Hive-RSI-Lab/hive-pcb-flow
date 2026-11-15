"""routelib's .kicad_dru value reader (#1273). KiCad 10 reads each (min /
opt / max ...) value of a rule through its expression evaluator, and one value
it rejects (0.5MM, 20mils, a bare 0.2 on a length) drops the WHOLE file while
kicad-cli says nothing: DRC then holds none of its rules. The fixture is what
KiCad 10.0.6's DRC did with each of 190 rule files; dru_unit_errors is held to
it. Pure text tests; no KiCad needed."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))
import routelib  # noqa: E402

PROBE = json.loads((REPO / "tests" / "fixtures" / "dru_values"
                    / "kicad10_probe.json").read_text(encoding="utf-8"))


def _probe_text(case: dict) -> str:
    """A case's .kicad_dru as the probe wrote it (see the fixture's _source):
    base + text + a newline, every line named in drop taken out, then CRLF
    line ends or a UTF-8 BOM."""
    lines = (PROBE["base"] + case["text"] + "\n").splitlines(keepends=True)
    text = "".join(ln for ln in lines
                   if ln.rstrip("\n") not in case.get("drop", ()))
    if case.get("crlf"):
        text = text.replace("\n", "\r\n")
    if case.get("bom"):
        text = "\ufeff" + text
    return text


def test_probe_fixture_is_consistent():
    """A file KiCad read must not be flagged, and a flagged one must be one
    KiCad dropped; the fixture covers every verdict."""
    cases = PROBE["cases"]
    assert len(cases) == 190
    for name, case in cases.items():
        assert case["kicad"] in ("ok", "void"), name
        if case["kicad"] == "ok":
            assert case["unit_error"] is False, name
        else:
            assert case["unit_error"] in (True, None), name
    verdicts = {c["unit_error"] for c in cases.values()}
    assert verdicts == {True, False, None}


@pytest.mark.parametrize("name", sorted(PROBE["cases"]))
def test_dru_unit_errors_matches_kicad(name):
    case = PROBE["cases"][name]
    errs = routelib.dru_unit_errors(_probe_text(case))
    if case["unit_error"] is True:
        assert errs, f"{name}: KiCad dropped the file, nothing was found"
        assert all(e.startswith('rule "') for e in errs), errs
    elif case["unit_error"] is False:
        assert errs == [], f"{name}: KiCad read the file: {errs}"


def test_base_rules_file_reads_clean():
    """rules_gen's own output (the probe's base) holds no rejected value."""
    assert routelib.dru_unit_errors(PROBE["base"]) == []


@pytest.mark.parametrize("value, ctype, mm", [
    ("0.2mm", "clearance", 0.2),
    ("0.5 mm", "clearance", 0.5),
    ("20mil", "clearance", 0.508),
    ("0.02in", "track_width", 0.508),
    ("(0.2 mm)", "clearance", 0.2),
    ("+0.01mm", "clearance", 0.01),
    ("0,01mm", "clearance", 0.01),
    ("-0.01mm", "solder_mask_expansion", -0.01),
    ("0.3mm", "hole", 0.3),
])
def test_dru_value_reads_a_plain_length(value, ctype, mm):
    got, why = routelib.dru_value(value, ctype)
    assert why is None and got == pytest.approx(mm)


@pytest.mark.parametrize("value, ctype", [
    ("0.1mm + 0.05mm", "clearance"),    # a sum KiCad reads, not evaluated
    ("1deg", "clearance"),              # an angle KiCad takes on a length
    ("2", "via_count"),                 # a count
    ("45", "track_angle"),
    ("100ps", "length"),                # a delay
    ('"0.01mm"', "clearance"),          # a string: no verdict here
    ("solid", "zone_connection"),       # a type not read this way
])
def test_dru_value_reads_but_does_not_evaluate(value, ctype):
    assert routelib.dru_value(value, ctype) == (None, None)


@pytest.mark.parametrize("value, ctype, why", [
    ("0.5", "clearance", "has no unit"),
    ("(1)", "clearance", "has no unit"),
    ("+1", "clearance", "has no unit"),
    ("500um", "clearance", "'um'"),
    ("0.05cm", "clearance", "'cm'"),
    ("0.5MM", "clearance", "'MM'"),
    ("20mils", "clearance", "'mils'"),
    ("0.0004inch", "clearance", "'inch'"),
    ("1e-2mm", "clearance", "exponent"),
    (".5mm", "clearance", "no digit before its point"),
    ("2mm", "via_count", "takes a plain number"),
    ("90deg", "track_angle", "takes a plain number"),
    ("1ps", "clearance", "only for length and skew"),
    ("100ps/mm", "length", "after '/'"),
    ("0.005mm\t+ 0.005mm", "clearance", "tab or line break"),
    ("", "clearance", "has no value"),
    ("mm", "clearance", "no number before it"),
    ("(1)mm", "clearance", "after ')'"),
    ("0.01mm-", "clearance", "ends in '-'"),
    ("2*mm", "clearance", "after '*'"),
    ("0.01mm\f", "clearance", "form feed"),
])
def test_dru_value_names_why_kicad_rejects(value, ctype, why):
    mm, got = routelib.dru_value(value, ctype)
    assert mm is None and got and why in got


def test_dru_unit_errors_names_rule_clause_and_reason():
    text = ('(version 1)\n'
            '(rule "a" (constraint clearance (min 0.2mm)))\n'
            '(rule "b"\n\t(constraint track_width (opt 0.3mm) (min 0.5MM))\n)\n'
            '(rule "c" (constraint length (min 1mm) (max 100ps)))\n'
            '(rule "d" (constraint clearance (min 0.005mm\t+ 0.005mm)))\n')
    assert routelib.dru_unit_errors(text) == [
        """rule "b": (track_width (min 0.5MM)) has unit 'MM', which KiCad """
        "does not read (mm, mil, in)",
        'rule "c": (length (max 100ps)) is a delay after a length, and KiCad '
        "takes one kind per constraint (a value reads as its last unit; deg "
        "or none is unitless)",
        'rule "d": (clearance (min 0.005mm\\t+ 0.005mm)) has a tab or line '
        "break inside, which KiCad's evaluator rejects"]
    # comment lines are not read, and an empty file is clean
    assert routelib.dru_unit_errors("# (rule x (constraint clearance "
                                    "(min 0.5)))\n") == []
    assert routelib.dru_unit_errors("") == []


def test_dru_rule_constraints_keeps_every_clause_in_order():
    form = ('(rule "r" (layer outer)\n'
            '  (constraint hole (max 1mm) (min 0.3 mm))\n'
            '  (constraint clearance (opt 0.6mm) (min 20mil) (min 0.4mm))\n'
            '  (constraint disallow track via)\n'
            '  (condition "A.NetName == \'/X\'"))')
    assert routelib.dru_rule_constraints(form) == ("r", [
        ("hole_size", [("max", "1mm"), ("min", "0.3 mm")]),
        ("clearance", [("opt", "0.6mm"), ("min", "20mil"),
                       ("min", "0.4mm")]),
        ("disallow", [])])
    assert routelib.dru_rule_constraints("(version 1)") is None
