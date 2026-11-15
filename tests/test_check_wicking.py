"""check_wicking: open vias in/beside SMD pads and exposed-pad thermal vias
under paste (#475). Known answers on small planted-fault boards; the slow
test cross-checks the mask-merge decision against kicad-cli's F.Mask."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import check_wicking  # noqa: E402

SMD = '(layers "F.Cu" "F.Paste" "F.Mask")'
LAYERS = """(layers (0 "F.Cu" signal) (2 "B.Cu" signal) (9 "F.Adhes" user)
 (11 "B.Adhes" user) (13 "F.Paste" user) (15 "B.Paste" user)
 (5 "F.SilkS" user) (7 "B.SilkS" user) (1 "F.Mask" user) (3 "B.Mask" user)
 (25 "Edge.Cuts" user))"""


def _setup(tent=("yes", "yes"), margin=0.0, fill="no", cap="no",
           plug=("no", "no")) -> str:
    return (f"(setup (pad_to_mask_clearance {margin}) "
            f"(tenting (front {tent[0]}) (back {tent[1]})) "
            f"(plugging (front {plug[0]}) (back {plug[1]})) "
            f"(capping {cap}) (filling {fill}))")


def _r0603(x=10.0, y=10.0, ref="R1", margin=None) -> str:
    """0603 pads 0.8 x 0.95 at +-0.775 mm."""
    m = f" (solder_mask_margin {margin})" if margin is not None else ""
    pads = "\n ".join(
        f'(pad "{n}" smd roundrect (at {dx} 0) (size 0.8 0.95) '
        f'(roundrect_rratio 0.25) {SMD} (net "N{n}"){m})'
        for n, dx in (("1", -0.775), ("2", 0.775)))
    return (f'(footprint "Resistor_SMD:R_0603_1608Metric" (layer "F.Cu") '
            f'(at {x} {y})\n (property "Reference" "{ref}" (at 0 -1.5 0) '
            f'(layer "F.SilkS"))\n {pads})\n')


def _via(x, y, size=0.6, drill=0.3, extra="") -> str:
    return (f'(via (at {x} {y}) (size {size}) (drill {drill}) '
            f'(layers "F.Cu" "B.Cu") {extra} (net "N2"))\n')


def _qfn() -> str:
    """QFN-16, 0.5 mm pitch, 2.6 mm exposed pad at (20, 20) with 4 vias."""
    pads, n = [], 1
    for side in range(4):
        for i in range(4):
            off = (i - 1.5) * 0.5
            x, y, rot = [(off, 2.0, 0), (2.0, -off, 90), (-off, -2.0, 0),
                         (-2.0, off, 90)][side]
            pads.append(f'(pad "{n}" smd rect (at {x} {y} {rot}) '
                        f'(size 0.25 0.8) {SMD})')
            n += 1
    pads.append(f'(pad "17" smd rect (at 0 0) (size 2.6 2.6) {SMD} '
                f'(net "GND"))')
    return ('(footprint "Package_DFN_QFN:QFN-16-1EP_4x4mm" (layer "F.Cu") '
            '(at 20 20)\n (property "Reference" "U1" (at 0 -3 0) '
            '(layer "F.SilkS"))\n ' + "\n ".join(pads) + ")\n")


def _ep_vias() -> str:
    return "".join(
        f'(via (at {20 + dx} {20 + dy}) (size 0.6) (drill 0.3) '
        f'(layers "F.Cu" "B.Cu") (net "GND"))\n'
        for dx in (-0.6, 0.6) for dy in (-0.6, 0.6))


def _board(tmp_path: Path, setup: str, *items: str, name="b") -> Path:
    pcb = tmp_path / f"{name}.kicad_pcb"
    pcb.write_text(
        "(kicad_pcb (version 20241229) (generator \"pcbnew\")\n"
        f"(general (thickness 1.6)) (paper \"A4\")\n{LAYERS}\n{setup}\n"
        '(net 0 "") (net 1 "N1") (net 2 "N2") (net 3 "GND")\n'
        '(gr_rect (start 0 0) (end 30 30) (stroke (width 0.1) (type solid)) '
        '(fill no) (layer "Edge.Cuts"))\n'
        + "".join(items) + ")\n", encoding="utf-8")
    return pcb


def _kinds(pcb, **kw):
    vios, seen = check_wicking.check(pcb, **kw)
    return sorted(v["kind"] for v in vios), vios, seen


# pad 2's copper edge is at x = 10.775 + 0.4 = 11.175; a 0.6 mm via 0.2 mm
# off it is centred at 11.175 + 0.2 + 0.3 = 11.675.
BESIDE = 11.675


def test_open_via_in_an_0603_pad_warns(tmp_path):
    pcb = _board(tmp_path, _setup(), _r0603(), _via(10.775, 10.0))
    kinds, vios, _ = _kinds(pcb)
    assert kinds == ["wick_via_in_pad"]
    assert vios[0]["severity"] == "warning"
    assert vios[0]["refs"] == ["R1"] and vios[0]["pad"] == "2"


def test_filled_and_capped_via_in_pad_passes(tmp_path):
    pcb = _board(tmp_path, _setup(fill="yes", cap="yes"), _r0603(),
                 _via(10.775, 10.0))
    kinds, _, seen = _kinds(pcb)
    assert kinds == [] and seen["vias_filled_or_capped"] == 1


def test_per_via_capping_overrides_the_board(tmp_path):
    pcb = _board(tmp_path, _setup(), _r0603(),
                 _via(10.775, 10.0, extra="(capping yes) (filling yes)"))
    assert _kinds(pcb)[0] == []


def test_untented_via_with_merged_mask_openings_warns(tmp_path):
    # 0.1 mm mask margin on both: 0.2 mm copper gap -> 0 mm web, merged
    pcb = _board(tmp_path, _setup(tent=("no", "no"), margin=0.1),
                 _r0603(margin=0.1), _via(BESIDE, 10.0))
    kinds, vios, _ = _kinds(pcb)
    assert kinds == ["wick_via_mask_web"]
    assert vios[0]["merged"] is True and vios[0]["web_mm"] == 0.0
    assert vios[0]["layer"] == "F.Mask"


def test_untented_via_behind_a_0p1_mask_web_passes(tmp_path):
    pcb = _board(tmp_path, _setup(tent=("no", "no"), margin=0.05),
                 _r0603(margin=0.05), _via(BESIDE, 10.0))
    kinds, _, seen = _kinds(pcb)
    assert kinds == [] and seen["pairs_near"] == 1


def test_thin_web_under_the_dam_minimum_warns(tmp_path):
    # 0.07 margins: 0.06 mm web, under JLC's 0.1 mm dam
    pcb = _board(tmp_path, _setup(tent=("no", "no"), margin=0.07),
                 _r0603(margin=0.07), _via(BESIDE, 10.0))
    kinds, vios, _ = _kinds(pcb)
    assert kinds == ["wick_via_mask_web"]
    assert vios[0]["merged"] is False and vios[0]["web_mm"] == 0.06


def test_tented_via_beside_a_pad_passes(tmp_path):
    pcb = _board(tmp_path, _setup(tent=("yes", "yes"), margin=0.1),
                 _r0603(margin=0.1), _via(BESIDE, 10.0))
    assert _kinds(pcb)[0] == []


def test_per_via_tenting_overrides_the_board(tmp_path):
    v = _via(BESIDE, 10.0, extra="(tenting (front yes) (back yes))")
    pcb = _board(tmp_path, _setup(tent=("no", "no"), margin=0.1),
                 _r0603(margin=0.1), v)
    assert _kinds(pcb)[0] == []


def test_untented_via_far_from_pads_passes(tmp_path):
    pcb = _board(tmp_path, _setup(tent=("no", "no"), margin=0.1),
                 _r0603(margin=0.1), _via(13.0, 10.0))
    kinds, _, seen = _kinds(pcb)
    assert kinds == [] and seen["pairs_near"] == 0


def test_exposed_pad_vias_are_left_to_check_ep_paste(tmp_path):
    # untented, under full paste: check_ep_paste's paste_on_via, not ours
    pcb = _board(tmp_path, _setup(tent=("no", "no")), _qfn(), _ep_vias())
    kinds, _, seen = _kinds(pcb)
    assert kinds == [] and seen["ep_vias_skipped"] == 4


def test_kicad8_list_form_tenting(tmp_path):
    pcb = _board(tmp_path, "(setup (pad_to_mask_clearance 0.1) "
                 "(tenting back))", _r0603(margin=0.1), _via(BESIDE, 10.0))
    kinds, _, _ = _kinds(pcb)
    assert kinds == ["wick_via_mask_web"]   # front not listed: untented


def test_cli_contract(tmp_path, capsys):
    pcb = _board(tmp_path, _setup(), _r0603(), _via(10.775, 10.0))
    out = tmp_path / "r.json"
    assert check_wicking.main(["--pcb", str(pcb), "--out", str(out)]) == 1
    assert out.is_file()


def _kicad_cli():
    import env
    return env.find_kicad_cli()


@pytest.mark.skipif(_kicad_cli() is None, reason="needs kicad-cli")
def test_mask_web_agrees_with_the_kicad_f_mask_gerber(tmp_path):
    """The board model's web (and merge) decision against kicad-cli's own
    F.Mask plot of the same planted boards: merged (0 mm), 0.06 and 0.1."""
    import kc
    for margin, web in ((0.1, 0.0), (0.07, 0.06), (0.05, 0.1)):
        d = tmp_path / f"m{margin}"
        d.mkdir()
        pcb = _board(d, _setup(tent=("no", "no"), margin=margin),
                     _r0603(margin=margin), _via(BESIDE, 10.0))
        g = d / "gerbers"
        res = kc.export_gerbers(Path(_kicad_cli()), pcb, g,
                                layers="F.Mask,B.Mask")
        assert any(p.name.endswith("F_Mask.gbr") or p.name.endswith(".gts")
                   for p in g.iterdir()), res
        vios, seen = check_wicking.check(pcb, gerbers=g)
        cross = seen["mask_crosscheck"]
        assert cross["disagree"] == [] and cross["agree"] == 1, cross
        model, _ = check_wicking.check(pcb)
        assert [v["kind"] for v in vios] == [v["kind"] for v in model]
        if web < 0.1:
            assert vios[0]["web_mm"] == pytest.approx(web, abs=0.005)
        else:
            assert vios == []


def test_a_drill_grazing_the_pad_edge_is_not_a_via_in_pad(tmp_path):
    # tented 0.3 mm drill reaching 0.03 mm into pad 2 (edge at x = 11.175):
    # inside the mask's registration, not counted
    pcb = _board(tmp_path, _setup(), _r0603(), _via(11.175 + 0.15 - 0.03, 10.0))
    assert _kinds(pcb)[0] == []
    # 0.08 mm in is a via in the pad
    pcb = _board(tmp_path, _setup(), _r0603(), _via(11.175 + 0.15 - 0.08, 10.0),
                 name="c")
    assert _kinds(pcb)[0] == ["wick_via_in_pad"]


# --- via_under_body (#1088) ---------------------------------------------

def _body_fp(lib, x, y, w, h, ref="Y1", layer="F", crtyd=False) -> str:
    """A footprint with one pad off to the side and its body drawn as a Fab
    rectangle (or, with crtyd, only a courtyard 0.25 mm larger)."""
    lay, g = (f"{layer}.CrtYd", 0.25) if crtyd else (f"{layer}.Fab", 0.0)
    return (f'(footprint "{lib}" (layer "{layer}.Cu") (at {x} {y})\n'
            f' (property "Reference" "{ref}" (at 0 -3 0) (layer "F.SilkS"))\n'
            f' (fp_rect (start {-w / 2 - g} {-h / 2 - g}) '
            f'(end {w / 2 + g} {h / 2 + g}) (stroke (width 0.1) (type solid)) '
            f'(fill no) (layer "{lay}"))\n'
            f' (pad "1" smd rect (at {w / 2 - 0.4} 0) (size 0.6 0.6) '
            f'(layers "{layer}.Cu" "{layer}.Paste" "{layer}.Mask") '
            f'(net "N1")))\n')


XTAL = "Crystal:Crystal_SMD_3225-4Pin_3.2x2.5mm"


def test_open_via_under_a_crystal_warns(tmp_path):
    pcb = _board(tmp_path, _setup(tent=("no", "no")),
                 _body_fp(XTAL, 20, 20, 3.2, 2.5), _via(19.0, 20.0))
    kinds, vios, seen = _kinds(pcb)
    assert kinds == ["via_under_body"]
    assert vios[0]["refs"] == ["Y1"] and vios[0]["body"] == "crystal"
    assert seen["low_standoff_bodies"] == 1


def test_tented_via_under_a_crystal_passes(tmp_path):
    # the quiet twin: the same via, tented on top
    pcb = _board(tmp_path, _setup(tent=("no", "no")),
                 _body_fp(XTAL, 20, 20, 3.2, 2.5),
                 _via(19.0, 20.0, extra="(tenting (front yes) (back no))"))
    assert _kinds(pcb)[0] == []


def test_plugged_or_filled_via_under_a_body_passes(tmp_path):
    for setup in (_setup(tent=("no", "no"), plug=("yes", "no")),
                  _setup(tent=("no", "no"), fill="yes", cap="yes")):
        pcb = _board(tmp_path, setup, _body_fp(XTAL, 20, 20, 3.2, 2.5),
                     _via(19.0, 20.0))
        assert _kinds(pcb)[0] == []


def test_gull_wing_parts_stand_off_and_pass(tmp_path):
    pcb = _board(tmp_path, _setup(tent=("no", "no")),
                 _body_fp("Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", 20, 20,
                          3.9, 4.9, ref="U1"), _via(19.0, 20.0))
    assert _kinds(pcb)[0] == []


def test_body_from_the_courtyard_and_classes(tmp_path):
    cases = [("Package_DFN_QFN:QFN-20-1EP_4x4mm_P0.5mm", "belly"),
             ("Package_SON:WSON-8-1EP_2x2mm_P0.5mm", "belly"),
             ("Inductor_SMD:L_Bourns_SRN6045TA", "inductor"),
             ("Connector_USB:USB_C_Receptacle_GCT_USB4085", "shell")]
    for i, (lib, cls) in enumerate(cases):
        pcb = _board(tmp_path, _setup(tent=("no", "no")),
                     _body_fp(lib, 20, 20, 4, 4, ref="U1", crtyd=True),
                     _via(18.5, 20.0), name=f"c{i}")
        kinds, vios, _ = _kinds(pcb)
        assert kinds == ["via_under_body"] and vios[0]["body"] == cls, lib
    # a chip inductor (0603) is not a molded power inductor
    assert check_wicking.body_class("Inductor_SMD:L_0603_1608Metric") is None


def test_via_just_outside_the_body_passes(tmp_path):
    # body edge at x = 18.4; a 0.6 mm via centred 0.35 mm out clears it
    pcb = _board(tmp_path, _setup(tent=("no", "no")),
                 _body_fp(XTAL, 20, 20, 3.2, 2.5), _via(18.05, 20.0))
    assert _kinds(pcb)[0] == []


def test_bottom_side_body_only_sees_the_back(tmp_path):
    fp = _body_fp(XTAL, 20, 20, 3.2, 2.5, layer="B")
    open_top = _board(tmp_path, _setup(tent=("no", "yes")), fp,
                      _via(19.0, 20.0), name="t")
    open_back = _board(tmp_path, _setup(tent=("yes", "no")), fp,
                       _via(19.0, 20.0), name="k")
    assert _kinds(open_top)[0] == []
    assert _kinds(open_back)[0] == ["via_under_body"]


def test_kicad5_viasonmask_plots_every_via_open(tmp_path):
    setup = ("(setup (pad_to_mask_clearance 0) "
             "(pcbplotparams (viasonmask true)))")
    pcb = _board(tmp_path, setup, _body_fp(XTAL, 20, 20, 3.2, 2.5),
                 _via(19.0, 20.0))
    kinds, _, seen = _kinds(pcb)
    assert kinds == ["via_under_body"] and not seen.get("tenting_assumed")


def test_kicad6_fp_text_reference_names_the_part():
    import sexpdata
    k8 = sexpdata.loads('(footprint "X" (property "Reference" "U7"))')
    k6 = sexpdata.loads('(footprint "X" (fp_text reference "U301" (at 0 0)))')
    bare = sexpdata.loads('(footprint "X" (fp_text value "10k" (at 0 0)))')
    assert check_wicking._fp_ref(k8) == "U7"
    assert check_wicking._fp_ref(k6) == "U301"
    assert check_wicking._fp_ref(bare) == "?"
