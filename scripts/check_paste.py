#!/usr/bin/env python
"""check_paste.py - stencil apertures that won't print and exposed pads with
the wrong paste (board file; dfm-assembly ingestion, task 261031-011721).

hwde's dfm checks that a paste aperture has a mask opening (dfm_pad_tented)
and nothing more: no check asks whether the stencil can release the paste
or how much paste an exposed pad gets. Every pinned board ships its QFN and
HTSSOP thermal pads with an aperture the size of the pad (100 % coverage),
and paste apertures on 0.4 mm-pitch pads are only printable on a thin foil.
The rules, each reproduced in reference/ingest/dfm-assembly/stencil_rules.py
and cited in reference/ingest/dfm-assembly.md:

  paste_area_ratio   warning: an aperture's area ratio, opening area over
                     wall area, A / (P * t), is under 0.66 at the stencil
                     thickness t (IPC-7525's release floor; fine-pitch and
                     0201 apertures fail it on a 0.12 mm foil). One finding
                     per footprint, naming the pads.
  paste_ep_missing   error: an exposed (thermal/ground) pad with no paste at
                     all - no solder joint under the part.
  paste_ep_coverage  warning: a leadless part's (QFN, DFN, SON, LGA) exposed
                     pad gets paste over more than 90 % or less than 40 %
                     of it. Amkor/Microchip, NXP AN1902, TI SLUA271 and
                     Broadcom ask for 50-85 % in a window-pane array: a
                     full aperture floats the part off its leads, bridges
                     them and voids; too little leaves voids and a poor
                     thermal joint. Leaded exposed-pad packages (TI
                     PowerPAD HTSSOP/TQFP, SO-8EP) are left alone: TI asks
                     for a 1:1 opening there (SLOA120). JLC may reduce an
                     IC's aperture itself unless the order asks for 1:1;
                     the board's paste layer is what the check reads.

An exposed pad is a footprint's largest SMD pad with copper on the
footprint's own side (all of its SMD pads when none is there, so a stock
`*_ThermalVias` belly's bottom copy is not it) when the footprint has three
or more other pads on the same side, it is at least four times their median
area, and its centre sits inside the hull of their centres (QFN, DFN,
HTSSOP, SO-8EP and modules' ground pads; not a SOT-223 tab or a 2-pad
inductor).

An aperture is the pad's shape grown by KiCad's paste margin: the pad's own
`solder_paste_margin` / `solder_paste_margin_ratio`, else the footprint's,
else the board setup's `pad_to_paste_clearance(_ratio)`; per axis,
size + 2 * (margin + ratio * size). Paste-only pads (a window-pane
footprint's paste squares) are apertures of their own, at their own size:
KiCad gives a pad with no copper no margin. Both rules are checked against
KiCad's own plot by reference/ingest/dfm-assembly/paste_margin_vs_kicad.py. Custom-shape pads
are counted as unchecked, not guessed. The thickness is `--stencil-mm`,
else constraints.json's `stencil_mm`, else 0.12 mm (JLCPCB's usual foil for
boards without 0.4 mm-pitch parts; say so in constraints.json when the
order asks for 0.10).

CLI: --pcb board.kicad_pcb [--stencil-mm T] [--constraints C] [--out report.json]
     exit 0 pass / 1 violations / 2 error, per SPEC section 6.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path

import sexpdata
from shapely.geometry import MultiPoint, Point
from shapely.ops import unary_union

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "lib"))
import checklib  # noqa: E402
import geom  # noqa: E402
from geom import _kid, _kids, _nums, _strs, _tok  # noqa: E402

SCRIPT = "check_paste"
SOURCE = SCRIPT

STENCIL_MM = 0.12        # default foil
AREA_RATIO_MIN = 0.66    # IPC-7525 (stencil_rules.py reproduces the cases)
EP_COVER_MAX = 0.90
EP_COVER_MIN = 0.40
EP_AREA_FACTOR = 4.0     # exposed pad >= this x the median of the others
EP_MIN_OTHERS = 3
# footprint names of leadless packages, whose exposed pad wants a window pane
LEADLESS = re.compile(r"QFN|DFN|SON|LGA|MLF|MLP|LLP", re.I)


def _margin(node) -> tuple[float | None, float | None]:
    m = _kid(node, "solder_paste_margin")
    r = _kid(node, "solder_paste_margin_ratio")
    return (_nums(m)[0] if m is not None and _nums(m) else None,
            _nums(r)[0] if r is not None and _nums(r) else None)


def _board_margin(root) -> tuple[float, float]:
    setup = _kid(root, "setup")
    m = r = None
    if setup is not None:
        mn = _kid(setup, "pad_to_paste_clearance")
        rn = _kid(setup, "pad_to_paste_clearance_ratio")
        m = _nums(mn)[0] if mn is not None and _nums(mn) else None
        r = _nums(rn)[0] if rn is not None and _nums(rn) else None
    return m or 0.0, r or 0.0


def _layers(pad) -> list[str]:
    ln = _kid(pad, "layers")
    return _strs(ln) if ln is not None else []


def _paste_side(layers: list[str]) -> str | None:
    for side in ("F", "B"):
        if f"{side}.Paste" in layers:
            return side
    if "*.Paste" in layers:
        return "F"
    return None


def _cu_side(layers: list[str]) -> str | None:
    for side in ("F", "B"):
        if f"{side}.Cu" in layers:
            return side
    return None


def aperture_size(w: float, h: float, margin: float, ratio: float
                  ) -> tuple[float, float]:
    """KiCad's paste aperture: each axis grows by 2 * (margin + ratio * axis),
    and never below zero."""
    return (max(0.0, w + 2 * (margin + ratio * w)),
            max(0.0, h + 2 * (margin + ratio * h)))


def area_ratio(poly, t: float) -> float:
    """IPC-7525 area ratio: the opening's area over its wall area, P * t."""
    return poly.area / (poly.length * t) if poly.length and t else 0.0


