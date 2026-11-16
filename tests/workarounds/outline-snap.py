"""outline-snap: does dfm read an outline whose corners meet within 0.05 um?

board_init draws the outline at a non-round origin (lite-main's at
x = 22.864999), and KiCad's Gerber writer then leaves its corner arcs up to
0.05 um off the straight edges. gerblib snapped the Edge.Cuts lines to 0.01 um
before it closed them, so the outline came out open, and dfm skipped every
copper- and hole-to-edge check on it. Since #858 it joins line ends within
0.1 um first. lite-main (hifi-receiver-2) snaps its
outline to 1 um by hand before fab (kicad/gen/snap_outline.py). Fixed when
gerblib closes a 100 x 90 mm outline with a 0.05 um gap at one corner. (If
the fix lands in board_init instead, round origins, this probe moves there.)
"""
import _probe

import gerblib

d = _probe.scratch() / "fab"
d.mkdir()
x0, y0 = 22864999, -62323000           # nm, 4.6 format
w, h = 100_000_000, 90_000_000
pts = [(x0, y0), (x0 + w, y0), (x0 + w, y0 - h), (x0, y0 - h), (x0 - 50, y0)]
body = [f"X{pts[0][0]}Y{pts[0][1]}D02*"] + [f"X{x}Y{y}D01*" for x, y in pts[1:]]
(d / "probe-Edge_Cuts.gm1").write_text("\n".join([
    "%TF.FileFunction,Profile,NP*%", "%FSLAX46Y46*%", "%MOMM*%", "%LPD*%",
    "G01*", "%ADD10C,0.100000*%", "D10*", *body, "M02*", ""]))
fab = gerblib.open_fab(d)
if fab.edge_file is None:
    raise SystemExit("gerblib found no Edge.Cuts file")
if fab.outline.is_empty:
    _probe.stands("an outline with a 0.05 um corner gap reads as open")
_probe.fixed(f"the outline closes ({fab.outline.area:.0f} mm2)")
