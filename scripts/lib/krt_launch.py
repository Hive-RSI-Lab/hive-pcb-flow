"""Run a KRT script with hwde's patches (#489, #529).

  cd <krt plugins>
  python krt_launch.py [--layer-widths W.json] [--via-joint-ends]
                       [--hole-to-hole MM] [--escape-open O.json] SCRIPT ARGS...

--layer-widths (#489): KRT (KiCadRoutingTools 0.19.0) draws each layer at
  GridRouteConfig.layer_widths[layer] when that map is set, but only its own
  --impedance option fills it, from KRT's own solver. hwde wants the widths
  rules_gen wrote into the .kicad_dru (zrules.layer_geometry), so the
  router, rules_gen and DRC agree. W.json = {"F.Cu": 0.0948, "In2.Cu":
  0.0889}. A run that already sets layer_widths (KRT's --impedance) keeps
  its own.

--via-joint-ends (#529): KRT's connectivity.find_stub_free_ends clusters a
  stub's endpoints per (x, y, layer), so a dog-bone escape (ball -> F.Cu
  neck -> via -> In2 stub to the field edge) reports the via joint itself
  as a "free end" on F.Cu and on In2, beside the real tip. route_diff picks
  its terminals among the free ends by P-N spacing, every candidate ties at
  the ball pitch, and the first one (the via, deep in the ball field) wins:
  the pair then has no room to launch (no-escape-path). With this flag a
  free end that coincides with an endpoint of the same group on ANOTHER
  layer (a via joint) is not a free end; a group whose only ends are such
  joints keeps KRT's answer.

--hole-to-hole MM (#552): KRT checks drill hole-to-hole against its own
  constant (routing_defaults.HOLE_TO_HOLE_CLEARANCE, JLC's 0.20 via-to-via
  spacing), not the --fab-overrides value. A board whose floor is wider
  (rules_gen's aiee_hole_to_hole_floor, 0.5 on JLC 6L) then gets dog-bone
  gap vias 0.566 mm from a via-in-pad at 0.8 mm pitch: hole_to_hole and
  hole_clearance errors DRC rips both nets for. The constant is raised to
  MM (never lowered) before KRT imports it.

--escape-open O.json (#547): KRT's dog-bone engine escapes every ball and
  every coupled pair toward the NEARER edge of the ball field. O.json =
  {"open": {"/DRAM_SDCLK0_P": ["left", "up"], ...}} names, per net, the
  field sides (board axes, up = -y) its escape may leave by. KRT's
  under-pad/dog-bone engine (bga_fanout/underpad.py) is re-compiled from
  its own source with four edits (escape_open_source): the A* goal is
  "outside the field on an open side" for such a net and the margin past a
  closed side is not walkable; a coupled pair (side-by-side or end-on) does
  not try a closed direction; the dog-bone gap site prefers the nearest
  OPEN side. Nets not named escape as before. Each edit must match KRT's
  source exactly once (an edit carries one anchor per KRT version line,
  0.19 and 0.24), else the launch fails (a KRT bump needs a look).

Always on, net-tie copper stays copper (#1008): KRT's writer
  (kicad_writer.move_copper_graphics_to_silkscreen) moves every copper
  graphic without a (net ...) to silkscreen, so that routed copper and pours
  can't short to a net-less copper logo. A footprint's graphics never carry
  a net in the file, so the move also took the bridge of a net-tie footprint
  (net_tie_pad_groups) off copper and left its pads open, which DRC can't
  see (KiCad's connectivity ignores footprint graphics; check_net_tie
  catches it). The mover now gets the board with every net-tie footprint
  held out, and those footprints come back byte for byte. Logos and other
  net-less copper still move, footprint logos included (a copper logo is
  often a footprint, and keeping all footprint copper would bring back the
  shorts the move is there for). KiCad itself defines a net-tie
  footprint's copper as the tie. hwde starts every KRT script through here
  (route_critical.run_krt, route_escape._run); KRT's KiCad plugin runs its
  own copy of the move (gui_utils) and is not covered.

The KRT script itself runs unchanged (runpy).
"""
from __future__ import annotations