def read_pads(pcb: Path) -> tuple[dict, dict]:
    """{ref: [pad dict]} for every pad of every footprint, and the counts
    of what could not be modelled."""
    root = sexpdata.loads(pcb.read_text(encoding="utf-8"))
    b_m, b_r = _board_margin(root)
    out: dict[str, list[dict]] = {}
    skipped = {"custom_pads": 0}
    for fp in _kids(root, "footprint"):
        at = _kid(fp, "at")
        fnums = _nums(at) if at is not None else [0, 0, 0]
        fx, fy = fnums[0], fnums[1]
        fangle = fnums[2] if len(fnums) > 2 else 0.0
        ref = next((pv[1] for prop in _kids(fp, "property")
                    if len(pv := _strs(prop)) >= 2 and pv[0] == "Reference"),
                   "?")
        f_m, f_r = _margin(fp)
        lib = _strs(fp)[0] if _strs(fp) else ""
        leadless = bool(LEADLESS.search(lib.split(":")[-1]))
        side = fp_side(fp)
        for pad in _kids(fp, "pad"):
            if len(pad) < 4:
                continue
            number = str(pad[1])
            kind, shape = str(_tok(pad[2])), str(_tok(pad[3]))
            pat, size = _kid(pad, "at"), _kid(pad, "size")
            if pat is None or size is None:
                continue
            layers = _layers(pad)
            lx, ly, *rest = _nums(pat)
            angle = rest[0] if rest else 0.0
            dx, dy = geom._rot(lx, ly, -fangle)
            sz = _nums(size)
            w, h = sz[0], (sz[1] if len(sz) > 1 else sz[0])
            rr = _kid(pad, "roundrect_rratio")
            rratio = _nums(rr)[0] if rr is not None and _nums(rr) else 0.0
            p_m, p_r = _margin(pad)
            m = next(v for v in (p_m, f_m, b_m) if v is not None)
            r = next(v for v in (p_r, f_r, b_r) if v is not None)
            if _cu_side(layers) is None:
                m = r = 0.0     # KiCad plots a paste-only pad at its size
            center = (fx + dx, fy + dy)
            psh = "rect" if shape == "custom" else shape
            copper = geom._pad_polygon(psh, w, h, rratio, center, angle)
            ap = None
            pside = _paste_side(layers)
            if pside is not None:
                if shape == "custom":
                    skipped["custom_pads"] += 1
                else:
                    aw, ah = aperture_size(w, h, m, r)
                    if aw > 0 and ah > 0:
                        ap = geom._pad_polygon(psh, aw, ah, rratio, center,
                                               angle)
            out.setdefault(ref, []).append({
                "number": number, "kind": kind, "shape": shape,
                "cu_side": _cu_side(layers), "paste_side": pside,
                "copper": copper, "aperture": ap, "center": center,
                "leadless": leadless, "fp_side": side})
    return out, skipped


def fp_side(fp) -> str | None:
    """A footprint's own side from its `(layer)`: "F", "B" or None."""
    fl = _kid(fp, "layer")
    return {"F.Cu": "F", "B.Cu": "B"}.get(
        str(_tok(fl[1])) if fl is not None and len(fl) > 1 else "")


