"""check_rf_match.py - a matching network's populated values match the load
it feeds across the band (P8 verify suite, hive-hwde #488).

The hit: every chip antenna and RF IC puts a pi-match footprint between the
radio pin and the antenna, and the values on the board are the ones a
reference design used for ITS antenna, ground and case. ERC and DRC see two
caps and an inductor; nothing says the network still matches this antenna,
or that a value was typed one decade off.

What is checked: each constraints `rf_match` entry names the load (an
antenna or connector ref, or its net) and its impedance - `zin` "R+jX@f"
(from the antenna's datasheet or a measurement) or `s1p` (a Touchstone
one-port, relative to the constraints file) - and the band. The check walks
the board from the load toward the source: on each net, two-pad L/C/R parts
to ground are shunt arms and a single two-pad part to another net is the
series arm, until the net reaches a part that is not a two-pad passive (the
radio). DNP parts are open; a 0-ohm link joins its nets. Then it computes
the return loss the source (z0, 50 ohm) sees across the band with the
populated values and generic 0402 parasitics (lib/rfmatch.py).

Findings:
  rf_match_rl             warning: RL under rl_db (10 dB) somewhere in the
                          band; the message names the worst frequency and
                          the `hwde rf_synth match` that designs a fix
  rf_match_no_network     warning: the load names no part / net on the
                          board, or no ladder of passives starts at it
  rf_match_value_unknown  warning: a populated L/C in the network has no
                          readable value

constraints:
  "rf_match": [{"load": "AE1", "zin": "22-j31@2.45G" | "s1p": "ae1.s1p",
                "band_hz": [2.4e9, 2.4835e9], "net": "/ANT" (optional),
                "z0": 50, "rl_db": 10, "model": "generic0402" | "ideal"}]
  (band_hz defaults to the zin frequency alone.)

CLI: --pcb board.kicad_pcb [--constraints c.json] [--out report.json]
     exit 0 pass / 1 violations / 2 error, per SPEC section 6.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import sexpdata

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "lib"))
import checklib  # noqa: E402
import rfmatch  # noqa: E402
from geom import _is_node, _kid, _kids, _tok  # noqa: E402

SCRIPT = "check_rf_match"
SOURCE = SCRIPT
POINTS = 21
MAX_ARMS = 8   # a matching ladder longer than this is not one
GND = re.compile(r"^/?(?:.*/)?(?:[ADP]?GND\w*|GNDA|GNDD|VSS\w*|0V)$", re.I)
KIND_REF = re.compile(r"^(L|C|R|FB)\d", re.I)
DNP_VALUE = re.compile(r"^(DNP|DNF|NC|N/?C|NF|NP|OPEN|-)$", re.I)
UNIT = {"L": "H", "C": "F", "R": "ohm"}
KEYS = {"load", "net", "zin", "s1p", "band_hz", "z0", "rl_db", "model",
        "note", "reason"}


def read_parts(root) -> list[dict]:
    """Every footprint: ref, value, position, dnp, pads {number: net}."""
    out = []
    for fp in _kids(root, "footprint"):
        props = {}
        for p in _kids(fp, "property"):
            if len(p) > 2:
                props[str(_tok(p[1]))] = str(_tok(p[2]))
        if not props and _kids(fp, "fp_text"):
            for t in _kids(fp, "fp_text"):
                kind = str(_tok(t[1]))
                if kind in ("reference", "value"):
                    props[kind.capitalize()] = str(_tok(t[2]))
        at = _kid(fp, "at")
        pos = (float(at[1]), float(at[2])) if at is not None else None
        attr = _kid(fp, "attr")
        flags = {str(_tok(a)) for a in (attr[1:] if attr is not None
                                        else []) if not _is_node(a)}
        dnp = "dnp" in flags or any(
            _is_node(n) and str(_tok(n[0])) == "dnp" and
            (len(n) < 2 or str(_tok(n[1])) != "no") for n in fp)
        pads = {}
        for pad in _kids(fp, "pad"):
            num = str(_tok(pad[1]))
            net = _kid(pad, "net")
            if net is None or not num:
                continue
            name = str(_tok(net[-1]))
            pads.setdefault(num, name)
        out.append({"ref": props.get("Reference", "?"),
                    "value": props.get("Value", ""), "pos": pos,
                    "dnp": dnp or bool(DNP_VALUE.match(
                        props.get("Value", "").strip())),
                    "pads": pads})
    return out


def passive(p: dict) -> str | None:
    """'L', 'C' or 'R' for a two-net, two-pad passive; else None."""
    m = KIND_REF.match(p["ref"])
    if not m or len(p["pads"]) != 2 or len(set(p["pads"].values())) != 2:
        return None
    k = m.group(1).upper()
    return "L" if k == "FB" else k


def parse_value(kind: str, text: str) -> float | None:
    s = text.strip().split()[0] if text.strip() else ""
    s = re.sub(r"(?i)(ohms?|Ω)$", "", s)
    if kind == "R" and re.fullmatch(r"0R?|0R0+", s, re.I):
        return 0.0
    if kind == "R":
        s = re.sub(r"(?i)^(\d+)R(\d*)$", lambda m: m.group(1) + "." +
                   (m.group(2) or "0"), s)
    try:
        v = rfmatch.parse_si(s, UNIT[kind])
    except ValueError:
        return None
    return v if v >= 0 else None


def walk(parts: list[dict], start: str, load_ref: str | None
         ) -> tuple[list[dict], dict]:
    """The ladder from net `start` toward the source -> (elements listed
    from the load, facts). Elements carry ref, where, kind, value (None if
    unreadable)."""
    by_net: dict[str, list[dict]] = {}
    for p in parts:
        for n in set(p["pads"].values()):
            by_net.setdefault(n, []).append(p)
    seen = {load_ref} if load_ref else set()
    els, nets, stop = [], [start], "source"
    cur = {start}
    for _ in range(MAX_ARMS):
        here = [p for n in cur for p in by_net.get(n, [])
                if p["ref"] not in seen]
        series, others = [], []
        for p in here:
            seen.add(p["ref"])
            k = passive(p)
            if k is None:
                others.append(p)
                continue
            if p["dnp"]:
                continue
            other = [n for n in p["pads"].values() if n not in cur][0]
            v = parse_value(k, p["value"])
            if k == "R" and v == 0.0:     # a 0-ohm link: same node
                series.append((p, k, v, other, True))
                continue
            if GND.match(other):
                els.append({"ref": p["ref"], "where": "shunt", "kind": k,
                            "value": v, "text": p["value"]})
            else:
                series.append((p, k, v, other, False))
        links = [s for s in series if s[4]]
        if links and len(series) == len(links):
            cur |= {s[3] for s in links}
            nets.append(links[0][3])
            continue
        if others:
            stop = "source"
            src = sorted(p["ref"] for p in others)
            break
        if len(series) != 1:
            stop = "open" if not series else "branch"
            src = sorted(s[0]["ref"] for s in series)
            break
        p, k, v, other, _ = series[0]
        els.append({"ref": p["ref"], "where": "series", "kind": k,
                    "value": v, "text": p["value"]})
        cur = {other}
        nets.append(other)
    else:
        stop, src = "long", []
    return els, {"nets": nets, "stop": stop, "at": src}


def _cfg_load(entry: dict, base: Path | None) -> tuple[rfmatch.Load, list]:
    if entry.get("zin") is not None:
        z, f = rfmatch.parse_z(entry["zin"])
        if z.real <= 0:
            raise checklib.CheckError(f"rf_match zin {entry['zin']!r}: "
                                      "resistance must be positive")
        band = entry.get("band_hz") or ([f] if f else None)
        if not band:
            raise checklib.CheckError("rf_match: a zin without @f needs "
                                      "band_hz")
        return rfmatch.Load(z0=z, f0=f or (band[0] + band[-1]) / 2,
                            source=str(entry["zin"])), band
    if entry.get("s1p"):
        p = Path(entry["s1p"])
        if not p.is_absolute() and base is not None:
            p = base / p
        try:
            load = rfmatch.read_s1p(p)
        except (OSError, ValueError) as exc:
            raise checklib.CheckError(f"rf_match s1p: {exc}")
        band = entry.get("band_hz") or [load.freqs[0], load.freqs[-1]]
        return load, band
    raise checklib.CheckError("rf_match entry needs zin or s1p")


def _entries(constraints: dict | None) -> list[dict]:
    raw = (constraints or {}).get("rf_match")
    if raw is None:
        return []
    if isinstance(raw, dict):
        raw = [dict(v, load=k) for k, v in raw.items()
               if not str(k).startswith("_")]
    if not isinstance(raw, list) or not all(isinstance(e, dict)
                                            for e in raw):
        raise checklib.CheckError("constraints rf_match must be a list of "
                                  "objects")
    for e in raw:
        bad = checklib.data_keys(e) - KEYS
        if bad:
            raise checklib.CheckError(f"rf_match: unknown keys "
                                      f"{sorted(bad)}")
        if not (e.get("load") or e.get("net")):
            raise checklib.CheckError("rf_match entry needs load or net")
    return raw


def check(pcb: Path, constraints: dict | None = None,
          base: Path | None = None) -> tuple[list[dict], dict]:
    entries = _entries(constraints)
    if not entries:
        return [], {"networks": [], "note": "no rf_match in constraints"}
    root = sexpdata.loads(pcb.read_text(encoding="utf-8"))
    parts = read_parts(root)
    by_ref = {p["ref"]: p for p in parts}
    vios, facts = [], []
    for e in entries:
        load, band = _cfg_load(e, base)
        z0 = float(e.get("z0", rfmatch.Z0_DEFAULT))
        target = float(e.get("rl_db", 10.0))
        model = rfmatch.PartModel(kind=e.get("model", "generic0402"))
        ref = e.get("load")
        lp = by_ref.get(ref) if ref else None
        start = e.get("net")
        if start is None and lp is not None:
            sig = sorted({n for n in lp["pads"].values()
                          if not GND.match(n)
                          and not n.startswith("unconnected-")})
            start = sig[0] if len(sig) == 1 else None
        pos = lp["pos"] if lp else None
        label = ref or start
        if start is None:
            why = (f"no part {ref} on the board" if ref and lp is None else
                   f"{ref} has {len(lp['pads'])} pads on several signal "
                   "nets; name the feed with rf_match net")
            vios.append(checklib.violation(
                SCRIPT, "warning", pos, None, None, [ref] if lp else [],
                f"rf_match {label}: {why}", SOURCE,
                kind="rf_match_no_network"))
            continue
        els, walked = walk(parts, start, ref if lp else None)
        refs = [x["ref"] for x in els]
        fact = {"load": label, "net": start, "zin": load.source,
                "band_hz": band, "z0": z0, "rl_db": target, **walked,
                "elements": [{k: x[k] for k in ("ref", "where", "kind",
                                                "text")} for x in els]}
        facts.append(fact)
        if not els:
            vios.append(checklib.violation(
                SCRIPT, "warning", pos, None, start, [ref] if lp else [],
                f"rf_match {label}: no matching network of two-pad "
                f"passives starts at {start} (stopped: {walked['stop']} "
                f"{', '.join(walked['at'])})", SOURCE,
                kind="rf_match_no_network"))
            continue
        unknown = [x for x in els if x["value"] is None]
        if unknown:
            for x in unknown:
                p = by_ref[x["ref"]]
                vios.append(checklib.violation(
                    SCRIPT, "warning", p["pos"], None, start, [x["ref"]],
                    f"{x['ref']} in the {label} matching network has no "
                    f"readable value ({x['text']!r}); the match can't be "
                    "judged", SOURCE, kind="rf_match_value_unknown"))
            continue
        freqs = rfmatch.band(band[0] if len(band) == 1 else
                             (band[0] + band[-1]) / 2,
                             0.0 if len(band) == 1 else band[-1] - band[0],
                             POINTS)
        r = rfmatch.rl_band(els, load, freqs, z0, model)
        fact.update(rl_min_db=r["min_db"], worst_hz=round(r["worst_hz"]),
                    rl_mean_db=r["mean_db"], model=model.kind)
        if r["min_db"] >= target:
            continue
        vals = ", ".join(f"{x['ref']} {x['text']} ({x['where']})"
                         for x in els)
        f0 = (band[0] + band[-1]) / 2
        bw = band[-1] - band[0]
        zf = load.z(f0)
        hint = (f"hwde rf_synth match --z {zf.real:.4g}"
                f"{'+' if zf.imag >= 0 else '-'}j{abs(zf.imag):.4g} "
                f"--f0 {f0 / 1e6:.6g}M" +
                (f" --bw {bw / 1e6:.6g}M" if bw else "") +
                f" --z0 {z0:g}")
        vios.append(checklib.violation(
            SCRIPT, "warning", pos, None, start,
            refs + ([ref] if lp else []),
            f"rf_match {label}: the populated network ({vals}) gives "
            f"{r['min_db']:.1f} dB return loss at "
            f"{r['worst_hz'] / 1e6:.6g} MHz into {load.source}, under "
            f"the {target:g} dB asked across "
            f"{band[0] / 1e6:.6g}-{band[-1] / 1e6:.6g} MHz; redesign with "
            f"`{hint}`", SOURCE, kind="rf_match_rl",
            rl_min_db=r["min_db"], worst_hz=round(r["worst_hz"])))
    return vios, {"networks": facts}


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcb", required=True)
    ap.add_argument("--constraints")
    ap.add_argument("--out", help="write the JSON report here")
    args = ap.parse_args(argv)
    pcb = Path(args.pcb)
    if not pcb.is_file():
        raise checklib.CheckError(f"board not found: {pcb}")
    cons, base = None, None
    if args.constraints and Path(args.constraints).is_file():
        cons = checklib.load_json(args.constraints, "constraints")
        base = Path(args.constraints).resolve().parent
    vios, seen = check(pcb, cons, base)
    return checklib.report(SCRIPT, pcb, vios, checked=[seen],
                           nothing_to_judge=None if seen.get("networks") else
                           "no rf_match in constraints"), args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    sys.exit(main())
