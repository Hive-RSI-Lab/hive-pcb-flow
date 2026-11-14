"""check_silk's pin_label_wrong rule (#1028). Seeded from errata corpus
sizif-2: rev C's legend by the power header J3 read "+12 / G / G / +5" for
pins +12V, VCC, +9V, GND, +5V, and rev C1 (the fix) reads "+12 / VCC / +9 /
GND / +5", which must stay quiet. The per-pin form (a one-word label nearest
a pin it doesn't name) is planted by hand."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
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
