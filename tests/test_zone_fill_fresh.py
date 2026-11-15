"""Unfilled-zone handling that used to crash check_current/check_thermal
with StaleFillError (a03cb ask, hwde-reroute-tools). Two parse causes seen
on visible training boards, and the no-crash path for a real unfilled zone:

- cmm2-jlc (KiCad 5): copper layers renamed "GND"/"POWER" in the layer
  table, and every item names them so. geom took only *.Cu names as copper,
  so those zones had no layer and looked unfilled.
- ambient rev_d (KiCad 4): a 2-layer board keeping zones on In1/In2.Cu,
  layers it doesn't have. No copper is made of them; they aren't stale.
- a zone that is really unfilled: fresh_board() judges a refilled scratch
  copy, or, when kicad-cli can't refill, the board as committed with the
  reason recorded.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))
import check_current  # noqa: E402
import geom  # noqa: E402

FILL = '(filled_polygon (layer {l}) (pts (xy 1 1) (xy 9 1) (xy 9 9) (xy 1 9)))'


def board(layers: str, zone_layer: str, filled: bool, extra: str = "") -> str:
    fill = FILL.format(l=zone_layer) if filled else ""
    return f"""(kicad_pcb (version 20171130) (host pcbnew 5.1.6)
  (general (thickness 1.6))
  (layers {layers} (44 Edge.Cuts user))
  (net 0 "") (net 1 GND)
  (gr_line (start 0 0) (end 10 0) (layer Edge.Cuts) (width 0.1))
  (segment (start 1 1) (end 5 1) (width 0.5) (layer {zone_layer}) (net 1))
  (zone (net 1) (net_name GND) (layer {zone_layer}) (hatch edge 0.508)
    (polygon (pts (xy 0 0) (xy 10 0) (xy 10 10) (xy 0 10))) {fill})
  {extra}
)
"""


KICAD5_RENAMED = "(0 F.Cu signal) (1 GND power) (2 POWER power) (31 B.Cu signal)"
TWO_LAYER = "(0 F.Cu signal) (31 B.Cu signal)"


def write(tmp_path, name, text) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


# ---------------------------------------------- KiCad 5 renamed copper layers

def test_renamed_copper_layer_filled_zone_is_fresh(tmp_path):
    """Quiet twin: cmm2-jlc's shape, filled. The "GND" layer is In1.Cu."""
    bg = geom.BoardGeom.from_file(
        write(tmp_path, "r.kicad_pcb", board(KICAD5_RENAMED, "GND", True)))
    assert bg.copper_layers == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    assert bg.layer_aliases == {"GND": "In1.Cu", "POWER": "In2.Cu"}
    (z,) = bg.zones_of()
    assert z.layers == ("In1.Cu",) and z.filled
    assert bg.net_area_by_layer("GND").get("In1.Cu", 0) > 60
    bg.assert_fresh()


def test_renamed_copper_layer_unfilled_zone_is_still_caught(tmp_path,
                                                             monkeypatch):
    """Planted fault: the same zone with no fill is unfilled, on In1.Cu."""
    monkeypatch.setattr(geom, "_refill_copy",
                        lambda p: (_ for _ in ()).throw(RuntimeError("x")))
    bg = geom.BoardGeom.from_file(
        write(tmp_path, "r.kicad_pcb", board(KICAD5_RENAMED, "GND", False)))
    (z,) = bg.unfilled_zones()
    assert z.layers == ("In1.Cu",)
    with pytest.raises(geom.StaleFillError):
        bg.assert_fresh()


# ---------------------------------------------- zones on layers off the stack

def test_zone_on_a_layer_the_board_lacks_is_not_copper(tmp_path):
    """Quiet twin: ambient rev_d's shape. In1.Cu on a 2-layer board."""
    bg = geom.BoardGeom.from_file(
        write(tmp_path, "a.kicad_pcb", board(TWO_LAYER, "In1.Cu", False)))
    assert bg.zones_of() == [] and bg.unfilled_zones() == []
    assert bg.off_stack_zones == [{"net": "GND", "layers": ["In1.Cu"]}]
    bg.assert_fresh()


def test_off_stack_zone_doesnt_hide_a_real_unfilled_one(tmp_path, monkeypatch):
    """Planted fault: an unfilled B.Cu zone beside it is still caught."""
    monkeypatch.setattr(geom, "_refill_copy",
                        lambda p: (_ for _ in ()).throw(RuntimeError("x")))
    extra = ('(zone (net 1) (net_name GND) (layer B.Cu) (polygon (pts '
             '(xy 0 0) (xy 10 0) (xy 10 10) (xy 0 10))))')
    bg = geom.BoardGeom.from_file(write(
        tmp_path, "a.kicad_pcb", board(TWO_LAYER, "In1.Cu", False, extra)))
    assert [z.layers for z in bg.unfilled_zones()] == [("B.Cu",)]
    with pytest.raises(geom.StaleFillError):
        bg.assert_fresh()


# ---------------------------------------------- fresh_board: no crash

def test_fresh_board_judges_a_refilled_copy(tmp_path, monkeypatch):
    """A zone a refill fills: judged on the refilled scratch copy, which
    keeps the board's own path (siblings resolve); the note says so."""
    src = write(tmp_path, "f.kicad_pcb", board(KICAD5_RENAMED, "GND", False))
    filled = board(KICAD5_RENAMED, "GND", True)

    def refill(pcb):
        d = tmp_path / "refill"
        d.mkdir()
        return write(d, pcb.name, filled)
    monkeypatch.setattr(geom, "_refill_copy", refill)
    bg, note = geom.fresh_board(src)
    assert note["refilled"] is True and note["why"] is None
    assert note["zones"] == [{"id": 0, "net": "GND", "layers": ["In1.Cu"]}]
    assert bg.path == src and not bg.unfilled_zones()
    assert not (tmp_path / "refill").exists()        # scratch copy cleaned
    assert src.read_text(encoding="utf-8").count("filled_polygon") == 0


def test_fresh_board_records_why_a_refill_failed(tmp_path, monkeypatch):
    """kicad-cli fails: the board as committed, with the reason (it used
    to be swallowed, leaving a bare StaleFillError)."""
    def fail(pcb):
        raise RuntimeError("kicad-cli exited 3: zone outline self-intersects")
    monkeypatch.setattr(geom, "_refill_copy", fail)
    src = write(tmp_path, "g.kicad_pcb", board(KICAD5_RENAMED, "GND", False))
    bg, note = geom.fresh_board(src)
    assert note["refilled"] is False
    assert "self-intersects" in note["why"]
    assert len(bg.unfilled_zones()) == 1
    assert "could not be refilled" in geom.fill_note_text(note)


def test_check_current_reports_instead_of_crashing(tmp_path, monkeypatch):
    """check_current on an unrefillable board: a report, not an error, with
    the reason in not_judged and a power-net finding cut to a warning."""
    monkeypatch.setattr(geom, "_refill_copy",
                        lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
    src = write(tmp_path, "h.kicad_pcb", board(KICAD5_RENAMED, "GND", False))
    cons = write(tmp_path, "c.json",
                 '{"power": [{"net": "GND", "current_a": 20}]}')
    payload, _ = check_current.run(["--pcb", str(src), "--constraints",
                                    str(cons)])
    assert payload["status"] in ("pass", "violations")
    assert payload["zone_fill"]["refilled"] is False
    assert any("boom" in n for n in payload["not_judged"])
    assert all(v["severity"] != "error" for v in payload["violations"]
               if v.get("net") == "GND")
