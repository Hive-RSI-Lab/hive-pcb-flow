"""routelib.dsn_hole_floor / ses_restore_vias / dsn_npth_keepouts: KiCad's
DSN export drops the board's hole rules, so Freerouting placed vias 0.46-0.49
mm apart drill to drill (JLC floor 0.5) and routed 0.16 mm from NPTH pegs on
lite-main (hifi-receiver-2). Pure text tests; no KiCad needed."""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))
import routelib  # noqa: E402

DSN = """(pcb "/x/b.dsn"
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
    (layer B.Cu
      (type signal)
    )
    (via "Via[0-1]_600:300_um" "Via[0-1]_800:400_um")
    (rule
      (width 200)
      (clearance 127)
      (clearance 31.75 (type smd_smd))
    )
  )
  (library
    (padstack "Via[0-1]_600:300_um"
      (shape (circle F.Cu 600))
      (shape (circle B.Cu 600))
      (attach off)
    )
    (padstack "Via[0-1]_800:400_um"
      (shape (circle F.Cu 800))
      (shape (circle B.Cu 800))
      (attach off)
    )
  )
  (network
    (class kicad_default "" A B
      (circuit
        (use_via "Via[0-1]_600:300_um")
      )
      (rule
        (width 200)
        (clearance 150)
      )
    )
  )
)
"""

SES = """(session "b.ses"
  (routes
    (resolution um 10)
    (parser
      (host_cad "KiCad's Pcbnew")
    )
    (library_out
      (padstack "Via[0-1]_600:300_um"
        (shape
          (circle F.Cu 6730 0 0)
        )
        (shape
          (circle B.Cu 6730 0 0)
        )
        (attach off)
      )
    )
    (network_out
      (net A
        (via "Via[0-1]_600:300_um" 10000 -20000)
      )
    )
  )
)
"""


def _circles(text, name):
    m = re.search(r'\(padstack "' + re.escape(name) + r'"', text)
    block = text[m.start():routelib._sexp_end(text, m.start())]
    return re.findall(r"\(circle \S+ ([\d.]+)", block)


def test_via_padstack_grows_to_the_hole_floor():
    text, grown = routelib.dsn_hole_floor(DSN, 0.5)
    # 500 + 300 - 127 (the least clearance) = 673
    assert grown == {"Via[0-1]_600:300_um": (600.0, 673.0)}
    assert _circles(text, "Via[0-1]_600:300_um") == ["673", "673"]
    # 500 + 400 - 127 = 773 < 800: already enough, untouched
    assert _circles(text, "Via[0-1]_800:400_um") == ["800", "800"]
    assert "Via[0-1]_800:400_um" not in grown
    # the rest of the file is as it was
    assert text.replace("673", "600") == DSN


def test_hole_floor_needs_um_and_a_clearance():
    assert routelib.dsn_hole_floor(DSN.replace("(unit um)", "(unit mil)"),
                                   0.5) == (DSN.replace("(unit um)",
                                                        "(unit mil)"), {})
    assert routelib.dsn_hole_floor(DSN, 0.25)[1] == {}   # 250+300-127 < 600


def test_ses_restore_puts_the_real_diameter_back():
    _, grown = routelib.dsn_hole_floor(DSN, 0.5)
    out, n = routelib.ses_restore_vias(SES, grown)
    assert n == 1
    assert "(circle F.Cu 6000 0 0)" in out and "(circle B.Cu 6000 0 0)" in out
    assert "6730" not in out
    assert routelib.ses_restore_vias(SES, {}) == (SES, 0)


def test_npth_keepouts_cover_holes_with_margin():
    holes = [{"at": [10.0, 20.0], "drill": [1.0, 1.0], "angle": 0},
             {"at": [30.0, 5.0], "drill": [1.7, 2.4], "angle": 0}]
    text, n = routelib.dsn_npth_keepouts(DSN, holes, 0.3)
    assert n == 2
    # a round hole: a circle of hole + 2 x margin on each signal layer
    assert '(keepout "aiee_npth" (circle F.Cu 1600.0 10000.0 -20000.0))' in text
    assert '(keepout "aiee_npth" (circle B.Cu 1600.0 10000.0 -20000.0))' in text
    # a slot: a polygon covering the grown stadium (y flipped)
    polys = re.findall(r'\(keepout "aiee_npth" \(polygon (\S+) 0  ([^)]*)\)\)',
                       text)
    assert [ly for ly, _ in polys] == ["F.Cu", "B.Cu"]
    nums = [float(v) for v in polys[0][1].split()]
    xs, ys = nums[0::2], nums[1::2]
    assert min(xs) <= 30000 - 1150 and max(xs) >= 30000 + 1150
    assert min(ys) <= -5000 - 1500 and max(ys) >= -5000 + 1500
    assert max(xs) - min(xs) < 2400 and max(ys) - min(ys) < 3200
    # inside the structure, which still closes
    s, e = routelib._structure_span(routelib._parser_blanked(text))
    assert text.index("aiee_npth") > s and text.rindex("aiee_npth") < e
    assert text.index("(library") > e


