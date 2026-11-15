#!/usr/bin/env python
"""check_observability.py - what the board can see of itself, from its netlist.

The person, 10-28: "whatever can be done for minimal increased cost and
complexity to have the board monitor itself and its waveforms is best". A
board the Hive brings up unattended can only report what it is wired to
measure. This script reads the exported netlist and says, for each rail and
each status output, whether the board itself can observe it, whether a
bench can (a test point), or neither - so the gaps are seen while a 0402
divider still costs a cent. It is a report an agent weighs, not a gate:
reference/self-observation.md says what is worth adding and what it costs.

  rails (every net with a known voltage above 0 V, the sim_board ladder):
    self        an MCU's I/O pin reads it through a divider (a resistor to
                ground on the way: a lone resistor to a GPIO is a pull-up
                or a strap, not a reading), or any IC's ADC/AIN-named pin
                or a monitor IC's sense pin (INA*, ADS*, MCP3*, PAC19*,
                ...) reaches it directly or through resistors (at most two
                resistors deep). An I/O expander (TCA95xx, PCF857x, ...)
                watches status lines but reads no rail.
    test_point  a TP* part or a test-point footprint sits on it
    blind       neither: warning rail_blind
  status outputs (an IC pin named PG, PGOOD, FAULT, INT, ALERT, OTW, CLIP,
  CHRG, STAT...):
    watched     reaches a GPIO-named pin of another IC (an MCU or an I/O
                expander), directly or through one resistor
    test_point  only a test point
    unwatched   only pull-ups or nothing: warning status_unwatched
  console       a USB-UART bridge (CH340, CP210x, FT23x, PL2303) or an MCU's
                own USB D+/D- pins on the board - how a first flash and a
                provisioning script reach it (warning no_console when the
                board has an MCU and neither)

Facts: `rails`, `status_pins`, `mcus` (ICs with eight or more GPIO-named
pins that are not I/O expanders), `expanders`,
`console`, and a `summary` of the counts. A board with no MCU cannot watch
itself; its rails are judged by test points alone, and it says so.

Heuristics, stated: pin roles come from the symbol's pin names (the same
names sim_board reads). An MCU's IO/GPIO/Pxn pin counts as able to read a
rail, though not every GPIO of every MCU has an ADC behind it (ESP32: ADC1
only while Wi-Fi runs) - the review checks each path's pin against the
datasheet. A regulator's own FB/VSENSE pin regulates, it does not report,
so it never counts.

CLI:
  check_observability.py --workspace boards/<b> [--out reports/observability.json]
  check_observability.py --net x.net [--constraints c.json] [--parts parts/]
                         [--rail NET=V]
Exit 0 pass / 1 warnings / 2 error.
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
import sim_board  # noqa: E402

SCRIPT = "check_observability"
SOURCE = "observability"

# a pin that can read a voltage or a logic level (an MCU or expander I/O)
SENSE_FN = re.compile(r"^(GPIO\d+|IO\d+|P[A-K]?\d+(_\d+)?|PIO\d+|ADC\d*(_\w+)?"
                      r"|AIN\d*|A\d+|.*_ADC\d*|SENSOR_V[PN]|VDET_\d)$")
ADC_FN = re.compile(r"^(ADC\d*(_\w+)?|AIN\d*|.*_ADC\d*)$")
GPIO_FN = re.compile(r"^(GPIO\d+|IO\d+|P[A-K]?\d+(_\d+)?|PIO\d+|SENSOR_V[PN]"
                     r"|VDET_\d)$")
EXPANDER_RE = re.compile(r"^(TCA95|TCA64|PCA95|PCAL95|PCF857|MCP230|AW9523|"
                         r"XL95)", re.I)
STATUS_FN = re.compile(r"^N?(PG|PGOOD|POK|PWRGD|FAULT\w*|FLT\w*|INT(_?N|B|\d)?|IRQ"
                       r"|ALERT\w*|OTW\w*|CLIP\w*|CHRG|STAT\w*|OC\w*FLAG)$")
MONITOR_RE = re.compile(r"^(INA\d|ADS1\d|MCP3\d|PAC19|ACS7|LTC29|MAX1704|"
                        r"INA2\d\d|TMP\d|ADS7)", re.I)
MONITOR_SENSE_FN = re.compile(r"^(IN[+-]|IN[PN]|VIN[+-]|VBUS|SENSE[+-]?|AIN\d*"
                              r"|CH\d|ISENSE\w*|VS[+-])$")
BRIDGE_RE = re.compile(r"^(CH34\d|CH9102|CP210\d|FT23\d|FT2232|PL2303)", re.I)
USB_FN = re.compile(r"^(USB_?)?D[+-]$|^(USB_?)?D[PM]$|^USB_?D[PN]$")
TP_FP = re.compile(r"TestPoint|Test_Point|\bTP\b", re.I)
MCU_MIN_GPIO = 8


def _index(board: dict) -> dict[str, list[tuple[dict, str, list[str]]]]:
    """{net: [(comp, pin, fns)]}"""
    out: dict[str, list] = {}
    for c in board["comps"].values():
        for pin, p in c["pins"].items():
            out.setdefault(p["net"], []).append((c, pin, p["fn"]))
    return out


def _is_ic(c: dict) -> bool:
    return c["kind"] in ("U", "IC")


def _mpn(c: dict, parts: dict) -> str:
    return (parts.get(c["ref"]) or {}).get("mpn") or c["value"] or ""


def _is_tp(c: dict) -> bool:
    return c["kind"] == "TP" or (len(c["pins"]) == 1
                                 and bool(TP_FP.search(c["footprint"])))


def _two_pin_other(c: dict, net: str) -> str | None:
    nets = [p["net"] for p in c["pins"].values()]
    if len(nets) != 2 or net not in nets:
        return None
    return nets[1] if nets[0] == net else nets[0]


def _sense_pins(net: str, idx: dict, parts: dict,
                mcus: set[str]) -> list[tuple[str, bool]]:
    """(`REF.PIN(fn)`, analog) for every pin on net that can read it: an
    MCU's I/O (analog False: it reads a rail only through a divider, else it
    is a pull-up or a strap), an ADC-named pin of any IC or a monitor IC's
    sense input (analog True). A regulator's own FB/VSENSE regulates rather
    than reports, so it never counts."""
    hits = []
    for c, pin, fns in idx.get(net, []):
        if not _is_ic(c):
            continue
        mon = bool(MONITOR_RE.match(_mpn(c, parts)))
        for f in fns:
            if sim_board.POWER_FN.match(f) or sim_board.FB_FN.match(f):
                continue
            analog = bool(ADC_FN.match(f)) or (
                mon and bool(MONITOR_SENSE_FN.match(f) or SENSE_FN.match(f)))
            if analog or (c["ref"] in mcus and SENSE_FN.match(f)):
                hits.append((f"{c['ref']}.{pin}({f})", analog))
                break
    return hits


def _to_ground(net: str, idx: dict, grounds: set[str]) -> bool:
    """A resistor from net to ground: the bottom of a divider."""
    return any(c["kind"] == "R" and _two_pin_other(c, net) in grounds
               for c, _p, _f in idx.get(net, []))


def rail_view(net: str, idx: dict, parts: dict, rails: dict,
              grounds: set[str], mcus: set[str]) -> dict:
    """How a rail is seen: the sense paths (at most two resistors deep) and
    the test points on it. An MCU I/O pin counts only behind a divider (a
    resistor to ground on the way); an analog pin through any path."""
    power = set(rails)
    paths = [{"via": [], "pin": h} for h, analog in
             _sense_pins(net, idx, parts, mcus) if analog]
    seen = {net}
    frontier = [(net, [], False)]
    for _ in range(2):
        nxt = []
        for n, via, divided in frontier:
            for c, _pin, _fns in idx.get(n, []):
                if c["kind"] != "R":
                    continue
                o = _two_pin_other(c, n)
                if o is None or o in seen or o in power or o in grounds:
                    continue
                seen.add(o)
                v = via + [c["ref"]]
                d = divided or _to_ground(o, idx, grounds)
                paths += [{"via": v, "node": o, "pin": h, "divider": d}
                          for h, analog in _sense_pins(o, idx, parts, mcus)
                          if analog or d]
                nxt.append((o, v, d))
        frontier = nxt
    tps = sorted(c["ref"] for c, _p, _f in idx.get(net, []) if _is_tp(c))
    status = "self" if paths else "test_point" if tps else "blind"
    return {"net": net, "volts": rails[net][0], "status": status,
            "paths": paths, "test_points": tps}


def status_view(c: dict, pin: str, fn: str, net: str | None, idx: dict,
                parts: dict, rails: dict, grounds: set[str]) -> dict:
    out = {"ref": c["ref"], "pin": pin, "fn": fn, "net": net}
    if net is None:
        return {**out, "status": "unwatched", "watchers": [],
                "test_points": [], "why": "pin not connected"}
    watchers = _gpio_pins(net, idx, c["ref"])
    for r, _p, _f in idx.get(net, []):
        if r["kind"] != "R":
            continue
        o = _two_pin_other(r, net)
        if o and o not in rails and o not in grounds:
            watchers += [f"{h} via {r['ref']}" for h in _gpio_pins(o, idx, c["ref"])]
    tps = sorted(x["ref"] for x, _p, _f in idx.get(net, []) if _is_tp(x))
    status = "watched" if watchers else "test_point" if tps else "unwatched"
    return {**out, "status": status, "watchers": watchers, "test_points": tps}


def _gpio_pins(net: str, idx: dict, exclude: str) -> list[str]:
    out = []
    for c, pin, fns in idx.get(net, []):
        g = next((f for f in fns if GPIO_FN.match(f)), None)
        if g and _is_ic(c) and c["ref"] != exclude:
            out.append(f"{c['ref']}.{pin}({g})")
    return out


def analyze(board: dict, parts: dict, rails: dict,
            nc_pins: list[tuple[str, str, str]]) -> dict:
    idx = _index(board)
    grounds = {n for n in board["nets"] if re.fullmatch(
        r"/?(A|D|P)?GND\w*|/?VSS\w*|/?0V|/?EARTH", n, re.I)}
    live = {n: v for n, v in rails.items() if v[0] > 0 and n not in grounds}
    many = [c["ref"] for c in board["comps"].values() if _is_ic(c) and
            sum(any(GPIO_FN.match(f) for f in p["fn"])
                for p in c["pins"].values()) >= MCU_MIN_GPIO]
    expanders = sorted(r for r in many
                       if EXPANDER_RE.match(_mpn(board["comps"][r], parts)))
    mcus = sorted(r for r in many if r not in expanders)
    rv = [rail_view(n, idx, parts, live, grounds, set(mcus))
          for n in sorted(live, key=lambda n: (-live[n][0], n))]
    st = []
    for c in sorted(board["comps"].values(), key=lambda c: sim_board._natural(c["ref"])):
        if not _is_ic(c) or c["ref"] in mcus or c["ref"] in expanders:
            continue
        for pin, p in sorted(c["pins"].items()):
            f = next((x for x in p["fn"] if STATUS_FN.match(x)), None)
            if f and p["net"] not in live and p["net"] not in grounds:
                st.append(status_view(c, pin, f, p["net"], idx, parts, live,
                                      grounds))
    for ref, pin, f in nc_pins:
        c = board["comps"].get(ref)
        if c is not None and _is_ic(c) and c["ref"] not in mcus \
                and c["ref"] not in expanders and STATUS_FN.match(f):
            st.append(status_view(c, pin, f, None, idx, parts, live, grounds))
    bridges = sorted(c["ref"] for c in board["comps"].values()
                     if _is_ic(c) and BRIDGE_RE.match(_mpn(c, parts)))
    native = sorted({c["ref"] for c in board["comps"].values() if c["ref"] in mcus
                     for p in c["pins"].values()
                     if any(USB_FN.match(f) for f in p["fn"])})
    return {"rails": rv, "status_pins": st, "mcus": mcus,
            "expanders": expanders,
            "console": {"usb_uart": bridges, "native_usb": native}}


def findings(res: dict) -> list[dict]:
    vios = []
    mcu = bool(res["mcus"])
    for r in res["rails"]:
        if r["status"] == "blind":
            vios.append(checklib.violation(
                "rail_blind", "warning", None, None, r["net"], [],
                f"{r['net']} ({r['volts']:g} V): nothing on the board can read "
                "it and no test point reaches it"
                + (" - a divider into a spare ADC pin, or a TP" if mcu
                   else " - add a test point (the board has no MCU)"),
                SOURCE))
    for s in res["status_pins"]:
        if s["status"] == "unwatched":
            vios.append(checklib.violation(
                "status_unwatched", "warning", None, None, s["net"], [s["ref"]],
                f"{s['ref']} {s['fn']} (pin {s['pin']}) reaches no GPIO "
                f"({s.get('why') or 'pull-up or nothing only'}): the board "
                "cannot report this fault", SOURCE))
    con = res["console"]
    if mcu and not con["usb_uart"] and not con["native_usb"]:
        vios.append(checklib.violation(
            "no_console", "warning", None, None, None, res["mcus"],
            "an MCU with no USB-UART bridge and no USB D+/D- on its pins: "
            "first flash and Wi-Fi provisioning need a header and an adapter",
            SOURCE))
    return vios


def summary(res: dict) -> dict:
    def count(items, key):
        out: dict[str, int] = {}
        for i in items:
            out[i[key]] = out.get(i[key], 0) + 1
        return out
    return {"rails": count(res["rails"], "status"),
            "status_pins": count(res["status_pins"], "status"),
            "has_mcu": bool(res["mcus"])}


def nc_status_pins(net_path: Path) -> list[tuple[str, str, str]]:
    """(ref, pin, fn) of pins on unconnected-* nets (load_board drops
    them)."""
    import simlib
    out = []
    for name, nodes in simlib.parse_netlist(net_path)["nets"].items():
        if not name.startswith("unconnected-"):
            continue
        for nd in nodes:
            for f in sim_board.norm_fn(nd.get("pinfunction", ""), nd["pin"]):
                out.append((nd["ref"], nd["pin"], f))
    return out


def run(a) -> dict:
    a.power_tree = getattr(a, "power_tree", None)
    inp = sim_board.resolve_inputs(a)
    board = sim_board.load_board(inp["net"])
    parts = sim_board.load_parts(inp["parts"], board)
    cons = checklib.load_json(inp["constraints"], "constraints") \
        if inp["constraints"] else None
    rails = sim_board.resolve_rails(board, parts, cons, inp["power_tree"],
                                    sim_board._pairs(a.rail, "--rail"))
    res = analyze(board, parts, rails, nc_status_pins(inp["net"]))
    vios = findings(res)
    return checklib.report(SCRIPT, inp["net"], vios,
                           input={k: str(v) if v else None
                                  for k, v in inp.items()},
                           summary=summary(res), **res)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workspace")
    ap.add_argument("--net")
    ap.add_argument("--constraints")
    ap.add_argument("--parts", help="parts dir holding parts.json")
    ap.add_argument("--power-tree")
    ap.add_argument("--rail", action="append", default=[],
                    help="NET=volts, adds or overrides a rail")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    return checklib.cli_wrap(SCRIPT, lambda: (run(a), a.out))


if __name__ == "__main__":
    sys.exit(main())