import json
import os
import re
import runpy
import sys

JOINT_TOL_MM = 0.01


def via_joint_free_ends(orig):
    """Wrap KRT's find_stub_free_ends(segments, pads, ...) so via joints
    are not reported as stub tips."""
    def find_stub_free_ends(segments, pads, *a, **kw):
        ends = orig(segments, pads, *a, **kw)

        def joint(x, y, layer):
            for s in segments:
                if s.layer == layer:
                    continue
                for sx, sy in ((s.start_x, s.start_y), (s.end_x, s.end_y)):
                    if abs(sx - x) <= JOINT_TOL_MM and abs(sy - y) <= JOINT_TOL_MM:
                        return True
            return False

        keep = [e for e in ends if not joint(e[0], e[1], e[2])]
        return keep or ends
    find_stub_free_ends.__wrapped__ = orig
    return find_stub_free_ends


def _patch_layer_widths(widths: dict) -> None:
    import routing_config

    cls = routing_config.GridRouteConfig
    orig = cls.__init__

    def init(self, *a, **kw):
        if not kw.get("layer_widths"):
            kw["layer_widths"] = dict(widths)
        orig(self, *a, **kw)

    cls.__init__ = init


def _patch_free_ends() -> None:
    import connectivity

    orig = connectivity.find_stub_free_ends
    patched = via_joint_free_ends(orig)
    # every module already holding KRT's function (a `from connectivity
    # import find_stub_free_ends` that ran while connectivity loaded); the
    # ones the script imports later read the patched attribute
    for mod in list(sys.modules.values()):
        if getattr(mod, "find_stub_free_ends", None) is orig:
            mod.find_stub_free_ends = patched


def raise_hole_to_hole(mm: float) -> float:
    """Set KRT's hole-to-hole constant to max(KRT's, mm); returns it."""
    import routing_defaults

    v = max(float(routing_defaults.HOLE_TO_HOLE_CLEARANCE), float(mm))
    routing_defaults.HOLE_TO_HOLE_CLEARANCE = v
    return v


SIDES = ("left", "right", "up", "down")

_HW_HELPERS = """
    _hw_open = {}
    for _hp in fp_pads:
        _hs = HWDE_OPEN.get(_hp.net_name)
        if _hs:
            _hw_open[_hp.net_id] = frozenset(_hs)
    _hw_far = 10 ** 9

    def _hw_inb(ix, iy, nid):
        s = _hw_open.get(nid)
        if not s:
            return in_bbox(ix, iy)
        return ((bx0 - (0 if 'left' in s else _hw_far)) <= ix
                <= (bx1 + (0 if 'right' in s else _hw_far))
                and (by0 - (0 if 'up' in s else _hw_far)) <= iy
                <= (by1 + (0 if 'down' in s else _hw_far)))

    def _hw_shut(pad, e):
        s = _hw_open.get(pad.net_id)
        if not s:
            return False
        ex, ey = e
        if abs(ex) >= abs(ey):
            side = 'right' if ex > 0 else 'left'
        else:
            side = 'down' if ey > 0 else 'up'
        return side not in s

    def _hw_dists(p, d_e, d_w, d_s, d_n):
        s = _hw_open.get(p.net_id)
        if not s:
            return d_e, d_w, d_s, d_n
        big = 1e6
        return (d_e + (0 if 'right' in s else big),
                d_w + (0 if 'left' in s else big),
                d_s + (0 if 'down' in s else big),
                d_n + (0 if 'up' in s else big))
"""

