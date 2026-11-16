"""check_converter.py - a buck's or boost's inductor and switch against its
IC's current limits and minimum on-time (P8 verify suite; #355).

Each inductor with one end on a switch node that one switcher IC drives is a
converter. check_current's ripple readers (#798, #816) give its input and
output voltages, L and fsw, from declared voltages, the inductor's Value, and
reference/converter_fsw.yaml or a block's fsw_khz. The load is the output
rail's power entry current_a. Then:

  buck   dI = (Vin - Vout) * Vout / (Vin * fsw * L)        TI SLVA477C eq. 1
         Ipeak = Iout + dI / 2                             SLVA477C eq. 4
         Iout max = Ilim(min) - dI / 2                     SLVA477C eq. 3
         ton = Vout / (Vin * fsw)
  boost  D = 1 - Vin / Vout (lossless; losses raise D a little, so Ipeak
         here is a lower bound)
         dI = Vin * D / (fsw * L)                          TI SLVA372D eq. 2
         Ipeak = dI / 2 + Iout / (1 - D)                   SLVA372D eq. 5
         Iout max = (Ilim(min) - dI / 2) * (1 - D)         SLVA372D eq. 3
         RHP zero = Vout * (1 - D)^2 / (2 pi L Iout)

Findings, all on the inductor and its IC:
  converter_isat_low      error: the inductor's stated saturation current
                          (block operating_point inductor_isat_a) is below
                          the peak current at the stated load.
  converter_overload      error: the stated load is more than the IC's
                          minimum switch current limit allows: the peak
                          limit, less half the ripple (and, for a buck, the
                          valley limit plus half the ripple).
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

SCRIPT = "check_converter"
SOURCE = SCRIPT
TABLE = HERE.parent / "reference" / "converter_limits.yaml"
BUCK = "TI SLVA477C"
BOOST = "TI SLVA372D"
GENERIC_BAND = (0.2, 0.4)          # SLVA477C eq. 6, of the load


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


def converters(bg: geom.BoardGeom, cons: dict):
    """(inductor, node, rail, reading) for each inductor on a switch node,
    the reading a dict of the converter's inputs or a string naming what
    can't be read."""
    volts = cc._declared_volts(cons)
    blocks = cons.get("blocks") or []
    out = []
    for ind in sorted(r for r in bg.footprints if re.fullmatch(r"L\d+", r)):
        nets = sorted({p.net for p in bg.pads_of(ref=ind) if p.net})
        nodes = [n for n in nets if cc._switchers_on(bg, n)]
        if len(nodes) != 1 or len(nets) != 2:
            continue
        node = nodes[0]
        rail = next(n for n in nets if n != node)
        if rail not in volts:
            out.append((ind, node, rail, f"{rail} has no declared voltage"))
            continue
        outs = cc._boost_rails(bg, rail, node, volts)
        if len(outs) > 1:
            out.append((ind, node, rail, f"{node} lifts {rail} to {len(outs)} "
                                         f"rails; can't tell its output"))
        elif outs:
            vin, vout = volts[rail], volts[outs[0]]
            got = cc._boost_inputs(bg, ind, node, vin, vout, blocks)
            if isinstance(got, str):
                out.append((ind, node, rail, got))
                continue
            ic, henries, fsw, where = got
            out.append((ind, node, rail, {
                "topology": "boost", "ic": ic, "L_h": henries, "fsw_hz": fsw,
                "fsw_source": where, "vin_v": vin, "vout_v": vout,
                "out_net": outs[0]}))
        else:
            term = cc._buck_output(bg, rail, ind, node, volts, blocks)
            if isinstance(term, str):
                out.append((ind, node, rail, term))
                continue
            got = dict(term["inputs"])
            out.append((ind, node, rail, {
                "topology": "buck", "ic": term["from"], "L_h": got["L_h"],
                "fsw_hz": got["fsw_hz"], "fsw_source": got["fsw_source"],
                "vin_v": got["vin_v"], "vout_v": got["vout_v"],
                "out_net": rail}))
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
        op = _block(blocks, topo, vout, ind)
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
        rec = {"inductor": ind, "ic": ic, "topology": topo, "node": node,
               "vin_v": vin, "vout_v": vout, "L_h": henries, "fsw_hz": fsw,
               "fsw_source": rd["fsw_source"], "duty": checklib.rnd(duty, 4),
               "ripple_a": checklib.rnd(ripple, 4),
               "limits_row": row["mpn"] if row else None}
        records.append(rec)
        where = (f"{ind} ({topo}, {ic}, {vin:g} V to {vout:g} V, "
                 f"{henries * 1e6:.3g} uH at {fsw / 1e3:.4g} kHz)")
        found, missing = [], []

        def add(kind, sev, msg, **extra):
            found.append(checklib.violation(
                SCRIPT, sev, None, None, node, [ind, ic], f"{where}: {msg}",
                SOURCE, kind=kind, **extra))

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
                    f"{'eq. 4' if topo == 'buck' else 'eq. 5'})",
                    isat_a=isat, peak_a=checklib.rnd(peak, 4))
        if row is None:
            missing.append(f"{ic} has no row in reference/converter_limits.yaml "
                           f"for its current limits and minimum on-time")
        else:
            pk, vl = row.get("peak_limit_a") or {}, row.get("valley_limit_a") or {}
            if iout is not None and pk.get("min") is not None:
                cap = pk["min"] - ripple / 2
                why = f"peak limit {pk['min']:g} A min less dI/2"
                if topo == "boost":
                    cap *= 1 - duty
                    why = f"({why}) * (1 - D)"
                elif vl.get("min") is not None and vl["min"] + ripple / 2 < cap:
                    cap = vl["min"] + ripple / 2
                    why = f"valley limit {vl['min']:g} A min plus dI/2"
                rec["iout_max_a"] = checklib.rnd(cap, 4)
                if iout > cap:
                    add("converter_overload", "error",
                        f"the load {_fmt(iout)} is over the {_fmt(cap)} its "
                        f"switch limit allows ({why}; {ref} eq. 3)",
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
