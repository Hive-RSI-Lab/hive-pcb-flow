#!/usr/bin/env python
"""check_pinouts.py - is an off-board connector wired to its standard? (#1027)

Reads the board's netlist and judges each connector that an off-board mate
plugs into against reference/connector_pinouts.yaml (each row cites the
document that fixes the pinout):

 - pinout_mismatch: a pin of a standard connector carries the wrong role.
     jtag (found by the IC pins named TCK/TMS/TDI/TDO its nets reach, and
       the row's ground and supply pins): a header pin meets an IC pin of
       another JTAG function, judged at the IC end, as net names can agree
       with the header while the IC end is wrong (sizif rev.C: U6's TDO on
       pin 1, TCK).
     audio_jack (a part with pins named T, R/R1/R2, S): ground or common on
       the tip, left or right off T and R1, or ground on R1 of a headset
       (TRRS) jack, which neither CTIA nor OMTP allows (echoplayer rev2: the
       common on the tip).
     stated (constraints.json "pinouts": [{"ref", "standard"}], a mate with
       no standard of its own): a GND pin off ground, a SUPPLY pin on ground
       or a signal pin on a ground or supply rail (bed-lift v0.2: the hall
       supply and GND swapped on pins 2 and 3).
 - pinout_supply_volts: the supply the board feeds on the connector's power
   pins sits at other volts than the mate needs (bed-lift v0.2: the hall
   sensors' VHALL is U4's output, U4 on +3.3V, where they want 5 V). A
   net's volts come from its name (+5V, 3V3), else from the one IC driving
   it: the highest rail on that IC's other pins.
 - pinout_unresolved (warning): a ref or standard named in "pinouts" that
   the board or the table doesn't have.

    check_pinouts.py --pcb B.kicad_pcb [--netlist B.net] [--constraints C]
    check_pinouts.py --sch B.kicad_sch [--constraints C]

With --pcb alone it reads <stem>.kicad_sch beside the board (KiCad 4/5:
<stem>.sch), else <stem>.net. A board with no such connector is
nothing_to_judge.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

import yaml  # noqa: E402

import checklib  # noqa: E402

SCRIPT = "check_pinouts"
SOURCE = "pinouts"
TABLE = Path(__file__).resolve().parent.parent / "reference" / "connector_pinouts.yaml"
JTAG = ("TCK", "TMS", "TDI", "TDO")
CONN_RE = re.compile(r"^(J|P|CN|X|XS|XP)\d", re.I)
IC_RE = re.compile(r"^(U|IC)\d", re.I)
ROLE_WORDS = {
    "LEFT": {"L", "LEFT", "HPL", "LOUT", "OUTL", "LO"},
    "RIGHT": {"R", "RIGHT", "HPR", "ROUT", "OUTR", "RO"},
    "GND": {"GND", "AGND", "COM", "COMMON", "SLEEVE"},
    "MIC": {"MIC", "MICIN", "MIC_IN"},
}


def load_table(path: Path = TABLE) -> dict:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get(
        "standards", {})


def _role(spec) -> tuple[str, float | None]:
    if isinstance(spec, dict):
        return str(spec.get("role")), spec.get("volts")
    return str(spec), None


def _volts_from_name(net: str) -> float | None:
    from check_ratings import volts_from_name  # noqa: PLC0415
    if net.startswith("Net-(") or net.startswith("unconnected-"):
        return None
    return volts_from_name(net)


class Board:
    """The netlist, indexed: ref -> {pin: net}, and net -> members."""

    def __init__(self, parsed: dict):
        self.comps = parsed["components"]
        self.nets = parsed["nets"]
        self.pins: dict[str, dict[str, str]] = {}
        self.func: dict[tuple[str, str], str] = {}
        for net, members in self.nets.items():
            for m in members:
                self.pins.setdefault(m["ref"], {})[str(m["pin"])] = net
                self.func[m["ref"], str(m["pin"])] = m.get("pinfunction") or ""
        self._volts: dict[str, float | None] = {}

    def is_ground(self, net: str) -> bool:
        return _volts_from_name(net) == 0.0

    def volts(self, net: str) -> float | None:
        """Named rail volts, else the one IC driving it: the highest rail on
        that IC's other pins (a logic buffer's output sits at its VCC)."""
        if net in self._volts:
            return self._volts[net]
        v = _volts_from_name(net)
        if v is None:
            ics = {m["ref"] for m in self.nets.get(net, ())
                   if IC_RE.match(m["ref"] or "")}
            if len(ics) == 1:
                ic = next(iter(ics))
                rails = [_volts_from_name(n) for p, n in
                         self.pins.get(ic, {}).items() if n != net]
                rails = [r for r in rails if r is not None and r > 0]
                v = max(rails) if rails else None
        self._volts[net] = v
        return v

    def driver(self, net: str) -> str | None:
        ics = sorted({m["ref"] for m in self.nets.get(net, ())
                      if IC_RE.match(m["ref"] or "")})
        return ics[0] if len(ics) == 1 else None

    def jtag_at(self, net: str) -> list[tuple[str, str, str]]:
        """IC pins with a JTAG function on `net`, or one 2-pin resistor
        beyond it (a series resistor) when the net itself has none."""
        def on(n):
            out = []
            for m in self.nets.get(n, ()):
                if not IC_RE.match(m["ref"] or ""):
                    continue
                words = set(re.split(r"[^A-Z0-9]+",
                                     (m.get("pinfunction") or "").upper()))
                for f in JTAG:
                    if f in words:
                        out.append((f, m["ref"], str(m["pin"])))
            return out
        hits = on(net)
        if hits:
            return hits
        for m in self.nets.get(net, ()):
            ref = m["ref"] or ""
            if ref.upper().startswith("R") and len(self.pins.get(ref, {})) == 2:
                far = [n for p, n in self.pins[ref].items() if p != str(m["pin"])]
                if far and far[0] != net:
                    hits += on(far[0])
        return hits


def _cite(row: dict) -> str:
    """The source's title: its text up to the first comma, colon or '('."""
    return re.split(r"[,(:]", row.get("source", ""))[0].strip()


def _viol(kind, ref, net, msg, severity="error", **extra):
    return checklib.violation(SCRIPT, severity, None, None, net, [ref], msg,
                              SOURCE, kind=kind, **extra)


def judge_jtag(board: Board, ref: str, pins: dict, name: str, row: dict):
    """(violations, judged?) for one connector against a jtag row."""
    n = int(row.get("pins", 0)) or None
    if n and not all(p.isdigit() and 1 <= int(p) <= n for p in pins):
        return [], False  # unconnected pins drop out of the netlist
    at = {p: board.jtag_at(n) for p, n in pins.items()}
    funcs = {f for hits in at.values() for f, _, _ in hits}
    if len(funcs) < 3:
        return [], False
    if not all(p in pins and board.is_ground(pins[p]) for p in row["ground"]):
        return [], False
    if any(p in pins and board.is_ground(pins[p]) for p in row.get("supply", ())):
        return [], False
    out = []
    for p, spec in row["pinout"].items():
        role, _ = _role(spec)
        if role not in JTAG or p not in pins:
            continue
        hits = at[p]
        got = sorted({f for f, _, _ in hits})
        if hits and role not in got:
            ic = ", ".join(f"{r} pin {n} ({f})" for f, r, n in hits)
            out.append(_viol(
                "pinout_mismatch", ref, pins[p],
                f"{ref} pin {p} is {role} in the {name} pinout "
                f"({_cite(row)}), but its net "
                f"{pins[p]} meets {ic}: the programmer's {role} lands on the "
                f"IC's {'/'.join(got)}", pin=p, standard=name,
                expected=role, got=got))
    return out, True


def _jack_role(net: str, board: Board) -> str | None:
    if board.is_ground(net):
        return "GND"
    if net.startswith("Net-(") or net.startswith("unconnected-"):
        return None
    words = set(re.split(r"[^A-Z0-9]+", net.rsplit("/", 1)[-1].upper())) - {""}
    found = [r for r, ws in ROLE_WORDS.items() if words & ws]
    return found[0] if len(found) == 1 else None


def is_jack(board: Board, ref: str, pins: dict) -> bool:
    names = set(pins)
    return {"T", "S"} <= names and bool(names & {"R", "R1"})


def judge_jack(board: Board, ref: str, pins: dict, rows: dict):
    """(violations, standards it fits) for a 3.5 mm phone jack."""
    roles = {p: _jack_role(n, board) for p, n in pins.items()}
    trrs = {"R1", "R2"} <= set(pins)
    probs = []
    if roles.get("T") == "GND":
        probs.append(f"tip T is on {pins['T']}, the ground/common: every "
                     "headphone puts left on the tip")
    for p, r in roles.items():
        if r == "LEFT" and p != "T":
            probs.append(f"left ({pins[p]}) is on {p}, not the tip T")
        if r == "RIGHT" and p not in ("R", "R1"):
            probs.append(f"right ({pins[p]}) is on {p}, not ring 1")
    if trrs and roles.get("R1") == "GND":
        probs.append(f"ring 1 is on {pins['R1']}: a headset jack has ground "
                     "on ring 2 (CTIA) or the sleeve (OMTP)")
    fits = []
    for name, row in rows.items():
        known = [p for p in row["pinout"] if p in pins and roles.get(p)]
        if known and all(roles[p] == _role(row["pinout"][p])[0] for p in known):
            fits.append(name)
    if not probs:
        return [], fits
    want = "audio-trrs-ctia" if trrs else "audio-trs"
    msg = (f"{ref} is a 3.5 mm jack wired against the standard headphone "
           f"pinout ({_cite(rows[want])}): "
           + "; ".join(probs))
    gnd = next((pins[p] for p, r in roles.items() if r == "GND"), None)
    return [_viol("pinout_mismatch", ref, gnd, msg, standard=want,
                  roles={p: r for p, r in roles.items() if r})], fits


def judge_stated(board: Board, ref: str, pins: dict, name: str, row: dict):
    out = []
    want_v = None
    for p, spec in row["pinout"].items():
        role, volts = _role(spec)
        if role == "SUPPLY" and volts is not None:
            want_v = float(volts)
        if role == "NC" or p not in pins:
            continue
        net = pins[p]
        gnd = board.is_ground(net)
        rail = _volts_from_name(net)
        bad = None
        if role == "GND" and not gnd:
            bad = f"is GND for the mate but carries {net}"
        elif role == "SUPPLY" and gnd:
            bad = f"is the mate's supply but sits on ground ({net})"
        elif role not in ("GND", "SUPPLY") and (gnd or rail is not None):
            bad = f"is the mate's {role} but carries the rail {net}"
        if bad:
            out.append(_viol("pinout_mismatch", ref, net,
                             f"{ref} pin {p} {bad} ({name}: "
                             f"{_cite(row)})",
                             pin=p, standard=name, expected=role))
    if want_v is not None:
        power_pins = [p for p, s in row["pinout"].items()
                      if _role(s)[0] in ("GND", "SUPPLY") and p in pins]
        for p in power_pins:
            net = pins[p]
            if board.is_ground(net):
                continue
            v = board.volts(net)
            if v is None or abs(v - want_v) <= 0.05 * want_v:
                continue
            how = ("" if _volts_from_name(net) is not None else
                   f", the output of {board.driver(net)} on its "
                   f"{v:g} V rail")
            out.append(_viol(
                "pinout_supply_volts", ref, net,
                f"{ref} feeds the mate's supply on pin {p} from {net} at "
                f"{v:g} V{how}, where {name} needs {want_v:g} V",
                pin=p, standard=name, volts=v, want_volts=want_v))
    return out


def check(parsed: dict, constraints: dict, table: dict | None = None):
    table = load_table() if table is None else table
    board = Board(parsed)
    viols: list[dict] = []
    judged: list[dict] = []
    stated = {}
    for ent in constraints.get("pinouts", []) or []:
        ref, std = ent.get("ref"), ent.get("standard")
        if ref not in board.pins or std not in table:
            what = (f"standard {std!r} is not in connector_pinouts.yaml"
                    if std not in table else f"{ref} is not in the netlist")
            viols.append(_viol("pinout_unresolved", ref, None,
                               f"pinouts entry {ent}: {what}", "warning"))
            continue
        stated[ref] = std
    jacks = {k: r for k, r in table.items() if r.get("kind") == "audio_jack"}
    for ref in sorted(board.pins):
        pins = board.pins[ref]
        if ref in stated:
            name = stated[ref]
            row = table[name]
            if row["kind"] == "stated":
                viols += judge_stated(board, ref, pins, name, row)
            elif row["kind"] == "jtag":
                viols += judge_jtag(board, ref, pins, name, row)[0]
            elif row["kind"] == "audio_jack":
                viols += judge_jack(board, ref, pins, {name: row})[0]
            judged.append({"ref": ref, "standard": name, "stated": True})
            continue
        if is_jack(board, ref, pins):
            v, fits = judge_jack(board, ref, pins, jacks)
            viols += v
            judged.append({"ref": ref, "standard": "audio_jack",
                           "fits": fits})
            continue
        if not CONN_RE.match(ref):
            continue
        for name, row in table.items():
            if row.get("kind") != "jtag":
                continue
            v, ok = judge_jtag(board, ref, pins, name, row)
            if ok:
                viols += v
                judged.append({"ref": ref, "standard": name})
                break
    return viols, judged


def _netlist_for(args) -> tuple[Path | None, str | None]:
    from netlist_audit import (_export_netlist, _have_kicad_cli,  # noqa: PLC0415
                               _modern_netlist)
    from lib import legacy_net  # noqa: PLC0415
    if args.netlist:
        return _modern_netlist(Path(args.netlist)), None
    sch = Path(args.sch) if args.sch else None
    if sch is None:
        pcb = Path(args.pcb)
        own_sch, own_net = pcb.with_suffix(".kicad_sch"), pcb.with_suffix(".net")
        legacy = pcb.with_suffix(".sch")
        if not own_sch.is_file() and legacy_net.is_legacy(legacy):
            sch = legacy
        elif own_sch.is_file() and _have_kicad_cli():
            sch = own_sch
        elif own_net.is_file():
            return _modern_netlist(own_net), None
        else:
            return None, (f"no readable {own_sch.name} or {own_net.name} "
                          "beside the board")
    return _export_netlist(sch), None


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcb")
    ap.add_argument("--sch")
    ap.add_argument("--netlist")
    ap.add_argument("--constraints")
    ap.add_argument("--out", help="write the JSON report here")
    args = ap.parse_args(argv)
    if not (args.pcb or args.sch or args.netlist):
        raise checklib.CheckError("give --pcb, --sch or --netlist")
    board = args.pcb or args.sch or args.netlist
    netlist, why = _netlist_for(args)
    if why:
        return checklib.report(SCRIPT, board, [], nothing_to_judge=why), args.out
    from netlist_audit import parse_netlist  # noqa: PLC0415
    constraints = (checklib.load_json(args.constraints, "constraints")
                   if args.constraints else {})
    viols, judged = check(parse_netlist(netlist), constraints)
    return checklib.report(
        SCRIPT, board, viols, connectors=judged,
        nothing_to_judge=None if judged else
        "no connector the pinout table knows (JTAG header, 3.5 mm jack, "
        "or one named in constraints.json pinouts)"), args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    sys.exit(main())
