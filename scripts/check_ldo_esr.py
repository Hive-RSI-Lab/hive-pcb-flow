#!/usr/bin/env python3
"""check_ldo_esr (#356): an ESR-compensated linear regulator's output
capacitor against the ESR window and minimum its datasheet states.

An LM1117 or AMS1117 relies on its output capacitor's ESR for the loop's
compensation zero (LM1117 SNOS412Q p. 15; AMS1117 p. 4). A bank of only
ceramic capacitors gives a few milliohms, under LM1117's 0.3 ohm floor, and
TI's AN-1482 (SNVA167A p. 5) calls ceramics of more than 1 uF "unsuitable"
for such an LDO. The table is reference/ldo_output_cap.yaml.

For each regulator the table matches (by Value or MPN):
  1. the output net: the pad whose pin function is VOUT/OUT/VO, else the
     table's output pads for the footprint (SOT-223 pads 2 and 4); else not
     judged.
  2. the output bank: each fitted capacitor between that net and ground,
     and each capacitor reached through one series resistor (whose ohms
     then count as the capacitor's ESR, the usual fix).
  3. each capacitor's dielectric: constraints `ldo_caps` {ref: {dielectric,
     esr_ohm}} first, then words in its Value or MPN (tantalum, X7R, ...),
     then its footprint library (Capacitor_Tantalum_SMD, CP_Elec, a plain
     Capacitor_SMD chip). The finding names which one it read.
Findings (kind):
  ldo_cout_missing  no output capacitor at all (error: both datasheets
                    require one).
  ldo_esr_low       every output capacitor is a ceramic with no series
                    resistor or declared ESR in the window. LM1117 with a
                    ceramic of 1 uF or more: error (p. 15 window, backed by
                    SNVA167A p. 5); otherwise a warning (AMS1117 states no
                    ESR bound, only a tantalum for stability).
  ldo_esr_window    a declared or series-resistor ESR outside the window
                    and nothing else in it (warning: the window is
                    guidance, p. 14).
  ldo_cout_low      LM1117's tantalum bank under its 10 uF minimum
                    (warning).
An electrolytic, polymer or unread capacitor with no tantalum beside it is
not judged, naming `ldo_caps` as the input that would settle it.
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
import engcalc  # noqa: E402
import geom  # noqa: E402
from check_current import MPN_PROPS  # noqa: E402
from check_ratings import _ohms, is_ground  # noqa: E402

SCRIPT = "check_ldo_esr"
TABLE = HERE.parent / "reference" / "ldo_output_cap.yaml"
OUT_FN = re.compile(r"^V?OUT$|^VO$", re.I)

# words in a capacitor's Value or MPN, first match wins
DIELECTRIC_WORDS = [
    ("tantalum", re.compile(r"tant|\bTA[JCRS]\w*|\bT49\d|\bTPS[A-Z]\d|\bF93", re.I)),
    ("polymer", re.compile(r"polymer|POSCAP|OS-?CON|\bSP-?CAP", re.I)),
    ("electrolytic", re.compile(r"electrolytic|\belec\b|\balu\b", re.I)),
    ("ceramic", re.compile(r"ceramic|MLCC|X[57][RS]|X6S|Y5V|C0G|NP0|\bGRM|\bCL[0-9]{2}", re.I)),
]
# footprint libraries
DIELECTRIC_LIBS = [
    ("tantalum", re.compile(r"Tantalum|CP_EIA", re.I)),
    ("electrolytic", re.compile(r"CP_Elec|CP_Radial|CP_Axial|Elec", re.I)),
    ("ceramic", re.compile(r"^Capacitor_SMD:C_\d{4}_", re.I)),
]


def load_table(path: Path = TABLE) -> tuple[list[dict], dict]:
    try:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise checklib.CheckError(f"LDO output-cap table {path}: {exc}") from exc
    rows = [dict(r, rx=re.compile(r["match"], re.I)) for r in doc.get("regulators") or []]
    return rows, doc.get("ceramic_note") or {}


def _texts(fp: dict) -> list[str]:
    return [t for t in [fp.get("value") or ""] + [fp["props"].get(k, "") for k in MPN_PROPS]
            if t]


def _row(fp: dict, rows: list[dict]) -> dict | None:
    return next((r for r in rows if any(r["rx"].search(t) for t in _texts(fp))), None)


def _fitted(fp: dict) -> bool:
    return "dnp" not in (fp.get("attrs") or ())


def _out_net(bg: geom.BoardGeom, ref: str, row: dict) -> tuple[str | None, str]:
    pads = bg.pads_of(ref=ref)
    by_fn = sorted({p.net for p in pads if p.net and OUT_FN.match(p.pinfunction or "")})
    if len(by_fn) == 1:
        return by_fn[0], "pin function"
    if len(by_fn) > 1:
        return None, f"its output pins are on {len(by_fn)} nets ({', '.join(by_fn)})"
    lib = bg.footprints[ref].get("lib") or ""
    for rx, nums in (row.get("out_pins") or {}).items():
        if re.search(rx, lib, re.I):
            nets = sorted({p.net for p in pads if p.net and p.number in nums})
            if len(nets) == 1:
                return nets[0], f"pads {'/'.join(nums)} of {lib.split(':')[-1]}"
            return None, (f"pads {'/'.join(nums)} of {lib.split(':')[-1]} are on "
                          f"{len(nets)} nets")
    return None, f"no pin functions, and {lib or 'its footprint'} is not in the table's out_pins"


def _two_pin(bg: geom.BoardGeom, ref: str) -> list[str | None]:
    return [p.net for p in bg.pads_of(ref=ref)]


def _dielectric(bg: geom.BoardGeom, ref: str, declared: dict) -> tuple[str, str]:
    d = declared.get(ref)
    if isinstance(d, str):
        return d.lower(), "constraints ldo_caps"
    if isinstance(d, dict) and d.get("dielectric"):
        return str(d["dielectric"]).lower(), "constraints ldo_caps"
    fp = bg.footprints[ref]
    for t in _texts(fp):
        for kind, rx in DIELECTRIC_WORDS:
            if rx.search(t):
                return kind, f"its value/MPN {t!r}"
    lib = fp.get("lib") or ""
    for kind, rx in DIELECTRIC_LIBS:
        if rx.search(lib):
            return kind, f"its footprint {lib.split(':')[-1]}"
    return "unknown", f"nothing in its value, MPN or footprint {lib.split(':')[-1] or '?'}"


def bank(bg: geom.BoardGeom, out: str, declared: dict) -> list[dict]:
    """The fitted capacitors on `out` to ground, direct or through one
    series resistor."""
    caps = [r for r in bg.footprints if re.fullmatch(r"C\d+", r) and _fitted(bg.footprints[r])]
    res = [r for r in bg.footprints if re.fullmatch(r"R\d+", r) and _fitted(bg.footprints[r])]

    def cap_to_ground(ref, net):
        nets = _two_pin(bg, ref)
        return len(nets) == 2 and net in nets and \
            any(n and n != net and is_ground(n) for n in nets)

    found = []
    for c in sorted(caps, key=lambda r: int(r[1:])):
        if cap_to_ground(c, out):
            found.append({"ref": c, "via": None})
    for r in sorted(res, key=lambda r: int(r[1:])):
        nets = _two_pin(bg, r)
        if len(nets) != 2 or out not in nets:
            continue
        mid = next(n for n in nets if n != out)
        if not mid or is_ground(mid):
            continue
        on_mid = {p.ref for p in bg.pads_of(net=mid)}
        if on_mid - {r} and all(re.fullmatch(r"C\d+", x) for x in on_mid - {r}):
            for c in sorted(on_mid - {r}, key=lambda x: int(x[1:])):
                if c in caps and cap_to_ground(c, mid):
                    found.append({"ref": c, "via": r})
    for cap in found:
        fp = bg.footprints[cap["ref"]]
        farads = engcalc.part_value(fp.get("value"), "C")
        cap["uf"] = checklib.rnd(farads * 1e6, 3) if farads else None
        cap["dielectric"], cap["read_from"] = _dielectric(bg, cap["ref"], declared)
        d = declared.get(cap["ref"])
        esr = d.get("esr_ohm") if isinstance(d, dict) else None
        if isinstance(esr, (int, float)) and not isinstance(esr, bool):
            cap["esr_ohm"], cap["esr_from"] = float(esr), "constraints ldo_caps"
        elif cap["via"]:
            ohms = _ohms(bg.footprints[cap["via"]].get("value"))
            if ohms is not None:
                cap["esr_ohm"], cap["esr_from"] = ohms, f"series {cap['via']}"
    return found


def _desc(c: dict) -> str:
    uf = f"{c['uf']:g} uF" if c.get("uf") else "? uF"
    return f"{c['ref']} {uf} {c['dielectric']}"


def judge(bg: geom.BoardGeom, cons: dict | None, rows: list[dict], note: dict):
    cons = cons or {}
    declared = cons.get("ldo_caps") or {}
    viols, records, not_judged = [], [], []
    for ref in sorted(bg.footprints, key=lambda r: (re.sub(r"\d", "", r),
                                                    int(re.sub(r"\D", "", r) or 0))):
        fp = bg.footprints[ref]
        row = _row(fp, rows)
        if row is None or not _fitted(fp):
            continue
        part = fp.get("value") or row["mpn"]
        rec = {"ref": ref, "part": part, "row": row["mpn"]}
        records.append(rec)
        if not row.get("verified"):
            rec["status"] = "not_judged"
            not_judged.append(f"{ref} {part}: [unverified: {row['mpn']} row in "
                              f"reference/ldo_output_cap.yaml]")
            continue
        out, how = _out_net(bg, ref, row)
        if out is None or is_ground(out):
            rec["status"] = "not_judged"
            not_judged.append(f"{ref} {part}: can't find its output net ({how})")
            continue
        rec.update(out_net=out, out_from=how)
        caps = bank(bg, out, declared)
        rec["caps"] = caps
        pads = [p for p in bg.pads_of(ref=ref) if p.net == out]
        pos = pads[0].center if pads else fp["at"]
        layer = pads[0].layers[0] if pads and pads[0].layers else None
        refs = [ref] + [c["ref"] for c in caps]
        win = row.get("esr_ohm") or None
        floor_uf = float(row["cout_min_uf"])
        has_win = bool(win)

        def hit(sev, kind, msg, **extra):
            viols.append(checklib.violation(
                SCRIPT, sev, pos, layer, out, refs, msg, SCRIPT,
                kind=kind, **extra))

        rec["status"] = "checked"
        if not caps:
            hit("error", "ldo_cout_missing",
                f"{ref} {part} has no capacitor from {out} to ground; it needs one "
                f"for its loop compensation ({row['mpn']}: at least "
                f"{floor_uf:g} uF {row['cout_min_dielectric']})")
            continue
        tants = [c for c in caps if c["dielectric"] == "tantalum"]
        in_win = [c for c in caps if win and c.get("esr_ohm") is not None
                  and win["min"] <= c["esr_ohm"] <= win["max"]]
        out_win = [c for c in caps if win and c.get("esr_ohm") is not None
                   and not win["min"] <= c["esr_ohm"] <= win["max"]]
        others = [c for c in caps if c["dielectric"] not in ("tantalum", "ceramic")]
        ok = tants + in_win
        if ok:
            got = sum(c["uf"] or 0 for c in ok)
            rec["esr_ok_by"] = [c["ref"] for c in ok]
            if has_win and got < floor_uf:
                hit("warning", "ldo_cout_low",
                    f"{ref} {part}: the output capacitors that give it ESR "
                    f"({', '.join(_desc(c) for c in ok)}) add to {got:g} uF, under "
                    f"the {floor_uf:g} uF the datasheet asks for (guidance, p. 14 "
                    f"note)", cout_uf=got, cout_min_uf=floor_uf)
            continue
        if others:
            rec["status"] = "not_judged"
            not_judged.append(
                f"{ref} {part}: output bank {', '.join(_desc(c) for c in caps)} has no "
                f"tantalum; the {', '.join(c['ref'] for c in others)} ESR is not known "
                f"(declare ldo_caps {{ref: {{dielectric, esr_ohm}}}})")
            continue
        if out_win:
            c = out_win[0]
            hit("warning", "ldo_esr_window",
                f"{ref} {part}: {c['ref']}'s ESR {c['esr_ohm']:g} ohm ({c['esr_from']}) "
                f"is outside the {win['min']:g}-{win['max']:g} ohm window the "
                f"datasheet gives for its output capacitor (guidance, p. 14 note), and "
                f"no other output capacitor is in it", esr_ohm=c["esr_ohm"])
            continue
        big = [c for c in caps if (c["uf"] or 0) >= float(note.get("min_uf", 1e9))]
        sev = "error" if (has_win and big) else "warning"
        srcs = sorted({c["read_from"].split(" ")[1] if c["read_from"].startswith("its ")
                       else c["read_from"] for c in caps})
        reads = f"dielectric read from {' and '.join(srcs)}; declare ldo_caps if wrong"
        if has_win:
            why = (f"its ESR must be {win['min']:g}-{win['max']:g} ohm; a ceramic "
                   f"over {note.get('min_uf', 1):g} uF is a few milliohms (TI "
                   f"SNVA167A p. 5)")
        else:
            why = (f"the datasheet makes the output capacitor part of the "
                   f"compensation and states stability with "
                   f"{floor_uf:g} uF {row['cout_min_dielectric']}, with no ESR "
                   f"bound for a ceramic")
        hit(sev, "ldo_esr_low",
            f"{ref} {part}'s output {out} has only ceramic capacitors "
            f"({', '.join(_desc(c) for c in caps)}): {why}. Fit a "
            f"{floor_uf:g} uF tantalum"
            + (f", or a series resistor of {win['min']:g} ohm or more" if has_win else "")
            + f" ({reads})", ceramic_uf=checklib.rnd(sum(c["uf"] or 0 for c in caps), 3))
    return viols, records, not_judged


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcb", required=True)
    ap.add_argument("--constraints")
    ap.add_argument("--table", default=str(TABLE),
                    help="the LDO output-cap table (reference/ldo_output_cap.yaml)")
    ap.add_argument("--out", help="write the JSON report here")
    args = ap.parse_args(argv)
    pcb = Path(args.pcb)
    if not pcb.is_file():
        raise checklib.CheckError(f"board not found: {pcb}")
    cons = None
    if args.constraints and Path(args.constraints).is_file():
        cons = json.loads(Path(args.constraints).read_text(encoding="utf-8"))
    bg = geom.load_board(pcb)
    rows, note = load_table(Path(args.table))
    viols, records, not_judged = judge(bg, cons, rows, note)
    return checklib.report(
        SCRIPT, pcb, viols, checked=[{"regulators": len(records)}],
        regulators=records,
        nothing_to_judge=None if records else
        "no ESR-compensated regulator from reference/ldo_output_cap.yaml",
        not_judged=not_judged), args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    sys.exit(main())
