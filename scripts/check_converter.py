"""check_converter.py - a buck's or boost's inductor and switch against its
IC's current limits and minimum on-time (P8 verify suite; #355).

Each inductor with one end on a switch node that one switcher IC drives is a
converter. check_current's ripple readers (#798, #816) give its input and
output voltages, L and fsw, from the rail voltages below, the inductor's
Value, and reference/converter_fsw.yaml or a block's fsw_khz. The load is
the output rail's power entry current_a.

Rail voltages (#1433), strongest first; each record's vin_source and
vout_source say which, and vin_inferred / vout_inferred say how:
  declared  constraints `voltages`.
  divider   the switcher's plain two-resistor FB divider, both resistors read
            from the board: Vout = Vref * (1 + Rtop / Rbottom), Vref from a
            cited reference/regulator_vref.yaml row (check_ratings.
            fb_divider), with the parts and the window over Vref and
            resistor tolerance. It sets the divider's top net and the nets
            tied to it by net ties and 0 ohm parts.
  net name  a name like 3V3 or +12V (check_ratings.volts_from_name): weaker,
            so an error that rests on it is a warning and says so.
A net whose name disagrees with its divider (outside the window and more
than 5 % off the nominal) gets no voltage, and a converter on it is not
judged, with both readings. A converter whose input has none of the three is
not judged and names the input pin's net. Then:

  buck   dI = (Vin - Vout) * Vout / (Vin * fsw * L)        TI SLVA477C eq. 1, 2
         Ipeak = Iout + dI / 2                             SLVA477C eq. 4
         Iout max = Ilim(min) - dI / 2                     SLVA477C eq. 3
         ton = Vout / (Vin * fsw)
  boost  D = 1 - Vin / Vout (lossless; losses raise D a little, so Ipeak
         here is a lower bound)
         dI = Vin * D / (fsw * L)                          TI SLVA372D eq. 2
         Ipeak = dI / 2 + Iout / (1 - D)                   SLVA372D eq. 4
         Iout max = (Ilim(min) - dI / 2) * (1 - D)         SLVA372D eq. 3
         with only a valley limit (a valley-mode boost):
         Iout max = (Ivalley(min) + dI / 2) * (1 - D)      TI SLVSGK6B eq. 1
         RHP zero = Vout * (1 - D)^2 / (2 pi L Iout)

Findings, all on the inductor and its IC:
  converter_isat_low      error: the inductor's stated saturation current
                          (block operating_point inductor_isat_a) is below
                          the peak current at the stated load.
  converter_overload      error: the stated load is more than the IC's
                          minimum switch current limit allows: the peak
                          limit, less half the ripple (and, for a buck, the
                          valley limit plus half the ripple); with a valley
                          limit only, the valley limit plus half the ripple.
  converter_isat_below_limit
                          warning: Isat is below the current limit the
                          datasheet names for it (reference/converter_
                          limits.yaml isat_floor), else below the peak
                          limit's typical. A short drives the switch to its
                          limit. Part-dependent, so never an error.
  converter_min_on_time   warning: a buck's on-time at its highest input
                          (operating_point vin_max_v, else the stated input)
                          is under the IC's minimum: it folds back fsw or
                          skips pulses.
The ripple ratio, dI against the datasheet's band of the IC's rated current
(or SLVA477C's 20 to 40 % of the load, eq. 6, with no row), goes in each
converter's record (ripple_ratio, ripple_in_band) and is not a finding: it is
design guidance, and a ratio outside it is not a fault by itself. A boost's
right-half-plane zero goes there too.

The block for a converter is the buck or boost block whose vout_v is the
output's voltage (operating_point "inductor" names it when two share one).
A check that needs an input the board doesn't state goes to not_judged and
names it; a pass that needs vin_max_v is not judged without it. A row of
converter_limits.yaml or converter_fsw.yaml not `verified: true` sends its
findings to not_judged.

CLI: --pcb board.kicad_pcb [--constraints c.json] [--table limits.yaml]
     [--out report.json]; exit 0 pass / 1 violations / 2 error (SPEC 6).
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "lib"))
import checklib  # noqa: E402
import geom  # noqa: E402
import check_current as cc  # noqa: E402
import check_ratings as cr  # noqa: E402
import engcalc  # noqa: E402

SCRIPT = "check_converter"
SOURCE = SCRIPT
TABLE = HERE.parent / "reference" / "converter_limits.yaml"
BUCK = "TI SLVA477C"
BOOST = "TI SLVA372D"
GENERIC_BAND = (0.2, 0.4)          # SLVA477C eq. 6, of the load
DECLARED, DIVIDER, NAME = "declared", "divider", "net name"
NAME_TOL = 0.05     # a net name's nominal against a divider's (rail_volts)


def load_table(path: Path = TABLE) -> list[dict]:
    try:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise checklib.CheckError(f"converter limits table {path}: {exc}") from exc
    return [dict(r, rx=re.compile(r["match"], re.I))
            for r in doc.get("regulators") or []]


def _num(op: dict, key: str) -> float | None:
    v = op.get(key)
    return float(v) if isinstance(v, (int, float)) and not isinstance(
        v, bool) else None


def _block(blocks: list, topology: str, vout: float, ind: str):
    """The operating point of the block for this converter, {} with none."""
    found = [b.get("operating_point") or {} for b in blocks
             if topology in str(b.get("topology", ""))
             and _num(b.get("operating_point") or {}, "vout_v") == vout]
    named = [op for op in found if op.get("inductor") == ind]
    if named:
        return named[0]
    return found[0] if len(found) == 1 else {}


def _inductor_nodes(bg: geom.BoardGeom):
    """(inductor, switch node, rail) for each 2-net inductor with one end on
    a switch node."""
    for ind in sorted(r for r in bg.footprints if re.fullmatch(r"L\d+", r)):
        nets = sorted({p.net for p in bg.pads_of(ref=ind) if p.net})
        nodes = [n for n in nets if cc._switchers_on(bg, n)]
        if len(nodes) != 1 or len(nets) != 2:
            continue
        yield ind, nodes[0], next(n for n in nets if n != nodes[0])


def _fb_names(part: dict) -> dict:
    """pad -> pin name ('~{FB}' -> 'FB', 'FB_3' on pad 3 -> 'FB')."""
    out = {}
    for pad, fn in (part.get("fns") or {}).items():
        name = str(fn).strip()
        if name.endswith(f"_{pad}"):
            name = name[: -len(pad) - 1]
        out[pad] = re.sub(r"~\{([^}]*)\}", r"\1", name)
    return out


def _tied_nets(board: list[dict]) -> dict[str, list[str]]:
    """net -> every net joined to it by net ties and fitted 0 ohm parts, as
    check_ratings._fb_network merges them: a divider on one sets them all."""
    parent: dict[str, str] = {}

    def find(n: str) -> str:
        while parent.get(n, n) != n:
            n = parent[n]
        return n

    for r in board:
        nets = sorted(set((r.get("pads") or {}).values()))
        if len(nets) != 2 or cr._is_dnp(r):
            continue
        kind = cr._fb_kind(r)
        if kind == "tie" or (kind == "res" and
                             engcalc.parse_value(r["value"]) == 0 and
                             re.match(r"^\s*0", str(r["value"]))):
            a, c = find(nets[0]), find(nets[1])
            if a != c:
                parent[max(a, c)] = min(a, c)
    groups: dict[str, list[str]] = {}
    for n in list(parent) + list(parent.values()):
        groups.setdefault(find(n), [])
        if n not in groups[find(n)]:
            groups[find(n)].append(n)
    return {n: sorted(g) for g in groups.values() for n in g}


def _name_agrees(v: float, nom: float, lo: float, hi: float) -> bool:
    """A net name is a nominal label: it agrees with a divider when it is
    inside the divider's window or within NAME_TOL of its nominal."""
    return lo - cr.EPS <= v <= hi + cr.EPS or abs(v - nom) <= NAME_TOL * nom


