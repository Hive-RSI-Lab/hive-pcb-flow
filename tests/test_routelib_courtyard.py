"""#1512: a part's courtyard clearance rule (bgalib's aiee_bga_escape_<ref>)
never reached the DSN, so Freerouting held the 0.125 mm class clearance
from every ball and no 0.2 mm track fit a 0.45 mm BGA street.
routelib.dsn_shrink_part_pads pulls the part's pads in instead, and
route_auto._dsn_courtyards reads the rule. Pure text tests; no KiCad."""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))
import route_auto  # noqa: E402
import routelib  # noqa: E402

_DSN = '''(pcb test.dsn
  (parser
    (string_quote ")
    (space_in_quoted_tokens on)
  )
  (resolution um 10)
  (unit um)
  (structure
    (layer F.Cu
      (type signal)
    )
    (rule
      (width 200)
      (clearance 125)
    )
  )
  (placement
    (component t:BGA
      (place U1 14000 -14000 front 0)
      (place U2 34000 -14000 front 0)
    )
    (component t:R
      (place R1 1000 -1000 front 0)
    )
  )
  (library
    (image t:BGA
      (outline (polygon signal 0  1000 -1000  -1000 -1000  -1000 1000  1000 1000))
      (pin Round[T]Pad_350.000000_um A1 -400 400)
      (pin Round[T]Pad_350.000000_um (rotate 90) "A2" 400 400)
      (pin "Rect[T]Pad_400x600_um" B1 -400 -400)
    )
    (image t:R
      (outline (polygon signal 0  500 -300  -500 -300  -500 300  500 300))
      (pin Round[T]Pad_350.000000_um 1 -400 0)
    )
    (padstack Round[T]Pad_350.000000_um
      (shape (circle F.Cu 350))
      (attach off)
    )
    (padstack "Rect[T]Pad_400x600_um"
      (shape (rect F.Cu -200 -300 200 300))
      (attach off)
    )
  )
  (network
    (net /A
      (pins U1-A1 R1-1)
    )
    (net HV
      (pins "U1-A2")
    )
    (net /B
      (pins U1-B1 U2-A1)
    )
  )
  (class kicad_default /A /B
    (rule
      (width 200)
      (clearance 125)
    )
  )
  (class HVC HV
    (rule
      (width 200)
      (clearance 400)
    )
  )
)
'''


def _block(text: str, head: str) -> str:
    i = text.index(head)
    return text[i:routelib._sexp_end(routelib._parser_blanked(text), i)]


def test_shrink_clones_the_image_and_pulls_its_pads_in():
    text, facts = routelib.dsn_shrink_part_pads(_DSN, {"U1": 20}, {"HV"})
    assert facts == {"U1": {"shrink_um": 20, "image": "t:BGA_cy_U1",
                            "pads": 2}}
    assert text.count("(") == text.count(")")
    img = _block(text, '(image "t:BGA_cy_U1"')
    assert '(pin "Round[T]Pad_350.000000_um_cy20" A1 -400 400)' in img
    # HV holds 0.4 mm from everything: its pad keeps its size
    assert '(pin Round[T]Pad_350.000000_um (rotate 90) "A2" 400 400)' in img
    assert '(pin "Rect[T]Pad_400x600_um_cy20" B1 -400 -400)' in img
    circ = _block(text, '(padstack "Round[T]Pad_350.000000_um_cy20"')
    assert "(circle F.Cu 310)" in circ
    rect = _block(text, '(padstack "Rect[T]Pad_400x600_um_cy20"')
    assert "(rect F.Cu -180 -280 180 280)" in rect
    # U2 shares the image and keeps the real pads; U1 moves to the clone
    shared = _block(text, "(component t:BGA")
    assert "(place U2 " in shared and "(place U1 " not in shared
    assert "(place U1 14000 -14000 front 0)" in _block(
        text, '(component "t:BGA_cy_U1"')
    assert _block(_DSN, "(image t:BGA") in text
    assert _block(_DSN, "(image t:R") in text


def test_shrink_renames_a_component_holding_only_that_part():
    text, facts = routelib.dsn_shrink_part_pads(_DSN, {"R1": 10})
    assert facts["R1"]["pads"] == 1
    assert '(component "t:R_cy_R1"\n      (place R1 ' in text
    assert "(component t:R\n" not in text
    assert "(circle F.Cu 330)" in text


def test_shrink_leaves_other_dsns_alone():
    assert routelib.dsn_shrink_part_pads(_DSN, {}) == (_DSN, {})
    assert routelib.dsn_shrink_part_pads(_DSN, {"U9": 20}) == (_DSN, {})
    mil = _DSN.replace("(resolution um 10)", "(resolution mil 10)")
    assert routelib.dsn_shrink_part_pads(mil, {"U1": 20}) == (mil, {})


def test_net_class_clearances():
    assert routelib.dsn_net_class_clearances(_DSN) == {
        "/A": 125.0, "/B": 125.0, "HV": 400.0}


def test_dsn_courtyards_reads_the_escape_rule(tmp_path):
    staged = tmp_path / "b.kicad_pcb"
    staged.write_text("(kicad_pcb)")
    staged.with_suffix(".kicad_dru").write_text(
        "(version 1)\n"
        '(rule "aiee_bga_escape_U1"\n'
        "\t(constraint clearance (min 0.1000mm))\n"
        "\t(condition \"A.intersectsCourtyard('U1') || "
        "B.intersectsCourtyard('U1')\")\n)\n"
        '(rule "both_in_U2"\n'
        "\t(constraint clearance (min 0.1000mm))\n"
        "\t(condition \"A.intersectsCourtyard('U2') && "
        "B.intersectsCourtyard('U2')\")\n)\n")
    dsn = tmp_path / "b.dsn"
    dsn.write_text(_DSN)
    facts = route_auto._dsn_courtyards(staged, dsn)
    # 125 class - 100 rule - 5 kept: U2's && rule says nothing about a
    # street another net runs through, so it is not read
    assert list(facts) == ["U1"] and facts["U1"]["shrink_um"] == 20
    text = dsn.read_text()
    assert re.search(r'\(padstack "Round\[T\]Pad_350\.000000_um_cy20"', text)
    # HV's own 0.4 mm class keeps its pad
    assert '(rotate 90) "A2"' in _block(text, '(image "t:BGA_cy_U1"')
    assert ('Round[T]Pad_350.000000_um_cy20" (rotate 90)'
            not in text)


def test_dsn_courtyards_without_a_rule_leaves_the_dsn(tmp_path):
    staged = tmp_path / "b.kicad_pcb"
    staged.write_text("(kicad_pcb)")
    dsn = tmp_path / "b.dsn"
    dsn.write_text(_DSN)
    assert route_auto._dsn_courtyards(staged, dsn) == {}
    staged.with_suffix(".kicad_dru").write_text(
        "(version 1)\n"
        '(rule "aiee_bga_escape_U1"\n'
        "\t(constraint clearance (min 0.2000mm))\n"
        "\t(condition \"A.intersectsCourtyard('U1') || "
        "B.intersectsCourtyard('U1')\")\n)\n")
    # a rule above the class clearance asks nothing of the pads
    assert route_auto._dsn_courtyards(staged, dsn) == {}
    assert dsn.read_text() == _DSN
