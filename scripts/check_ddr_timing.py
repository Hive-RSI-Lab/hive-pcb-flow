"""check_ddr_timing.py - DDR timing budget from routed flight times
(hive-hwde #487).

check_length_groups holds DDR nets to millimetre windows (IMX6ULHDG Table
22). This check turns the same routed paths into TIME and puts them in a
budget against the DRAM's datasheet rows (reference/ddr_timing.yaml), the
way a memory-interface timing report does: margin in ps per group, with
every term listed.

Flight time per net: the path check_length_groups.net_length measures
(pad to pad on netconn, plus the via barrel travelled) with each track's
delay from lib/impedance.py's field solver for that layer and width
(check_si.LineModel: eps_eff of coated microstrip or stripline on the
board's stackup) and the barrel at the mean epsilon_r. A diff pair's
reference time is the mean of its two legs (where they cross).

Budgets (UI = 1e6 / rate_mts ps, tCK = 2 UI):

  dq    write DQ/DM vs their DQS at the DRAM, per byte lane.
        skew_i = t(net_i) - t(DQS). With write calibration (the controller
        centres DQS in the lane, MMDC MPWRDLCTL) skew is taken round the
        lane centre (max+min)/2 and the delay-line step/2 is charged.
          setup = UI/2 - tDS(VREF) - out_skew - cal/2 - allow - max(skew)
          hold  = UI/2 - tDH(VREF) - out_skew - cal/2 - allow + min(skew)
        Also a fact: the READ eye at the controller balls,
          tQH - tDQSQ - (max(skew) - min(skew)), which the controller's
          (unpublished, for the MMDC) input setup + hold must fit inside.
  addr  address/command/control vs CK at the DRAM (1T: CK edge centred in
        a tCK-wide eye; "timing": "2T" doubles the eye).
          setup = eye/2 - tIS(VREF) - out_skew - allow - max(skew)
          hold  = eye/2 - tIH(VREF) - out_skew - allow + min(skew)
  dqs   DQS vs CK at the DRAM (tDQSS) and write leveling. need = t(CK) -
        t(DQS); leveling delays DQS by `need`, so need/tCK must fall in the
        controller's leveling range; the residual is the step/2.
          tDQSS (levelled)   = 0.25 tCK - out_skew - cal/2 - allow
          tDQSS (unlevelled) = 0.25 tCK - out_skew - allow - |need|
        Without leveling the unlevelled margin is the one judged.

tDS/tDH/tIS/tIH (VREF) = base + delta(slew) + (V_level - VREF)/slew,
delta interpolated bilinearly in the datasheet's derating table (input
slew x strobe/clock differential slew). Slews default to the datasheet's
base point (1 V/ns single-ended, 2 V/ns differential, delta = 0) and can be
set in constraints (until an IBIS model gives them, #486).

constraints.json "ddr":
    {"controller": "U100", "dram": "U200",       # refs on the board
     "controller_part": "MCIMX6Y2CVM08AB",       # optional, else the ref's
     "dram_part": "MT41K256M16TW-107",           #   MPN / Value field
     "rate_mts": 800,
     "input_levels": {"data": "AC160", "addr": "AC160"},   # optional
     "slew_vns": {"dq": 1.0, "dqs": 2.0, "addr": 1.0, "ck": 2.0},
     "allowances_ps": {"isi": 0, "xtalk": 0, "vref": 0},   # charged on
                                                 #   every setup and hold
     "controller_terms": {...},                  # optional: override or
                                                 #   supply the controller
                                                 #   row (out_skew_ps,
                                                 #   write_calibration,
                                                 #   cal_step_tck,
                                                 #   write_leveling,
                                                 #   leveling_range_tck)
     "timing": "1T",
     "groups": [{"name": "BYTE0", "kind": "dq"},
                {"name": "ADDR_CMD", "kind": "addr"},
                {"name": "DQS0", "kind": "dqs", "clock": "CK"}]}
A group's `name` is a length_groups entry: its nets are the members and its
ref_nets the strobe (dq) or clock (addr); a dqs group's nets are the strobe
pair and `clock` names the CK length group (or give "strobe"/"clock" as net
globs directly). The ddr block extends length_groups; it does not repeat
the net lists.

Findings (kind):
  ddr_setup_violation       setup margin < 0 (dq or addr group)
  ddr_hold_violation        hold margin < 0
  ddr_dqss_violation        tDQSS margin < 0
  ddr_leveling_out_of_range need/tCK outside the leveling range
                            (warning when the range is unverified)
  ddr_rate_over             rate above the controller's or DRAM's rows
  ddr_part_unknown          no ddr_timing.yaml row for a part (warning;
                            without a DRAM row nothing is judged)
  ddr_slew_clamped          a slew outside / unsupported in the derating
                            table: clamped to the nearest supported cell
  ddr_unrouted              a group net not routed or open: not judged
A malformed ddr block stops the check (exit 2).

CLI: --pcb B [--constraints C] [--table reference/ddr_timing.yaml]
     [--out report.json]. Board only: without a ddr block it passes.
"""
from __future__ import annotations

