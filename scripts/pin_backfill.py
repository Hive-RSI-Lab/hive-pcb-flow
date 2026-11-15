"""pin_backfill.py - give an existing board's pads the schematic's pin names
(#1220).

board_init used to drop each netlist node's `(pinfunction ...)` and
`(pintype ...)`, so the pads of every board it wrote carry no pin names, and
every check that finds a pin by name (check_rail_budget, check_current,
check_straps, check_kelvin, check_esd, check_surge, check_cap_load,
check_pinouts, check_ratings, ...) can only judge such a board from
constraints. board_init and board_update now write them; this backfills
boards made before that.

It touches only the pads' `(pinfunction "...")` and `(pintype "...")`, and
only where the pad has none: an inserted line per field, by text, so the rest
of the file stays byte for byte (no pcbnew load/save, no format upgrade).

A pad is named only when its board net agrees with the netlist's net for the
same REF.PAD (equal names; or both a no-connect: an `unconnected-(...)` net on
the netlist side and no net or the same on the board), so a schematic that
moved on since the board was laid out names only the pins it still agrees
on. Below --min-agree of a board's netted, netlisted pads agreeing, the
source is taken to be the wrong schematic and nothing is written.

The source: --netlist or --schematic if given, else the first that agrees
best of: the board's same-stem .kicad_sch / .net; the root schematic of a
.kicad_pro beside it; then up to four parent folders (stopping at a folder
holding .git or ledger.json) for <stem>.kicad_sch or kicad/<stem>.kicad_sch,
or the root schematic of a .kicad_pro there or in its kicad/ folder.

  pin_backfill.py --pcb B.kicad_pcb                 # report what it would do
  pin_backfill.py --pcb B.kicad_pcb --write         # write B in place
  pin_backfill.py --pcb B.kicad_pcb --out C.kicad_pcb   # write a copy

Exit 0 when the report is written (whatever it found), 2 on error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "lib"))

import board_init  # noqa: E402  (parse_netlist)
import kc  # noqa: E402
from lib import env  # noqa: E402

SCRIPT = "pin_backfill"
MIN_AGREE = 0.5
UP_LEVELS = 4

TOK = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+')


def unq(tok: str) -> str:
    if len(tok) >= 2 and tok[0] == '"' and tok[-1] == '"':
        return re.sub(r'\\(.)', r'\1', tok[1:-1])
    return tok


def q(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


class _Frame:
    __slots__ = ("head", "start", "end", "atoms", "kids")

    def __init__(self, start):
        self.head, self.start, self.end = None, start, None
        self.atoms: list[str] = []
        self.kids: list[_Frame] | None = None


def footprints(text: str) -> list[dict]:
    """[{ref, pads: [{num, net, has_fn, has_type, fn, type, start, end,
    anchor}]}] for each top-level footprint, by a string-aware scan. anchor is
    where new pad fields go: after the pad's (net ...), else before its
    (uuid ...)/(tstamp ...), else before its closing paren."""
    keep = {"footprint", "module", "pad"}
    stack: list[_Frame] = []
    out = []
    for m in TOK.finditer(text):
        tok = m.group()
        if tok == "(":
            stack.append(_Frame(m.start()))
            continue
        if tok == ")":
            f = stack.pop()
            f.end = m.end()
            if not stack:
                continue
            parent = stack[-1]
            if parent.head in keep and parent.kids is not None:
                parent.kids.append(f)
            if f.head in ("footprint", "module") and len(stack) == 1:
                out.append(_footprint(f))
            continue
        f = stack[-1]
        if f.head is None:
            f.head = tok
            if tok in keep and (tok != "pad" or (len(stack) >= 2 and
                                stack[-2].head in ("footprint", "module"))):
                f.kids = []
        elif len(f.atoms) < 3:
            f.atoms.append(tok)
    return out


def _footprint(f: _Frame) -> dict:
    ref = None
    pads = []
    for k in f.kids or []:
        if k.head == "property" and k.atoms[:1] == ['"Reference"']:
            ref = unq(k.atoms[1]) if len(k.atoms) > 1 else ref
        elif k.head == "fp_text" and k.atoms[:1] == ["reference"] and ref is None:
            ref = unq(k.atoms[1]) if len(k.atoms) > 1 else ref
        elif k.head == "pad" and k.atoms:
            net, anchor, fn, ty = None, None, None, None
            uuid_at = None
            for c in k.kids or []:
                if c.head == "net" and c.atoms:
                    a = c.atoms
                    net = unq(a[1]) if len(a) > 1 and a[0].isdigit() else (
                        "" if a[0].isdigit() else unq(a[0]))
                    anchor = ("after", c.start, c.end)
                elif c.head == "pinfunction" and c.atoms:
                    fn = unq(c.atoms[0])
                elif c.head == "pintype" and c.atoms:
                    ty = unq(c.atoms[0])
                elif c.head in ("uuid", "tstamp") and uuid_at is None:
                    uuid_at = ("before", c.start, c.end)
            pads.append({"num": unq(k.atoms[0]), "net": net or "",
                         "fn": fn, "type": ty, "start": k.start,
                         "end": k.end,
                         "anchor": anchor or uuid_at
                         or ("close", k.end - 1, k.end)})
    return {"ref": ref, "pads": pads}


def _agrees(board_net: str, net_net: str) -> bool:
    if board_net == net_net:
        return True
    return net_net.startswith("unconnected-") and board_net == ""


def plan(text: str, comps: list[dict], netmap: dict) -> dict:
    """What to insert, and the counts the report gives."""
    pins = {c["ref"]: c.get("pins") or {} for c in comps}
    fps = footprints(text)
    n = {"footprints": len(fps), "pads": 0, "pads_netlisted": 0,
         "pads_agree": 0, "pads_net_differs": 0, "pads_already_named": 0,
         "pins_named": 0, "types_named": 0, "refs_not_in_netlist": 0}
    edits: list[tuple[int, str]] = []
    differs: list[str] = []
    for fp in fps:
        if fp["ref"] not in pins:
            n["refs_not_in_netlist"] += 1
            n["pads"] += len(fp["pads"])
            continue
        for pad in fp["pads"]:
            n["pads"] += 1
            key = f"{fp['ref']}.{pad['num']}"
            if key not in netmap or pad["num"] not in pins[fp["ref"]]:
                continue
            n["pads_netlisted"] += 1
            if not _agrees(pad["net"], netmap[key]):
                n["pads_net_differs"] += 1
                if len(differs) < 20:
                    differs.append(f"{key}: board {pad['net']!r}, "
                                   f"schematic {netmap[key]!r}")
                continue
            n["pads_agree"] += 1
            fn, ty = pins[fp["ref"]][pad["num"]]
            add = []
            if fn and pad["fn"] is None:
                add.append(f"(pinfunction {q(fn)})")
                n["pins_named"] += 1
            if ty and pad["type"] is None:
                add.append(f"(pintype {q(ty)})")
                n["types_named"] += 1
            if not add:
                n["pads_already_named"] += 1
                continue
            edits.append((pad, add))
    return {"counts": n, "edits": edits, "net_differs": differs}


def apply(text: str, edits) -> str:
    """Insert each pad's new fields at its anchor, in the pad's own layout:
    a line each, at the anchor's indent, when the anchor starts its own line;
    else a space-separated run on the anchor's line."""
    ins: list[tuple[int, str]] = []
    for pad, add in edits:
        how, s, e = pad["anchor"]
        ls = text.rfind("\n", 0, s) + 1
        lead = text[ls:s]
        own_line = ls > pad["start"] and not lead.strip()
        if how == "close" and own_line:
            pls = text.rfind("\n", 0, pad["start"]) + 1
            ind = re.match(r"[ \t]*", text[pls:pad["start"]]).group()
            step = "\t" if "\t" in lead or not lead else " " * 2
            ins.append((ls, "".join(ind + step + a + "\n" for a in add)))
        elif how == "after" and own_line:
            ins.append((e, "".join("\n" + lead + a for a in add)))
        elif how == "before" and own_line:
            ins.append((s, "".join(a + "\n" + lead for a in add)))
        elif how == "after":
            ins.append((e, " " + " ".join(add)))
        elif how == "close":
            ins.append((s, " " + " ".join(add)))
        else:
            ins.append((s, " ".join(add) + " "))
    parts, last = [], 0
    for pos, s in sorted(ins, key=lambda x: x[0]):
        parts.append(text[last:pos])
        parts.append(s)
        last = pos
    parts.append(text[last:])
    return "".join(parts)


