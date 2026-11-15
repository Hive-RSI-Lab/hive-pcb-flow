#!/usr/bin/env python
"""mlcc_dcbias.py - what a board's ceramic capacitors are worth at their DC.

A 22 uF 0805 on a 5 V rail is about 11 uF; a buck's loop, an LDO's
stability floor and a hold-up time are all set by the derated value. This
lists every capacitor with its DC across it, its effective capacitance
(lib/mlcc.py: the part's own curve from reference/mlcc_dcbias.yaml by MPN,
else the generic class-II curve, typical and low), and each rail's total
nominal against effective.

  one part:  mlcc_dcbias.py --c 22u --package 0805 --rated 25 --vdc 5.2
             [--dielectric X5R] [--mpn GRM21BR61E226] [--temp 85]
  a board:   mlcc_dcbias.py --workspace boards/<b> [--temp 60]
             mlcc_dcbias.py --net x.net [--constraints c.json] [--parts p/]
             [--rail NET=V] [--min-c NET=22u]
  verify:    mlcc_dcbias.py --pcb b.kicad_pcb [--netlist b.net] (checks.d)

The DC across a board capacitor comes from the rails sim_board resolves
(constraints.json voltages, power tree, regulator outputs, net names): a cap
between two known rails (ground included) sits at their difference; one on
any other net is listed as unknown.

A rail's floor, the effective capacitance it must keep (#1067), comes from
what the design states, first match wins:
  1. `--min-c NET=F` on the command line (an override);
  2. constraints.json `power[].min_cout` (farads, effective) on that net;
  3. the regulator's own row in reference/pin_requirements.yaml: a
     verified requirement with `effective: true` (its cited quote says
     effective, as TPS7A20's "Effective output capacitance of 0.47 uF
     minimum") between the regulator's output pin and ground; the net that
     pin is on gets the row's `min`. Several regulators on one net: the
     largest.
A rail a regulator feeds (an LDO's OUT/VOUT/VO pin, or a switcher's
inductor off its SW/LX/PH pin) with capacitors on it and no floor from any
of these goes under `not_judged`: it is not a pass.

Findings:
  rail_c_below_min  error when the rail's typical effective capacitance is
                    under its floor, warning when only the low curve is
  cap_dc_bias_low   per part. On a rail under its floor: warning for each
                    part keeping (low curve) less than floor / the rail's
                    nominal total, the fraction every part must keep for
                    the rail to hold it, so the floor's own citation sets
                    the line and names the parts to change.
                    Anywhere else: info under the generic 50 % screen
                    (lib/mlcc.py's curve is a +-25 % screen, not a
                    datasheet limit, so it never warns on its own).
  cap_overvoltage   error: DC over the rating.
Exit 0 pass / 1 violations / 2 error.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import checklib  # noqa: E402
import engcalc  # noqa: E402
import mlcc  # noqa: E402
import sim_board  # noqa: E402

SCRIPT = "mlcc_dcbias"
# the info screen only: a part under half its nominal is worth a look, not
# a fault (no datasheet sets it; the warning line is the rail's margin)
LOW_FRACTION = 0.5
PIN_REQUIREMENTS = SCRIPTS.parent / "reference" / "pin_requirements.yaml"
OUT_FNS = {"OUT", "VOUT", "VO"}
SW_FNS = {"SW", "LX", "PH", "SW1", "SW2", "LX1", "LX2"}
GND_PINS = re.compile(r"^(P|A|D)?GND\d*$|^VSS$|^EP$|^PAD$")


def _vio(kind, sev, refs, net, msg, **extra):
    return checklib.violation(SCRIPT, sev, None, None, net, refs, msg,
                              SCRIPT, kind=kind, **extra)


def _f(x: float) -> str:
    return sim_board._f(x, "F")


def part_text(part: dict | None) -> str:
    if not part:
        return ""
    return " ".join([part.get("description") or ""] +
                    list((part.get("attrs") or {}).values()))


def board_caps(board: dict, parts: dict, rails: dict, temp_c: float,
               table: list[dict]) -> list[dict]:
    grounds = {n for n in board["nets"] if sim_board.check_ratings.is_ground(n)}
    out = []
    for ref in sorted(board["comps"], key=sim_board._natural):
        c = board["comps"][ref]
        if c["kind"] != "C" or len(c["pins"]) != 2:
            continue
        nets = sorted({p["net"] for p in c["pins"].values()})
        if len(nets) != 2:
            continue
        part = parts.get(ref)
        cval = engcalc.parse_value(c["value"]) or engcalc.parse_value(
            sim_board.attr(part, r"^Capacit") or "")
        if cval is None:
            continue
        volts = [0.0 if n in grounds else rails[n][0] if n in rails else None
                 for n in nets]
        rated, _src = sim_board.cap_rating(c, part)
        row = {"ref": ref, "nets": nets, "value": c["value"],
               "footprint": c["footprint"], "rated_v": rated}
        if None in volts:
            row.update(vdc=None, c_nom=cval)
            out.append(row)
            continue
        vdc = abs(volts[0] - volts[1])
        r = mlcc.c_eff(cval, vdc, footprint=c["footprint"], value=c["value"],
                       text=part_text(part), mpn=(part or {}).get("mpn", ""),
                       rated_v=rated, temp_c=temp_c, table=table)
        row.update(r)
        row["rail"] = next((n for n in nets if n not in grounds), None) \
            if any(n in grounds for n in nets) else None
        out.append(row)
    return out


def load_pin_requirements(path: Path = PIN_REQUIREMENTS) -> list[dict]:
    import yaml  # noqa: PLC0415
    try:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise checklib.CheckError(f"pin requirements {path}: {exc}") from exc
    return doc.get("parts") or []


def _pin_fns(pin: dict, aliases: dict) -> set[str]:
    return {aliases.get(f, f) for f in pin["fn"]}


def regulator_floors(board: dict, parts: dict, reqs: list[dict]
                     ) -> dict[str, dict]:
    """{net: {min_c, source, refs}} from pin_requirements rows: a verified
    part's `effective: true` capacitor requirement from an output pin to
    ground. A row without `effective` states a nominal part (the
    pin_part_missing rule's business), never a derated floor."""
    out: dict[str, dict] = {}
    for c in sorted(board["comps"].values(), key=lambda c: c["ref"]):
        if c["kind"] != "U":
            continue
        names = [c["value"], (parts.get(c["ref"]) or {}).get("mpn") or ""]
        for row in reqs:
            if not row.get("verified") or not any(
                    n and re.search(row.get("match") or "^$", n, re.I)
                    for n in names):
                continue
            aliases = {k.upper(): v.upper()
                       for k, v in (row.get("aliases") or {}).items()}
            for req in row.get("requires") or []:
                if not req.get("effective") or req.get("min") is None or \
                        "C" not in (req.get("kinds") or []) or \
                        not GND_PINS.match(str(req.get("to", "")).upper()):
                    continue
                pin = str(req.get("pin", "")).upper()
                for p in c["pins"].values():
                    if pin not in _pin_fns(p, aliases):
                        continue
                    if sim_board.check_ratings.is_ground(p["net"]):
                        continue
                    floor = float(req["min"])
                    cur = out.get(p["net"])
                    if cur is None or floor > cur["min_c"]:
                        out[p["net"]] = {
                            "min_c": floor, "refs": [c["ref"]],
                            "source": f"{c['ref']} {row.get('mpn')} "
                                      f"{req.get('id')}: "
                                      f"{req.get('source', '')}".strip()}
    return out


def floors_for(board: dict, parts: dict, cons: dict | None,
               overrides: dict[str, float], reqs: list[dict]
               ) -> dict[str, dict]:
    """{net: {min_c, source, refs}}: --min-c, then constraints
    power[].min_cout, then the regulator rows."""
    out: dict[str, dict] = {}
    for net, v in overrides.items():
        out[net] = {"min_c": v, "source": "--min-c", "refs": []}
    for e in (cons or {}).get("power") or []:
        v = e.get("min_cout")
        if e.get("net") and isinstance(v, (int, float)) and \
                not isinstance(v, bool) and e["net"] not in out:
            out[e["net"]] = {"min_c": float(v), "refs": [],
                             "source": "constraints power[].min_cout"}
    for net, f in regulator_floors(board, parts, reqs).items():
        out.setdefault(net, f)
    return out


def regulator_fed(board: dict) -> dict[str, list[str]]:
    """{net: [regulator refs]}: nets on an IC's OUT/VOUT/VO pin, and the far
    end of an inductor from an IC's SW/LX/PH pin."""
    out: dict[str, set[str]] = {}
    sw_nets: dict[str, set[str]] = {}
    for c in board["comps"].values():
        if c["kind"] != "U":
            continue
        for p in c["pins"].values():
            fns = set(p["fn"])
            if fns & OUT_FNS:
                out.setdefault(p["net"], set()).add(c["ref"])
            if fns & SW_FNS:
                sw_nets.setdefault(p["net"], set()).add(c["ref"])
    for c in board["comps"].values():
        if c["kind"] != "L" or len(c["pins"]) != 2:
            continue
        nets = [p["net"] for p in c["pins"].values()]
        for a, b in (nets, nets[::-1]):
            if a in sw_nets and b not in sw_nets:
                out.setdefault(b, set()).update(sw_nets[a])
    return {n: sorted(r) for n, r in out.items()
            if not sim_board.check_ratings.is_ground(n)}


def _part_line(r: dict) -> str:
    return (f"{r['ref']} ({r['value']}, {r['package'] or '?'} "
            f"{r['dielectric']}{' assumed' if r['dielectric_assumed'] else ''}"
            f") at {r['vdc']:.3g} V keeps about {r['fraction']:.0%}: "
            f"{_f(r['c_eff'])} typical, {_f(r['c_eff_low'])} low "
            f"({r['curve']} curve)")


def findings(caps: list[dict], floors: dict[str, dict] | dict[str, float],
             skipped: list | None = None) -> tuple[list, dict]:
    floors = {n: (f if isinstance(f, dict) else
                  {"min_c": float(f), "source": "--min-c", "refs": []})
              for n, f in floors.items()}
    vios = []
    totals: dict[str, dict] = {}
    for r in caps:
        if r.get("vdc") is None:
            continue
        if r.get("rated_v") and r["vdc"] > r["rated_v"]:
            vios.append(_vio(
                "cap_overvoltage", "error", [r["ref"]], r["nets"][0],
                f"{r['ref']} sees {r['vdc']:.3g} V DC over its "
                f"{r['rated_v']:g} V rating", vdc=r["vdc"],
                rating_v=r["rated_v"]))
        if r.get("rail"):
            t = totals.setdefault(r["rail"], {"c_nom": 0.0, "c_eff": 0.0,
                                              "c_eff_low": 0.0, "refs": []})
            t["c_nom"] += r["c_nom"]
            t["c_eff"] += r["c_eff"]
            t["c_eff_low"] += r["c_eff_low"]
            t["refs"].append(r["ref"])
    for r in caps:
        if r.get("vdc") is None or r["kind"] != "class2":
            continue
        fl = floors.get(r.get("rail"))
        t = totals.get(r.get("rail"))
        need = fl["min_c"] / t["c_nom"] if fl and t and t["c_nom"] else None
        common = dict(c_nom=r["c_nom"], c_eff=r["c_eff"],
                      c_eff_low=r["c_eff_low"], vdc=r["vdc"])
        keeps_low = r["c_eff_low"] / r["c_nom"] if r["c_nom"] else 1.0
        if need is not None and keeps_low < need and \
                t["c_eff_low"] < fl["min_c"]:
            vios.append(_vio(
                "cap_dc_bias_low", "warning", [r["ref"]], r["nets"][0],
                _part_line(r) + f"; {r['rail']} holds its {_f(fl['min_c'])} "
                f"floor ({fl['source']}) "
                + (f"only if every part keeps {need:.0%} of its "
                   f"{_f(t['c_nom'])} nominal" if need < 1 else
                   f"only with more than its {_f(t['c_nom'])} nominal"),
                need_fraction=checklib.rnd(need, 4), floor_source=fl["source"],
                **common))
        elif r["fraction"] < LOW_FRACTION:
            vios.append(_vio(
                "cap_dc_bias_low", "info", [r["ref"]], r["nets"][0],
                _part_line(r) + " (generic 50 % screen, no floor judged "
                "here)", **common))
    for net, fl in sorted(floors.items()):
        floor = fl["min_c"]
        t = totals.get(net)
        if t is None:
            on_net = sorted(r["ref"] for r in caps if net in r.get("nets", ())
                            and r.get("vdc") is None)
            if on_net and skipped is not None:
                skipped.append(
                    f"{net}: {fl['source']} asks for {_f(floor)} effective, "
                    f"but its DC is unknown so {', '.join(on_net)} cannot be "
                    "derated (name the rail voltage: --rail NET=V or "
                    "constraints power[])")
                continue
            vios.append(_vio(
                "rail_c_below_min", "error", fl["refs"], net,
                f"{net} has no capacitor to ground with a known DC; "
                f"{fl['source']} asks for {_f(floor)}", min_c=floor,
                floor_source=fl["source"]))
            continue
        if t["c_eff_low"] < floor:
            sev = "error" if t["c_eff"] < floor else "warning"
            vios.append(_vio(
                "rail_c_below_min", sev, t["refs"] + fl["refs"], net,
                f"{net}: {_f(t['c_nom'])} nominal is {_f(t['c_eff'])} "
                f"typical / {_f(t['c_eff_low'])} low at its DC, against a "
                f"{_f(floor)} effective floor ({fl['source']})",
                min_c=floor, floor_source=fl["source"], **{
                    k: t[k] for k in ("c_nom", "c_eff", "c_eff_low")}))
    return vios, totals


def judge(a) -> dict:
    table = mlcc.load_table()
    if a.c:
        c = engcalc.parse_value(a.c)
        if c is None:
            raise checklib.CheckError(f"--c {a.c!r} does not read as a value")
        fp = f"C_{a.package}" if a.package else ""
        r = mlcc.c_eff(c, a.vdc, footprint=fp, value=a.c,
                       text=a.dielectric or "", mpn=a.mpn or "",
                       rated_v=a.rated, temp_c=a.temp, table=table)
        return checklib.report(SCRIPT, "cli", [], part=r)
    a.rail = a.rail or []
    if a.netlist and not a.net:
        a.net = a.netlist
    if a.pcb and not a.net and not a.workspace:
        pcb = Path(a.pcb)
        beside = pcb.with_suffix(".net")
        if not beside.is_file():
            nets = sorted(pcb.parent.glob("*.net"))
            beside = nets[0] if len(nets) == 1 else beside
        if not beside.is_file():
            return checklib.report(
                SCRIPT, pcb, [], nothing_to_judge="no netlist beside "
                f"{pcb.name}: export it (kicad-cli sch export netlist)")
        a.net = str(beside)
    inp = sim_board.resolve_inputs(a)
    board = sim_board.load_board(inp["net"])
    parts = sim_board.load_parts(inp["parts"])
    cons = checklib.load_json(inp["constraints"], "constraints") \
        if inp["constraints"] else None
    rails = sim_board.resolve_rails(board, parts, cons, inp["power_tree"],
                                    sim_board._pairs(a.rail, "--rail"))
    caps = board_caps(board, parts, rails, a.temp, table)
    min_c: dict[str, float] = {}
    for item in a.min_c:
        net, _, v = item.partition("=")
        fv = engcalc.parse_value(v)
        if not net or fv is None:
            raise checklib.CheckError(f"--min-c {item!r}: want NET=value")
        min_c[net] = fv
    floors = floors_for(board, parts, cons, min_c,
                        load_pin_requirements(Path(a.pin_requirements)))
    skipped: list[str] = []
    vios, totals = findings(caps, floors, skipped)
    fed = regulator_fed(board)
    not_judged = skipped + [f"{net}: fed by {', '.join(refs)}, no effective-"
                  "capacitance floor stated (constraints power[].min_cout or "
                  "the regulator's pin_requirements row)"
                  for net, refs in sorted(fed.items())
                  if net in totals and net not in floors]
    unknown = [r["ref"] for r in caps if r.get("vdc") is None]
    assumed = sorted(r["ref"] for r in caps if r.get("dielectric_assumed")
                     and r.get("kind") == "class2" and r.get("fraction", 1)
                     < 0.8)
    return checklib.report(
        SCRIPT, inp["net"], vios, temp_c=a.temp,
        rails={n: {"volts": v, "source": s} for n, (v, s) in
               sorted(rails.items())},
        rail_totals={n: {k: (checklib.rnd(x, 9) if isinstance(x, float)
                             else x) for k, x in t.items()}
                     for n, t in sorted(totals.items())},
        floors={n: {"min_c": f["min_c"], "source": f["source"]}
                for n, f in sorted(floors.items())},
        not_judged=not_judged,
        nothing_to_judge=None if totals else
        "no capacitor to ground on a rail with a known DC",
        capacitors=caps, dc_unknown=unknown,
        known_unknowns=[{"ref": r, "what": "dielectric",
                         "why": "not named in value or parts.json: X5R "
                                "assumed for >= 1 uF"} for r in assumed])


def parse(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--c", help="one part: nominal capacitance (22u)")
    ap.add_argument("--package", help="one part: 0402/0603/0805/1206...")
    ap.add_argument("--rated", type=float, help="rated volts")
    ap.add_argument("--vdc", type=float, default=0.0)
    ap.add_argument("--dielectric", help="X5R/X7R/... (one part)")
    ap.add_argument("--mpn")
    ap.add_argument("--temp", type=float, default=25.0,
                    help="part temperature, C (default 25)")
    ap.add_argument("--workspace")
    ap.add_argument("--net")
    ap.add_argument("--netlist", help="alias of --net (checks.d)")
    ap.add_argument("--pcb", help="the board; its netlist beside it is read "
                    "when no --net/--netlist is given")
    ap.add_argument("--pin-requirements", default=str(PIN_REQUIREMENTS))
    ap.add_argument("--constraints")
    ap.add_argument("--parts")
    ap.add_argument("--power-tree")
    ap.add_argument("--rail", action="append", default=[])
    ap.add_argument("--min-c", action="append", default=[],
                    help="NET=value: a rail's effective-capacitance floor")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    if not (a.c or a.workspace or a.net or a.netlist or a.pcb):
        ap.error("give --c (one part) or --workspace/--net/--pcb (a board)")
    return a


def run(argv: list[str] | None = None) -> tuple[dict, str | None]:
    """checks.d contract: (payload, out)."""
    a = parse(argv)
    return judge(a), a.out


def main(argv: list[str] | None = None) -> int:
    checklib.utf8_stdout()
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    sys.exit(main())