import argparse
import math
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import check_length_groups as clg  # noqa: E402
import checklib  # noqa: E402
import geom  # noqa: E402
from checklib import CheckError, violation  # noqa: E402

SCRIPT = "check_ddr_timing"
REPO = Path(__file__).resolve().parents[1]
TABLE = REPO / "reference" / "ddr_timing.yaml"
KINDS = ("dq", "addr", "dqs")
DDR_KEYS = {"controller", "dram", "controller_part", "dram_part", "rate_mts",
            "input_levels", "slew_vns", "allowances_ps", "controller_terms",
            "timing", "groups", "note"}
GROUP_KEYS = {"name", "kind", "clock", "strobe", "nets", "note"}
CTRL_TERMS = {"out_skew_ps", "write_calibration", "cal_step_tck",
              "write_leveling", "leveling_range_tck", "max_rate_mts"}
DEFAULT_SLEW = {"dq": 1.0, "dqs": 2.0, "addr": 1.0, "ck": 2.0}
DEFAULT_LEVELS = {"data": "AC160", "addr": "AC160"}
HOLD_LEVEL = "DC90"


# ------------------------------------------------------------ table

def load_table(path: Path = TABLE) -> dict:
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    drams = {r["name"]: r for r in doc.get("drams") or []}
    for r in drams.values():          # same_as: borrow the numbers
        if r.get("same_as"):
            base = drams[r["same_as"]]
            for k in ("levels_mv", "speeds", "derating"):
                r.setdefault(k, base[k])
    return {"drams": list(drams.values()),
            "controllers": list(doc.get("controllers") or [])}


def find_row(rows, part: str | None):
    if not part:
        return None
    for r in rows:
        if re.search(r["match"], part, re.IGNORECASE):
            return r
    return None


def _speed(row, rate):
    sp = {int(k): v for k, v in (row.get("speeds") or {}).items()}
    return sp.get(int(rate)), sorted(sp)


def derate(table: dict, in_slew: float, strobe_slew: float) -> dict:
    """Bilinear interpolation of [delta_setup, delta_hold] in one derating
    table {strobe_vns: [...], rows: {slew: [[ds, dh] | None, ...]}}.
    Returns {setup, hold, clamped (bool), cells [(row, col), ...]}. A
    point outside the table, or one whose surrounding cells include an
    unsupported (null) one, is moved to the nearest supported cell."""
    cols = [float(c) for c in table["strobe_vns"]]
    rows = {float(k): v for k, v in table["rows"].items()}
    rkeys = sorted(rows)
    clamped = False

    def bracket(keys, x):
        nonlocal clamped
        if x <= keys[0]:
            clamped |= x < keys[0] - 1e-9
            return keys[0], keys[0], 0.0
        if x >= keys[-1]:
            clamped |= x > keys[-1] + 1e-9
            return keys[-1], keys[-1], 0.0
        for k in keys:              # on a table point: no neighbour needed
            if abs(x - k) < 1e-9:
                return k, k, 0.0
        for a, b in zip(keys, keys[1:]):
            if a <= x <= b:
                return a, b, (x - a) / (b - a)
        raise AssertionError

    ckeys = sorted(cols)
    r0, r1, fr = bracket(rkeys, float(in_slew))
    c0, c1, fc = bracket(ckeys, float(strobe_slew))

    def cell(r, c):
        return rows[r][cols.index(c)]

    corners = [(r0, c0), (r0, c1), (r1, c0), (r1, c1)]
    if any(cell(r, c) is None for r, c in corners):
        # nearest supported cell (in V/ns distance); no interpolation
        clamped = True
        best = min(((abs(r - in_slew) + abs(c - strobe_slew), r, c)
                    for r in rkeys for c in ckeys if cell(r, c) is not None))
        _, r, c = best
        v = cell(r, c)
        return {"setup": float(v[0]), "hold": float(v[1]), "clamped": True,
                "cells": [(r, c)]}
    out = []
    for i in (0, 1):
        a = cell(r0, c0)[i] * (1 - fc) + cell(r0, c1)[i] * fc
        b = cell(r1, c0)[i] * (1 - fc) + cell(r1, c1)[i] * fc
        out.append(a * (1 - fr) + b * fr)
    return {"setup": out[0], "hold": out[1], "clamped": clamped,
            "cells": corners}