# ------------------------------------------------------------- the source

def _root_of_pro(d: Path) -> list[Path]:
    return [p.with_suffix(".kicad_sch") for p in sorted(d.glob("*.kicad_pro"))
            if p.with_suffix(".kicad_sch").is_file()]


def candidates(pcb: Path) -> list[Path]:
    """Schematics/netlists that may be this board's source, nearest first."""
    stem, d = pcb.stem, pcb.parent
    out = [d / f"{stem}.kicad_sch", d / f"{stem}.net", *_root_of_pro(d)]
    up = d
    for _ in range(UP_LEVELS):
        if (up / ".git").exists() or (up / "ledger.json").exists():
            break
        up = up.parent
        out += [up / f"{stem}.kicad_sch", up / "kicad" / f"{stem}.kicad_sch",
                *_root_of_pro(up), *_root_of_pro(up / "kicad")]
    seen, keep = set(), []
    for p in out:
        r = p.resolve()
        if p.is_file() and r not in seen:
            seen.add(r)
            keep.append(p)
    return keep


def load_source(src: Path, cache: Path | None) -> tuple[list, dict]:
    if src.suffix == ".net":
        return board_init.parse_netlist(src)
    tmpd = None
    if cache:
        h = hashlib.sha1(str(src.resolve()).encode()
                         + src.read_bytes()).hexdigest()[:16]
        net = cache / f"{h}.net"
    else:
        tmpd = tempfile.TemporaryDirectory(prefix="pinbf_")
        net = Path(tmpd.name) / "src.net"
    try:
        if not net.is_file():
            cli = env.find_kicad_cli()
            if cli is None:
                raise RuntimeError("kicad-cli not found (see check_env.py)")
            r = kc.export_netlist(cli, src, net)
            if r["status"] != "pass":
                net.unlink(missing_ok=True)
                raise RuntimeError(f"netlist export failed for {src}: "
                                   f"{r.get('error') or r}")
        return board_init.parse_netlist(net)
    finally:
        if tmpd:
            tmpd.cleanup()