def rail_volts(bg: geom.BoardGeom, cons: dict):
    """(volts, basis, conflicts) for every net: the declared voltage
    (constraints `voltages`), else the voltage a switcher's plain two-resistor
    FB divider sets (Vref from a cited reference/regulator_vref.yaml row times
    1 + Rtop/Rbottom, check_ratings.fb_divider), else the one its name gives
    (check_ratings.volts_from_name). basis[net] says which, with the parts.
    A net whose name and divider disagree gets no voltage: conflicts[net]
    says why."""
    volts: dict[str, float] = {}
    basis: dict[str, dict] = {}
    conflicts: dict[str, str] = {}
    for net, v in cc._declared_volts(cons).items():
        volts[net] = v
        basis[net] = {"source": DECLARED, "how": "constraints voltages"}
    nets = {p.net for p in bg.pads_of() if p.net}
    named = {n: v for n in nets if (v := cr.volts_from_name(n)) is not None}
    ics = sorted({ic for _, node, _ in _inductor_nodes(bg)
                  for ic in cc._switchers_on(bg, node)})
    if ics:
        board = [b for b in cr.load_parts_on_board(bg.path) if b["in_bom"]]
        vsrc = {n: "net name" for n in named}
        vsrc.update({n: "constraints" for n in volts})
        ctx = {"board": board, "bom_by_lcsc": {}, "volts": {**named, **volts},
               "vsrc": vsrc, "blocks": {}, "vrefs": cr.load_vrefs(),
               "not_judged": []}
        by_ref = {b["ref"]: b for b in board}
        same = _tied_nets(board)
        for ic in ics:
            b = by_ref.get(ic)
            if b is None:
                continue
            texts = [t for t in (b.get("value"), b.get("mpn")) if t]
            mpn = b.get("mpn") or b.get("value") or ""
            fb = cr.fb_pins_unextracted(b, _fb_names(b), mpn)
            div = cr.fb_divider(b, fb, ctx) if fb else None
            vr = cr.find_vref(texts, ctx["vrefs"])
            if not div or not div.get("out") or not div.get("plain") \
                    or div.get("pots") or vr is None:
                continue
            vref, vtol, src, verified = vr
            nom = vref * div["ratio_nom"]
            lo = vref * (1 - vtol) * div["ratio_lo"]
            hi = vref * (1 + vtol) * div["ratio_hi"]
            how = (f"inferred from {ic}'s divider {div['how']}: Vref "
                   f"{vref:g} V x (1 + Rtop/Rbottom) = {nom:.3g} V "
                   f"({lo:.3g}-{hi:.3g} V; Vref from {src.split(',')[0]}"
                   + ("" if verified else "; unverified") + ")")
            for net in dict.fromkeys(n for n in (div["out"], div.get("rail"),
                                                 *same.get(div["out"], ()))
                                     if n):
                if net in volts and basis[net]["source"] == DECLARED:
                    continue    # check_ratings judges a declared rail (#832)
                if net in named and not _name_agrees(named[net], nom, lo, hi):
                    conflicts[net] = (
                        f"{net}'s name says {named[net]:g} V but {ic}'s "
                        f"divider {div['how']} sets {nom:.3g} V ({lo:.3g}-"
                        f"{hi:.3g} V at Vref {vref:g} V), so its voltage is "
                        f"unknown")
                    volts.pop(net, None)
                    basis.pop(net, None)
                    continue
                if net in conflicts:
                    continue
                if net in basis and basis[net]["source"] == DIVIDER:
                    if abs(volts[net] - nom) > NAME_TOL * nom:
                        conflicts[net] = (f"two dividers set {net}: "
                                          f"{basis[net]['how']}; {how}")
                        volts.pop(net)
                        basis.pop(net)
                    continue
                volts[net] = nom
                basis[net] = {"source": DIVIDER, "how": how,
                              "parts": div["top"], "ic": ic,
                              "vref_verified": verified,
                              "window_v": [checklib.rnd(lo, 4),
                                           checklib.rnd(hi, 4)]}
    for net, v in named.items():
        if net not in volts and net not in conflicts:
            volts[net] = v
            basis[net] = {"source": NAME,
                          "how": f"read from the net name {net} only"}
    return volts, basis, conflicts