def input_req(dram: dict, rate: int, which: str, level: str,
              in_slew: float, strobe_slew: float) -> dict:
    """VREF-referenced setup and hold the DRAM needs. which: 'data' (tDS/
    tDH) or 'addr' (tIS/tIH). Returns {setup, hold, terms, clamped}."""
    sp, _ = _speed(dram, rate)
    s_key, h_key, tab = (("tDS_base", "tDH_base", "tDS_tDH") if which == "data"
                         else ("tIS_base", "tIH_base", "tIS_tIH"))
    s_name, h_name = s_key[:3], h_key[:3]
    base_s = (sp.get(s_key) or {}).get(level)
    base_h = (sp.get(h_key) or {}).get(HOLD_LEVEL)
    if base_s is None or base_h is None:
        have = sorted((sp.get(s_key) or {}))
        raise CheckError(f"{dram['name']} at {rate} MT/s has no {s_name}"
                         f"({level}); it lists {have}")
    tables = (dram.get("derating") or {}).get(tab) or {}
    if level not in tables:
        raise CheckError(f"{dram['name']}: no {tab} derating table for {level}")
    d = derate(tables[level], in_slew, strobe_slew)
    lv = dram["levels_mv"]
    vs = float(lv[level]) / in_slew
    vh = float(lv[HOLD_LEVEL]) / in_slew
    setup = base_s + d["setup"] + vs
    hold = base_h + d["hold"] + vh
    terms = [
        {"term": f"{s_name}(base, {level})", "ps": base_s},
        {"term": f"delta {s_name} @ {in_slew:g} V/ns, {strobe_slew:g} V/ns",
         "ps": checklib.rnd(d["setup"], 1)},
        {"term": f"V{level} - VREF over the slew ({lv[level]} mV / "
                 f"{in_slew:g} V/ns)", "ps": checklib.rnd(vs, 1)},
        {"term": f"{h_name}(base, {HOLD_LEVEL})", "ps": base_h},
        {"term": f"delta {h_name}", "ps": checklib.rnd(d["hold"], 1)},
        {"term": f"VREF - V{HOLD_LEVEL} over the slew", "ps": checklib.rnd(vh, 1)},
    ]
    return {"setup": setup, "hold": hold, "terms": terms,
            "clamped": d["clamped"]}


# ------------------------------------------------------------ flight time

class Flight:
    """Flight time (ps) per net along net_length's path, each track at its
    layer and width's field-solved eps_eff (check_si.LineModel)."""

    def __init__(self, bg):
        import check_si  # heavy-ish import; only when a board is judged
        self.bg = bg
        self.lm = check_si.LineModel(bg)
        self.cache: dict = {}
        self.barrel_tpd = math.sqrt(clg._mean_er(bg)) / clg.C_MM_PER_PS

    def of(self, net: str) -> dict:
        if net in self.cache:
            return self.cache[net]
        m = clg.net_length(self.bg, net)
        out = {k: m.get(k) for k in ("net", "routed", "connected",
                                     "length_mm", "via_mm", "vias")}
        if m.get("connected"):
            ps, models = 0.0, set()
            for t in m["path_tracks"]:
                ln = self.lm.line(t.layer, t.width)
                ps += t.length * ln["tpd_ps_mm"]
                models.add(ln["model"])
            ps += (m.get("via_mm") or 0.0) * self.barrel_tpd
            out.update(flight_ps=ps, models=sorted(models))
        self.cache[net] = out
        return out


# ------------------------------------------------------------ validation

