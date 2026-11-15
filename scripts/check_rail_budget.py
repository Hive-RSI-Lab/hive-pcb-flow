"""check_rail_budget.py - a load's supply pin still gets its minimum voltage
after the source's tolerance, the copper and the series parts on the way
(P8 verify suite; #1118).

The hit: a 1.8 V analog supply ball needs 1.71 V. Its LDO can sit at 1.782 V,
then 19.3 mV of copper to a ferrite bead, 32.5 mV in the bead (0.25 ohm max
DCR at 130 mA) and 22.0 mV of copper to the balls: the ball gets 1.708 V. No
single net breaks a rule, so check_irdrop (one net at a time, against a
hand-set limit) passes it; the budget only fails when it is added up.

For each load pin with a minimum supply this check adds it up:
  1. the source's low limit: a regulator in reference/rail_budget.yaml
     `sources` (its tolerance band on the rail's nominal, read from the net
     name or constraints) or constraints.json rail_budget.sources;
  2. the rail followed from the source's output through series parts: 2-pad
     ferrite beads (FB*) and inductors (L*), and any part named in
     rail_budget.series, up to 4 deep;
  3. each net's copper drop from check_irdrop's resistor-grid solver, with
     the upstream part as the source and the loads and series parts beyond
     as sinks at their currents;
  4. each series part's max DCR (rail_budget.series {"FB2":
     {"dcr_max_ohm": 0.25}}; a value string is never read as DCR) times the
     current through it;
  5. the margin at the load against its minimum: reference/rail_budget.yaml
     `loads` (supply pin regex, vmin) or rail_budget.loads {"U5": {"pins":
     "VDDA", "vmin_v": 1.71, "current_a": 0.13}}. A load's current is its
     constraints current_a, else source_limits.yaml's i_supply.

What it leaves out makes the drop a lower bound: a load with no current, a
bead with no DCR, copper it could not solve (zones not filled), other parts
on the rail it has no current for, load regulation. So a shortfall is an
error whatever is missing, and a margin with something missing is a warning
that says what, never a pass.

Findings:
  rail_budget_short    error: the load pin gets less than its minimum
  rail_budget_unknown  warning: the margin is positive but rests on a
                       missing current, DCR, copper or source limit
  A finding that rests on a `verified: false` row of rail_budget.yaml or
  source_limits.yaml goes to not_judged instead.

CLI: --pcb board.kicad_pcb [--constraints c.json] [--table rail_budget.yaml]
     [--out report.json]; exit 0 pass / 1 violations / 2 error (SPEC 6).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "lib"))
import checklib  # noqa: E402
import geom  # noqa: E402
from check_ratings import is_ground, load_parts_on_board, volts_from_name  # noqa: E402

SCRIPT = "check_rail_budget"
SOURCE = SCRIPT
TABLE = HERE.parent / "reference" / "rail_budget.yaml"
MAX_DEPTH = 4
_SERIES_REF = re.compile(r"^(?:FB|L)\d", re.I)


def load_table(path: Path = TABLE) -> dict:
    try:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise checklib.CheckError(f"rail budget table {path}: {exc}") from exc
    return {"sources": doc.get("sources") or [],
            "loads": doc.get("loads") or []}


def load_currents() -> list[dict]:
    """source_limits.yaml's loads rows (i_supply), shared with class 11."""
    from netlist_audit import load_source_limits  # noqa: PLC0415
    return load_source_limits()["loads"]


def _row(rows: list[dict], part: dict) -> dict | None:
    val = part.get("value") or ""
    return next((r for r in rows if re.search(r["match"], val, re.I)), None)


def _pins(part: dict, net: str | None, rx: str | None) -> list[str]:
    """Pad numbers of PART (on NET when given) whose pin function matches
    RX, or whose number does in full; every pad when RX is None."""
    out = []
    for num, n in sorted(part["pads"].items()):
        if net is not None and n != net:
            continue
        if rx is None:
            out.append(num)
            continue
        fn = part["fns"].get(num) or ""
        if (fn and re.search(rx, fn, re.I)) or re.fullmatch(rx, num, re.I):
            out.append(num)
    return out


def _num(d: dict, key: str) -> float | None:
    v = (d or {}).get(key)
    return float(v) if isinstance(v, (int, float)) and not isinstance(
        v, bool) else None