def test_npth_keepouts_rotated_slot_and_no_holes():
    holes = [{"at": [0.0, 0.0], "drill": [2.0, 1.0], "angle": 90}]
    text, _ = routelib.dsn_npth_keepouts(DSN, holes, 0.0)
    nums = [float(v) for v in re.search(
        r"\(polygon F.Cu 0  ([^)]*)\)", text).group(1).split()]
    xs, ys = nums[0::2], nums[1::2]
    # turned 90 deg: long axis along y
    assert max(ys) - min(ys) > 1900 and max(xs) - min(xs) < 1100
    assert routelib.dsn_npth_keepouts(DSN, [], 0.3) == (DSN, 0)


def test_dsn_shuffle_reorders_components_and_nets_only():
    dsn = DSN.replace("  (network\n", """  (placement
    (component R_A
      (place R1 0 0 front 0)
    )
    (component R_B
      (place R2 1 0 front 0)
    )
    (component R_C
      (place R3 2 0 front 0)
    )
  )
  (network
    (net A
      (pins R1-1 R2-1)
    )
    (net B
      (pins R2-2 R3-1)
    )
    (net "C(1)"
      (pins R3-2 R1-2)
    )
""")
    outs = {routelib.dsn_shuffle(dsn, s) for s in range(1, 8)}
    assert len(outs) > 1                         # the seed matters
    assert routelib.dsn_shuffle(dsn, 3) == routelib.dsn_shuffle(dsn, 3)
    for out in outs:
        assert sorted(out.splitlines()) == sorted(dsn.splitlines())
        # classes stay after the nets, sections stay in place
        assert out.index("(class kicad_default") > out.index('(net "C(1)"')
        assert out.index("(placement") < out.index("(network")
    assert routelib.dsn_shuffle("(pcb x", 1) == "(pcb x"


def test_edge_keepouts_band_every_edge_on_every_layer():
    # a 20 x 10 board with a 4 x 4 window: 8 segments, each once
    outer = [(0, 0), (20, 0), (20, 10), (0, 10), (0, 0)]
    window = [(8, 3), (12, 3), (12, 7), (8, 7), (8, 3)]
    segs = routelib.edge_segments([outer, window, list(reversed(window))])
    assert len(segs) == 8
    text, n = routelib.dsn_edge_keepouts(DSN, segs, 0.3)
    assert n == 8
    polys = re.findall(r'\(keepout "aiee_edge" \(polygon (\S+) 0  ([^)]*)\)\)',
                       text)
    assert len(polys) == 16
    assert {ly for ly, _ in polys} == {"F.Cu", "B.Cu"}
    # the bottom edge (y = 0): 0.3 mm either side, 0.3 mm past each end
    bottom = [[float(v) for v in c.split()] for _, c in polys
              if all(abs(float(y)) <= 300.1
                     for y in c.split()[1::2])][0]
    xs, ys = bottom[0::2], bottom[1::2]
    assert min(xs) == -300.0 and max(xs) == 20300.0
    assert min(ys) == -300.0 and max(ys) == 300.0
    s, e = routelib._structure_span(routelib._parser_blanked(text))
    assert text.index("aiee_edge") > s and text.rindex("aiee_edge") < e


def test_edge_keepouts_need_a_margin():
    segs = routelib.edge_segments([[(0, 0), (5, 0), (5, 5), (0, 0)]])
    assert routelib.dsn_edge_keepouts(DSN, segs, 0.0) == (DSN, 0)
    assert routelib.dsn_edge_keepouts(DSN, [], 0.3) == (DSN, 0)