def validate(ddr, lgroups: dict) -> None:
    if not isinstance(ddr, dict):
        raise CheckError("constraints ddr is not an object")
    bad = sorted(checklib.data_keys(ddr) - DDR_KEYS)
    if bad:
        raise CheckError(f"ddr: unknown key(s) {bad}")
    if not isinstance(ddr.get("rate_mts"), (int, float)) or ddr["rate_mts"] <= 0:
        raise CheckError("ddr: rate_mts must be a positive number (MT/s)")
    if ddr.get("timing", "1T") not in ("1T", "2T"):
        raise CheckError("ddr: timing must be 1T or 2T")
    ct = ddr.get("controller_terms") or {}
    if set(ct) - CTRL_TERMS:
        raise CheckError(f"ddr.controller_terms: unknown {sorted(set(ct) - CTRL_TERMS)}")
    for key in ("slew_vns", "allowances_ps"):
        v = ddr.get(key) or {}
        if not isinstance(v, dict) or not all(
                isinstance(x, (int, float)) for x in v.values()):
            raise CheckError(f"ddr.{key} must map names to numbers")
    if any(x <= 0 for x in (ddr.get("slew_vns") or {}).values()):
        raise CheckError("ddr.slew_vns must be positive")
    groups = ddr.get("groups")
    if not isinstance(groups, list) or not groups:
        raise CheckError("ddr.groups must be a non-empty list")
    for i, g in enumerate(groups):
        where = f"ddr.groups[{i}]"
        if not isinstance(g, dict):
            raise CheckError(f"{where} is not an object")
        if checklib.data_keys(g) - GROUP_KEYS:
            raise CheckError(
                f"{where}: unknown {sorted(checklib.data_keys(g) - GROUP_KEYS)}")
        if g.get("kind") not in KINDS:
            raise CheckError(f"{where}: kind must be one of {KINDS}")
        name = g.get("name")
        if not name:
            raise CheckError(f"{where}: no name")
        if name not in lgroups and not g.get("nets"):
            raise CheckError(f"{where} ({name}): not a length_groups name "
                             "and no nets given")
        if g["kind"] == "dqs":
            clk = g.get("clock")
            if not clk:
                raise CheckError(f"{where} ({name}): a dqs group needs clock")
        else:
            lg = lgroups.get(name) or {}
            ref = g.get("strobe") or g.get("clock") or clg._ref_nets(lg)
            if not ref:
                raise CheckError(f"{where} ({name}): no strobe/clock: give one "
                                 "or set ref_nets on the length group")


def _globs(x):
    return [x] if isinstance(x, str) else list(x or [])


def resolve(g: dict, lgroups: dict, nets) -> tuple[list, list]:
    """(member nets, reference nets) on the board for one ddr group."""
    lg = lgroups.get(g["name"]) or {}
    members = _globs(g.get("nets")) or _globs(lg.get("nets"))
    if g["kind"] == "dqs":
        clk = g["clock"]
        ref = _globs((lgroups.get(clk) or {}).get("nets")) if clk in lgroups \
            else _globs(clk)
    else:
        ref = _globs(g.get("strobe") or g.get("clock")) or clg._ref_nets(lg)
    m, _ = clg.match_nets(members, nets)
    r, _ = clg.match_nets(ref, nets)
    m = [n for n in m if n not in r]
    return m, r


# ------------------------------------------------------------ budgets

def _term(name, ps, source=None):
    t = {"term": name, "ps": checklib.rnd(ps, 1)}
    if source:
        t["source"] = source
    return t