VIN_PIN = re.compile(r"[PA]?V?IN\d?", re.I)


def _pin(pad) -> str:
    """A pad's pin name with KiCad overbars dropped ('~{VIN}' -> 'VIN')."""
    return re.sub(r"~\{([^}]*)\}", r"\1", str(pad.pinfunction or "")).strip()


def _no_volts(net: str, conflicts: dict) -> str:
    return conflicts.get(net) or (
        f"{net} has no voltage: none declared, no feedback divider sets it, "
        f"and its name gives none")


def converters(bg: geom.BoardGeom, cons: dict):
    """(inductor, node, rail, reading) for each inductor on a switch node,
    the reading a dict of the converter's inputs or a string naming what
    can't be read. Each reading's vin_basis and vout_basis say where its
    voltages came from (rail_volts)."""
    volts, basis, conflicts = rail_volts(bg, cons)
    blocks = cons.get("blocks") or []
    out = []
    for ind, node, rail in _inductor_nodes(bg):
        if rail not in volts:
            out.append((ind, node, rail, _no_volts(rail, conflicts)))
            continue
        outs = cc._boost_rails(bg, rail, node, volts)
        if len(outs) > 1:
            out.append((ind, node, rail, f"{node} lifts {rail} to {len(outs)} "
                                         f"rails; can't tell its output"))
        elif outs:
            vin, vout = volts[rail], volts[outs[0]]
            got = cc._boost_inputs(bg, ind, node, vin, vout,
                                   _blocks_at(blocks, vout, basis[outs[0]]))
            if isinstance(got, str):
                out.append((ind, node, rail, got))
                continue
            ic, henries, fsw, where = got
            out.append((ind, node, rail, {
                "topology": "boost", "ic": ic, "L_h": henries, "fsw_hz": fsw,
                "fsw_source": where, "vin_v": vin, "vout_v": vout,
                "out_net": outs[0], "vin_net": rail,
                "vin_basis": basis[rail], "vout_basis": basis[outs[0]]}))
        else:
            term = cc._buck_output(bg, rail, ind, node, volts,
                                   _blocks_at(blocks, volts[rail],
                                              basis[rail]))
            if isinstance(term, str):
                why = [conflicts[p.net] for ic in cc._switchers_on(bg, node)
                       for p in bg.pads_of(ref=ic) if p.net in conflicts]
                if "rails above" in term:
                    # name the IC's input pin net that has no voltage
                    why += [f"{ic}'s {_pin(p)} pin: "
                            + _no_volts(p.net, conflicts)
                            for ic in cc._switchers_on(bg, node)
                            for p in bg.pads_of(ref=ic)
                            if p.net and p.net not in volts
                            and VIN_PIN.fullmatch(_pin(p))]
                out.append((ind, node, rail, "; ".join(
                    [term] + list(dict.fromkeys(why)))))
                continue
            got = dict(term["inputs"])
            vin_net = next(p.net for p in bg.pads_of(ref=term["from"])
                           if p.net and p.net != node
                           and volts.get(p.net) == got["vin_v"])
            out.append((ind, node, rail, {
                "topology": "buck", "ic": term["from"], "L_h": got["L_h"],
                "fsw_hz": got["fsw_hz"], "fsw_source": got["fsw_source"],
                "vin_v": got["vin_v"], "vout_v": got["vout_v"],
                "out_net": rail, "vin_net": vin_net,
                "vin_basis": basis[vin_net], "vout_basis": basis[rail]}))
    return out


