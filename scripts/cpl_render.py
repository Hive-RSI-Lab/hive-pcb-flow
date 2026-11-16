#!/usr/bin/env python
"""cpl_render.py - draw each placed part the way JLC's assembly preview does.

The image half of the CPL placement check (the script half is cpl_verify.py).
For every part in the CPL it draws, in board coordinates:

  board     the footprint's silkscreen (white) and copper pads (gold), with
            the board's pad 1 outlined in cyan and labelled "1" - and, on a
            diode, LED or polarised cap, the board's K or + pad labelled.
  part      the part's OWN LCSC footprint model (lib/easyeda.py) at the CPL
            position and rotation: translucent grey pads inside a body
            outline, a red dot on the model's pin 1 and, when polarised, its
            K or + pad labelled in red.

A part is right when the red pin-1 dot sits on the cyan pad 1 (and the red K/+
label on the board's). A bottom-side part's crop is drawn as seen from below,
with the board turned about its y axis (x runs right to left) and the model
turned in that view, as cpl_verify.py reads the CPL; its title says "BOTTOM,
from below". That view is the exporters' rule, not JLC's own preview, so a match
here doesn't replace the check in JLC's preview. Where a polar part's model
numbers its pins the other way from the board (C2286's K is its pin 2), the red
dot rightly sits off pad 1: its title says "judge K/+, not pin 1" and the index
has "judge_by": "role". Parts go several to an image (--per-image, default 9),
one titled crop each. A part with no model is drawn with the board only and a
"NO LCSC MODEL" title, so the image pass sees it as unverifiable. A part whose
model fetch failed (an EasyEDA 403/429 or a network error) is a separate case:
it is titled "FETCH FAILED", and the fix is to rerun, not to treat it as no
model. A ball grid with no model gets an orange dot where the CPL rotation puts
a standard part's A1 (cpl_verify.footprint_a1), and the title says so: the dot
should sit on the board's pin 1, and that is a convention, not the part's model.

The images feed the fab step's vision agent (agents/dfm.md), which writes
cpl_visual.json; cpl_verify.merge_visual / dfm_check put that beside the
script verdict. This script never calls a model.

CLI:
  cpl_render.py --pcb board.kicad_pcb --cpl fab/CPL.csv --out-dir fab/cpl_render
                [--parts parts.json] [--cache-dir DIR] [--offline]
                [--per-image N] [--out index.json]
Writes cpl_render_<n>.png and index.json ({"images": [{"file", "refs"}],
"parts": {ref: {"lcsc", "cpl_rot", "polar", "has_model", "side"[, "fetch_error"]
[, "expected_pin1": {"basis": "footprint", "pin", "at": [x, y]}]}}}
plus "fetch_failed" [refs], "rate_limited" and a rerun "note" when a fetch
failed). "polar" is null for a part whose fetch failed, since it can't be judged
without the model.
Exit 0 / 1 fetch failed (index.json still written) / 2 error.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import cpl_verify as cv  # noqa: E402
import easyeda  # noqa: E402


def _rect(cx, cy, w, h, deg):
    pts = [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)]
    return [(cx + x, cy + y) for x, y in (cv.rot(px, py, deg) for px, py in pts)]


def board_shapes(fp: cv.Footprint) -> dict:
    """The board side in board coordinates: pads, silk, pad 1, role pads."""
    def place(x, y):
        dx, dy = cv.rot(x, y, fp.rot)
        return fp.x + dx, fp.y + dy
    pads = []
    for p in fp.pads:
        cx, cy = place(p.x, p.y)
        pads.append({"number": p.number, "center": (cx, cy),
                     "poly": _rect(cx, cy, p.w, p.h, p.angle)})
    silk = [[place(*pt[:2]) for pt in line] for line in fp.silk]
    return {"pads": pads, "silk": silk}


_BALL = cv._BALL
bga_pin1 = cv.bga_pin1


def _placed(xy, cpl: dict, side: str) -> tuple[float, float]:
    """A point of the part at the CPL position and rotation, in board
    coordinates. On the bottom the rotation is read from below, so the turned
    part comes back to the board frame with x negated."""
    dx, dy = cv.rot(xy[0], xy[1], cpl["rot"])
    return cpl["x"] + (dx if side == "top" else -dx), cpl["y"] + dy


def model_shapes(model, cpl: dict, side: str = "top") -> dict:
    """The LCSC model placed at the CPL position and rotation (see _placed)."""
    sx = 1.0 if side == "top" else -1.0

    def place(x, y):
        return _placed((x, y), cpl, side)
    pads = []
    for p in model.pads:
        cx, cy = place(p.x, p.y)
        poly = [(cx + sx * x, cy + y)
                for x, y in _rect(0.0, 0.0, p.w, p.h, cpl["rot"] - p.rot)]
        pads.append({"number": p.number, "center": (cx, cy), "poly": poly})
    xs = [x for p in model.pads for x in (p.x - p.w / 2, p.x + p.w / 2)]
    ys = [y for p in model.pads for y in (p.y - p.h / 2, p.y + p.h / 2)]
    body = [place(x, y) for x, y in ((min(xs), min(ys)), (max(xs), min(ys)),
                                     (max(xs), max(ys)), (min(xs), max(ys)))]
    pin1 = next((p["center"] for p in pads if p["number"] == "1"), None)
    if pin1 is None:
        balls = [(m.group(1), int(m.group(2)), p["center"]) for p in pads
                 if (m := _BALL.match(p["number"] or ""))]
        if len(balls) >= 4:
            pin1 = min(balls, key=lambda b: ((b[0], b[1]) != ("A", 1), len(b[0]),
                                             b[0], b[1]))[2]
    return {"pads": pads, "body": body, "pin1": pin1}


def _role_pad(roles: dict[str, str], want=("K", "+")):
    for num, r in roles.items():
        if r in want:
            return num, r
    return None, None


def polar_label(roles: dict[str, str], fp: cv.Footprint | None = None):
    """(pad number, label) for a polar part's K/+ mark. With no pin roles known
    nothing says which pad is the cathode, so pad 1 is marked "?", not "K"."""
    num, r = _role_pad(roles)
    return (num, r) if num else ("1", "?")


def render(pcb: Path, cpl: dict[str, dict], lcsc: dict[str, str],
           cache_dir: Path, out_dir: Path, fetch: bool = True,
           per_image: int = 9) -> dict:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Polygon

    fps = cv.board_footprints(pcb)
    out_dir.mkdir(parents=True, exist_ok=True)
    refs = sorted((r for r in cpl if r in fps), key=cv._natural)
    index = {"images": [], "parts": {}, "fetch_failed": []}
    for start in range(0, len(refs), per_image):
        chunk = refs[start:start + per_image]
        cols = min(3, len(chunk))
        rows = math.ceil(len(chunk) / cols)
        fig, axes = plt.subplots(rows, cols, figsize=(4 * cols, 4 * rows),
                                 squeeze=False)
        for ax in axes.flat:
            ax.set_axis_off()
        for ax, ref in zip(axes.flat, chunk):
            fp, c = fps[ref], cpl[ref]
            errors: dict = {}
            model = easyeda.get(lcsc.get(ref, ""), cache_dir, fetch=fetch,
                                errors=errors)
            polar = cv.is_polar(fp, model)
            index["parts"][ref] = {"lcsc": lcsc.get(ref), "cpl_rot": c["rot"],
                                   "polar": polar,
                                   "has_model": model is not None,
                                   "side": fp.side}
            failed = model is None and bool(errors)
            if failed:
                index["parts"][ref]["polar"] = None
                index["parts"][ref]["fetch_error"] = next(iter(errors.values()))
                index["fetch_failed"].append(ref)
            b = board_shapes(fp)
            ax.set_facecolor("#1d5e2f")
            for p in b["pads"]:
                ax.add_patch(Polygon(p["poly"], closed=True, fc="#c9a227",
                                     ec="none", zorder=1))
            for line in b["silk"]:
                ax.plot([q[0] for q in line], [q[1] for q in line],
                        color="white", lw=1.2, zorder=2)
            for p in b["pads"]:
                if p["number"] == "1":
                    ax.add_patch(Polygon(p["poly"], closed=True, fc="none",
                                         ec="cyan", lw=2.0, zorder=3))
                    ax.annotate("1", p["center"], color="cyan", fontsize=9,
                                ha="center", va="center", zorder=6)
            pb = bga_pin1(fp)
            if pb is not None:
                # BGA: mark pin 1 whatever it is named (A1 is not "1")
                pc = next(q["center"] for q in b["pads"] if q["number"] == pb.number)
                ax.annotate("\u25e4", pc, color="cyan", fontsize=14, ha="center",
                            va="center", zorder=6)
                ax.annotate(f"pin 1 = {pb.number}", pc, color="cyan", fontsize=7,
                            xytext=(0, 9), textcoords="offset points",
                            ha="center", zorder=6)
                index["parts"][ref]["bga_pin1"] = pb.number
            if polar:
                num, r = polar_label(cv.board_roles(fp, model), fp)
                for p in b["pads"]:
                    if p["number"] == num:
                        ax.annotate(r, p["center"], color="cyan", fontsize=9,
                                    ha="center",
                                    va="center" if num != "1" else "bottom",
                                    zorder=6)
            below = fp.side != "top"
            where = "  BOTTOM, from below" if below else ""
            title = f"{ref}  {lcsc.get(ref) or 'no LCSC'}  CPL {c['rot']:g}{where}"
            if failed:
                title = f"{ref}  FETCH FAILED{where}"
            elif model is None:
                title = f"{ref}  NO LCSC MODEL{where}"
                a1 = cv.footprint_basis(fp, lcsc.get(ref, ""), True)
                if a1 and "at" in a1:
                    at = _placed(a1["at"], c, fp.side)
                    ax.plot(*at, "o", color="orange", ms=7, zorder=7)
                    title = (f"{ref}  NO MODEL, A1 from footprint  "
                             f"CPL {c['rot']:g}{where}")
                    index["parts"][ref]["expected_pin1"] = {
                        "basis": "footprint", "pin": a1["pin"], "at": list(at)}
            else:
                m = model_shapes(model, c, fp.side)
                if cv.analyse(fp, model).get("number_corrections"):
                    title += "  judge K/+, not pin 1"
                    index["parts"][ref]["judge_by"] = "role"
                ax.add_patch(Polygon(m["body"], closed=True, fc="#80808040",
                                     ec="#b0b0b0", lw=1.0, zorder=4))
                for p in m["pads"]:
                    ax.add_patch(Polygon(p["poly"], closed=True,
                                         fc="#d0d0d070", ec="#e0e0e0",
                                         lw=0.6, zorder=4))
                if m["pin1"]:
                    ax.plot(*m["pin1"], "o", color="red", ms=7, zorder=7)
                if polar:
                    num, r = polar_label(cv.model_roles(model), fp)
                    for p in m["pads"]:
                        if p["number"] == num:
                            ax.annotate(r, p["center"], color="red",
                                        fontsize=11, ha="left", va="bottom",
                                        zorder=7, fontweight="bold")
            xs = [q[0] for p in b["pads"] for q in p["poly"]] or [fp.x]
            ys = [q[1] for p in b["pads"] for q in p["poly"]] or [fp.y]
            half = max(max(xs) - min(xs), max(ys) - min(ys)) / 2 + 1.0
            cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
            # from below, the board's x runs right to left
            ax.set_xlim(*((cx + half, cx - half) if below
                          else (cx - half, cx + half)))
            ax.set_ylim(cy + half, cy - half)  # board y points down
            ax.set_aspect("equal")
            ax.set_axis_on()
            ax.set_xticks([])
            ax.set_yticks([])
            ax.set_title(title, fontsize=9)
        name = f"cpl_render_{start // per_image + 1}.png"
        fig.suptitle("red dot = LCSC pin 1 as JLC places it;  cyan = board "
                     "pad 1 / K / +;  orange = expected A1, no model;  BOTTOM "
                     "crops are mirrored, seen from below", fontsize=9)
        fig.tight_layout()
        fig.savefig(out_dir / name, dpi=110)
        plt.close(fig)
        index["images"].append({"file": name, "refs": chunk})
    index["rate_limited"] = easyeda.rate_limited()
    if index["fetch_failed"]:
        index["note"] = ("fetch failed for " + ", ".join(index["fetch_failed"])
                         + (f" ({index['rate_limited']})"
                            if index["rate_limited"] else "")
                         + ": not a missing model; rerun in a few minutes")
    (out_dir / "index.json").write_text(json.dumps(index, indent=1),
                                        encoding="utf-8")
    return index


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcb", required=True)
    ap.add_argument("--cpl", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--parts")
    ap.add_argument("--cache-dir")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--per-image", type=int, default=9)
    ap.add_argument("--out", help="write the index JSON here too")
    args = ap.parse_args(argv)
    try:
        pcb = Path(args.pcb)
        pj = Path(args.parts) if args.parts else None
        idx = render(pcb, cv.read_cpl(Path(args.cpl)), cv.lcsc_map(pcb, pj),
                     Path(args.cache_dir) if args.cache_dir
                     else cv.default_cache(pj), Path(args.out_dir),
                     fetch=not (args.offline or cv.offline()),
                     per_image=max(1, args.per_image))
        rep = {"script": "cpl_render",
               "status": "fetch_failed" if idx["fetch_failed"] else "pass",
               "out_dir": args.out_dir, **idx}
    except Exception as exc:  # noqa: BLE001 (SPEC: any error -> exit 2)
        rep = {"script": "cpl_render", "status": "error",
               "error": f"{type(exc).__name__}: {exc}"}
    text = json.dumps(rep, indent=1)
    (Path(args.out).write_text(text, encoding="utf-8") if args.out
     else print(text))
    return {"pass": 0, "fetch_failed": 1}.get(rep["status"], 2)


if __name__ == "__main__":
    raise SystemExit(main())