def _low_limit(row: dict | None, given: dict, nominal: float | None):
    """(volts or None, why-not). Constraints first, then the row."""
    if _num(given, "vout_min_v") is not None:
        return _num(given, "vout_min_v"), None
    if not row:
        return None, "no low limit given"
    if _num(row, "vout_min_v") is not None:
        return _num(row, "vout_min_v"), None
    if nominal is None:
        return None, "its rail's nominal voltage is not in the net name"
    if _num(row, "vout_from_v") is not None and nominal < row["vout_from_v"]:
        return None, (f"{row['mpn']}'s band holds from {row['vout_from_v']:g}"
                      f" V; {nominal:g} V is below it")
    if _num(row, "vout_min_pct") is not None:
        return nominal * row["vout_min_pct"] / 100.0, None
    if _num(row, "vout_tol_pct") is not None:
        return nominal * (1.0 - row["vout_tol_pct"] / 100.0), None
    return None, f"{row['mpn']} row has no tolerance"


class Copper:
    """check_irdrop's solver over the board, loaded once."""

    def __init__(self, pcb: Path):
        self.bg, self.why = None, None
        try:
            bg = geom.load_board(Path(pcb))
            bg.assert_fresh()
            self.bg = bg
        except geom.StaleFillError as exc:
            self.why = f"zones not filled ({exc})"
        except Exception as exc:  # noqa: BLE001
            self.why = f"board geometry did not load ({exc})"

    def drops(self, net: str, source_ref: str, sinks: dict[str, float]):
        """({ref: volts}, None) or (None, why)."""
        import check_irdrop  # noqa: PLC0415
        if self.bg is None:
            return None, self.why
        live = {r: a for r, a in sinks.items() if a > 0}
        if not live:
            return {r: 0.0 for r in sinks}, None
        try:
            layers = [l for l in self.bg.copper_layers
                      if self.bg.net_area(net, l) > 0]
            if not layers:
                return None, f"{net} has no copper"
            entry = {"source_ref": source_ref,
                     "sinks": [{"ref": r, "current_a": a}
                               for r, a in sorted(live.items())]}
            cell, _ = check_irdrop.pick_cell_mm(self.bg, net, layers, entry)
            res = check_irdrop.solve_grid(self.bg, entry, net, layers, cell)
        except checklib.CheckError as exc:
            return None, f"{net}: {exc}"
        got = res["sink_drops"]
        missing = sorted(set(live) - set(got))
        if missing:
            return None, (f"{net}: {', '.join(missing)} not connected to "
                          f"{source_ref} on the solver grid")
        return {r: got.get(r, 0.0) for r in sinks}, None