def exposed_pad(pads: list[dict]) -> dict | None:
    """The footprint's exposed pad, or None (rule in the module docstring)."""
    smd = [p for p in pads if p["kind"] == "smd" and p["cu_side"]]
    # the part's own side; all of them when none is there (#1197)
    smd = [p for p in smd if p.get("fp_side") in (None, p["cu_side"])] or smd
    if len(smd) < EP_MIN_OTHERS + 1:
        return None
    big = max(smd, key=lambda p: p["copper"].area)
    others = [p for p in smd if p is not big and p["cu_side"] == big["cu_side"]]
    if len(others) < EP_MIN_OTHERS:
        return None
    med = statistics.median(p["copper"].area for p in others)
    if big["copper"].area < EP_AREA_FACTOR * med:
        return None
    hull = MultiPoint([p["center"] for p in others]).convex_hull
    if hull.geom_type != "Polygon" \
            or not hull.contains(Point(big["copper"].centroid)):
        return None
    return big


def check(pcb: Path, stencil_mm: float) -> tuple[list[dict], dict]:
    fps, skipped = read_pads(pcb)
    vios: list[dict] = []
    seen = {"footprints": len(fps), "apertures": 0, "exposed_pads": 0,
            "stencil_mm": stencil_mm, **skipped}
    for ref in sorted(fps):
        pads = fps[ref]
        low = []
        for p in pads:
            if p["aperture"] is None:
                continue
            seen["apertures"] += 1
            ar = area_ratio(p["aperture"], stencil_mm)
            if ar < AREA_RATIO_MIN:
                low.append((p, ar))
        if low:
            worst = min(low, key=lambda t: t[1])
            names = ", ".join(sorted({p["number"] for p, _ in low}))
            c = worst[0]["center"]
            vios.append(checklib.violation(
                SCRIPT, "warning", c, f"{worst[0]['paste_side']}.Paste",
                None, [ref],
                f"{ref}: {len(low)} paste aperture(s) (pads {names}) under "
                f"the 0.66 area ratio on a {stencil_mm} mm stencil (worst "
                f"{worst[1]:.2f}); the paste may not release. Order a "
                f"thinner foil (set constraints.json stencil_mm) or "
                f"enlarge the aperture", SOURCE, kind="paste_area_ratio",
                area_ratio=checklib.rnd(worst[1]), pads=sorted(
                    {p["number"] for p, _ in low}),
                stencil_mm=stencil_mm))
        ep = exposed_pad(pads)
        if ep is None:
            continue
        seen["exposed_pads"] += 1
        side = ep["cu_side"]
        aps = [p["aperture"] for p in pads if p["aperture"] is not None
               and p["paste_side"] == side
               and p["aperture"].intersects(ep["copper"])]
        cover = (unary_union(aps).intersection(ep["copper"]).area
                 / ep["copper"].area) if aps else 0.0
        c = ep["center"]
        if cover == 0.0:
            vios.append(checklib.violation(
                SCRIPT, "error", c, f"{side}.Paste", None, [ref],
                f"{ref}: exposed pad {ep['number']} has no solder paste; "
                f"it will not be soldered (no thermal or ground joint)",
                SOURCE, kind="paste_ep_missing", pad=ep["number"]))
        elif ep["leadless"] and (cover > EP_COVER_MAX
                                 or cover < EP_COVER_MIN):
            vios.append(checklib.violation(
                SCRIPT, "warning", c, f"{side}.Paste", None, [ref],
                f"{ref}: exposed pad {ep['number']} paste covers "
                f"{cover:.0%} of it; QFN guidance is 50-80 % in a "
                f"window-pane array ({'too much: the part floats, leads '
                'bridge and the pad voids' if cover > EP_COVER_MAX else
                'too little: voids and a weak thermal joint'})",
                SOURCE, kind="paste_ep_coverage", pad=ep["number"],
                coverage=checklib.rnd(cover)))
    return vios, seen


def _stencil(args) -> float:
    if args.stencil_mm:
        return float(args.stencil_mm)
    if args.constraints and Path(args.constraints).is_file():
        c = json.loads(Path(args.constraints).read_text(encoding="utf-8"))
        if c.get("stencil_mm"):
            return float(c["stencil_mm"])
    return STENCIL_MM


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcb", required=True)
    ap.add_argument("--stencil-mm", type=float)
    ap.add_argument("--constraints")
    ap.add_argument("--out", help="write the JSON report here")
    args = ap.parse_args(argv)
    pcb = Path(args.pcb)
    if not pcb.is_file():
        raise checklib.CheckError(f"board not found: {pcb}")
    vios, seen = check(pcb, _stencil(args))
    return checklib.report(SCRIPT, pcb, vios, checked=[seen]), args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    sys.exit(main())