def run(argv=None) -> tuple[dict, int, str | None]:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--pcb", required=True)
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--schematic", help="the board's root .kicad_sch")
    src.add_argument("--netlist", help="a kicadsexpr netlist of the board")
    wr = ap.add_mutually_exclusive_group()
    wr.add_argument("--write", action="store_true",
                    help="write the board in place")
    wr.add_argument("--out", help="write the backfilled board here")
    ap.add_argument("--min-agree", type=float, default=MIN_AGREE,
                    help="least share of netlisted pads whose nets agree "
                    "before anything is written (default %(default)s)")
    ap.add_argument("--cache-dir", help="keep exported netlists here")
    ap.add_argument("--out-report", help="write the JSON report here")
    args = ap.parse_args(argv)
    pcb = Path(args.pcb)
    text = pcb.read_text(encoding="utf-8")
    cache = Path(args.cache_dir) if args.cache_dir else None
    if cache:
        cache.mkdir(parents=True, exist_ok=True)
    if args.schematic or args.netlist:
        cands = [Path(args.schematic or args.netlist)]
    else:
        cands = candidates(pcb)
    rep: dict = {"script": SCRIPT, "board": str(pcb), "source": None,
                 "tried": [], "written": None}
    best = None
    for c in cands:
        try:
            comps, netmap = load_source(c, cache)
        except Exception as exc:  # noqa: BLE001 - one bad candidate
            rep["tried"].append({"source": str(c),
                                 "error": f"{type(exc).__name__}: {exc}"[:300]})
            continue
        p = plan(text, comps, netmap)
        k = p["counts"]
        agree = (k["pads_agree"] / k["pads_netlisted"]
                 if k["pads_netlisted"] else 0.0)
        rep["tried"].append({"source": str(c), "agree": round(agree, 3),
                             "pads_netlisted": k["pads_netlisted"]})
        if best is None or (agree, k["pads_agree"]) > best[0]:
            best = ((agree, k["pads_agree"]), c, p)
        if agree >= 0.98 and not (args.schematic or args.netlist):
            break                     # the nearest that fits is the one
    if best is None:
        rep["status"] = "no_source"
        rep["why"] = ("no schematic or netlist found for this board"
                      if not cands else "no candidate source could be read")
        return rep, 0, args.out_report
    (agree, _), c, p = best
    rep.update(source=str(c), agree=round(agree, 3), counts=p["counts"],
               net_differs=p["net_differs"])
    if agree < args.min_agree:
        rep["status"] = "mismatch"
        rep["why"] = (f"only {agree:.0%} of the pads the source names agree "
                      f"with the board's nets (< {args.min_agree:.0%}): "
                      f"taken as not this board's schematic, nothing written")
        return rep, 0, args.out_report
    if not p["edits"]:
        rep["status"] = "nothing_to_do"
        return rep, 0, args.out_report
    rep["status"] = "planned"
    target = pcb if args.write else (Path(args.out) if args.out else None)
    if target is not None:
        new = apply(text, p["edits"])
        tmp = target.with_name(f".{target.name}.pinbf.tmp")
        tmp.write_text(new, encoding="utf-8")
        if target.exists():
            os.chmod(tmp, target.stat().st_mode & 0o7777)
        os.replace(tmp, target)
        rep["status"] = "written"
        rep["written"] = str(target)
    return rep, 0, args.out_report


def main(argv=None) -> int:
    out = None
    try:
        rep, rc, out = run(argv)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - the error JSON contract
        rep, rc = {"script": SCRIPT, "status": "error",
                   "error": f"{type(exc).__name__}: {exc}"}, 2
    text = json.dumps(rep, indent=1)
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(text, encoding="utf-8")
    else:
        print(text)
    return rc


if __name__ == "__main__":
    sys.exit(main())