def check(pcb: Path, cons: dict | None, table: dict | None = None,
          currents: list[dict] | None = None):
    """(violations, seen facts, not_judged notes)."""
    table = table if table is not None else load_table()
    currents = currents if currents is not None else load_currents()
    rb = (cons or {}).get("rail_budget") or {}
    c_src = rb.get("sources") or {}
    c_ser = rb.get("series") or {}
    c_load = rb.get("loads") or {}
    parts = [p for p in load_parts_on_board(pcb) if p["in_bom"]]
    by_ref = {p["ref"]: p for p in parts}
    on_net: dict[str, set[str]] = {}
    for p in parts:
        for n in p["pads"].values():
            on_net.setdefault(n, set()).add(p["ref"])

    viols: list[dict] = []
    not_judged: list[str] = []
    budgets: list[dict] = []
    copper = None

    def load_spec(part: dict, net: str):
        ref = part["ref"]
        given = c_load.get(ref) or {}
        row = _row(table["loads"], part)
        if not given and not row:
            return None
        rx = given.get("pins") or (row["supply"] if row else None)
        pins = _pins(part, net, rx)
        if not pins:
            return None
        vmin = _num(given, "vmin_v")
        rows = []
        if vmin is None and row:
            vmin = _num(row, "vmin")
            rows.append(row)
        amps = _num(given, "current_a")
        if amps is None:
            crow = _row(currents, part)
            if crow and _num(crow, "i_supply") is not None:
                amps = crow["i_supply"]
                rows.append(crow)
        return {"ref": ref, "pins": pins, "vmin": vmin, "amps": amps,
                "rows": rows}

    sources_seen = 0
    loads_seen = 0
    for src in parts:
        given = c_src.get(src["ref"]) or {}
        row = _row(table["sources"], src)
        if not given and not row:
            continue
        rx = given.get("pins") or (row or {}).get("out")
        out_nets = sorted({src["pads"][n] for n in _pins(src, None, rx)}
                          - {n for n in src["pads"].values() if is_ground(n)}) \
            if rx else []
        if not out_nets:
            why = ("its pads carry no pin names on this board"
                   if not any(src["fns"].values()) else "no output pin found")
            not_judged.append(f"source {src['ref']} ({src['value']}): {why} "
                              "(give rail_budget.sources."
                              f"{src['ref']}.pins)")
            continue
        sources_seen += 1
        for root_net in out_nets:
            nominal = _num(given, "vout_v")
            if nominal is None:
                nominal = volts_from_name(root_net)
            low, why_low = _low_limit(row, given, nominal)
            src_rows = [row] if row and _num(given, "vout_min_v") is None \
                else []
            # --- walk the rail through series parts
            nodes = []   # {net, up, via, parent, loads, children}
            seen_nets = {root_net}
            stack = [{"net": root_net, "up": src["ref"], "via": None,
                      "parent": None, "depth": 0}]
            while stack:
                node = stack.pop(0)
                nodes.append(node)
                node["children"], node["loads"] = [], []
                skip = {node["up"]}
                for ref in sorted(on_net.get(node["net"], ())):
                    if ref in skip or ref == src["ref"]:
                        continue
                    part = by_ref[ref]
                    nets = set(part["pads"].values())
                    if (len(part["nums"]) == 2 and len(nets) == 2
                            and (_SERIES_REF.match(ref) or ref in c_ser)):
                        other = next(iter(nets - {node["net"]}))
                        if (is_ground(other) or other in seen_nets
                                or other in src["pads"].values()
                                or node["depth"] >= MAX_DEPTH):
                            continue
                        seen_nets.add(other)
                        child = {"net": other, "up": ref, "via": ref,
                                 "parent": node, "depth": node["depth"] + 1}
                        node["children"].append(child)
                        stack.append(child)
                        continue
                    spec = load_spec(part, node["net"])
                    if spec:
                        node["loads"].append(spec)

            def through(node) -> tuple[float, list[str]]:
                amps, miss = 0.0, []
                for ld in node["loads"]:
                    if ld["amps"] is None:
                        miss.append(f"{ld['ref']} has no current")
                    else:
                        amps += ld["amps"]
                for ch in node["children"]:
                    a, m = through(ch)
                    amps += a
                    miss += m
                node["amps"], node["miss"] = amps, miss
                return amps, miss

            through(nodes[0])
            if not any(ld["vmin"] is not None for n in nodes
                       for ld in n["loads"]):
                continue
            if copper is None:
                copper = Copper(pcb)
            for node in nodes:
                sinks = {ld["ref"]: ld["amps"] or 0.0 for ld in node["loads"]}
                sinks.update({ch["via"]: ch["amps"]
                              for ch in node["children"]})
                node["drops"], node["copper_why"] = copper.drops(
                    node["net"], node["up"], sinks)

            for node in nodes:
                for ld in node["loads"]:
                    if ld["vmin"] is None:
                        continue
                    loads_seen += 1
                    viols_for_load(viols, not_judged, budgets, src, root_net,
                                   low, why_low, src_rows, node, ld, c_ser)
    seen = {"sources": sources_seen, "loads": loads_seen,
            "budgets": budgets}
    return viols, seen, not_judged


