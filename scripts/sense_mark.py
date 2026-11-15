"""sense_mark - mark a board's sense lines for the sense-line width rule
(#1077).

A sense line carries microamps: a feedback divider's top, an amplifier's or
ADC's input. rules_gen's aiee_sense_width_<net> rule lets a track of a power
net run at the fab's minimum width only inside an aiee_sensew_* rule area.
route_critical draws that mark round each sense line it lays. A line routed
by hand has none, so DRC holds it to the rail's width. This draws it:

  declared lines  each power entry's sense_pads {sense pad: tap pad} (#936),
                  walked by check_kelvin's line walk. Tagged REF_PAD of the
                  sense pad.
  found lines     each shunt's two lines into an amplifier's or ADC's input
                  pair, found from the netlist (check_kelvin.found_lines).
                  Tagged REF_PAD of the reader's tap pad.

Only a clean line is marked: one that reaches its pad on copper of its own.
A line that joins rail copper first is not a sense line yet. For a declared
line that is an error (check_kelvin's sense_tap_off_pad says where); for a
found line it is a warning, since the netlist only suggests it. The mark is
each line track grown by route_critical.SENSE_MARK_MM, with nothing
disallowed. KiCad's enclosedByArea takes no wider (rail) track.

Idempotent: every aiee_sensew_* area is redrawn, so run it again after
any sense line is moved. route_critical keeps marks it did not lay.

Contract (SPEC section 6):
  sense_mark.py --pcb B.kicad_pcb [--constraints c.json] [--dry-run]
      [--out-report r.json]
  Payload: {areas: [{name, layers, tag, net}], lines: [...], removed}.
  exit 0; 1 = a declared line could not be marked (the rest are);
  2 = error (board untouched).
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

from shapely.ops import unary_union

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import check_kelvin  # noqa: E402
import checklib  # noqa: E402
import env  # noqa: E402
import geom  # noqa: E402
import routelib  # noqa: E402
from checklib import CheckError  # noqa: E402

SCRIPT = "sense_mark"
SENSE_MARK = "aiee_sensew"   # rules_gen.SENSE_MARK, route_critical.SENSE_MARK
SENSE_MARK_MM = 0.02         # route_critical.SENSE_MARK_MM


def tag_of(pad) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", f"{pad.ref}_{pad.number}")


def mark_areas(items, tag: str) -> list[dict]:
    """set_rule_areas entries marking a line's tracks: per layer, the union
    of each track grown by SENSE_MARK_MM, nothing disallowed. Vias and pads
    in `items` are skipped (the width rule holds tracks only)."""
    marks: dict[str, list] = {}
    for t in items:
        if isinstance(t, geom.Track):
            marks.setdefault(t.layer, []).append(t.poly.buffer(SENSE_MARK_MM))
    areas = []
    for ly in sorted(marks):
        shape = unary_union(marks[ly])
        for k, poly in enumerate(q for q in getattr(shape, "geoms", [shape])
                                 if q.geom_type == "Polygon"
                                 and not q.is_empty):
            pts = [[round(x, 4), round(y, 4)] for x, y in list(
                poly.simplify(SENSE_MARK_MM / 10).exterior.coords)[:-1]]
            areas.append({"name": f"{SENSE_MARK}_{tag}_{ly.replace('.', '_')}"
                                  f"_{k}", "layers": [ly], "poly": pts,
                          "tracks": False, "vias": False, "pours": False,
                          "tag": tag})
    return areas


def plan(bg, cons: dict) -> tuple[list[dict], list[dict], list[dict]]:
    """(areas, lines, violations) for every declared and found sense line."""
    areas, lines, vios = [], [], []
    tags: set[str] = set()

    def add(net, kind, frm, to, w, refs):
        line = {"net": net, "kind": kind, "from": frm, "to": to,
                "reached": w["reached"], "joins": len(w["joins"])}
        lines.append(line)
        if not w["reached"] or w["joins"]:
            why = ("joins rail copper before it reaches " + to if w["joins"]
                   else "never reaches " + to + " on copper of its own")
            vios.append(checklib.violation(
                SCRIPT, "error" if kind == "declared" else "warning", None,
                None, net, refs,
                f"{kind} sense line {frm} -> {to} on {net} {why}: left "
                "unmarked, so DRC holds it to the rail's width. Route it on "
                "a track of its own first (check_kelvin says where it joins)",
                SCRIPT, kind="sense_mark_refused"))
            return
        tag = tag_of(w["pad"])
        if tag in tags:
            return
        tags.add(tag)
        new = mark_areas(w["line"], tag)
        line["areas"] = len(new)
        areas.extend(new)

    for e in cons.get("power") or []:
        net = e.get("net")
        for s_name, t_name in sorted((e.get("sense_pads") or {}).items()):
            sense = check_kelvin._pad_named(bg, s_name)
            tap = check_kelvin._pad_named(bg, t_name)
            if sense is None or tap is None or sense is tap \
                    or tap.net != net:
                continue        # check_kelvin's sense_tap_bad names it
            w = check_kelvin._line_walk(bg, sense, tap)
            add(net, "declared", s_name, t_name, {**w, "pad": sense},
                sorted({sense.ref, tap.ref}))
    for ref, ic, nets, found in check_kelvin.found_lines(bg, cons):
        for net, w in found.items():
            p = w["tap"]["pad"]
            add(net, "found", f"{p.ref}.{p.number}",
                f"{ref}.{w['end'].number}", {**w, "pad": p}, [ref, ic])
    return areas, lines, vios


def run(argv: list[str] | None = None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcb", required=True)
    ap.add_argument("--constraints", default=None,
                    help="constraints.json (default: next to the board)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out-report", default=None)
    args = ap.parse_args(argv)

    pcb = Path(args.pcb).resolve()
    if not pcb.is_file():
        raise CheckError(f"board not found: {pcb}")
    cons = Path(args.constraints) if args.constraints else \
        pcb.parent / "constraints.json"
    constraints = checklib.load_json(cons, "constraints") \
        if cons.is_file() else {}
    bg = geom.BoardGeom.from_file(pcb)
    areas, lines, vios = plan(bg, constraints)
    old = [ra.get("name") for ra in bg.rule_areas
           if str(ra.get("name") or "").startswith(SENSE_MARK)]
    facts = {"areas": [{"name": a["name"], "layers": a["layers"]}
                       for a in areas],
             "lines": lines, "removed": len(old), "dry_run": args.dry_run}
    if not args.dry_run and (areas or old):
        cli = env.find_kicad_cli()
        bp = env.find_kicad_python(cli) if cli else None
        if bp is None:
            raise CheckError("KiCad bundled python not found (env.py)")
        stage = Path(tempfile.mkdtemp(prefix=".aiee_sensew_", dir=pcb.parent))
        try:
            staged = stage / pcb.name
            shutil.copy2(pcb, staged)
            res = routelib.run_worker(bp, {
                "verb": "set_rule_areas", "board": str(staged),
                "out": str(staged), "prefix": SENSE_MARK, "areas": areas},
                stage)
            got = [ra.get("name") for ra in
                   geom.BoardGeom.from_file(staged).rule_areas
                   if str(ra.get("name") or "").startswith(SENSE_MARK)]
            if sorted(got) != sorted(a["name"] for a in areas):
                raise CheckError(f"rule areas after the edit {sorted(got)} "
                                 "are not the planned ones (board untouched)")
            os.replace(staged, pcb)
            facts["removed"] = res["removed"]
        finally:
            shutil.rmtree(stage, ignore_errors=True)
    bad = any(v["severity"] == "error" for v in vios)
    payload = {"script": SCRIPT, "status": "fail" if bad else "pass",
               "board": str(pcb), "violations": vios, "facts": facts}
    return payload, args.out_report


def main(argv: list[str] | None = None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    sys.exit(main())