def budget_dq_addr(kind, skews, ui, req, ctrl, allow_ps, eye_ps,
                   calibrated, cal_step_ps):
    """Setup/hold margin for one dq or addr group. skews {net: ps}."""
    vals = list(skews.values())
    centre = (max(vals) + min(vals)) / 2 if calibrated else 0.0
    late = max(vals) - centre
    early = min(vals) - centre
    half = eye_ps / 2
    cal = cal_step_ps / 2 if calibrated else 0.0
    out_skew = float(ctrl.get("out_skew_ps") or 0.0)
    common = [_term("half eye" + (" (UI/2)" if kind == "dq" else ""), half),
              _term("controller output skew (rise/fall + SSN)", -out_skew,
                    ctrl.get("_source")),
              _term("allowances (isi + xtalk + vref)", -allow_ps)]
    if calibrated:
        common.append(_term("write calibration step / 2", -cal))
    setup = half - req["setup"] - out_skew - allow_ps - cal - late
    hold = half - req["hold"] - out_skew - allow_ps - cal + early
    s_terms = common + [_term("DRAM setup at VREF", -req["setup"]),
                        _term("worst late skew" + (" round the lane centre"
                                                   if calibrated else ""), -late)]
    h_terms = common + [_term("DRAM hold at VREF", -req["hold"]),
                        _term("worst early skew" + (" round the lane centre"
                                                    if calibrated else ""), early)]
    worst_late = max(skews, key=skews.get)
    worst_early = min(skews, key=skews.get)
    return {"setup_ps": checklib.rnd(setup, 1), "hold_ps": checklib.rnd(hold, 1),
            "setup_terms": s_terms, "hold_terms": h_terms,
            "calibrated": calibrated,
            "lane_centre_ps": checklib.rnd(centre, 1),
            "worst_late": worst_late, "worst_early": worst_early,
            "uncalibrated_setup_ps": checklib.rnd(
                half - req["setup"] - out_skew - allow_ps - max(vals), 1),
            "uncalibrated_hold_ps": checklib.rnd(
                half - req["hold"] - out_skew - allow_ps + min(vals), 1)}


# ------------------------------------------------------------ run

def _part_of(bg, ref, override):
    if override:
        return override
    fp = bg.footprints.get(ref) if ref else None
    if not fp:
        return None
    props = fp.get("props") or {}
    for k in ("MPN", "Manufacturer Part Number", "Part Number", "MFR_PN"):
        if props.get(k):
            return props[k]
    return fp.get("value")