def viols_for_load(viols, not_judged, budgets, src, root_net, low, why_low,
                   src_rows, node, ld, c_ser):
    chain = []
    n = node
    while n is not None:
        chain.append(n)
        n = n["parent"]
    chain.reverse()
    steps, missing, rows = [], [], list(src_rows) + list(ld["rows"])
    total = 0.0
    if why_low:
        missing.append(f"source {src['ref']}: {why_low}")
    missing += chain[0]["miss"] if chain else []
    for i, n in enumerate(chain):
        sink = chain[i + 1]["via"] if i + 1 < len(chain) else ld["ref"]
        if n["drops"] is None:
            missing.append(f"copper of {n['net']}: {n['copper_why']}")
        else:
            d = n["drops"].get(sink, 0.0)
            total += d
            steps.append({"what": f"{n['net']} copper {n['up']}->{sink}",
                          "mv": checklib.rnd(d * 1e3)})
        if i + 1 < len(chain):
            ser = chain[i + 1]
            dcr = _num(c_ser.get(ser["via"]) or {}, "dcr_max_ohm")
            if dcr is None:
                missing.append(f"{ser['via']} has no dcr_max_ohm")
            else:
                d = dcr * ser["amps"]
                total += d
                steps.append({"what": f"{ser['via']} {dcr:g} ohm x "
                              f"{ser['amps'] * 1e3:.0f} mA",
                              "mv": checklib.rnd(d * 1e3)})
    path = [src["ref"]] + [n["via"] for n in chain[1:]] + [ld["ref"]]
    pins = ",".join(ld["pins"])
    where = f"{ld['ref']}.{pins} on {node['net']}"
    rec = {"load": ld["ref"], "pins": ld["pins"], "net": node["net"],
           "path": path, "vmin_v": ld["vmin"], "steps": steps,
           "drop_mv": checklib.rnd(total * 1e3), "missing": missing}
    budgets.append(rec)
    unverified = [r.get("mpn") or r.get("match") for r in rows
                  if r.get("verified") is not True]
    if low is None:
        rec["status"] = "unknown"
        msg = (f"{where}: no budget, {src['ref']} ({src['value']}) on "
               f"{root_net} has no low limit: {why_low}")
        if unverified:
            not_judged.append(msg + f" [unverified: {', '.join(unverified)}]")
            return
        viols.append(checklib.violation(
            SCRIPT, "warning", None, None, node["net"], path, msg, SOURCE,
            kind="rail_budget_unknown", missing=missing))
        return
    v_at = low - total
    margin = v_at - ld["vmin"]
    rec.update(source_low_v=checklib.rnd(low, 4), v_at_v=checklib.rnd(v_at, 4),
               margin_mv=checklib.rnd(margin * 1e3))
    terms = " + ".join(f"{s['mv']:g} mV {s['what']}" for s in steps) or "0"
    body = (f"{where} gets {v_at:.3f} V against its {ld['vmin']:g} V "
            f"minimum ({margin * 1e3:+.1f} mV): {src['ref']} "
            f"({src['value']}) low limit {low:.3f} V - ({terms})")
    if margin < 0:
        rec["status"] = "short"
        if unverified:
            not_judged.append(body + f" [unverified: {', '.join(unverified)}]")
            return
        if missing:
            body += "; and the drop is a lower bound: " + "; ".join(missing)
        viols.append(checklib.violation(
            SCRIPT, "error", None, None, node["net"], path, body, SOURCE,
            kind="rail_budget_short", load=ld["ref"],
            margin_mv=rec["margin_mv"],
            v_at_v=rec["v_at_v"], vmin_v=ld["vmin"], steps=steps,
            missing=missing))
    elif missing:
        rec["status"] = "unknown"
        msg = body + "; not judged, it leaves out: " + "; ".join(missing)
        if unverified:
            not_judged.append(msg + f" [unverified: {', '.join(unverified)}]")
            return
        viols.append(checklib.violation(
            SCRIPT, "warning", None, None, node["net"], path, msg, SOURCE,
            kind="rail_budget_unknown", load=ld["ref"],
            margin_mv=rec["margin_mv"],
            missing=missing))
    else:
        rec["status"] = "ok"


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcb", required=True)
    ap.add_argument("--constraints")
    ap.add_argument("--table", default=str(TABLE),
                    help="the source/load table (reference/rail_budget.yaml)")
    ap.add_argument("--out", help="write the JSON report here")
    args = ap.parse_args(argv)
    pcb = Path(args.pcb)
    if not pcb.is_file():
        raise checklib.CheckError(f"board not found: {pcb}")
    cons = None
    if args.constraints:
        cons = json.loads(Path(args.constraints).read_text(encoding="utf-8"))
    viols, seen, not_judged = check(pcb, cons, load_table(Path(args.table)))
    return checklib.report(
        SCRIPT, pcb, viols, checked=[seen],
        nothing_to_judge=None if seen["loads"] else
        "no load with a minimum supply on a rail from a source with a "
        "low-limit record",
        not_judged=not_judged), args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    sys.exit(main())
