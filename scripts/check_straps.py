"""check_straps.py - an MCU's strapping pins read the level they need at
power-on and on an MCU-only reset, the pins it pulls at reset leave no load
on, and an IO expander's pins come up at a defined level (P8 verify suite).

The hit: hifi-receiver-2's core passed every gate while its ESP32 GPIO0
strap could latch a 50 MHz clock: the RMII oscillator's OE came from a
TCA9555 pin whose internal 100 k pull-up fought a 100 k pull-down (1.65 V,
mid-band, and the oscillator reads a floating OE as enabled), and once
firmware set that pin, it stayed set through every ESP32 reset (review-2
DIGITAL-1/2). ERC can't see an internal pull-up or what holds through a
reset; this check resolves each net from the netlist and
reference/reset_states.yaml.

How a net resolves (for one event, `por` or `mcu_reset`): every resistor
to a rail is a pull; a resistor to another net brings that net's own pulls
and drivers in series; 0 R, inductors, ferrites and fuses join nets, and so
does a net tie (a bridged solder jumper, a NetTie) whose own copper on the
board joins its pads: the netlist keeps each of its pads on its own net, so
a bridge drawn only on silk joins nothing; a part
in reset_states.yaml adds its pins' internal pulls; an oscillator output
drives a clock while its enable resolves high or floating. An expander's
pins hold whatever firmware set through an MCU-only reset unless its RESET
pin is on the MCU's enable net. The level is the pulls' Thevenin voltage
against the MCU's VIL/VIH (with its own internal pull), taken at the rail on
the row's vdd_pins where it names one (a SoC whose boot pins sit on a 1.8 V
or 3.3 V IO bank) and from the `levels` entry nearest that rail. A strap
wants high, low, any (no level is wrong) or defined (high or low, never
undefined or floating: a boot-mode pin the chip latches with no pull of its
own). Capacitors, diodes,
transistors and switches don't drive at reset (a transistor is taken as
off). Any other IC pin, and any connector pin, is a part whose reset state
the file doesn't know.

Findings:
  strap_level         error (the strap's severity): a strapping pin reads
                      the wrong level, an undefined one, a clock or a level
                      firmware left behind, at power-on or on an MCU reset
                      (for want: defined, any level but high or low)
  strap_unknown       warning: a strap with a wanted level also reaches pins
                      with no known reset state (confirm each is an input or
                      hi-Z at reset, or add the part to reset_states.yaml)
  load_on_at_reset    warning: an MCU pin the chip pulls at reset (a
                      strap's pull, or reset_pulls: the ESP32-C3's JTAG
                      pins) holds a FET gate past its VGS(th) min or a
                      part's enable asserted, so the load is on before
                      firmware runs (minuet v3 GPIO6, the LED strip)
  reset_pull_offboard warning: a reset_pulls pin's net leaves the board on
                      a connector with no pull but the chip's own (a debug
                      header excepted): whatever is plugged in sees it
  io_reset_undefined  error: an expander pin sits between VIL and VIH of its
                      supply at power-on (its pull fights an external one)
  fet_on_at_reset     error: a MOSFET gate on an expander pin (directly or
                      through one resistor) sits at or above the FET's
                      minimum VGS(th) at power-on (reset_states.yaml fets)
  io_reset_floating   warning: an expander pin with no pull at all, and
                      nothing else on its net that could hold it
  io_reset_holds      warning: an expander whose outputs survive an MCU
                      reset (no RESET pin on the MCU's enable)
  mcu_unknown         warning: a part reset_states.yaml's mcu_like calls an
                      MCU has no row, so nothing about it was judged
  no_netlist          warning: the board has an MCU from the file but no
                      netlist beside it, so nothing was resolved

The netlist is the board's own `<board>.net` beside the .kicad_pcb (the one
the pipeline exports), or --netlist.

CLI: --pcb board.kicad_pcb [--netlist board.net] [--out report.json]
     exit 0 pass / 1 violations / 2 error, per SPEC section 6.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "lib"))
import check_net_tie  # noqa: E402
import checklib  # noqa: E402
import engcalc  # noqa: E402
import geom  # noqa: E402
import simlib  # noqa: E402
from check_ratings import volts_from_name  # noqa: E402

SCRIPT = "check_straps"
SOURCE = SCRIPT  # verify_all, gate and the scorecard match a finding to its check by source
RESET_YAML = HERE.parent / "reference" / "reset_states.yaml"
EVENTS = ("por", "mcu_reset")
EVENT_TEXT = {"por": "at power-on", "mcu_reset": "on an MCU-only reset"}
WANT_TEXT = {"defined": "a defined level (high or low)"}
# an expander pin's load is taken as CMOS: VIL 0.3, VIH 0.7 of its supply
IO_VIL, IO_VIH = 0.3, 0.7
DEFAULT_VDD = 3.3
MAX_DEPTH = 4       # series resistors followed from a pin
SHORT_PREFIXES = ("L", "FB", "F", "NT")
PASSIVE_PREFIXES = ("C", "D", "LED", "Q", "SW", "S", "TP", "H", "MH", "BT",
                    "K")
CONNECTOR_PREFIXES = ("J", "P", "CN", "CON", "X")
# a switch named by its job rather than SW ("boot1", "reset1" on oasis v0.2)
# is still a switch: open at reset, no drive and no pull of its own
PASSIVE_VALUE = re.compile(r"^(SW_|SW$|Button|Switch|TestPoint)", re.I)


def load_kb(path: Path = RESET_YAML) -> dict:
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    rows = {r["family"]: r for r in doc.get("mcus") or []}
    for row in doc.get("mcus") or []:
        # `like: FAMILY`: a board or module that is that chip (a Pico, an
        # ESP-12F) takes every field it doesn't set from the chip's row
        base = rows.get(row.get("like") or "")
        if base is not None:
            for k, v in base.items():
                if k not in row and k not in ("family", "match", "cite",
                                              "source", "verified"):
                    row[k] = v
        row["_match"] = re.compile(row["match"], re.I)
        if row.get("vdd_pins"):
            row["_vdd_pins"] = re.compile(row["vdd_pins"], re.I)
    for row in doc.get("parts") or []:
        row["_match"] = re.compile(row["match"], re.I)
        row["_pins"] = re.compile(row.get("pins") or "$^", re.I)
    osc = doc.get("oscillators") or {}
    for k in ("refs", "output", "enable"):
        osc["_" + k] = re.compile(osc.get(k) or "$^", re.I)
    doc["oscillators"] = osc
    return doc


def pin_name(pinfunction: str, pin: str) -> str:
    """'IO0_25' -> 'IO0', '~{INT}_1' -> 'INT', 'GPIO12' -> 'IO12'."""
    name = (pinfunction or "").strip()
    if pin and name.endswith(f"_{pin}"):
        name = name[: -len(pin) - 1]
    name = re.sub(r"~\{([^}]*)\}", r"\1", name).upper()
    return re.sub(r"^GPIO(\d+)$", r"IO\1", name)


def prefix(ref: str) -> str:
    m = re.match(r"^[A-Za-z]+", ref)
    return m.group(0).upper() if m else ""


class Board:
    """The netlist as nets and pins, with 0 R / inductor joins merged.
    joins: more (net, net) pairs to merge (tie_joins: a net tie's copper);
    ties: refs that join nets but are no part (a net tie), left out of
    the nodes as a 0 R is."""

    def __init__(self, parsed: dict, joins=(), ties=()):
        self.comps = parsed["components"]
        self.pins: dict[str, dict[str, dict]] = {}
        for net, nodes in parsed["nets"].items():
            for nd in nodes:
                self.pins.setdefault(nd["ref"], {})[nd["pin"]] = {
                    "net": net, "name": pin_name(nd.get("pinfunction"),
                                                 nd["pin"]),
                    "bar": "~{" in (nd.get("pinfunction") or "")}
        parent = {n: n for n in parsed["nets"]}

        def find(n):
            while parent[n] != n:
                parent[n] = parent[parent[n]]
                n = parent[n]
            return n
        self.shorts = set()
        for ref, pins in self.pins.items():
            if len(pins) == 2 and self.is_short(ref):
                a, b = (p["net"] for p in pins.values())
                parent[find(a)] = find(b)
                self.shorts.add(ref)
        for a, b in joins:
            if a in parent and b in parent:
                parent[find(a)] = find(b)
        self.ties = set(ties)
        self.group = {n: find(n) for n in parent}
        self.nodes: dict[str, list] = {}
        for net, nodes in parsed["nets"].items():
            for nd in nodes:
                if nd["ref"] in self.shorts or nd["ref"] in self.ties:
                    continue
                self.nodes.setdefault(self.group[net], []).append(
                    (nd["ref"], nd["pin"]))
        self.names: dict[str, list] = {}
        for net in parsed["nets"]:
            self.names.setdefault(self.group[net], []).append(net)

    def value(self, ref: str) -> str:
        return (self.comps.get(ref) or {}).get("value") or ""

    def is_short(self, ref: str) -> bool:
        p = prefix(ref)
        if p in SHORT_PREFIXES:
            return True
        return p == "R" and engcalc.parse_value(self.value(ref)) == 0

    def net(self, ref: str, pin: str) -> str:
        return self.group[self.pins[ref][pin]["net"]]

    def rail(self, g: str) -> float | None:
        for name in self.names.get(g, []):
            v = volts_from_name(name)
            if v is not None:
                return v
        return None

    def label(self, g: str) -> str:
        return sorted(self.names.get(g, [g]), key=len)[0]

    def unconnected(self, g: str) -> bool:
        return all(n.startswith("unconnected-") for n in self.names.get(g, []))


def tie_joins(bg, parsed: dict) -> tuple[list, list, list]:
    """([(net, net)] the board's net ties join, [their refs], [facts]).
    A net tie's pads keep their own nets in the netlist; what joins them is
    the copper its footprint draws, so this reads that copper (check_net_tie's
    pieces) rather than the groups it declares: a bridge moved to silk joins
    nothing, and the pin it fed reads floating."""
    joins, refs, facts = [], [], []
    if bg is None:
        return joins, refs, facts
    pin_net = {(nd["ref"], nd["pin"]): net
               for net, nodes in parsed["nets"].items() for nd in nodes}
    on_copper = set(bg.copper_layers)
    for tie in bg.net_ties:
        ref = tie["ref"]
        copper = [g for g in tie["graphics"] if g["layer"] in on_copper]
        split = check_net_tie.pieces(
            tie["pads"], copper, {p.number for p in tie["pads"] if p.number})
        joined = []
        for piece in split:
            nets = sorted({pin_net[(ref, n)] for n in piece
                           if (ref, n) in pin_net})
            joins += list(zip(nets, nets[1:]))
            if len(nets) > 1:
                joined.append(nets)
        refs.append(ref)
        facts.append({"ref": ref, "pieces": split, "joins": joined})
    return joins, refs, facts


def netlist_board(netlist: Path, bg) -> tuple[Board, list]:
    """The Board for `netlist`, with the net ties on `bg` (the parsed
    .kicad_pcb, or None) joined as their copper joins them."""
    parsed = simlib.parse_netlist(netlist)
    joins, refs, facts = tie_joins(bg, parsed)
    return Board(parsed, joins=joins, ties=refs), facts


class Resolver:
    def __init__(self, board: Board, kb: dict, mcu: dict | None,
                 mcu_pulls: dict | None = None):
        self.b, self.kb, self.mcu = board, kb, mcu
        # {(mcu ref, pin): (volts, ohm, label)}: MCU pins whose own reset
        # pull counts (check_reset_pulls); the strap check leaves it empty
        self.mcu_pulls = mcu_pulls or {}
        self.osc = kb["oscillators"]
        self.parts = {}
        for ref in board.pins:
            val = board.value(ref)
            for row in kb.get("parts") or []:
                if row["_match"].search(val):
                    self.parts[ref] = row
                    break
        self.enable_net = None
        if mcu:
            for pin, meta in board.pins[mcu["ref"]].items():
                if meta["name"] in [e.upper() for e in mcu["row"]["enable"]]:
                    self.enable_net = board.net(mcu["ref"], pin)

    def part_vdd(self, ref: str) -> float:
        for meta in self.b.pins.get(ref, {}).values():
            if re.match(r"^(VCC|VDD|VDDIO|VIO|3V3|VDD3P3)", meta["name"]):
                v = self.b.rail(self.b.group[meta["net"]])
                if v:
                    return v
        return DEFAULT_VDD

    def is_osc(self, ref: str) -> bool:
        if not self.osc["_refs"].match(ref):
            return False
        return any(self.osc["_output"].match(m["name"])
                   for m in self.b.pins[ref].values())

    def reset_follows_mcu(self, ref: str, row: dict) -> bool:
        names = [n.upper() for n in (row.get("reset") or [])]
        for pin, meta in self.b.pins[ref].items():
            if meta["name"] in names and self.enable_net \
                    and self.b.net(ref, pin) == self.enable_net:
                return True
        return False

    def contributions(self, g: str, event: str, skip: set, series: float = 0,
                      depth: int = 0, seen: frozenset = frozenset()) -> dict:
        """Pulls, drivers and unknown pins on net group g, seen through
        `series` ohms. skip: (ref, pin) pairs not to count (the observer)."""
        out = {"pulls": [], "drivers": [], "unknown": []}
        seen = seen | {g}
        v = self.b.rail(g)
        if v is not None:
            out["pulls"].append((v, series, self.b.label(g)))
            return out
        for ref, pin in self.b.nodes.get(g, []):
            if (ref, pin) in skip:
                continue
            meta = self.b.pins[ref][pin]
            p = prefix(ref)
            where = f"{ref}.{pin} ({meta['name'] or self.b.value(ref)})"
            if (ref, pin) in self.mcu_pulls:
                pv, ohm, label = self.mcu_pulls[(ref, pin)]
                out["pulls"].append((pv, series + ohm, label))
                continue
            if self.mcu and ref == self.mcu["ref"]:
                continue  # its other pins are inputs at reset
            if p == "R" and len(self.b.pins[ref]) == 2:
                r = engcalc.parse_value(self.b.value(ref))
                other = [q for q in self.b.pins[ref] if q != pin][0]
                og = self.b.net(ref, other)
                if r is None:
                    out["unknown"].append(f"{ref} (value "
                                          f"{self.b.value(ref)!r})")
                    continue
                if og in seen or depth >= MAX_DEPTH:
                    continue
                sub = self.contributions(og, event, skip, series + r,
                                         depth + 1, seen)
                for k in out:
                    out[k] += sub[k]
                continue
            row = self.parts.get(ref)
            if row is not None:
                if row["kind"] == "expander" and row["_pins"].match(
                        meta["name"]):
                    if event == "mcu_reset" and not \
                            self.reset_follows_mcu(ref, row):
                        out["drivers"].append({
                            "kind": "firmware", "series": series,
                            "where": where})
                    elif row.get("pull"):
                        pv = (self.part_vdd(ref) if row["pull"]["dir"] == "up"
                              else 0.0)
                        out["pulls"].append(
                            (pv, series + row["pull"]["ohm"],
                             f"{where} internal {row['pull']['dir']}"))
                    continue
                if row["kind"] == "input" and row["_pins"].match(
                        meta["name"]):
                    continue
                if row["kind"] == "expander":
                    continue  # INT, SCL/SDA, address pins: no drive at reset
            if self.is_osc(ref):
                if self.osc["_output"].match(meta["name"]):
                    out["drivers"].append(self.osc_drive(ref, event, series,
                                                         where, seen))
                continue
            if p in PASSIVE_PREFIXES or ref in self.b.shorts \
                    or PASSIVE_VALUE.match(self.b.value(ref) or ""):
                continue
            if p in CONNECTOR_PREFIXES:
                out["unknown"].append(f"{where}, a connector")
                continue
            out["unknown"].append(f"{where} {self.b.value(ref)}".strip())
        return out

    def osc_drive(self, ref, event, series, where, seen) -> dict:
        ens = [(pin, m) for pin, m in self.b.pins[ref].items()
               if self.osc["_enable"].match(m["name"])]
        d = {"kind": "clock", "series": series, "where": where}
        if not ens:
            d["why"] = "no enable pin"
            return d
        pin, _ = ens[0]
        eg = self.b.net(ref, pin)
        if self.b.unconnected(eg):
            d["why"] = "enable pin open"
            return d
        st = self.state(eg, event, skip={(ref, pin)},
                        vdd=self.part_vdd(ref), vil=0.3, vih=0.7,
                        seen=seen)
        en = st["state"]
        if en == "low":
            d["kind"] = "off"
        elif en in ("high", "floating"):
            d["why"] = f"enable {self.b.label(eg)} {en}"
        else:
            d["kind"] = "maybe_clock"
            d["why"] = f"enable {self.b.label(eg)} {st['text']}"
        return d

    def state(self, g, event, skip=frozenset(), vdd=DEFAULT_VDD, vil=0.25,
              vih=0.75, internal=None, seen=frozenset()) -> dict:
        """The level of net group g: high | low | undefined | floating |
        toggling | may_toggle | firmware, with the pulls behind it."""
        c = self.contributions(g, event, set(skip), seen=frozenset(seen))
        pulls = list(c["pulls"])
        if internal:
            pulls.append(internal)
        drivers = [d for d in c["drivers"] if d["kind"] != "off"]
        res = {"unknown": c["unknown"], "pulls": [p[2] for p in pulls],
               "drivers": drivers}
        for kind, st in (("clock", "toggling"), ("maybe_clock", "may_toggle"),
                         ("firmware", "firmware")):
            hits = [d for d in drivers if d["kind"] == kind]
            if hits:
                d = hits[0]
                text = {"toggling": f"a clock from {d['where']}",
                        "may_toggle": f"maybe a clock from {d['where']}",
                        "firmware": f"whatever firmware left on "
                                    f"{d['where']}"}[st]
                if d.get("why"):
                    text += f" ({d['why']})"
                return {**res, "state": st, "volts": None, "text": text}
        hard = [p for p in pulls if p[1] == 0]
        if hard:
            volts = hard[0][0]
        elif pulls:
            gsum = sum(1 / p[1] for p in pulls)
            volts = sum(p[0] / p[1] for p in pulls) / gsum
        else:
            return {**res, "state": "floating", "volts": None,
                    "text": "floating (no pull)"}
        if volts >= vih * vdd:
            st = "high"
        elif volts <= vil * vdd:
            st = "low"
        else:
            st = "undefined"
        pulls_txt = ", ".join(
            f"{p[2]}{'' if p[1] == 0 else f' {p[1] / 1000:.3g} k'}"
            for p in pulls)
        return {**res, "state": st, "volts": volts,
                "text": f"{volts:.2f} V, {st} (VIL {vil * vdd:.2f} / VIH "
                        f"{vih * vdd:.2f} V; {pulls_txt})"}


def unknown_mcus(values: dict[str, str], kb: dict) -> list[str]:
    """Refs whose Value is an MCU (kb mcu_like) that no `mcus` row matches:
    nothing about their straps or reset states can be judged."""
    like = kb.get("mcu_like")
    if not like:
        return []
    return sorted(ref for ref, val in values.items()
                  if re.search(like, val or "", re.I)
                  and not any(r["_match"].search(val)
                              for r in kb.get("mcus") or []))


def mcu_verdict(known: list, unknown: dict[str, str]) -> str | None:
    """Why an MCU check judged nothing (None when it had an MCU to judge):
    the coverage line in place of a pass."""
    if known:
        return None
    if unknown:
        return ("reference/reset_states.yaml has no row for "
                + ", ".join(f"{r} ({v})" for r, v in sorted(unknown.items())))
    return "no MCU on the board"


def find_mcus(board: Board, kb: dict) -> list[dict]:
    out = []
    for ref in sorted(board.pins):
        val = board.value(ref)
        for row in kb.get("mcus") or []:
            if row["_match"].search(val):
                out.append({"ref": ref, "row": row, "value": val})
                break
    return out


def mcu_pins(board: Board, mcu: dict) -> dict[str, str]:
    """{canonical strap name: pin number} for one MCU."""
    aliases = {k.upper(): v.upper() for k, v in
               (mcu["row"].get("aliases") or {}).items()}
    out = {}
    for pin, meta in board.pins[mcu["ref"]].items():
        for tok in re.split(r"[/,]", meta["name"]):
            # GPIO12, GP12 and a Pico's GPIO26_ADC0 are all IO12 / IO26
            tok = re.sub(r"^(?:GPIO|GP)(\d+)(?:_.*)?$", r"IO\1",
                         tok.strip().upper())
            tok = aliases.get(tok, tok)
            out.setdefault(tok, pin)
    return out


def io_levels(board: Board, mcu: dict) -> tuple[float, float, float]:
    """(vdd, vil, vih) for one MCU's straps: the rail on the row's vdd_pins
    (the IO supply its boot pins sit on) where the netlist names one, else
    the row's vdd; VIL/VIH (x vdd) from the row's `levels` entry nearest
    that rail, else its vil/vih."""
    row = mcu["row"]
    vdd = row.get("vdd", DEFAULT_VDD)
    pat = row.get("_vdd_pins")
    if pat is not None:
        for pin, meta in sorted(board.pins[mcu["ref"]].items()):
            if pat.match(meta["name"]):
                v = board.rail(board.net(mcu["ref"], pin))
                if v:
                    vdd = v
                    break
    vil, vih = row.get("vil", 0.25), row.get("vih", 0.75)
    levels = row.get("levels") or []
    if levels:
        near = min(levels, key=lambda lv: abs(lv["vdd"] - vdd))
        vil, vih = near["vil"], near["vih"]
    return vdd, vil, vih


def pad_pos(bg, ref: str, pin: str):
    if bg is None:
        return None
    for p in bg.pads_of(ref=ref):
        if p.number == pin:
            return p.center
    return None


def check_mcu(board, kb, mcu, bg) -> tuple[list, list]:
    viols, facts = [], []
    row = mcu["row"]
    res = Resolver(board, kb, mcu)
    vdd, vil, vih = io_levels(board, mcu)
    pins = mcu_pins(board, mcu)
    unverified = "" if row.get("verified", False) else \
        " (reset_states.yaml: unverified)"
    for strap, spec in row["straps"].items():
        pin = pins.get(strap)
        if pin is None:
            continue
        g = board.net(mcu["ref"], pin)
        internal = None
        if spec.get("pull") in ("up", "down"):
            internal = (vdd if spec["pull"] == "up" else 0.0,
                        row.get("pull_ohm", 45000),
                        f"{mcu['ref']} internal {spec['pull']}")
        want = spec.get("want", "any")
        fact = {"mcu": mcu["ref"], "strap": strap, "pin": pin,
                "net": board.label(g), "want": want, "vdd": vdd}
        bad = {}
        floating = False
        unknown = []
        for event in EVENTS:
            st = res.state(g, event, skip={(mcu["ref"], pin)}, vdd=vdd,
                           vil=vil, vih=vih, internal=internal)
            fact[event] = st["text"]
            unknown = st["unknown"]
            ok = st["state"] == want or (
                want == "any" and st["state"] in ("high", "low", "undefined",
                                                  "floating")) or (
                want == "defined" and st["state"] in ("high", "low"))
            if not ok:
                bad[event] = st["text"]
                floating = floating or st["state"] == "floating"
        if bad and floating and row.get("onboard") == "unknown":
            # a module that may pull this strap itself (an ESP-12F): left
            # floating by the board, its level is the module's, which no
            # datasheet here gives
            fact["not_judged"] = (
                f"{strap} is left floating by the board and {mcu['value']} "
                "may pull it on the module; reset_states.yaml doesn't know "
                "the module's own strap resistors")
            bad = {}
        facts.append(fact)
        pos = pad_pos(bg, mcu["ref"], pin)
        if bad:
            events = " and ".join(EVENT_TEXT[e] for e in bad)
            texts = sorted(set(bad.values()))
            viols.append(checklib.violation(
                SCRIPT, spec.get("severity", "error"), pos, None,
                board.label(g), [mcu["ref"]],
                f"{mcu['ref']} {strap} (pin {pin}, {board.label(g)}) must "
                f"read {WANT_TEXT.get(want, want)} at reset, but {events} it "
                "reads "
                f"{' / '.join(texts)}: {spec.get('why', '')}{unverified}",
                SOURCE, kind="strap_level", strap=strap, events=sorted(bad),
                states=bad))
        elif unknown and want != "any":
            viols.append(checklib.violation(
                SCRIPT, "warning", pos, None, board.label(g), [mcu["ref"]],
                f"{mcu['ref']} {strap} (pin {pin}, {board.label(g)}) also "
                f"reaches {', '.join(sorted(set(unknown)))}, whose reset "
                "state hwde doesn't know: confirm each is an input or hi-Z "
                f"until the strap latches (or add the part to "
                "reset_states.yaml)", SOURCE, kind="strap_unknown",
                strap=strap, unknown=sorted(set(unknown))))
    return viols, facts


def check_expanders(board, kb, mcu, bg) -> tuple[list, list]:
    viols, facts = [], []
    res = Resolver(board, kb, mcu)
    for ref, row in sorted(res.parts.items()):
        if row["kind"] != "expander":
            continue
        vdd = res.part_vdd(ref)
        used = 0
        for pin, meta in sorted(board.pins[ref].items(),
                                key=lambda kv: kv[0].zfill(4)):
            if not row["_pins"].match(meta["name"]):
                continue
            g = board.net(ref, pin)
            if board.unconnected(g):
                continue
            used += 1
            st = res.state(g, "por", vdd=vdd, vil=IO_VIL, vih=IO_VIH)
            facts.append({"part": ref, "pin": pin, "name": meta["name"],
                          "net": board.label(g), "por": st["text"]})
            pos = pad_pos(bg, ref, pin)
            if st["state"] == "undefined":
                viols.append(checklib.violation(
                    SCRIPT, "error", pos, None, board.label(g), [ref],
                    f"{ref} {meta['name']} ({board.label(g)}) comes up at "
                    f"{st['text']} at power-on, before firmware writes it: "
                    "make the pulls agree (one pull, or a part with no "
                    "internal pull-up)", SOURCE, kind="io_reset_undefined",
                    volts=checklib.rnd(st["volts"], 3)))
            for gate in gates_on(board, g):
                gref, gpin, gg = gate
                vth = fet_vth(kb, board.value(gref))
                if vth is None:
                    continue
                gst = res.state(gg, "por", vdd=vdd, vil=IO_VIL, vih=IO_VIH)
                if gst["volts"] is not None and gst["volts"] >= vth:
                    viols.append(checklib.violation(
                        SCRIPT, "error", pad_pos(bg, gref, gpin), None,
                        board.label(gg), [ref, gref],
                        f"{gref} ({board.value(gref)}) gate on "
                        f"{board.label(gg)} sits at {gst['volts']:.2f} V at "
                        f"power-on, from {ref} {meta['name']}, at or above "
                        f"its minimum VGS(th) {vth:g} V: a low-threshold "
                        "part conducts before firmware runs. Hold the gate "
                        "well below VGS(th) min until firmware drives it",
                        SOURCE, kind="fet_on_at_reset",
                        volts=checklib.rnd(gst["volts"], 3), vth_min=vth))
            if st["state"] == "floating" and not st["unknown"]:
                viols.append(checklib.violation(
                    SCRIPT, "warning", pos, None, board.label(g), [ref],
                    f"{ref} {meta['name']} ({board.label(g)}) floats from "
                    "power-on until firmware drives it: add a pull to the "
                    "level its load must see", SOURCE,
                    kind="io_reset_floating"))
        if used and mcu and not res.reset_follows_mcu(ref, row):
            how = ("has no RESET pin" if not row.get("reset")
                   else "its RESET pin isn't on the MCU's enable")
            viols.append(checklib.violation(
                SCRIPT, "warning", pad_pos(bg, ref, "1"), None, None,
                [ref, mcu["ref"]],
                f"{ref} ({board.value(ref)}) {how}, so its outputs keep "
                f"what firmware set through every {mcu['ref']} reset "
                "(watchdog, panic, EN): anything they enable stays on while "
                "the straps latch", SOURCE, kind="io_reset_holds"))
    return viols, facts


# An IC pin that switches its part (a load switch, regulator, LED driver)
# on: asserted means on for an enable, off for a shutdown. An enable is
# active high unless overbarred or n-named; a shutdown, sleep or standby
# pin is active low (the convention even when the symbol drops the bar).
ENABLE_NAMES = re.compile(r"^(N|N_)?(EN|ENA|ENABLE|EN[0-9]|CE|ON|PWR_?EN|"
                          r"LED_?EN|LOAD_?EN)(_?N|B)?$")
SHUTDOWN_NAMES = re.compile(r"^(N|N_)?(SHDN|SHUTDOWN|SLEEP|STBY|STANDBY|SDZ|"
                            r"SDN|SD_N|SDB)"
                            r"(_?N|B)?$")
DISABLE_NAMES = re.compile(r"^(N|N_)?(DIS|DISABLE|OFF|DRVOFF)(_?N|B)?$")
# a connector pin whose net or pin is a debug port: its pull is the point
DEBUG_NAMES = re.compile(r"JTAG|TMS|TCK|TDI|TDO|SWD|SWCLK|SWO", re.I)


def part_on(name: str, bar: bool, level: str) -> bool | None:
    """Whether a pin at `level` (high | low) leaves its part on; None if the
    pin doesn't switch the part."""
    low_named = bool(re.match(r"^N_?[A-Z]", name)
                     and not name.startswith(("NC", "ON"))) \
        or bool(re.search(r"(_N|[^A]B)$", name))
    if ENABLE_NAMES.match(name):
        active = "low" if bar or low_named else "high"
        return level == active
    if SHUTDOWN_NAMES.match(name):
        return level == "high"
    if DISABLE_NAMES.match(name):
        active = "low" if bar or low_named else "high"
        return level != active
    return None


def near_nodes(board, g) -> list[tuple]:
    """(ref, pin, net group) of every pin on net group g or one resistor
    away from it (the far side of that resistor)."""
    out = [(ref, pin, g) for ref, pin in board.nodes.get(g, [])]
    for ref, pin in board.nodes.get(g, []):
        if prefix(ref) == "R" and len(board.pins[ref]) == 2:
            other = [q for q in board.pins[ref] if q != pin][0]
            og = board.net(ref, other)
            if og != g and board.rail(og) is None:
                out += [(r, p, og) for r, p in board.nodes.get(og, [])
                        if r != ref]
    return out


def check_reset_pulls(board, kb, mcu, bg) -> tuple[list, list]:
    """Every MCU pin the chip pulls at reset (a strap's pull, or a pin in
    the row's reset_pulls: the ESP32-C3's JTAG pins) holds its net from
    power-on until firmware drives it. Where that net switches a load (a
    FET gate past VGS(th), a part's enable) the load comes up on before
    firmware runs; where it leaves the board on a connector with nothing
    but the MCU's own pull, whatever the connector feeds sees it."""
    viols, facts = [], []
    row = mcu["row"]
    vdd, vil, vih = io_levels(board, mcu)
    ohm = row.get("pull_ohm", 45000)
    pins = mcu_pins(board, mcu)
    unverified = "" if row.get("verified", False) else \
        " (reset_states.yaml: unverified)"
    pulled = {}
    for name, spec in (row.get("straps") or {}).items():
        if spec.get("pull") in ("up", "down"):
            pulled[name] = {**spec, "strap": True}
    for name, spec in (row.get("reset_pulls") or {}).items():
        if spec.get("pull") in ("up", "down"):
            pulled.setdefault(name, {**spec, "strap": False})
    mpulls = {}
    for name, spec in pulled.items():
        pin = pins.get(name)
        if pin is not None:
            mpulls[(mcu["ref"], pin)] = (
                vdd if spec["pull"] == "up" else 0.0, spec.get("ohm", ohm),
                f"{mcu['ref']} {name} internal {spec['pull']}")
    res = Resolver(board, kb, mcu, mcu_pulls=mpulls)
    for name, spec in sorted(pulled.items()):
        pin = pins.get(name)
        if pin is None:
            continue
        g = board.net(mcu["ref"], pin)
        if board.unconnected(g):
            continue
        st = res.state(g, "por", vdd=vdd, vil=vil, vih=vih)
        facts.append({"mcu": mcu["ref"], "pin": pin, "name": name,
                      "net": board.label(g), "pull": spec["pull"],
                      "por": st["text"]})
        if st["state"] in ("toggling", "may_toggle", "firmware"):
            continue
        pos = pad_pos(bg, mcu["ref"], pin)
        head = (f"{mcu['ref']} {name} (pin {pin}, {board.label(g)}) is "
                f"pulled {spec['pull']} by the chip from reset until "
                "firmware drives it")
        why = f" ({spec['why']})" if spec.get("why") else ""
        seen = set()
        for ref, rpin, gg in near_nodes(board, g):
            if ref == mcu["ref"] or (ref, rpin) in seen:
                continue
            seen.add((ref, rpin))
            meta = board.pins[ref][rpin]
            gst = st if gg == g else res.state(gg, "por", vdd=vdd,
                                               vil=IO_VIL, vih=IO_VIH)
            if prefix(ref) == "Q" and meta["name"] in GATE_NAMES:
                vth = fet_vth(kb, board.value(ref))
                if vth is None or gst["volts"] is None \
                        or gst["volts"] < vth:
                    continue
                viols.append(checklib.violation(
                    SCRIPT, "warning", pad_pos(bg, ref, rpin), None,
                    board.label(gg), [mcu["ref"], ref],
                    f"{head}{why}, so {ref} ({board.value(ref)}) gate on "
                    f"{board.label(gg)} sits at {gst['volts']:.2f} V, at or "
                    f"above its minimum VGS(th) {vth:g} V: its load is on "
                    "before firmware runs. Hold the gate off with a pull "
                    "the chip's can't beat, or use a pin with no reset "
                    f"pull{unverified}", SOURCE, kind="load_on_at_reset",
                    pin=name, volts=checklib.rnd(gst["volts"], 3),
                    vth_min=vth))
                continue
            if prefix(ref) in PASSIVE_PREFIXES + CONNECTOR_PREFIXES \
                    or ref in board.shorts or prefix(ref) == "R" \
                    or PASSIVE_VALUE.match(board.value(ref) or ""):
                continue
            if gst["state"] not in ("high", "low"):
                continue
            on = part_on(meta["name"], meta.get("bar", False),
                         gst["state"])
            if not on:
                continue
            viols.append(checklib.violation(
                SCRIPT, "warning", pad_pos(bg, ref, rpin), None,
                board.label(gg), [mcu["ref"], ref],
                f"{head}{why}, so {ref} {meta['name']} "
                f"({board.value(ref)}) reads {gst['state']} and the part "
                "is on before firmware runs. Hold it off with an external "
                f"pull, or use a pin with no reset pull{unverified}",
                SOURCE, kind="load_on_at_reset", pin=name,
                part_pin=meta["name"]))
        if spec["strap"] or spec.get("offboard") is False:
            continue  # a boot pin on a programming header is meant to be
        ext = [p for p in st["pulls"] if not p.startswith(f"{mcu['ref']} ")]
        conns = [f"{ref}.{rpin}" for ref, rpin in board.nodes.get(g, [])
                 if prefix(ref) in CONNECTOR_PREFIXES]
        if not conns or ext or st["unknown"] and any(
                not u.endswith("a connector") for u in st["unknown"]):
            continue
        names = " ".join(board.names.get(g, [])) + " " + " ".join(
            board.pins[c.split(".")[0]][c.split(".")[1]]["name"]
            for c in conns)
        if DEBUG_NAMES.search(names):
            continue
        viols.append(checklib.violation(
            SCRIPT, "warning", pos, None, board.label(g), [mcu["ref"]],
            f"{head}{why}, and the net leaves the board on "
            f"{', '.join(sorted(conns))} with nothing else holding it: "
            f"whatever is plugged in sees it {st['state']} from power-on "
            "and through every reset (an LED strip or a load switch "
            "comes on early). Add an external pull to the idle level, or "
            f"move the signal to a pin with no reset pull{unverified}",
            SOURCE, kind="reset_pull_offboard", pin=name))
    return viols, facts


GATE_NAMES = ("G", "GATE")


def gates_on(board, g) -> list[tuple]:
    """(ref, pin, gate net group) of every FET gate on net group g or one
    resistor away from it."""
    groups = [g]
    for ref, pin in board.nodes.get(g, []):
        if prefix(ref) == "R" and len(board.pins[ref]) == 2:
            other = [q for q in board.pins[ref] if q != pin][0]
            groups.append(board.net(ref, other))
    out = []
    for gg in groups:
        for ref, pin in board.nodes.get(gg, []):
            if prefix(ref) == "Q" and \
                    board.pins[ref][pin]["name"] in GATE_NAMES:
                out.append((ref, pin, gg))
    return out


def fet_vth(kb: dict, value: str) -> float | None:
    for row in kb.get("fets") or []:
        if re.search(row["match"], value or "", re.I):
            return row["vth_min"]
    return None


def find_netlist(pcb: Path) -> Path | None:
    own = pcb.with_suffix(".net")
    if own.is_file():
        return own
    nets = sorted(pcb.parent.glob("*.net"))
    return nets[0] if len(nets) == 1 else None


def check(pcb: Path, netlist: Path | None = None, kb: dict | None = None):
    kb = load_kb() if kb is None else kb
    bg = geom.load_board(pcb)
    netlist = netlist or find_netlist(pcb)
    if netlist is None:
        mcus = [ref for ref, fp in bg.footprints.items()
                if any(r["_match"].search(fp.get("value") or "")
                       for r in kb.get("mcus") or [])]
        if not mcus:
            return [], {"netlist": None, "mcus": [],
                        "nothing_to_judge": "no netlist beside the board, "
                                            "and no known MCU on it"}
        return [checklib.violation(
            SCRIPT, "warning", None, None, None, mcus,
            f"{', '.join(sorted(mcus))}: no netlist beside {pcb.name} "
            f"({pcb.with_suffix('.net').name}), so the straps and reset "
            "states weren't checked: export it (kicad-cli sch export "
            "netlist)", SOURCE, kind="no_netlist")], {"netlist": None,
                                                      "mcus": mcus}
    board, ties = netlist_board(netlist, bg)
    viols, straps, ios = [], [], []
    mcus = find_mcus(board, kb)
    pulls = []
    for mcu in mcus:
        v, f = check_mcu(board, kb, mcu, bg)
        viols += v
        straps += f
        v, f = check_reset_pulls(board, kb, mcu, bg)
        viols += v
        pulls += f
    v, f = check_expanders(board, kb, mcus[0] if mcus else None, bg)
    viols += v
    ios += f
    values = {r: board.value(r) for r in board.pins}
    unknown = {r: values[r] for r in unknown_mcus(values, kb)}
    for ref, val in sorted(unknown.items()):
        viols.append(checklib.violation(
            SCRIPT, "warning", None, None, None, [ref],
            f"{ref} ({val}) is an MCU reference/reset_states.yaml has no row "
            "for, so its straps and reset pulls weren't judged: add its row "
            "from its datasheet", SOURCE, kind="mcu_unknown"))
    seen = {"netlist": str(netlist),
            "mcus": [m["ref"] for m in mcus],
            "mcus_unknown": sorted(unknown),
            "straps": straps, "reset_pulls": pulls,
            "expander_pins": ios, "net_ties": ties}
    left = [f"{f['mcu']} {f['strap']}: {f['not_judged']}" for f in straps
            if f.get("not_judged")]
    if left:
        seen["not_judged"] = left
    if not mcus and not ios:
        seen["nothing_to_judge"] = mcu_verdict(mcus, unknown)
    return viols, seen


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcb", required=True)
    ap.add_argument("--netlist")
    ap.add_argument("--out", help="write the JSON report here")
    args = ap.parse_args(argv)
    pcb = Path(args.pcb)
    if not pcb.is_file():
        raise checklib.CheckError(f"board not found: {pcb}")
    nl = Path(args.netlist) if args.netlist else None
    if nl is not None and not nl.is_file():
        raise checklib.CheckError(f"netlist not found: {nl}")
    viols, seen = check(pcb, nl)
    return checklib.report(SCRIPT, pcb, viols, checked=[seen],
                           nothing_to_judge=seen.get("nothing_to_judge"),
                           not_judged=seen.get("not_judged")), args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    sys.exit(main())