# (anchor, replacement) on bga_fanout/underpad.py, or a list of them, one
# per KRT version line whose source differs (0.19, 0.24): of a list, the one
# whose anchor occurs exactly once is applied, and an edit none of whose
# anchors does fails the launch
ESCAPE_OPEN_EDITS = [
    ("    def heur(ix, iy):\n"
     "        return max(0, min(ix - bx0, bx1 - ix, iy - by0, by1 - iy))\n",
     "    def heur(ix, iy):\n"
     "        return max(0, min(ix - bx0, bx1 - ix, iy - by0, by1 - iy))\n"
     + _HW_HELPERS),
    # A* goal: past an open side for a steered net
    [("            if not in_bbox(cx, cy):\n                path = [cur]\n",
      "            if not _hw_inb(cx, cy, net_id):\n                path = [cur]\n"),
     # 0.24: the goal test is inlined and honours KRT's own planned side
     ("                         and (side is None or _left_on(side, cx, cy)))\n",
      "                         and (side is None or _left_on(side, cx, cy))\n"
      "                         and not _hw_inb(cx, cy, net_id))\n")],
    # the margin past a closed side is not walkable (0.19: outside the bbox
    # KRT checks no obstacle, so a walk around the field would short things;
    # 0.24 checks it, but a closed side must still not be left by)
    [("                    if not occ.inside(nx, ny):\n"
      "                        continue\n",
      "                    if not occ.inside(nx, ny):\n"
      "                        continue\n"
      "                    if not in_bbox(nx, ny) and _hw_inb(nx, ny, net_id):\n"
      "                        continue\n"),
     ("                    inwin = bx0 <= nx <= bx1 and by0 <= ny <= by1\n",
      "                    inwin = bx0 <= nx <= bx1 and by0 <= ny <= by1\n"
      "                    if not inwin and _hw_inb(nx, ny, net_id):\n"
      "                        continue\n")],
    # coupled pair, side by side
    ("          for e in cand_e:\n            ex, ey = e\n",
     "          for e in cand_e:\n            ex, ey = e\n"
     "            if _hw_shut(pp, e):\n                continue\n"),
    # coupled pair, end-on
    ("            ex, ey = sgn * ax, sgn * ay\n"
     "            De = boundary_dist(mx, my, (ex, ey))\n",
     "            ex, ey = sgn * ax, sgn * ay\n"
     "            if _hw_shut(pp, (ex, ey)):\n                continue\n"
     "            De = boundary_dist(mx, my, (ex, ey))\n"),
    # dog-bone gap site toward the nearest open side
    ("        d_s, d_n = grid.max_y - gy, gy - grid.min_y\n",
     "        d_s, d_n = grid.max_y - gy, gy - grid.min_y\n"
     "        d_e, d_w, d_s, d_n = _hw_dists(p, d_e, d_w, d_s, d_n)\n"),
]


def escape_open_source(src: str) -> str:
    """KRT's underpad.py source with the --escape-open edits applied."""
    for edit in ESCAPE_OPEN_EDITS:
        alts = edit if isinstance(edit, list) else [edit]
        hits = [(a, r) for a, r in alts if src.count(a) == 1]
        if len(hits) != 1:
            raise SystemExit(
                "krt_launch --escape-open: KRT underpad.py anchor found "
                f"{[src.count(a) for a, _ in alts]}x (expected one of them "
                f"once): {alts[0][0].strip()[:60]!r}")
        anchor, repl = hits[0]
        src = src.replace(anchor, repl)
    return src


def _patch_escape_open(plan: dict) -> None:
    import bga_fanout.underpad as up

    opened = {str(k): [s for s in v if s in SIDES]
              for k, v in (plan.get("open") or {}).items()}
    src = escape_open_source(open(up.__file__, encoding="utf-8").read())
    up.__dict__["HWDE_OPEN"] = opened
    # re-define the module's functions from the edited source in its own
    # namespace (bga_fanout imports generate_underpad_escape at call time)
    exec(compile(src, up.__file__, "exec"), up.__dict__)


_NET_TIE = re.compile(r'\(net_tie_pad_groups\s+"[^"]')
_HELD = "[hwde-net-tie-held-{}]"