def judge(bg, ddr: dict, lgroups: dict, table: dict) -> tuple[list, dict]:
    vs: list[dict] = []
    rate = int(ddr["rate_mts"])
    ui = 1e6 / rate
    tck = 2 * ui
    dram_part = _part_of(bg, ddr.get("dram"), ddr.get("dram_part"))
    ctrl_part = _part_of(bg, ddr.get("controller"), ddr.get("controller_part"))
    dram = find_row(table["drams"], dram_part)
    crow = find_row(table["controllers"], ctrl_part)
    facts = {"rate_mts": rate, "ui_ps": checklib.rnd(ui, 1),
             "tck_ps": checklib.rnd(tck, 1),
             "dram_part": dram_part, "controller_part": ctrl_part,
             "dram_row": dram["name"] if dram else None,
             "controller_row": crow["name"] if crow else None, "groups": []}
    if dram is None:
        vs.append(violation(SCRIPT, "warning", None, None, None,
                            [ddr.get("dram")] if ddr.get("dram") else [],
                            f"DDR: no ddr_timing.yaml row for the DRAM "
                            f"{dram_part!r}: no timing judged",
                            SCRIPT, kind="ddr_part_unknown", part=dram_part))
        return vs, facts
    ctrl = dict(crow or {})
    ctrl.update(ddr.get("controller_terms") or {})
    if crow is None and not ddr.get("controller_terms"):
        vs.append(violation(SCRIPT, "warning", None, None, None,
                            [ddr.get("controller")] if ddr.get("controller") else [],
                            f"DDR: no ddr_timing.yaml row for the controller "
                            f"{ctrl_part!r}: controller terms taken as 0 "
                            f"(DRAM-only budget; set ddr.controller_terms)",
                            SCRIPT, kind="ddr_part_unknown", part=ctrl_part))
    ctrl["_source"] = (crow or {}).get("source", "constraints controller_terms")
    sp, rates = _speed(dram, rate)
    if sp is None:
        vs.append(violation(SCRIPT, "error", None, None, None, [],
                            f"DDR: {dram['name']} has no {rate} MT/s row "
                            f"(rows: {rates})", SCRIPT, kind="ddr_rate_over",
                            part=dram_part))
        return vs, facts
    if ctrl.get("max_rate_mts") and rate > float(ctrl["max_rate_mts"]):
        vs.append(violation(SCRIPT, "error", None, None, None, [],
                            f"DDR: {rate} MT/s is above the controller's "
                            f"{ctrl['max_rate_mts']} MT/s ({ctrl.get('name')})",
                            SCRIPT, kind="ddr_rate_over", part=ctrl_part))
    slew = {**DEFAULT_SLEW, **(ddr.get("slew_vns") or {})}
    levels = {**DEFAULT_LEVELS, **(ddr.get("input_levels") or {})}
    allow = sum(float(v) for v in (ddr.get("allowances_ps") or {}).values())
    cal_step_ps = float(ctrl.get("cal_step_tck") or 0.0) * tck
    calibrated = bool(ctrl.get("write_calibration"))
    leveling = ctrl.get("write_leveling", bool(ctrl.get("leveling_range_tck")))
    lv_range = ctrl.get("leveling_range_tck")
    flight = Flight(bg)
    facts.update(slew_vns=slew, input_levels=levels,
                 allowances_ps=ddr.get("allowances_ps") or {},
                 controller_terms={k: v for k, v in ctrl.items()
                                   if k in CTRL_TERMS})

    for g in ddr["groups"]:
        kind, name = g["kind"], g["name"]
        members, refs = resolve(g, lgroups, bg.nets)
        times, missing = {}, []
        for n in members + refs:
            f = flight.of(n)
            if f.get("flight_ps") is None:
                missing.append(n)
            else:
                times[n] = f["flight_ps"]
        gf = {"name": name, "kind": kind, "members": members, "refs": refs,
              "flight_ps": {n: checklib.rnd(t, 1) for n, t in times.items()}}
        facts["groups"].append(gf)
        if missing:
            vs.append(violation(
                SCRIPT, "warning", None, None, missing[0], [],
                f"DDR group {name}: {len(missing)} net(s) unrouted or open "
                f"({', '.join(missing[:4])}...): group not judged",
                SCRIPT, kind="ddr_unrouted", group=name, nets=missing))
            gf["judged"] = False
            continue
        if not members or not refs:
            vs.append(violation(
                SCRIPT, "warning", None, None, None, [],
                f"DDR group {name}: no {'members' if not members else 'reference'}"
                f" nets on the board", SCRIPT, kind="ddr_unrouted", group=name))
            gf["judged"] = False
            continue
        gf["judged"] = True
        t_ref = sum(times[r] for r in refs) / len(refs)
        gf["ref_ps"] = checklib.rnd(t_ref, 1)
        skews = {n: times[n] - t_ref for n in members}
        gf["skew_ps"] = {n: checklib.rnd(s, 1) for n, s in skews.items()}

        if kind in ("dq", "addr"):
            which = "data" if kind == "dq" else "addr"
            in_s = slew["dq" if kind == "dq" else "addr"]
            st_s = slew["dqs" if kind == "dq" else "ck"]
            req = input_req(dram, rate, which, levels[which], in_s, st_s)
            if req["clamped"]:
                vs.append(violation(
                    SCRIPT, "warning", None, None, None, [],
                    f"DDR group {name}: slew {in_s:g} V/ns vs {st_s:g} V/ns is "
                    f"outside or unsupported in the derating table; clamped",
                    SCRIPT, kind="ddr_slew_clamped", group=name))
            if kind == "dq":
                eye = ui
                cal = calibrated
            else:
                eye = tck * (2 if ddr.get("timing") == "2T" else 1)
                cal = False
            b = budget_dq_addr(kind, skews, ui, req, ctrl, allow, eye,
                               cal, cal_step_ps)
            b["dram_terms"] = req["terms"]
            gf.update(b)
            if kind == "dq":
                spread = max(skews.values()) - min(skews.values())
                gf["read_eye_ps"] = checklib.rnd(
                    sp["tQH_tck"] * tck - sp["tDQSQ"] - spread, 1)
                gf["read_eye_terms"] = [
                    _term("tQH", sp["tQH_tck"] * tck),
                    _term("tDQSQ", -sp["tDQSQ"]),
                    _term("lane flight spread", -spread)]
            for side, net in (("setup", b["worst_late"]), ("hold", b["worst_early"])):
                m = b[f"{side}_ps"]
                if m < 0:
                    terms = b[f"{side}_terms"]
                    vs.append(violation(
                        SCRIPT, "error", clg._rep_point(bg, net),
                        clg._rep_layer(bg, net), net, [],
                        f"DDR {kind} group {name}: {side} margin {m:+.1f} ps "
                        f"at {rate} MT/s ({net} skew "
                        f"{skews[net]:+.1f} ps vs "
                        f"{'/'.join(refs)}); terms: "
                        + ", ".join(f"{t['term']} {t['ps']:+g}" for t in terms),
                        SCRIPT, kind=f"ddr_{side}_violation", group=name,
                        margin_ps=m, terms=terms))
        else:   # dqs vs CK
            out_skew = float(ctrl.get("out_skew_ps") or 0.0)
            need = {n: -s for n, s in skews.items()}   # t(CK) - t(DQS)
            lim = sp["tDQSS_tck"][1] * tck
            worst = max(need, key=lambda n: abs(need[n]))
            unlev = lim - out_skew - allow - abs(need[worst])
            lev = lim - out_skew - allow - cal_step_ps / 2
            gf.update(need_ps={n: checklib.rnd(v, 1) for n, v in need.items()},
                      tdqss_unlevelled_ps=checklib.rnd(unlev, 1),
                      tdqss_levelled_ps=checklib.rnd(lev, 1),
                      leveling=bool(leveling),
                      tdqss_terms=[_term("tDQSS max (0.25 tCK)", lim),
                                   _term("controller output skew", -out_skew),
                                   _term("allowances", -allow),
                                   _term("|t(CK) - t(DQS)| (unlevelled)",
                                         -abs(need[worst])),
                                   _term("leveling step / 2 (levelled)",
                                         -cal_step_ps / 2)])
            judged = lev if leveling else unlev
            if judged < 0:
                vs.append(violation(
                    SCRIPT, "error", clg._rep_point(bg, worst),
                    clg._rep_layer(bg, worst), worst, [],
                    f"DDR group {name}: tDQSS margin {judged:+.1f} ps "
                    f"({'levelled' if leveling else 'no write leveling'}; "
                    f"t(CK) - t({worst}) = {need[worst]:+.1f} ps)",
                    SCRIPT, kind="ddr_dqss_violation", group=name,
                    margin_ps=checklib.rnd(judged, 1)))
            if leveling and lv_range:
                lo, hi = float(lv_range[0]) * tck, float(lv_range[1]) * tck
                lv_m = min(min(v - lo, hi - v) for v in need.values())
                gf["leveling_range_ps"] = [checklib.rnd(lo, 1), checklib.rnd(hi, 1)]
                gf["leveling_margin_ps"] = checklib.rnd(lv_m, 1)
                if lv_m < 0:
                    bad = min(need, key=lambda n: min(need[n] - lo, hi - need[n]))
                    sev = ("error" if ctrl.get("leveling_verified", True)
                           else "warning")
                    vs.append(violation(
                        SCRIPT, sev, clg._rep_point(bg, bad),
                        clg._rep_layer(bg, bad), bad, [],
                        f"DDR group {name}: write leveling needs "
                        f"{need[bad]:+.1f} ps on {bad} (t(CK) - t(DQS)); the "
                        f"controller covers [{lo:.0f}, {hi:.0f}] ps "
                        f"({lv_range[0]:g}..{lv_range[1]:g} tCK); "
                        f"DQS must not arrive after CK",
                        SCRIPT, kind="ddr_leveling_out_of_range", group=name,
                        margin_ps=checklib.rnd(lv_m, 1),
                        range_verified=bool(ctrl.get("leveling_verified", True))))
    return vs, facts


def run(argv=None):
    ap = argparse.ArgumentParser(description="DDR timing budget (#487).")
    ap.add_argument("--pcb", required=True)
    ap.add_argument("--constraints")
    ap.add_argument("--table", default=str(TABLE))
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    cons = checklib.load_json(args.constraints, "constraints") \
        if args.constraints else {}
    ddr = cons.get("ddr")
    if not ddr:
        return checklib.report(SCRIPT, args.pcb, [], judged=False,
                               nothing_to_judge="no ddr block in constraints"
                               ), args.out
    lgroups = {g.get("name"): g for g in cons.get("length_groups") or []
               if isinstance(g, dict) and g.get("name")}
    validate(ddr, lgroups)
    table = load_table(Path(args.table))
    bg = geom.load_board(Path(args.pcb))
    vs, facts = judge(bg, ddr, lgroups, table)
    return checklib.report(SCRIPT, args.pcb, vs, judged=True, **facts), args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())
