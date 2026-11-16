"""check_silk's pin_label_wrong rule (#1028). Seeded from errata corpus
sizif-2: rev C's legend by the power header J3 read "+12 / G / G / +5" for
pins +12V, VCC, +9V, GND, +5V, and rev C1 (the fix) reads "+12 / VCC / +9 /
GND / +5", which must stay quiet. The per-pin form (a one-word label nearest
a pin it doesn't name) is planted by hand."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import sexpdata
from shapely.geometry import box

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import check_silk as cs  # noqa: E402


class _Pad:
    def __init__(self, ref, number, net, x, y, r=0.85):
        self.ref, self.number, self.net = ref, number, net
        self.center = (x, y)
        self.poly = box(x - r, y - r, x + r, y + r)


class _Board:
    def __init__(self, pads):
        self._pads = pads

    def pads_of(self, ref=None):
        return [p for p in self._pads if ref is None or p.ref == ref]


def _text(text, x, y, angle=0.0, just=(0, 0)):
    first = text.split("\n")[0]
    g = cs._text_box(x, y, angle, first if "\n" in text else text,
                     1.0, 1.0, 0.15, *just)
    return cs.Silk("F", "text", g, text=text, height=1.0, thickness=0.15,
                   pos=(x, y), angle=angle, size=(1.0, 1.0), just=just)


def _sizif():
    """J3 as on sizif rev C/C1: one column at x 255.4, pins 1-5 down y, and
    C2 just below it, nearer the legend than J3 is."""
    nets = ["+12V", "VCC", "+9V", "GND", "+5V"]
    pads = [_Pad("J3", str(i + 1), n, 255.4, 66.8 + 2.54 * i)
            for i, n in enumerate(nets)]
    pads += [_Pad("C2", "1", "Net-(C2-Pad1)", 255.5, 82.0, 0.6),
             _Pad("C2", "2", "GND", 255.5, 84.5, 0.6)]
    return _Board(pads)


def test_sizif_rev_c_legend_is_the_planted_fault():
    v = cs.check_label_nets(_sizif(), [_text("+12\nG\nG\n+5", 255.524, 81.788)])
    assert len(v) == 1
    h = v[0]
    assert h["kind"] == "pin_label_wrong" and h["form"] == "stack"
    assert h["refs"] == ["J3"] and h["severity"] == "warning"
    assert '"G" has no pin left' in h["msg"]


def test_sizif_rev_c1_legend_is_quiet():
    t = _text("+12\nVCC\n+9\nGND\n+5", 257.0, 83.0, just=(1, 0))
    assert cs.check_label_nets(_sizif(), [t]) == []


def test_a_legend_that_names_too_few_pins_is_not_judged():
    t = _text("PWR\nIN\nG", 255.5, 81.8)
    assert cs.check_label_nets(_sizif(), [t]) == []


def _header():
    """A 4-pin SWD header J1 in a row along x, a one-word label above each
    pin."""
    nets = ["+3V3", "/SWCLK", "/SWDIO", "GND"]
    return _Board([_Pad("J1", str(i + 1), n, 10.0 + 2.54 * i, 20.0)
                   for i, n in enumerate(nets)])


def test_swapped_one_word_labels_fire():
    labels = [("3V3", 10.0), ("DIO", 12.54), ("CLK", 15.08), ("GND", 17.62)]
    v = cs.check_label_nets(_header(), [_text(w, x, 17.8) for w, x in labels])
    assert sorted((h["silk_text"], h["pin"], h["named_pin"]) for h in v) == [
        ("CLK", "3", "2"), ("DIO", "2", "3")]
    assert all(h["form"] == "pin" for h in v)


def test_right_one_word_labels_are_quiet():
    labels = [("3V3", 10.0), ("CLK", 12.54), ("DIO", 15.08), ("GND", 17.62)]
    assert cs.check_label_nets(_header(),
                               [_text(w, x, 17.8) for w, x in labels]) == []


def test_a_label_between_two_pins_gets_the_slack():
    # "CLK" a hair nearer pin 3 than pin 2: within LABEL_SLACK_MM, no call
    t = _text("CLK", 13.81 + 0.1, 17.8)
    assert cs.check_label_nets(_header(), [t]) == []


def test_text_far_from_any_connector_is_ignored():
    assert cs.check_label_nets(_header(), [_text("DIO", 12.54, 40.0)]) == []


@pytest.mark.parametrize("word,net", [
    ("+12", "+12V"), ("12V", "/pwr/+12V"), ("3V3", "+3.3V"), ("3.3", "+3V3"),
    ("G", "GND"), ("0V", "GND"), ("~{RST}", "/RST"),
    ("CLK", "/SWCLK")])
def test_names(word, net):
    assert cs._names(word, net)


@pytest.mark.parametrize("word,net", [
    ("5V", "+3V3"), ("CK", "/SWCLK"), ("GND", "Net-(C2-Pad1)"),
    ("CLK", "unconnected-(J1-Pad2)"), ("RX", "/TX")])
def test_does_not_name(word, net):
    assert not cs._names(word, net)


def test_parse_silk_takes_a_kicad6_footprints_ref_from_fp_text():
    # fc980c: KiCad 6 keeps the ref in fp_text, not a Reference property;
    # it once raised UnboundLocalError (or kept the last footprint's ref)
    import sexpdata
    root = sexpdata.loads('''(kicad_pcb (version 20211014)
      (footprint "x" (layer "F.Cu") (at 10 10)
        (fp_text reference "J7" (at 0 -2) (layer "F.SilkS")
          (effects (font (size 1 1) (thickness 0.15))))
        (fp_text user "GND" (at 0 2) (layer "F.SilkS")
          (effects (font (size 1 1) (thickness 0.15))))))''')
    items = cs.parse_silk(root)
    assert {s.text for s in items} == {"J7", "GND"}
    assert all(s.ref == "J7" for s in items)


# -- class 10: two-column legends -------------------------------------------

def _qwiic2x2():
    """A 2x2 header J2, columns at x 10 and 12.54: pin 1 SDA, 2 SCL (left
    column down), 3 +3V3, 4 GND (right column down)."""
    at = [("1", "/SDA", 10.0, 20.0), ("2", "/SCL", 10.0, 22.54),
          ("3", "+3V3", 12.54, 20.0), ("4", "GND", 12.54, 22.54)]
    return _Board([_Pad("J2", n, net, x, y) for n, net, x, y in at])


def _btext(text, x, y):
    t = _text(text, x, y)
    t.side = "B"
    return t


def test_two_column_legend_with_columns_swapped_fires():
    v = cs.check_label_nets(_qwiic2x2(), [_text("3V3 SDA\nGND SCL", 11.3, 17.5)])
    assert len(v) == 1 and v[0]["form"] == "columns"
    assert v[0]["refs"] == ["J2"] and "reader's left are SCL, SDA" in v[0]["msg"]


def test_two_column_legend_in_pad_order_is_quiet():
    assert cs.check_label_nets(_qwiic2x2(),
                               [_text("SDA 3V3\nSCL GND", 11.3, 17.5)]) == []


def test_bottom_legend_not_mirrored_fires():
    v = cs.check_label_nets(_qwiic2x2(), [_btext("SDA 3V3\nSCL GND", 11.3, 17.5)])
    assert len(v) == 1 and v[0]["form"] == "mirrored"
    assert "reader's left are +3V3, GND" in v[0]["msg"]


def test_bottom_legend_mirrored_is_quiet():
    assert cs.check_label_nets(_qwiic2x2(),
                               [_btext("3V3 SDA\nGND SCL", 11.3, 17.5)]) == []


def test_two_words_a_line_by_a_single_row_is_not_judged():
    t = _text("3V3 CLK\nDIO GND", 13.8, 17.0)
    assert cs.check_label_nets(_header(), [t]) == []


# ------------------------------- golden mutant and its quiet twin (class 10)

@pytest.mark.parametrize("mirrored, fires", [(False, True), (True, False)])
def test_silk_legend_unmirrored_mutant_and_twin(tmp_path, mirrored, fires):
    sys.path.insert(0, str(REPO / "tests" / "golden" / "mutations"))
    import mutlib
    import silk_legend_unmirrored as m

    text, _ = m.surgery(mutlib.load_golden("blinky2"), mirrored=mirrored)
    pcb = tmp_path / "blinky2.kicad_pcb"
    pcb.write_text(text, encoding="utf-8")
    payload, _ = cs.run(["--pcb", str(pcb)])
    hits = [v for v in payload["violations"]
            if v.get("kind") == "pin_label_wrong" and "J9" in v["refs"]]
    assert bool(hits) is fires
    if fires:
        assert hits[0]["form"] == "mirrored"


def _kicad6_fp(hide):
    # KiCad 6 marks a hidden text with a bare `hide` atom at the end
    return sexpdata.loads(
        '(kicad_pcb (footprint "TestPoint:TP" (layer "F.Cu") (at 50 50)'
        ' (fp_text reference "REF**5" (at 0 0) (layer "F.SilkS")'
        + (" hide" if hide else "") +
        ' (effects (font (size 1 1) (thickness 0.15))))))')


def test_kicad6_hidden_reference_is_not_silk():
    # #1429: the bare `hide` was missed and read as the text, so every
    # hidden REF** printed as silk "hide" over its own pad
    assert [s for s in cs.parse_silk(_kicad6_fp(True))
            if s.kind == "text"] == []


def test_kicad6_shown_reference_is_silk_with_its_own_text():
    t = [s for s in cs.parse_silk(_kicad6_fp(False)) if s.kind == "text"]
    assert [s.text for s in t] == ["REF**5"]