def _sexpr_end(text: str, start: int) -> int:
    """Index just past the s-expression opening at text[start] (quoted
    strings, with their escapes, are skipped)."""
    depth, i, n = 0, start, len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def net_tie_footprints(content: str) -> list[tuple[int, int]]:
    """(start, end) of each footprint block that declares
    net_tie_pad_groups, in file order."""
    spans: list[tuple[int, int]] = []
    for m in _NET_TIE.finditer(content):
        if spans and m.start() < spans[-1][1]:
            continue
        start = content.rfind("(footprint ", 0, m.start())
        if start < 0:
            continue
        end = _sexpr_end(content, start)
        if m.start() < end and (not spans or start >= spans[-1][1]):
            spans.append((start, end))
    return spans


def keep_net_tie_copper(orig):
    """Wrap KRT's move_copper_graphics_to_silkscreen(content) so that the
    footprints declaring net_tie_pad_groups keep their copper: the mover
    sees a placeholder for each and they are put back unchanged."""
    def move_copper_graphics_to_silkscreen(content, *a, **kw):
        spans = net_tie_footprints(content)
        if not spans:
            return orig(content, *a, **kw)
        held, parts, last = [], [], 0
        for k, (s, e) in enumerate(spans):
            held.append(content[s:e])
            parts += [content[last:s], _HELD.format(k)]
            last = e
        parts.append(content[last:])
        out = orig("".join(parts), *a, **kw)
        for k, block in enumerate(held):
            mark = _HELD.format(k)
            if out.count(mark) != 1:
                raise RuntimeError(
                    f"krt_launch: net-tie footprint placeholder {mark} came "
                    f"back {out.count(mark)}x from KRT's mover (expected 1)")
            out = out.replace(mark, block, 1)
        return out
    move_copper_graphics_to_silkscreen.__wrapped__ = orig
    return move_copper_graphics_to_silkscreen


def _patch_net_tie_copper() -> None:
    try:
        import kicad_writer
    except ModuleNotFoundError as exc:
        if exc.name != "kicad_writer":
            raise
        return          # no KRT writer, so no mover to wrap (a test's stand-in)
    orig = getattr(kicad_writer, "move_copper_graphics_to_silkscreen", None)
    if orig is None:
        raise SystemExit("krt_launch: KRT's kicad_writer has no "
                         "move_copper_graphics_to_silkscreen (a KRT bump "
                         "needs a look, #1008)")
    patched = keep_net_tie_copper(orig)
    # kicad_writer's own calls read its module global; output_writer and
    # plane_io import the function by name, so any module already holding
    # it is rebound (the ones the script imports later get the patched one)
    for mod in list(sys.modules.values()):
        if getattr(mod, "__dict__", {}).get(
                "move_copper_graphics_to_silkscreen") is orig:
            mod.move_copper_graphics_to_silkscreen = patched


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    widths, joints, h2h, opened = None, False, None, None
    while argv and argv[0].startswith("--"):
        flag = argv.pop(0)
        if flag == "--layer-widths":
            with open(argv.pop(0), encoding="utf-8") as fh:
                widths = {str(k): float(v) for k, v in json.load(fh).items()}
        elif flag == "--via-joint-ends":
            joints = True
        elif flag == "--hole-to-hole":
            h2h = float(argv.pop(0))
        elif flag == "--escape-open":
            with open(argv.pop(0), encoding="utf-8") as fh:
                opened = json.load(fh)
        else:
            raise SystemExit(f"krt_launch: unknown flag {flag}")
    if not argv:
        raise SystemExit("krt_launch: no KRT script given")
    script = argv.pop(0)
    here = os.getcwd()                  # KRT runs with cwd = its plugins dir
    if here not in sys.path:
        sys.path.insert(0, here)
    if widths:
        _patch_layer_widths(widths)
    if joints:
        _patch_free_ends()
    if h2h is not None:
        raise_hole_to_hole(h2h)
    if opened:
        _patch_escape_open(opened)
    _patch_net_tie_copper()
    sys.argv = [script, *argv]
    runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    main()
