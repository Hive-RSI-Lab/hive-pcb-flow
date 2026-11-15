#!/usr/bin/env python
"""sim_inrush.py - a board's load switches against a USB PD source.

sim_board holds every rail at its voltage; a load switch's inrush, its
turn-on SOA and what a charger sees are exactly what that hides. This
builds the board's own passive and discrete network the way sim_board does
(its netlist reading and models), but with the power entry driven by a PD
source through a cable (lib/pdsource.py) and each load switch's output left
free, then runs the scenarios a sink meets: attach, the 5 V -> contract
step at 0.5/3/30 V/ms with firmware off, firmware turning the switch on,
and a hard reset with firmware still on.

Load switches: a P-MOSFET whose source is on a rail and whose drain net is
a rail or carries >= 10 uF to ground (a high-side N-MOSFET the same way
round). The entry: the net on a connector's VBUS pin, else the first
switch's input; the nets it reaches through resistors of 100 ohm or less
and inductors are the source side and are not held by ideal sources.
Every other rail is an ideal source from t = 0 (the board's logic rails up
before anything else - the worst case for a switch's gate). IC I/O nets
are driven through 25 ohm low with firmware off and to their IC's supply
with it on; open-drain status nets (PG, FAULT...) are pulled low through
25 ohm while the contract is good. A FET found in reference/fet_power.yaml
uses its fitted model and thermal data; its Vth corners (min/max) are run
on the 30 V/ms step. Ceramic capacitors are taken at their DC-bias value
(lib/mlcc.py) where their DC is known.

Findings (kind; severity):
  pd_overcurrent     the source delivers more than the contract current
                     for over 1 ms, or over twice it at any instant
                     (error: a source's OCP trips and hard-resets); above
                     the contract briefly (warning)
  switch_on_fw_off   a switch output rises with firmware off: over half
                     the contract voltage (error), over 1 V (warning)
  fet_soa            the switch's peak junction temperature over its
                     maximum (error), within 25 C of it (warning) - from
                     the simulated p(t) and the part's Zth (lib/soa.py);
                     generic package data only warns
  fet_vgs_transient  |Vgs| over the part's rating at any instant (error)
  sink_bulk_over_pd  more than 100 uF effective on the source side
                     (cSnkBulkPd; error); over 10 uF is a fact (cSnkBulk)

  hotplug_overvoltage  a source already at the contract voltage closed onto
                     the entry through 0.08 ohm and 0.5/1/2 uH of cable
                     (no switch in the way: the entry's own ceramics, RC
                     dampers and TVS take the edge) rings a net on the
                     source side over an IC pin's abs max (error). Abs max
                     from the part's extraction (<parts>/<LCSC>.json),
                     else its reference/pin_ratings.yaml abs_max_v; a
                     pin with neither is a known unknown. Runs with or
                     without a load switch; --no-hotplug skips it. A TVS
                     is given the series resistance its family's clamping
                     voltage implies.

Facts: per scenario the peak source current, time above contract, and per
switch peak power, energy, Tj peak, output peak/final, Vgs peak, the
10-90 % rise time with firmware on; `hotplug`: per cable L the peak on
each source-side net and the source current, and the pins judged.
`known_unknowns` names the FETs on
generic models or data, assumed dielectrics and the open IC loads.

CLI:
  sim_inrush.py --workspace boards/<b> [--contract-v 28] [--contract-a 5]
                [--entry VBUS_IN] [--load NET=ohms] [--ta 25]
                [--slews 0.5,3,30] [--window-ms 400] [--out ...]
constraints.json may carry {"pd_source": {"contract_v": 28,
"contract_a": 5}}; the default contract is the entry rail's voltage at 3 A.
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
import mlcc  # noqa: E402
import pdsource  # noqa: E402
import sim_board  # noqa: E402
import soa  # noqa: E402
from lib import env  # noqa: E402

SCRIPT = "sim_inrush"
BULK_F = 10e-6
SOURCE_SIDE_OHMS = 100.0
DOWNSTREAM_OHMS = 1.0
ESR_FROM_F = 10e-6
ESR_OHMS = 0.02
CSNKBULK_F = 10e-6
CSNKBULKPD_F = 100e-6
FW_OFF_WARN_V = 1.0
DEFAULT_CONTRACT_A = 3.0
VTO_RE = re.compile(r"Vto=\s*-?[\d.eE+-]+", re.I)
# hot-plug: a source already at its voltage switched onto the entry net
# through a cable (contact closure), no load switch in the way
HOTPLUG_L = (0.5e-6, 1e-6, 2e-6)
HOTPLUG_R = 0.08
HOTPLUG_T0 = 1e-6
HOTPLUG_RISE = 10e-9
HOTPLUG_TSTOP = 100e-6
# TVS families: peak pulse power (10/1000 us) and Vc / Vrwm at Ipp
TVS_FAMILY = (("SMAJ", 400.0), ("P4SMA", 400.0), ("SMBJ", 600.0),
              ("P6KE", 600.0), ("SMCJ", 1500.0), ("SMF", 200.0))
TVS_VC_X = 1.62
TVS_RE = re.compile(r"(SMAJ|P4SMA|SMBJ|P6KE|SMCJ|SMF)(\d+(?:\.\d+)?)", re.I)


def _vio(kind, sev, refs, net, msg, **extra):
    return checklib.violation(SCRIPT, sev, None, None, net, refs, msg,
                              SCRIPT, kind=kind, **extra)


# ------------------------------------------------------------- the board

def _clusters(model: dict, ohms: float) -> dict[str, str]:
    """net -> representative, joined through R <= ohms and inductors."""
    parent = {n: n for n in model["node"]}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for e in model["elems"]:
        if e["kind"] == "L" or (e["kind"] == "R" and e["value"] <= ohms):
            a, b = e["nets"]
            if a in model["grounds"] or b in model["grounds"]:
                continue
            parent[find(a)] = find(b)
    return {n: find(n) for n in parent}


def ground_caps(model: dict) -> dict[str, float]:
    out: dict[str, float] = {}
    for e in model["elems"]:
        if e["kind"] != "C":
            continue
        a, b = e["nets"]
        for x, y in ((a, b), (b, a)):
            if y in model["grounds"] and x not in model["grounds"]:
                out[x] = out.get(x, 0.0) + e["value"]
    return out


def fet_row(board: dict, parts: dict, ref: str, table: dict) -> dict | None:
    c = board["comps"][ref]
    mpn = (parts.get(ref) or {}).get("mpn") or ""
    return soa.find(mpn, c["value"], c["footprint"], table)


def find_switches(board, parts, rails, model, table) -> list[dict]:
    caps = ground_caps(model)
    down = _clusters(model, DOWNSTREAM_OHMS)
    out = []
    for e in model["elems"]:
        if e["kind"] != "Q" or e["type"] not in ("pmos", "nmos"):
            continue
        r = e["roles"]
        if e["type"] == "pmos":
            vin, vout = r["S"], r["D"]
        else:
            vin, vout = r["D"], r["S"]
        if vin not in rails or rails[vin][0] <= 0 or vout in model["grounds"]:
            continue
        bulk = sum(v for n, v in caps.items() if down.get(n) == down[vout])
        if vout not in rails and bulk < BULK_F:
            continue
        out.append({"ref": e["ref"], "type": e["type"], "in": vin,
                    "out": vout, "gate": r["G"], "bulk_f": bulk,
                    "row": fet_row(board, parts, e.get("comp", e["ref"]),
                                   table)})
    return out


def entry_net(board: dict, model: dict, switches: list[dict],
              forced: str | None) -> str | None:
    if forced:
        if forced not in model["node"]:
            raise checklib.CheckError(f"--entry {forced}: no such net")
        return forced
    for ref in sorted(board["comps"], key=sim_board._natural):
        c = board["comps"][ref]
        if c["kind"] not in ("J", "USB", "CN", "P"):
            continue
        for p in c["pins"].values():
            if any(f.startswith("VBUS") for f in p["fn"]):
                return p["net"]
    return switches[0]["in"] if switches else None


# ------------------------------------------------------------- the deck

def derated(e: dict, board: dict, parts: dict, volts: dict) -> float:
    a, b = e["nets"]
    if a not in volts or b not in volts:
        return e["value"]
    c = board["comps"][e["ref"]]
    part = parts.get(e["ref"]) or {}
    text = " ".join([part.get("description") or ""] +
                    list((part.get("attrs") or {}).values()))
    rated, _ = sim_board.cap_rating(c, parts.get(e["ref"]))
    return mlcc.c_eff(e["value"], abs(volts[a] - volts[b]),
                      footprint=c["footprint"], value=c["value"], text=text,
                      mpn=part.get("mpn", ""), rated_v=rated)["c_eff"]


def tvs_card(e: dict, board: dict, parts: dict) -> str | None:
    """A TVS card with the series resistance its clamping voltage implies:
    sim_board's card clamps hard at BV, which hides a hot-plug's overshoot.
    RS = (Vc - Vbr) / Ipp, Vc = 1.62 x Vrwm and Ipp = Ppk / Vc for the
    SMAJ/SMBJ/SMCJ-style families (SMAJ24A: Vbr 26.7, 38.9 V at 10.3 A)."""
    text = " ".join([board["comps"][e["ref"]]["value"],
                     (parts.get(e["ref"]) or {}).get("mpn") or ""])
    m = TVS_RE.search(text)
    if not m or not e.get("bv"):
        return None
    ppk = dict(TVS_FAMILY)[m.group(1).upper()]
    vc = TVS_VC_X * float(m.group(2))
    rs = max(vc - e["bv"], 0.0) / (ppk / vc)
    return f"D(IS=1e-12 N=1 BV={e['bv']:g} IBV=1m RS={rs:.3g})"


def build(board, parts, rails, model, switches, entry, vc, table, loads):
    """Everything but the scenario: element lines, models, which rails get
    ideal sources, the io/open-drain nets, and the per-net DC the caps are
    derated at."""
    node = model["node"]
    src_side = _clusters(model, SOURCE_SIDE_OHMS)
    down = _clusters(model, DOWNSTREAM_OHMS)
    side = {n for n in node if src_side[n] == src_side[entry]}
    outs = {n for n in node for s in switches if down[n] == down[s["out"]]}
    held = {n: v for n, (v, _s) in rails.items()
            if n not in side and n not in outs and node.get(n) != "0"}
    volts = {n: 0.0 for n in model["grounds"]}
    volts.update({n: v for n, v in held.items()})
    volts.update({n: vc for n in side | outs})
    sw = {s["ref"]: s for s in switches}
    lines, models, unknowns, cards = [], [], [], {}
    for e in model["elems"]:
        if e["kind"] == "C":
            a, b = (node[n] for n in e["nets"])
            c = derated(e, board, parts, volts)
            if c >= ESR_FROM_F:
                lines.append(f"C{e['ref']} {a} esr_{e['ref']} {c:.6g}")
                lines.append(f"RESR{e['ref']} esr_{e['ref']} {b} {ESR_OHMS:g}")
            else:
                lines.append(f"C{e['ref']} {a} {b} {c:.6g}")
            continue
        if e["kind"] == "Q" and e["type"] in ("pmos", "nmos"):
            row = fet_row(board, parts, e.get("comp", e["ref"]), table)
            r = e["roles"]
            d, g, s = node[r["D"]], node[r["G"]], node[r["S"]]
            if e["ref"] in sw:
                lines.append(f"VSW_{e['ref']} {d} swd_{e['ref']} 0")
                d = f"swd_{e['ref']}"
            name = f"Q_{e['ref']}".upper()
            if row and row.get("spice") and not row.get("generic"):
                cards[e["ref"]] = row["spice"]
                lines.append(f"M{e['ref']} {d} {g} {s} {name}")
                models.append(f".model {name} {row['spice']}")
            else:
                lines.append(f"M{e['ref']} {d} {g} {s} {s} {name}")
                models.append(e["model"])
                if e["ref"] in sw:
                    unknowns.append({"ref": e["ref"], "what": "FET model",
                                     "why": "not in reference/fet_power.yaml:"
                                            " a generic LEVEL 1 model with "
                                            "no capacitances"})
            continue
        if e.get("tran"):
            lines.append(e["tran"])
        if e["kind"] == "D" and e.get("type") == "tvs":
            card = tvs_card(e, board, parts)
            if card:
                models.append(f".model D_{e['ref']}".upper() + f" {card}")
                continue
            unknowns.append({"ref": e["ref"], "what": "TVS clamp",
                             "why": "family not read: clamps hard at BV "
                                    "(no series resistance)"})
        if e.get("model"):
            models.append(e["model"])
    for net, ohms in loads.items():
        lines.append(f"RLOAD_{node[net]} {node[net]} 0 {ohms:g}")
    io, od = sim_board.io_nets(board, rails, model)
    # an open-drain status pin is pulled low whatever its IC's supply (a PD
    # controller's PG, its IC fed from a net no rail names)
    for c in board["comps"].values():
        if c["kind"] not in ("U", "IC"):
            continue
        for pin in c["pins"].values():
            n = pin["net"]
            if n in rails or n in model["grounds"] or n in io:
                continue
            if any(sim_board.OPEN_DRAIN_FN.match(f) for f in pin["fn"]):
                io[n] = 0.0
                od.add(n)
    io = {n: v for n, v in io.items() if n not in side and n not in outs}
    return {"lines": lines, "models": models, "held": held, "side": side,
            "outs": outs, "io": io, "od": od, "unknowns": unknowns,
            "cards": cards, "volts": volts}


def deck(model, b, entry, sc, corner: tuple[str, float] | None = None,
         src: list[str] | None = None, tran: str | None = None) -> str:
    node = model["node"]
    out = [f"* sim_inrush {sc['name']}", ".options rshunt=1e9"]
    out += src if src is not None else \
        pdsource.source_lines(node[entry], sc)
    used = {t for ln in b["lines"] for t in ln.split()[1:]}
    for net, v in sorted(b["held"].items()):
        nd = node[net]
        if nd in used and v:
            out.append(f"VR_{nd} {nd} 0 DC {v:.6g}")
    for net, v in sorted(b["io"].items()):
        nd = node[net]
        if nd not in used:
            continue
        if net in b["od"]:
            out.append(f"VPGC_{nd} pgc_{nd} 0 "
                       f"{pdsource.drive_pwl(sc['pg'], 1.0)}")
            out.append(f"SPG_{nd} {nd} 0 pgc_{nd} 0 SW_OD")
        else:
            out.append(f"VIO_{nd} io_{nd} 0 {pdsource.drive_pwl(sc['fw'], v)}")
            out.append(f"RIO_{nd} io_{nd} {nd} 25")
    out += b["lines"]
    models = list(b["models"])
    if corner:
        ref, vth = corner
        name = f"Q_{ref}".upper()
        card = b["cards"][ref]
        sign = "-" if "pchan" in card.lower() else ""
        models = [m for m in models if not m.startswith(f".model {name} ")]
        models.append(f".model {name} " + VTO_RE.sub(f"Vto={sign}{vth:g}",
                                                    card))
    out += models
    out.append(".model SW_OD SW(Ron=25 Roff=1e9 Vt=0.5 Vh=0.1)")
    out.append(tran or f".tran 50u {sc['tstop']:.6g} 0 50u")
    out.append(".end")
    return "\n".join(out) + "\n"


# ------------------------------------------------------------- hot-plug

def pin_abs_max(board: dict, parts: dict, parts_dir: Path | None,
                nets: set[str]) -> tuple[list[dict], list[dict]]:
    """IC pins on `nets` with their abs max to ground: the part's datasheet
    extraction (<parts>/<LCSC>.json) first, then the abs_max_v of its
    reference/pin_ratings.yaml row. -> (rated [{ref, pin, fn, net, max_v,
    source}], unrated [{ref, pin, fn, net}])."""
    import check_ratings
    table = [r for r in sim_board.load_pin_ratings() if r.get("abs_max_v")]
    raw: dict[str, dict] = {}
    pj = parts_dir / "parts.json" if parts_dir else None
    if pj and pj.is_file():
        for p in checklib.load_json(pj, "parts.json").get("parts") or []:
            for r in p.get("refdes") or []:
                raw[r] = p
    rated, unrated = [], []
    for ref in sorted(board["comps"], key=sim_board._natural):
        c = board["comps"][ref]
        if c["kind"] not in ("U", "IC"):
            continue
        pins = {k: p for k, p in c["pins"].items() if p["net"] in nets}
        if not pins:
            continue
        lcsc = (raw.get(ref) or {}).get("lcsc")
        ext_r = {}
        ext = parts_dir / f"{lcsc}.json" if parts_dir and lcsc else None
        if ext and ext.is_file():
            ext_r = check_ratings.part_ratings(
                checklib.load_json(ext, "extraction"))
        names = [x for x in ((parts.get(ref) or {}).get("mpn"), c["value"])
                 if x]
        for pin, p in sorted(pins.items()):
            hit = None
            rows = [r for r in (ext_r.get(pin) or {}).get(
                ("voltage", "abs")) or [] if r["ref"] is None
                and isinstance(r["hi"], (int, float))]
            if rows:
                r = min(rows, key=lambda r: r["hi"])
                hit = (float(r["hi"]), f"{lcsc}.json {r['src']}")
            else:
                fns = sim_board._pin_fns(p)
                r = next((r for r in table
                          if any(r["_m"].search(x) for x in names)
                          and any(r["_p"].match(f) for f in fns)), None)
                if r:
                    hit = (float(r["abs_max_v"]), "reference/pin_ratings."
                           f"yaml: {str(r.get('source', '')).strip()}")
            item = {"ref": ref, "pin": pin, "fn": "/".join(p["fn"]),
                    "net": p["net"]}
            if hit:
                rated.append(item | {"max_v": hit[0], "source": hit[1]})
            else:
                unrated.append(item)
    return rated, unrated


def hotplug_src(node: str, vc: float, ind: float) -> list[str]:
    t1 = HOTPLUG_T0 + HOTPLUG_RISE
    return [f"VHP hp_src 0 PWL(0 0 {HOTPLUG_T0:g} 0 {t1:g} {vc:.6g})",
            f"RHP hp_src hp_r {HOTPLUG_R:g}",
            f"LHP hp_r {node} {ind:g}"]


def hotplug(model, b, entry, vc, dll, timeout) -> list[dict]:
    """The source at vc closed onto the entry through each cable L: the
    peak on every source-side net and the peak current."""
    node = model["node"]
    sc = {"name": "hotplug", "fw": [(0.0, 0)], "pg": [(0.0, 0)]}
    tran = f".tran 5n {HOTPLUG_TSTOP:g} 0 10n"
    decks = [deck(model, b, entry, dict(sc, name=f"hotplug_{ind * 1e6:g}uH"),
                  src=hotplug_src(node[entry], vc, ind), tran=tran)
             for ind in HOTPLUG_L]
    plots, _v = sim_board.run_engine(decks, dll, timeout)
    out = []
    for ind, plot in zip(HOTPLUG_L, plots):
        peaks = {}
        for net in sorted(b["side"]):
            v = _series(plot, node[net])
            if v:
                peaks[net] = max(v)
        out.append({"scenario": f"hotplug_{ind * 1e6:g}uH", "cable_l_h": ind,
                    "peak_v": peaks, "src_peak_a": max(
                        (-x for x in plot.get("vhp#branch") or []),
                        default=0.0)})
    return out


def judge_hotplug(runs, rated, vc, vios):
    """One finding per IC and net: its lowest-rated pin there."""
    by: dict[tuple, dict] = {}
    for p in rated:
        k = (p["ref"], p["net"])
        q = by.get(k)
        fns = sorted(set((q["fn"].split("/") if q else []) +
                         p["fn"].split("/")))
        if q is None or p["max_v"] < q["max_v"]:
            q = dict(p)
        by[k] = q | {"fn": "/".join(fns)}
    for p in by.values():
        worst = max(runs, key=lambda r: r["peak_v"].get(p["net"], 0.0))
        v = worst["peak_v"].get(p["net"])
        if v is None or v <= p["max_v"]:
            continue
        vios.append(_vio(
            "hotplug_overvoltage", "error", [p["ref"]], p["net"],
            f"{worst['scenario']}: a {vc:g} V source plugged in through "
            f"{worst['cable_l_h'] * 1e6:g} uH of cable rings {p['net']} to "
            f"{v:.3g} V, over {p['ref']} {p['fn']}'s {p['max_v']:g} V abs "
            f"max ({p['source']}) - damp the entry (an RC snubber or "
            "electrolytic across it, a TVS that clamps below the abs max) "
            "or add an inrush-limited switch", scenario=worst["scenario"],
            peak_v=v, abs_max_v=p["max_v"], pin=p["pin"]))


# ------------------------------------------------------------- judging

def _series(plot: dict, nd: str) -> list[float] | None:
    if nd == "0":
        return None
    return plot.get(nd.lower())


def measure(plot, model, b, sc, switches, contract_a, ta) -> dict:
    t = plot.get("time") or []
    i_src = [-x for x in plot.get("vpd#branch") or []]
    k0 = next((k for k, x in enumerate(t) if x >= sc["judge_from"]), 0)
    tw, iw = t[k0:], i_src[k0:]
    above = sum(tw[k + 1] - tw[k] for k in range(len(tw) - 1)
                if iw[k] > contract_a and iw[k + 1] > contract_a)
    res = {"scenario": sc["name"], "src_peak_a": max(iw) if iw else 0.0,
           "t_above_contract_s": above, "switches": {}}
    node = model["node"]
    for s in switches:
        vin = _series(plot, node[s["in"]]) or [0.0] * len(t)
        vout = _series(plot, node[s["out"]]) or [0.0] * len(t)
        vg = _series(plot, node[s["gate"]]) or [0.0] * len(t)
        ids = [abs(x) for x in plot.get(f"vsw_{s['ref'].lower()}#branch")
               or [0.0] * len(t)]
        vsrc = vin if s["type"] == "pmos" else vout
        ps = [abs(a - c) * i for a, c, i in zip(vin, vout, ids)]
        row = {"out_peak_v": max(vout[k0:]) if vout[k0:] else 0.0,
               "out_final_v": vout[-1] if vout else 0.0,
               "i_peak_a": max(ids[k0:]) if ids[k0:] else 0.0,
               "vgs_peak_v": max((abs(g - x) for g, x in
                                  zip(vg[k0:], vsrc[k0:])), default=0.0)}
        if s["row"]:
            j = soa.junction(s["row"], t[k0:], ps[k0:], ta,
                             vds=[abs(a - c) for a, c in
                                  zip(vin[k0:], vout[k0:])])
            row.update(p_peak_w=j["p_peak_w"], energy_j=j["energy_j"],
                       tj_peak_c=j["tj_peak_c"], width_s=j["width_s"])
        else:
            pk = max(ps[k0:]) if ps[k0:] else 0.0
            row.update(p_peak_w=pk, energy_j=sum(
                0.5 * (ps[k] + ps[k + 1]) * (t[k + 1] - t[k])
                for k in range(k0, len(t) - 1)))
        if not sc["fw_off"] and vout:
            fin = row["out_final_v"]
            t10 = next((x for x, v in zip(t[k0:], vout[k0:])
                        if v >= 0.1 * fin), None)
            t90 = next((x for x, v in zip(t[k0:], vout[k0:])
                        if v >= 0.9 * fin), None)
            row["rise_10_90_s"] = (t90 - t10) if t10 is not None and \
                t90 is not None and fin > 1 else None
        res["switches"][s["ref"]] = row
    return res


def judge(results, switches, contract_v, contract_a, vios):
    for r in results:
        name = r["scenario"]
        pk, above = r["src_peak_a"], r["t_above_contract_s"]
        if above > pdsource.OCP_S or pk > pdsource.OCP_PEAK_X * contract_a:
            vios.append(_vio(
                "pd_overcurrent", "error", sorted(r["switches"]), None,
                f"{name}: the source delivers {pk:.3g} A peak, above the "
                f"{contract_a:g} A contract for {above * 1e3:.3g} ms - a "
                "source's OCP trips here and hard-resets", scenario=name,
                src_peak_a=pk, t_above_s=above))
        elif pk > contract_a:
            vios.append(_vio(
                "pd_overcurrent", "warning", sorted(r["switches"]), None,
                f"{name}: the source delivers {pk:.3g} A peak, briefly above "
                f"the {contract_a:g} A contract ({above * 1e3:.3g} ms)",
                scenario=name, src_peak_a=pk, t_above_s=above))
        for s in switches:
            m = r["switches"][s["ref"]]
            if r.get("fw_off"):
                v = m["out_peak_v"]
                if v > 0.5 * contract_v or v > FW_OFF_WARN_V:
                    sev = "error" if v > 0.5 * contract_v else "warning"
                    vios.append(_vio(
                        "switch_on_fw_off", sev, [s["ref"]], s["out"],
                        f"{name}: {s['ref']} passes current with firmware "
                        f"off: {s['out']} rises to {v:.3g} V "
                        f"({m['i_peak_a']:.3g} A peak through it)",
                        scenario=name, out_peak_v=v, i_peak_a=m["i_peak_a"]))
            row = s["row"]
            if row and "tj_peak_c" in m:
                sev, why = soa.judge(row, {"tj_peak_c": m["tj_peak_c"]})
                if sev:
                    vios.append(_vio(
                        "fet_soa", sev, [s["ref"]], s["out"],
                        f"{name}: {s['ref']} {why} ({m['p_peak_w']:.3g} W "
                        f"peak, {m['energy_j']:.3g} J"
                        f"{', generic package data' if row.get('generic') else ''})",
                        scenario=name, tj_peak_c=m["tj_peak_c"],
                        p_peak_w=m["p_peak_w"], energy_j=m["energy_j"]))
            vmax = (row or {}).get("vgs_max")
            if vmax and m["vgs_peak_v"] > vmax:
                vios.append(_vio(
                    "fet_vgs_transient", "error", [s["ref"]], s["gate"],
                    f"{name}: {s['ref']} |Vgs| reaches {m['vgs_peak_v']:.3g} V"
                    f", over its {vmax:g} V rating", scenario=name,
                    vgs_peak_v=m["vgs_peak_v"]))


def run(a) -> dict:
    inp = sim_board.resolve_inputs(a)
    dll = env.find_ngspice_dll()
    if dll is None:
        raise checklib.CheckError(
            "no ngspice shared library found - install KiCad 10 with its "
            "simulator or set HWDE_NGSPICE_DLL")
    board = sim_board.load_board(inp["net"])
    parts = sim_board.load_parts(inp["parts"], board)
    cons = checklib.load_json(inp["constraints"], "constraints") \
        if inp["constraints"] else {}
    rails = sim_board.resolve_rails(board, parts, cons, inp["power_tree"],
                                    sim_board._pairs(a.rail, "--rail"))
    model = sim_board.build_model(board, parts, rails)
    table = soa.load()
    switches = find_switches(board, parts, rails, model, table)
    entry = entry_net(board, model, switches, a.entry)
    if entry is None:
        return checklib.report(SCRIPT, inp["net"], [], switches=[],
                               note="no load switch with bulk and no "
                                    "connector VBUS entry found")
    pd = (cons or {}).get("pd_source") or {}
    vc = a.contract_v or pd.get("contract_v") or \
        max((v for n, (v, _s) in rails.items()
             if n in {s["in"] for s in switches} or n == entry),
            default=None)
    if vc is None:
        return checklib.report(SCRIPT, inp["net"], [], switches=[],
                               entry=entry, note=f"{entry} has no voltage: "
                               "give --contract-v or a --rail")
    ia = a.contract_a or pd.get("contract_a") or DEFAULT_CONTRACT_A
    loads = sim_board._pairs(a.load, "--load")
    for n in loads:
        if n not in model["node"]:
            raise checklib.CheckError(f"--load {n}: no such net")
    b = build(board, parts, rails, model, switches, entry, vc, table, loads)
    vios: list[dict] = []
    hp, rated, unrated = [], [], []
    if not a.no_hotplug:
        rated, unrated = pin_abs_max(board, parts, inp["parts"],
                                     b["side"])
        hp = hotplug(model, b, entry, vc, dll, a.timeout)
        judge_hotplug(hp, rated, vc, vios)
    hp_facts = {"source_v": vc, "cable_r_ohm": HOTPLUG_R, "runs": [
        {"scenario": r["scenario"], "cable_l_h": r["cable_l_h"],
         "src_peak_a": checklib.rnd(r["src_peak_a"], 4),
         "peak_v": {n: checklib.rnd(v, 4) for n, v in r["peak_v"].items()}}
        for r in hp], "pins_judged": rated} if hp else None
    if not switches:
        unknowns = list(b["unknowns"]) + [
            {"ref": u["ref"], "what": "pin abs max", "why":
             f"{u['fn']} on {u['net']}: no extraction or "
             "reference/pin_ratings.yaml row, hot-plug peak not judged"}
            for u in unrated]
        return checklib.report(
            SCRIPT, inp["net"], vios, switches=[], entry=entry,
            source_side=sorted(b["side"]), hotplug=hp_facts,
            note="no load switch with bulk found: hot-plug only",
            known_unknowns=unknowns)
    slews = tuple(float(x) for x in a.slews.split(","))
    scs = pdsource.scenarios(vc, slews, a.window_ms * 1e-3)
    runs = [(sc, None) for sc in scs]
    fast = next((sc for sc in scs if sc["name"] == f"step_{max(slews):g}"),
                None)
    for s in switches:
        vth = (s["row"] or {}).get("vth")
        if fast and s["ref"] in b["cards"] and vth:
            for v in (vth[0], vth[-1]):
                runs.append((dict(fast, name=f"{fast['name']}_{s['ref']}"
                                  f"_vth{v:g}"), (s["ref"], v)))
    decks = [deck(model, b, entry, sc, corner) for sc, corner in runs]
    plots, version = sim_board.run_engine(decks, dll, a.timeout)
    results = []
    for (sc, _c), plot in zip(runs, plots):
        m = measure(plot, model, b, sc, switches, ia, a.ta)
        m["fw_off"] = sc["fw_off"]
        results.append(m)
    judge(results, switches, vc, ia, vios)
    caps = ground_caps(model)
    side_c = sum(derated(e, board, parts, b["volts"]) for e in model["elems"]
                 if e["kind"] == "C" and set(e["nets"]) & b["side"]
                 and set(e["nets"]) & model["grounds"])
    if side_c > CSNKBULKPD_F:
        vios.append(_vio("sink_bulk_over_pd", "error", [], entry,
                         f"{sim_board._f(side_c, 'F')} effective on the "
                         f"source side of the switches, over USB PD's "
                         f"cSnkBulkPd {sim_board._f(CSNKBULKPD_F, 'F')}",
                         c_f=side_c))
    unknowns = list(b["unknowns"]) + [
        {"ref": u["ref"], "what": "pin abs max", "why":
         f"{u['fn']} on {u['net']}: no extraction or "
         "reference/pin_ratings.yaml row, hot-plug peak not judged"}
        for u in unrated]
    for s in switches:
        if s["row"] is None:
            unknowns.append({"ref": s["ref"], "what": "thermal data",
                             "why": "no fet_power.yaml row or generic "
                                    "package: no Tj judged"})
        elif s["row"].get("generic"):
            unknowns.append({"ref": s["ref"], "what": "thermal data",
                             "why": f"generic {s['row']['match']} row"})
        elif not s["row"].get("verified"):
            unknowns.append({"ref": s["ref"], "what": "thermal data",
                             "why": "fet_power.yaml row not verified "
                                    "against the datasheet"})
    ics = sorted(u["ref"] for u in model["unmodelled"]
                 if u["why"].startswith("IC/"))
    if ics and not loads:
        unknowns.append({"ref": None, "what": "IC loads", "why":
                         "ICs are open: no regulator or amplifier input "
                         "current is drawn (add --load NET=ohms)"})
    rnd = checklib.rnd
    return checklib.report(
        SCRIPT, inp["net"], vios,
        engine={"dll": str(dll), "ngspice_version": version},
        entry=entry, source_side=sorted(b["side"]), hotplug=hp_facts,
        contract={"v": vc, "a": ia, "source": "--contract" if a.contract_v
                  or a.contract_a else "constraints pd_source" if pd else
                  "entry rail voltage, 3 A default"},
        source_side_c_eff_f=rnd(side_c, 9),
        csnkbulk_over_10uf=side_c > CSNKBULK_F,
        switches=[{"ref": s["ref"], "type": s["type"], "in": s["in"],
                   "out": s["out"], "gate": s["gate"],
                   "bulk_nom_f": rnd(s["bulk_f"], 9),
                   "fet_data": (s["row"] or {}).get("source"),
                   "model": "fet_power.yaml" if s["ref"] in b["cards"]
                   else "generic"} for s in switches],
        held_rails=sorted(b["held"]),
        scenarios=[{k: (rnd(v, 6) if isinstance(v, float) else v)
                    for k, v in r.items() if k != "switches"} |
                   {"switches": {ref: {k: rnd(v, 6) if isinstance(v, float)
                                       else v for k, v in m.items()}
                                 for ref, m in r["switches"].items()}}
                   for r in results],
        caps_seen=len(caps), known_unknowns=unknowns)


def main(argv: list[str] | None = None) -> int:
    checklib.utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workspace")
    ap.add_argument("--net")
    ap.add_argument("--constraints")
    ap.add_argument("--parts")
    ap.add_argument("--power-tree")
    ap.add_argument("--rail", action="append", default=[])
    ap.add_argument("--entry", help="the net the PD source drives")
    ap.add_argument("--contract-v", type=float)
    ap.add_argument("--contract-a", type=float)
    ap.add_argument("--load", action="append", default=[],
                    help="NET=ohms, a resistive load (a regulator's input)")
    ap.add_argument("--ta", type=float, default=25.0)
    ap.add_argument("--slews", default=",".join(
        f"{s:g}" for s in pdsource.STEP_SLEWS))
    ap.add_argument("--window-ms", type=float,
                    default=pdsource.WINDOW * 1e3)
    ap.add_argument("--no-hotplug", action="store_true",
                    help="skip the hot-plug runs (source closed onto the "
                         "entry through the cable)")
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    return checklib.cli_wrap(SCRIPT, lambda: (run(a), a.out))


if __name__ == "__main__":
    sys.exit(main())