def _blocks_at(blocks: list, vout: float, basis: dict) -> list:
    """The blocks, with an inferred output's vout_v snapped to it: a block
    whose vout_v sits within NAME_TOL of a voltage the board doesn't declare
    is that output's block."""
    if basis["source"] == DECLARED:
        return blocks
    out = []
    for b in blocks:
        op = b.get("operating_point") or {}
        v = _num(op, "vout_v")
        if v is not None and v != vout and abs(v - vout) <= NAME_TOL * vout:
            b = dict(b, operating_point=dict(op, vout_v=vout))
        out.append(b)
    return out


def _row(bg: geom.BoardGeom, ic: str, rows: list[dict]) -> dict | None:
    return cc._fsw_row(bg, ic, rows)


def _fmt(a: float) -> str:
    return f"{a:.3g} A"


def judge(bg: geom.BoardGeom, cons: dict | None, rows: list[dict]):
    """(violations, records, not_judged) over every converter on the board."""
    cons = cons or {}
    blocks = cons.get("blocks") or []
    power = {e.get("net"): e.get("current_a") for e in cons.get("power") or []}
    fsw_unverified = {r["match"] for r in cc.fsw_rows()
                      if r.get("verified") is not True}
    viols, records, not_judged = [], [], []
    for ind, node, rail, rd in converters(bg, cons):
        if isinstance(rd, str):
            not_judged.append(f"{ind} on {node}: {rd}")
            continue
        ic, topo = rd["ic"], rd["topology"]
        vin, vout, henries, fsw = rd["vin_v"], rd["vout_v"], rd["L_h"], rd["fsw_hz"]
        op = _block(_blocks_at(blocks, vout, rd["vout_basis"]), topo, vout,
                    ind)
        row = _row(bg, ic, rows)
        unverified = [r for r in ([row] if row else []) if r.get("verified") is not True]
        fsw_row = cc._fsw_row(bg, ic, cc.fsw_rows())
        if fsw_row and fsw_row["match"] in fsw_unverified:
            unverified.append(fsw_row)
        if topo == "buck":
            duty = vout / vin
            ripple = (vin - vout) * vout / (vin * fsw * henries)
            ref = BUCK
        else:
            duty = 1 - vin / vout
            ripple = vin * duty / (fsw * henries)
            ref = BOOST
        vb, ob = rd["vin_basis"], rd["vout_basis"]
        rec = {"inductor": ind, "ic": ic, "topology": topo, "node": node,
               "vin_v": checklib.rnd(vin, 4), "vout_v": checklib.rnd(vout, 4),
               "vin_net": rd["vin_net"], "vout_net": rd["out_net"],
               "vin_source": vb["source"], "vout_source": ob["source"],
               "L_h": henries, "fsw_hz": fsw,
               "fsw_source": rd["fsw_source"], "duty": checklib.rnd(duty, 4),
               "ripple_a": checklib.rnd(ripple, 4),
               "limits_row": row["mpn"] if row else None}
        for side, bs in (("vin", vb), ("vout", ob)):
            if bs["source"] != DECLARED:
                rec[f"{side}_inferred"] = {k: v for k, v in bs.items()
                                           if k != "source"}
        records.append(rec)
        where = (f"{ind} ({topo}, {ic}, {vin:.3g} V to {vout:.3g} V, "
                 f"{henries * 1e6:.3g} uH at {fsw / 1e3:.4g} kHz)")
        # where an undeclared voltage came from, said on every finding
        said = "".join(
            f"; {lab} {v:.3g} V on {net} {bs['how']}"
            for lab, v, net, bs in (("Vin", vin, rd["vin_net"], vb),
                                    ("Vout", vout, rd["out_net"], ob))
            if bs["source"] != DECLARED)
        weak = [net for net, bs in ((rd["vin_net"], vb), (rd["out_net"], ob))
                if bs["source"] == NAME]
        found, missing = [], []

        def add(kind, sev, msg, **extra):
            if weak and sev == "error":
                # a name is weaker evidence than a declared or divider volt
                sev = "warning"
                msg += (f" (a warning, not an error: it rests on "
                        f"{' and '.join(weak)} read from the net name only)")
            found.append(checklib.violation(
                SCRIPT, sev, None, None, node, [ind, ic],
                f"{where}: {msg}{said}", SOURCE, kind=kind,
                voltage_basis={"vin": vb["source"], "vout": ob["source"]},
                **extra))

        iout = power.get(rd["out_net"])
        iout = float(iout) if isinstance(iout, (int, float)) else None
        isat = _num(op, "inductor_isat_a")
        peak = None
        if iout is None:
            missing.append(f"{rd['out_net']} has no power entry current_a, so "
                           f"its peak current and load limit can't be read")
        else:
            peak = (iout + ripple / 2 if topo == "buck"
                    else ripple / 2 + iout / (1 - duty))
            rec.update(iout_a=iout, peak_a=checklib.rnd(peak, 4))
            if topo == "boost":
                rec["rhp_zero_hz"] = checklib.rnd(
                    vout * (1 - duty) ** 2 / (2 * math.pi * henries * iout), 1)
            if isat is None:
                missing.append(f"no {topo} block for {vout:g} V states "
                               f"inductor_isat_a")
            elif isat < peak:
                add("converter_isat_low", "error",
                    f"Isat {_fmt(isat)} is below the peak current {_fmt(peak)} "
                    f"at {_fmt(iout)} out (dI {_fmt(ripple)}; {ref} "
                    "eq. 4)",
                    isat_a=isat, peak_a=checklib.rnd(peak, 4))
        if row is None:
            missing.append(f"{ic} has no row in reference/converter_limits.yaml "
                           f"for its current limits and minimum on-time")
        else:
            pk, vl = row.get("peak_limit_a") or {}, row.get("valley_limit_a") or {}
            cap = None
            if iout is not None and pk.get("min") is not None:
                cap = pk["min"] - ripple / 2
                why = f"peak limit {pk['min']:g} A min less dI/2"
                cite = f"{ref} eq. 3"
                if topo == "boost":
                    cap *= 1 - duty
                    why = f"({why}) * (1 - D)"
                elif vl.get("min") is not None and vl["min"] + ripple / 2 < cap:
                    cap = vl["min"] + ripple / 2
                    why = f"valley limit {vl['min']:g} A min plus dI/2"
            elif iout is not None and vl.get("min") is not None:
                # A valley-mode part states no peak limit: the switch
                # current at the valley is Ilim, so the inductor's average
                # is Ilim + dI/2 (TI SLVSGK6B eq. 1 for a boost).
                cap = vl["min"] + ripple / 2
                why = f"valley limit {vl['min']:g} A min plus dI/2"
                cite = "TI SLVSGK6B eq. 1" if topo == "boost" else f"{ref} eq. 3"
                if topo == "boost":
                    cap *= 1 - duty
                    why = f"({why}) * (1 - D)"
            if cap is not None:
                rec["iout_max_a"] = checklib.rnd(cap, 4)
                if iout > cap:
                    add("converter_overload", "error",
                        f"the load {_fmt(iout)} is over the {_fmt(cap)} its "
                        f"switch limit allows ({why}; {cite})",
                        iout_a=iout, iout_max_a=checklib.rnd(cap, 4))
            floor = row.get("isat_floor") or {}
            lim = (vl if floor.get("limit") == "valley" else pk)
            lim_a = lim.get("min") if floor else pk.get("typ")
            if isat is not None and lim_a is not None and isat < lim_a:
                said = (f"{row['mpn']}'s datasheet: {floor['quote'].strip()}"
                        if floor else
                        f"below {row['mpn']}'s typical peak limit; a short "
                        f"drives the switch to its limit (part-dependent)")
                add("converter_isat_below_limit", "warning",
                    f"Isat {_fmt(isat)} is below {_fmt(lim_a)}, {said}",
                    isat_a=isat, limit_a=lim_a)
            ton = row.get("ton_min_s") or {}
            if topo == "buck" and ton.get("value"):
                vmax = _num(op, "vin_max_v")
                v_at = vmax if vmax is not None else vin
                t_on = vout / (v_at * fsw)
                rec.update(ton_s=t_on, ton_min_s=ton["value"])
                kind_s = "typical" if ton.get("kind") == "typ" else "maximum"
                if t_on < ton["value"]:
                    add("converter_min_on_time", "warning",
                        f"on-time {t_on * 1e9:.0f} ns at {v_at:g} V in is under "
                        f"the {ton['value'] * 1e9:.0f} ns minimum ({kind_s}): "
                        f"the IC folds back its frequency or skips pulses",
                        ton_ns=checklib.rnd(t_on * 1e9, 1),
                        ton_min_ns=checklib.rnd(ton["value"] * 1e9, 1))
                elif vmax is None:
                    missing.append(f"no {topo} block for {vout:g} V states "
                                   f"vin_max_v, so its on-time at the highest "
                                   f"input can't be read")
        band = (row or {}).get("ripple_band")
        if band and band.get("of") == "rated" and row.get("iout_rated_a"):
            base, lo, hi = float(row["iout_rated_a"]), band["lo"], band["hi"]
            of = f"{row['mpn']}'s rated {base:g} A"
        elif iout:
            base, (lo, hi) = iout, GENERIC_BAND
            of = f"the {iout:g} A load ({BUCK} eq. 6)"
        else:
            base = None
        if base:
            ratio = ripple / base
            rec.update(ripple_ratio=checklib.rnd(ratio, 3),
                       ripple_band=[lo, hi], ripple_in_band=lo <= ratio <= hi,
                       ripple_note=f"dI {_fmt(ripple)} is {ratio:.0%} of {of}; "
                                   f"guidance {lo:.0%} to {hi:.0%}")
        rec["missing"] = missing
        if unverified and found:
            names = ", ".join(r.get("mpn") or r["match"] for r in unverified)
            not_judged.extend(f"{v['msg']} [unverified: {names}]" for v in found)
            rec["status"] = "not_judged"
        else:
            viols.extend(found)
            rec["status"] = "fail" if any(
                v["severity"] == "error" for v in found) else "checked"
        if missing:
            not_judged.extend(f"{where}: {m}" for m in missing)
    return viols, records, not_judged


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcb", required=True)
    ap.add_argument("--constraints")
    ap.add_argument("--table", default=str(TABLE),
                    help="the IC limits table (reference/converter_limits.yaml)")
    ap.add_argument("--out", help="write the JSON report here")
    args = ap.parse_args(argv)
    pcb = Path(args.pcb)
    if not pcb.is_file():
        raise checklib.CheckError(f"board not found: {pcb}")
    cons = None
    if args.constraints:
        cons = json.loads(Path(args.constraints).read_text(encoding="utf-8"))
    bg = geom.load_board(pcb)
    viols, records, not_judged = judge(bg, cons, load_table(Path(args.table)))
    return checklib.report(
        SCRIPT, pcb, viols, checked=[{"converters": len(records)}],
        converters=records,
        nothing_to_judge=None if records or not_judged else
        "no inductor on a switch node driven by one switcher IC",
        not_judged=not_judged), args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    sys.exit(main())
