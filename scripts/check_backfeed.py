"""check_backfeed.py - a powered domain driving current into a domain
that can be off (#394, power-domain back-feed).

The mode: a part whose rail is off still has its pins' input protection
diodes to that rail. A push-pull output, a pull-up or a transistor junction
on a live rail that reaches such a pin feeds the dead rail through the
diode: the part half powers up, latches or browns out, and the rail never
falls to 0 V for a clean reset. linux-computer found it by hand: the CH340
on the USB jack's own LDO drives DTR/RTS through NPN base-collector
junctions into the SoC's +3V3 reset pull-up (0.27 mA) until a BAT54 went
in each collector.

How the domains are read (from the netlist only):
 - power nets: a net with a rail name and volts, or a power_in/power_out
   pin on it (not a converter's SW/LX or bootstrap pin); never ground. Nets joined by a 2-pin L, FB, F, NT, JP, SB or
   a resistor of 10 ohm or less are one rail.
 - sources of a rail: a regulator (an IC with a power_out pin or a VOUT/OUT
   pin on the rail, or a SW/LX pin whose inductor ends on it) is on when its inputs (its other power_in rails, bar
   cap-only pins) are on and its enable is on (no enable pin: always); a diode from one rail to
   another ORs the anode's rail in; a FET between two rails is a switch
   (gate on ground: always on). An enable tied to a rail or pulled up to it
   follows that rail; tied to ground it is off (on, for an active-low
   name); driven by another regulator's PG pin it follows that regulator's
   output; driven by an IC output, a connector or a transistor it is an
   independent switch; anything else leaves the rail unknown.
 - independent inputs: a connector no on-board source drives (all the
   power nets entering on one connector come and go together: one cable,
   one supply), a battery, and the independent switches above. Every
   combination is tried (up to 16 inputs); a rail on a cycle with nothing
   else feeding it counts as off. A connector rail that a part with its
   own VSYS/VIN/VBUS/5V pin on another rail also sits on may be that
   module's output (a Pico's 3V3, typed power_in): unknown.
A domain pair is judged only when both rails' sources and enables are
known; a pair with an unknown rail goes in not_judged with the reason.

A part's domain: its I/O supply pins (VDDIO, VIO, VCCIO...), else its
other supply pins (VDD, VCC; not VBAT, VDDA, VREF, VIN); a translator's A
and B pins take VCCA and VCCB. A part whose supply pins sit on rails that
can be apart is not judged. Regulators are left out as both sources and
sinks (their control pins are judged by the enable reading above).

Sources (domain A on): a push-pull IC output (pin type output, tri_state
or bidirectional; not an I2C pin or a pin on an I2C-named net) and a
pull-up resistor to rail A. Sinks (domain B off): any signal pin of an IC
on B with no reference/ioff.yaml row (a part whose datasheet gives Ioff
blocks the path), a pull-up resistor to rail B, a diode or transistor
junction into rail B. Paths run through series resistors (adding their ohms; up to
1 Mohm), 0 ohm links and beads, diodes anode to cathode, an NPN's base to
collector or emitter and a PNP's collector or emitter to base, up to four
parts deep. A FET's body diode is not followed.

Current: (VA - 0.6 V per silicon junction, 0.3 V per Schottky, 1.8 V per
LED, 0.5 V for the sink's clamp diode) / the path's ohms.

Findings (kind backfeed, one per sink pin, its worst source):
  backfeed  error: 100 uA or more, or a dedicated output pin straight into
            the pin with no resistor; warning: 50-100 uA, or a GPIO-type
            pin straight in (firmware can hold it low while B is off).
            Below 50 uA it is listed in the small_backfeed fact.

The netlist is the board's own `<board>.net` beside the .kicad_pcb, or
--netlist; with none, or with no two domains that can be apart, the check
reports nothing_to_judge. --constraints adds its `voltages`.

CLI: --pcb board.kicad_pcb [--netlist board.net] [--constraints c.json]
     [--out report.json]
     exit 0 pass / 1 violations / 2 error, per SPEC section 6.
"""
from __future__ import annotations

import argparse
import itertools
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "lib"))
import checklib  # noqa: E402
import engcalc  # noqa: E402

SCRIPT = "check_backfeed"
SOURCE = SCRIPT
IOFF = HERE.parent / "reference" / "ioff.yaml"

ERROR_UA = 100.0
WARN_UA = 50.0
MAX_ATOMS = 16
MAX_HOPS = 4
MAX_OHM = 1e6
ALIAS_OHM = 10.0
CLAMP_V = 0.5

_IC_RE = re.compile(r"^(U|IC|A|M|MOD|MD|N)\d")
_CONN_RE = re.compile(r"^(J|P|CN|X|CON|USB)\d")
_ALIAS_RE = re.compile(r"^(L|FB|F|NT|JP|SB)\d")
_POWER_TYPES = {"power_in", "power_out"}
_IO_SUPPLY = re.compile(
    r"^(VIO|V_?IO|VDDIO\w*|VDD_IO\w*|VCCIO\w*|VCC_IO\w*|IOVDD\w*|OVDD\w*|"
    r"VCCO\w*|VL|VLOGIC|VDDL|VDD_?LOGIC)$")
_SUPPLY = re.compile(r"^(V(CC|DD)\w*|[AD]?V(CC|DD)\w*|\+?\d+V\d*|3V3|5V|"
                     r"VS|V\+)$")
_NOT_IO = re.compile(
    r"^(AVDD\w*|AVCC\w*|VDDA\w*|VREF\w*|VBAT\w*|VBACKUP|VBUS\w*|VUSB\w*|"
    r"VDD_?USB\w*|VDD33_?USB|VCORE\w*|VDD_?CORE\w*|VCCINT\w*|VCCAUX\w*|"
    r"VCAP\w*|VPP|VLCD\w*|VIN\w*|VDD_?RTC|VRTC|VREG\w*|VOUT\w*|VEE|VSS\w*|"
    r"VM|VMOT\w*|VDRV|VDD_?RF\w*|VCC_?RF)$")
_VOUT = re.compile(r"^(VOUT\d*|OUT\d*|VO\d*|V_?OUT\w*|LDO_?OUT\w*)$")
_VIN = re.compile(r"^(VIN\d*|IN\d*|PVIN\d*|AVIN|VI|V_?IN\w*|VBUS|VCC|VDD)$")
_SW = re.compile(r"^(SW\d*|LX\d*|PH\d*|SW_?OUT\d*|SWITCH)$")
_MOD_IN = re.compile(r"^(VSYS|VIN\d*|VBUS|\+?5V\w*)$")
# a converter's switch node and bootstrap pins: whatever their pin type,
# they do not make a net a rail
_SW_NODE = re.compile(r"^(SW\d*|LX\d*|PH\d*|SW_?OUT\d*|SWITCH|"
                      r"C?BO?O?ST\d*|CB|BOOT\d*|BST\d*|CBOOT)$")
_EN = re.compile(r"^(EN\d*|ENABLE\d*|CE|ON|ON/OFF|RUN|CTRL|PWR_?EN|"
                 r"EN/UVLO|EN_?UVLO|SHDN|SD|SHUTDOWN|INH|STBY|STANDBY)$")
_EN_LOW = re.compile(r"^(SHDN|SD|SHUTDOWN|INH|STBY|STANDBY)$")
_PG = re.compile(r"^(PG\d*|PGOOD\d*|POK|PWRGD|POWER_?GOOD|PG/\w+)$")
_OD_NAME = re.compile(r"^(SDA\d*|SCL\d*|I2C\d*_?(SDA|SCL)|SMB(DAT|CLK)\w*|"
                      r"\w*_SDA|\w*_SCL)$")
_GPIO = re.compile(r"^(GPIO\d+|IO\d+|P[A-K]\d+|P\d+[._]\d+|RA\d|RB\d|RC\d|"
                   r"D\d+|A\d+|PIO\d+\w*)(/|$)")
_NPN = re.compile(r"NPN|3904|2222|BC54[6-9]|BC81[78]|BC84[6-9]|S8050|"
                  r"MMBT2222|PMBT3904|MMBTA0[56]|DTC\d", re.I)
_PNP = re.compile(r"PNP|3906|2907|BC55[6-9]|BC80[78]|BC85[6-9]|S8550|"
                  r"MMBTA[59]\d|DTA\d", re.I)
_SCHOTTKY = re.compile(r"BAT\d|SS\d|SB\d|1N58|1N581|B5\d|RB\d|PMEG|"
                       r"SCHOTTKY|MBR|SD\d", re.I)
_LED = re.compile(r"LED", re.I)


# --------------------------------------------------------------- netlist

def _norm(name):
    return re.sub(r"~\{([^}]*)\}", r"\1", name or "").strip().upper()


def _base_type(t):
    return (t or "").split("+", 1)[0]


def _is_ground(net):
    import netlist_audit as na  # noqa: PLC0415
    return na._is_ground_name(net)


def _ohm(comp):
    v = engcalc.parse_value(comp.get("value"))
    return float(v) if v is not None else None


def load_ioff(path: Path = IOFF) -> list[dict]:
    """reference/ioff.yaml's parts (its header says the form)."""
    import yaml  # noqa: PLC0415
    try:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise checklib.CheckError(f"ioff table {path}: {exc}") from exc
    return doc.get("parts") or []


def _ioff_row(rows, comp):
    texts = [comp.get("mpn") or "", comp.get("value") or "",
             (comp.get("libsource") or ("", ""))[1]]
    for r in rows:
        rx = re.compile(r["match"], re.I)
        if any(rx.search(t) for t in texts if t):
            return r
    return None


class Board:
    """The netlist read for this check."""

    def __init__(self, nl: dict, volts: dict[str, float]):
        self.comps = nl.get("components") or {}
        self.nets = nl.get("nets") or {}
        self.pins: dict[str, dict[str, dict]] = {}
        for net, ms in self.nets.items():
            for m in ms:
                self.pins.setdefault(m["ref"], {})[m["pin"]] = {
                    "net": net, "func": _norm(m.get("pinfunction")),
                    "type": _base_type(m.get("pintype"))}
        self.volts = volts

    def two_pin(self, ref):
        ps = self.pins.get(ref, {})
        if len(ps) != 2:
            return None
        a, b = sorted(ps.items())
        return a, b

    def text(self, ref):
        c = self.comps.get(ref, {})
        return " ".join([c.get("value") or "", c.get("mpn") or "",
                         (c.get("libsource") or ("", ""))[1],
                         c.get("libdesc") or ""])


def _live(net):
    return not net.startswith("unconnected-")


# ------------------------------------------------------------ the rails

class _UF:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        self.p[self.find(a)] = self.find(b)


def _diode_ends(b: Board, ref):
    """(anode net, cathode net, drop) of a 2-pin diode, else None."""
    tp = b.two_pin(ref)
    if not tp:
        return None
    (p1, i1), (p2, i2) = tp
    by = {i1["func"]: i1["net"], i2["func"]: i2["net"]}
    if "A" in by and "K" in by:
        a, k = by["A"], by["K"]
    elif {i1["func"], i2["func"]} <= {"", "1", "2", "~"}:
        # KiCad's Device:D: pin 1 is the cathode
        pp = dict([(p1, i1["net"]), (p2, i2["net"])])
        if set(pp) != {"1", "2"}:
            return None
        k, a = pp["1"], pp["2"]
    else:
        return None
    t = b.text(ref)
    drop = 1.8 if _LED.search(t) or ref.startswith("LED") else \
        0.3 if _SCHOTTKY.search(t) else 0.6
    return a, k, drop


def _roles(b: Board, ref):
    import netlist_audit as na  # noqa: PLC0415
    out = {}
    for p, i in b.pins.get(ref, {}).items():
        r = na._transistor_role(i["func"])
        if r:
            out[r] = i["net"]
    return out


class Domains:
    """Which rails can be on while another is off."""

    def __init__(self, b: Board):
        self.b = b
        self.uf = _UF()
        self.notes: list[str] = []
        self._power_nets()
        self._aliases()
        self.group_nets: dict[str, set[str]] = {}
        for n in self.power:
            self.group_nets.setdefault(self.uf.find(n), set()).add(n)
        self.regs: set[str] = set()
        self.atoms: list[str] = []
        self.unknown: dict[str, str] = {}
        # group -> [ (atoms set, deps set) ]: on when any term is all on
        self.drivers: dict[str, list[tuple[frozenset, frozenset]]] = {
            g: [] for g in self.group_nets}
        self._sources()
        self._resolve_unknown()
        self._enumerate()

    # -- nets
    def _power_nets(self):
        b = self.b
        self.power = set()
        for net, ms in b.nets.items():
            if not _live(net) or _is_ground(net):
                continue
            v = b.volts.get(net)
            if (v is not None and v > 0) or any(
                    _base_type(m.get("pintype")) in _POWER_TYPES
                    and not _SW_NODE.match(_norm(m.get("pinfunction")))
                    for m in ms):
                self.power.add(net)

    def _aliases(self):
        b = self.b
        for ref in b.comps:
            tp = b.two_pin(ref)
            if not tp:
                continue
            na_, nb_ = tp[0][1]["net"], tp[1][1]["net"]
            if na_ == nb_ or na_ not in self.power or nb_ not in self.power:
                continue
            if _ALIAS_RE.match(ref):
                self.uf.union(na_, nb_)
            elif re.match(r"^R\d", ref):
                ohm = _ohm(b.comps[ref])
                if ohm is not None and ohm <= ALIAS_OHM:
                    self.uf.union(na_, nb_)

    def g(self, net):
        return self.uf.find(net) if net in self.power else None

    def label(self, g):
        nets = sorted(self.group_nets.get(g, {g}),
                      key=lambda n: (self.b.volts.get(n) is None, len(n), n))
        return nets[0]

    def volts(self, g):
        vs = [self.b.volts[n] for n in self.group_nets.get(g, ())
              if self.b.volts.get(n)]
        if vs:
            return max(vs)
        return self.reg_v.get(g)

    # -- sources
    def _local_only(self, ref, net):
        """A power pin net only REF and capacitors sit on (a bypass pin)."""
        return all(m["ref"] == ref or re.match(r"^C\d", m["ref"])
                   for m in self.b.nets.get(net, []))

    def _sources(self):
        b = self.b
        self.reg_v: dict[str, float] = {}
        driven: set[str] = set()
        pending_en: list[tuple] = []
        for ref in sorted(b.comps):
            if not _IC_RE.match(ref):
                continue
            pins = b.pins.get(ref, {})
            outs = {self.g(i["net"]) for i in pins.values()
                    if i["net"] in self.power and (
                        i["type"] == "power_out" or _VOUT.match(i["func"]))}
            outs |= self._switch_outs(pins)
            outs.discard(None)
            if not outs:
                continue
            self.regs.add(ref)
            ins = set()
            for i in pins.values():
                gi = self.g(i["net"])
                if gi is None or gi in outs:
                    continue
                if i["type"] == "power_in" or _VIN.match(i["func"]):
                    if self._local_only(ref, i["net"]):
                        continue
                    ins.add(gi)
            from check_ratings import reg_out_volts  # noqa: PLC0415
            rv = reg_out_volts(b.comps[ref].get("mpn")
                               or b.comps[ref].get("value") or "")
            for o in outs:
                if rv and len(outs) == 1:
                    self.reg_v.setdefault(o, rv)
                pending_en.append((ref, o, frozenset(ins)))
            driven |= outs
        self._pending = pending_en
        for ref, o, ins in pending_en:
            en = self._enable(ref)
            if en is None:
                en = ((), ())  # no enable pin: on whenever its inputs are
            if en == "unknown":
                self.unknown.setdefault(
                    o, f"{ref}'s enable is not tied, pulled or driven by a "
                       "part this check can read")
                continue
            if en == "never":
                continue
            atoms, deps = en
            self.drivers[o].append((frozenset(atoms), ins | frozenset(deps)))
        # diodes and FETs between rails
        for ref in sorted(b.comps):
            if re.match(r"^D\d", ref):
                de = _diode_ends(b, ref)
                if de and self.g(de[0]) and self.g(de[1]) and \
                        self.g(de[0]) != self.g(de[1]):
                    ga, gk = self.g(de[0]), self.g(de[1])
                    self.drivers[gk].append((frozenset(), frozenset([ga])))
                    driven.add(gk)
            elif re.match(r"^Q\d", ref):
                r = _roles(b, ref)
                gs, gd = self.g(r.get("S", "")), self.g(r.get("D", ""))
                if gs and gd and gs != gd:
                    gate = r.get("G", "")
                    if _is_ground(gate):
                        self.drivers[gd].append(
                            (frozenset(), frozenset([gs])))
                    else:
                        a = f"{ref} on"
                        self.atoms.append(a)
                        self.drivers[gd].append(
                            (frozenset([a]), frozenset([gs])))
                    driven.add(gd)
        # connectors and batteries: independent inputs when nothing on the
        # board drives the rail
        import netlist_audit as na  # noqa: PLC0415
        bats = {self.g(n) for n in na._battery_nets(b.nets)} - {None}
        for g, nets in self.group_nets.items():
            if g in driven:
                continue
            conn = sorted({m["ref"] for n in nets for m in b.nets.get(n, [])
                           if _CONN_RE.match(m["ref"])})
            mod = None if g in bats else self._module_on(g)
            if conn and mod:
                self.unknown[g] = (
                    f"{conn[0]} carries it and {mod[0]} (fed through its "
                    f"{mod[1]} pin from {mod[2]}) may make it: which one "
                    "feeds it is not known")
            elif g in bats or conn:
                a = (f"{conn[0]} power" if conn
                     else f"battery {self.label(g)}")
                self.atoms.append(a)
                self.drivers[g].append((frozenset([a]), frozenset()))
            elif not self.drivers[g] and g not in self.unknown:
                self.unknown[g] = "no source on the board (no regulator, " \
                    "connector or battery feeds it)"

    def _module_on(self, g):
        """(ref, pin, rail) for a part with a supply pin on G and a module
        input pin (VSYS, VIN, VBUS, 5V) on another rail: a Pico's or a
        breakout's own regulator output, which its symbol may type
        power_in."""
        b = self.b
        for ref in sorted(b.pins):
            if not _IC_RE.match(ref) or ref in self.regs:
                continue
            pins = b.pins[ref].values()
            if not any(self.g(i["net"]) == g and i["type"] in _POWER_TYPES
                       for i in pins):
                continue
            for i in pins:
                gi = self.g(i["net"])
                if gi is not None and gi != g and _MOD_IN.match(i["func"]):
                    return ref, i["func"], self.label(gi)
        return None

    def _switch_outs(self, pins):
        """Rails a switcher's SW/LX pin feeds through its inductor."""
        b = self.b
        out = set()
        for i in pins.values():
            if not _SW.match(i["func"]) or i["net"] in self.power:
                continue
            for m in b.nets.get(i["net"], []):
                tp = b.two_pin(m["ref"])
                if tp and re.match(r"^L\d", m["ref"]):
                    far = next(x["net"] for q, x in tp if q != m["pin"])
                    if far in self.power:
                        out.add(self.g(far))
        return out

    def _enable(self, ref):
        """None: no enable pin (always on with its inputs); 'unknown';
        'never'; or (atoms, deps) it follows."""
        b = self.b
        ens = [(i["func"], i["net"]) for i in b.pins[ref].values()
               if _EN.match(i["func"])]
        if not ens:
            return None
        atoms, deps = set(), set()
        for func, net in ens:
            low = bool(_EN_LOW.match(func))
            if _is_ground(net):
                if low:
                    continue
                return "never"
            if not _live(net):
                return "unknown"
            if net in self.power:
                if low:
                    return "unknown"
                deps.add(self.g(net))
                continue
            r = self._en_net(ref, net, low)
            if r is None:
                return "unknown"
            if r == "never":
                return "never"
            atoms |= r[0]
            deps |= r[1]
        return atoms, deps

    def _en_net(self, ref, net, low):
        """Read an enable net: (atoms, deps) | 'never' | None."""
        b = self.b
        pulls_up, pulls_down = set(), False
        drivers = []
        for m in b.nets.get(net, []):
            if m["ref"] == ref:
                continue
            r = m["ref"]
            tp = b.two_pin(r)
            if re.match(r"^R\d", r) and tp:
                far = next(i["net"] for p, i in tp if p != m["pin"])
                if far in self.power:
                    pulls_up.add(self.g(far))
                elif _is_ground(far):
                    pulls_down = True
                continue
            if re.match(r"^C\d", r):
                continue
            func = _norm(m.get("pinfunction"))
            typ = _base_type(m.get("pintype"))
            if _IC_RE.match(r) and _PG.match(func) and r in self.regs:
                drivers.append(("pg", r))
            elif _IC_RE.match(r) and typ in ("output", "bidirectional",
                                             "tri_state", "open_collector",
                                             "passive", "open_emitter"):
                drivers.append(("atom", f"{r} {func or m['pin']} on {net}"))
            elif _CONN_RE.match(r):
                drivers.append(("atom", f"{r}.{m['pin']} on {net}"))
            elif re.match(r"^Q\d", r):
                drivers.append(("atom", f"{r} on {net}"))
            elif _IC_RE.match(r):
                continue  # another part's enable input on the same net
            else:
                return None
        if low:
            if drivers:
                pass
            elif pulls_down and not pulls_up:
                return set(), set()
            else:
                return None
        for kind, what in drivers:
            if kind == "pg":
                outs = {o for rr, o, _ in self._pending if rr == what}
                if len(outs) != 1:
                    return None
                return set(), {outs.pop()}
        if drivers:
            a = drivers[0][1]
            self.atoms.append(a)
            return {a}, set()
        if len(pulls_up) == 1:
            return set(), set(pulls_up)
        return None

    def _resolve_unknown(self):
        changed = True
        while changed:
            changed = False
            for g, terms in self.drivers.items():
                if g in self.unknown:
                    continue
                for _atoms, deps in terms:
                    bad = next((d for d in deps if d in self.unknown), None)
                    if bad is not None:
                        self.unknown[g] = (f"fed from {self.label(bad)}, "
                                           "whose source is unknown")
                        changed = True
                        break

    # -- states
    def _enumerate(self):
        self.atoms = sorted(set(self.atoms))
        self.states: list[tuple[dict, frozenset]] = []
        if len(self.atoms) > MAX_ATOMS:
            self.too_many = True
            return
        self.too_many = False
        for bits in itertools.product((False, True), repeat=len(self.atoms)):
            on_atoms = {a for a, v in zip(self.atoms, bits) if v}
            on: set[str] = set()
            changed = True
            while changed:
                changed = False
                for g, terms in self.drivers.items():
                    if g in on or g in self.unknown:
                        continue
                    if any(t[0] <= on_atoms and t[1] <= on
                           for t in terms):
                        on.add(g)
                        changed = True
            self.states.append((dict(zip(self.atoms, bits)), frozenset(on)))

    def apart(self, ga, gb):
        """A witness assignment with GA on and GB off, else None."""
        for assign, on in self.states:
            if ga in on and gb not in on:
                return assign
        return None

    def ever_on(self, g):
        return any(g in on for _, on in self.states)

    def witness(self, assign):
        if not assign:
            return "always"
        on = [a for a, v in assign.items() if v]
        off = [a for a, v in assign.items() if not v]
        return "; ".join(x for x in (
            ("on: " + ", ".join(on)) if on else "",
            ("off: " + ", ".join(off)) if off else "") if x)


# ------------------------------------------------------------ part rails

def _translator_side(func):
    if re.match(r"^(A\d|\d+A$|SDA1|SCL1|IO_?VCCA\w*|A_\w+)", func):
        return "A"
    if re.match(r"^(B\d|\d+B$|SDA2|SCL2|IO_?VCCB\w*|B_\w+)", func):
        return "B"
    return None


def part_rail(d: Domains, ref, func):
    """(group, None) | (None, why not judged) | (None, None) not a part
    with a rail this check reads."""
    b = d.b
    pins = b.pins.get(ref, {})
    names = {i["func"]: i["net"] for i in pins.values()}
    if {"VCCA", "VCCB"} <= set(names) or {"VA", "VB"} <= set(names):
        side = _translator_side(func)
        if side is None:
            return None, None
        net = names.get(f"VCC{side}") or names.get(f"V{side}")
        g = d.g(net)
        return (g, None) if g else (None, None)
    io = {d.g(i["net"]) for i in pins.values()
          if _IO_SUPPLY.match(i["func"]) and d.g(i["net"])}
    if not io:
        io = {d.g(i["net"]) for i in pins.values()
              if d.g(i["net"]) and (i["type"] == "power_in"
                                    or _SUPPLY.match(i["func"]))
              and not (_NOT_IO.match(i["func"])
                       and not re.match(r"^V(CC|DD)\d*$", i["func"]))
              and not d._local_only(ref, i["net"])}
    if not io:
        return None, None
    if len(io) == 1:
        return next(iter(io)), None
    gs = sorted(io)
    for x, y in itertools.permutations(gs, 2):
        if x in d.unknown or y in d.unknown or d.apart(x, y):
            return None, (f"{ref}'s supply pins sit on rails that can be "
                          f"apart ({', '.join(d.label(g) for g in gs)})")
    return gs[0], None


# ------------------------------------------------------------- the paths

def _edges(d: Domains, net):
    """[(far net, ohm, drop, ref, how)] one part away from a signal NET."""
    b = d.b
    out = []
    for m in b.nets.get(net, []):
        r = m["ref"]
        tp = b.two_pin(r)
        if tp and re.match(r"^(R|RN)\d", r):
            far = next(i["net"] for p, i in tp if p != m["pin"])
            ohm = _ohm(b.comps[r])
            if ohm is None or ohm > MAX_OHM:
                continue
            out.append((far, ohm, 0.0, r, "R"))
        elif tp and _ALIAS_RE.match(r):
            far = next(i["net"] for p, i in tp if p != m["pin"])
            out.append((far, 0.0, 0.0, r, "link"))
        elif re.match(r"^(D|LED)\d", r):
            de = _diode_ends(b, r)
            if de and de[0] == net:
                out.append((de[1], 0.0, de[2], r, "diode"))
        elif re.match(r"^Q\d", r):
            ro = _roles(b, r)
            if "B" not in ro:
                continue
            t = b.text(r)
            pol = "P" if _PNP.search(t) else "N" if _NPN.search(t) else None
            if pol == "N" and ro["B"] == net:
                out += [(ro[k], 0.0, 0.6, r, f"B-{k}") for k in "CE"
                        if k in ro]
            elif pol == "P" and net in (ro.get("C"), ro.get("E")):
                k = "C" if ro.get("C") == net else "E"
                out.append((ro["B"], 0.0, 0.6, r, f"{k}-B"))
    return [e for e in out if _live(e[0]) and not _is_ground(e[0])]


def _paths(d: Domains, start, start_ohm):
    """Yield (net, ohm, drop, parts) reachable from signal net START."""
    stack = [(start, start_ohm, 0.0, (), frozenset([start]))]
    while stack:
        net, ohm, drop, parts, seen = stack.pop()
        yield net, ohm, drop, parts
        if net in d.power or len(parts) >= MAX_HOPS:
            continue
        for far, o, dr, ref, how in _edges(d, net):
            if far in seen or ohm + o > MAX_OHM or ref in [p[0] for p in parts]:
                continue
            stack.append((far, ohm + o, drop + dr, parts + ((ref, how),),
                          seen | {far}))


def analyse(nl: dict, volts: dict[str, float], ioff: list[dict]):
    """(hits, facts): hits are dicts for violations, facts the rest."""
    b = Board(nl, volts)
    d = Domains(b)
    facts: dict = {"domains": {}, "small_backfeed": [], "checked": 0}
    not_judged: list[str] = []
    for g in sorted(d.group_nets):
        facts["domains"][d.label(g)] = (
            {"unknown": d.unknown[g]} if g in d.unknown else
            {"nets": sorted(d.group_nets[g]), "volts": d.volts(g)})
    if d.too_many:
        facts["nothing_to_judge"] = (
            f"{len(d.atoms)} independent power inputs (over {MAX_ATOMS}); "
            "the domains were not worked out")
        return [], facts
    known = [g for g in d.group_nets if g not in d.unknown]
    if not any(d.apart(x, y) for x, y in itertools.permutations(known, 2)):
        facts["nothing_to_judge"] = ("no two rails whose sources are known "
                                     "can be on and off apart")
        for g, why in sorted(d.unknown.items()):
            not_judged.append(f"{d.label(g)}: {why}")
        facts["not_judged"] = not_judged
        return [], facts

    # sources: (group, volts, start net, start ohm, what, kind)
    sources = []
    rails: dict[tuple, tuple] = {}
    jd_parts: set[str] = set()

    def rail_of(ref, func):
        key = (ref, func if ({"VCCA", "VCCB"} <= {
            i["func"] for i in b.pins[ref].values()}) else None)
        if key not in rails:
            rails[key] = part_rail(d, ref, func)
        return rails[key]

    for ref in sorted(b.pins):
        if not _IC_RE.match(ref) or ref in d.regs:
            continue
        for pnum, i in sorted(b.pins[ref].items()):
            net = i["net"]
            if net in d.power or _is_ground(net) or not _live(net):
                continue
            if i["type"] not in ("output", "tri_state", "bidirectional"):
                continue
            if _OD_NAME.match(i["func"]) or _OD_NAME.match(
                    net.rsplit("/", 1)[-1].upper()):
                continue  # open-drain by name, or a pin on an I2C bus
            g, why = rail_of(ref, i["func"])
            if g is None:
                if why:
                    jd_parts.add(why)
                continue
            kind = "gpio" if (i["type"] == "bidirectional"
                              or _GPIO.match(i["func"])) else "output"
            sources.append((g, net, 0.0, f"{ref} {i['func'] or pnum}",
                            kind, ref))
    for net in sorted(b.nets):
        if net in d.power or _is_ground(net) or not _live(net):
            continue
        for far, ohm, _dr, r, how in _edges(d, net):
            if how == "R" and far in d.power:
                sources.append((d.g(far), net, ohm,
                                f"{r} pull-up", "pull", r))

    best: dict[tuple, dict] = {}
    unknown_pairs: dict[str, set] = {}
    checked = 0
    for g_a, start, ohm0, what, kind, sref in sources:
        if g_a in d.unknown:
            continue
        va = d.volts(g_a)
        for net, ohm, drop, parts in _paths(d, start, ohm0):
            sinks = []
            if net in d.power:
                if parts and parts[-1][1] != "link":
                    g_b = d.g(net)
                    sinks.append((g_b, ("rail", d.label(g_b)), 0.0,
                                  f"{parts[-1][0]} into {d.label(g_b)}",
                                  [parts[-1][0]]))
            else:
                for m in b.nets.get(net, []):
                    r = m["ref"]
                    if (not _IC_RE.match(r) or r in d.regs or r == sref
                            or _base_type(m.get("pintype")) in _POWER_TYPES
                            | {"no_connect"}):
                        continue
                    func = _norm(m.get("pinfunction"))
                    g_b, why = rail_of(r, func)
                    if g_b is None:
                        if why:
                            jd_parts.add(why)
                        continue
                    if _ioff_row(ioff, b.comps.get(r, {})):
                        continue
                    sinks.append((g_b, (r, m["pin"]), CLAMP_V,
                                  f"{r} {func or m['pin']} (pin {m['pin']})",
                                  [r]))
            for g_b, key, clamp, sink_what, refs in sinks:
                if g_b == g_a or g_b is None:
                    continue
                if g_b in d.unknown:
                    unknown_pairs.setdefault(d.label(g_b), set()).add(
                        d.unknown[g_b])
                    continue
                if not d.ever_on(g_b):
                    unknown_pairs.setdefault(d.label(g_b), set()).add(
                        "it is on in no state this check can read")
                    continue
                checked += 1
                wit = d.apart(g_a, g_b)
                if wit is None:
                    continue
                if va is None:
                    unknown_pairs.setdefault(d.label(g_a), set()).add(
                        "its volts are unknown")
                    continue
                v = va - drop - clamp
                if v <= 0:
                    continue
                direct = ohm <= 0
                ua = None if direct else v / ohm * 1e6
                if direct:
                    sev = "error" if kind == "output" else "warning"
                elif ua >= ERROR_UA:
                    sev = "error"
                elif ua >= WARN_UA:
                    sev = "warning"
                else:
                    sev = "fact"
                hit = {"severity": sev, "ua": ua, "net": start,
                       "source": what, "sink": sink_what,
                       "from": d.label(g_a), "to": d.label(g_b),
                       "via": [p[0] for p in parts],
                       "ohm": ohm, "witness": d.witness(wit),
                       "refs": sorted({sref, *refs, *[p[0] for p in parts]}),
                       "kind": kind, "into_rail": key[0] == "rail"}
                rank = (2 if sev == "error" else 1 if sev == "warning"
                        else 0, float("inf") if ua is None else ua)
                old = best.get(key)
                if old is None or rank > old["_rank"]:
                    hit["_rank"] = rank
                    best[key] = hit
    hits = []
    for key, h in sorted(best.items(), key=lambda kv: str(kv[0])):
        h.pop("_rank")
        if h["severity"] == "fact":
            facts["small_backfeed"].append(
                f"{h['source']} -> {h['sink']}: {h['ua']:.0f} uA "
                f"({h['from']} on, {h['to']} off)")
        else:
            hits.append(h)
    facts["checked"] = checked
    for lab, whys in sorted(unknown_pairs.items()):
        not_judged.append(f"paths into or from {lab}: "
                          + "; ".join(sorted(whys)))
    not_judged += sorted(jd_parts)
    facts["not_judged"] = not_judged
    return hits, facts


def _msg(h):
    via = f" through {', '.join(h['via'])}" if h["via"] else ""
    amt = ("driven straight in" if h["ua"] is None
           else f"about {h['ua']:.0f} uA")
    tail = (" (a GPIO: fine only if firmware holds it low while "
            f"{h['to']} is off)" if h["kind"] == "gpio" and h["ua"] is None
            else "")
    into = (h["to"] if h.get("into_rail")
            else f"{h['sink']} on {h['to']}")
    return (f"{h['source']} on {h['from']} feeds {into}"
            f"{via}: {amt} into a rail that can be off ({h['witness']}). "
            "Block it with a series diode, an Ioff buffer, or power the "
            f"pull-up from {h['to']}{tail}")


def check(pcb: Path, netlist: Path | None = None,
          constraints: Path | None = None, ioff: list | None = None):
    import netlist_audit as na  # noqa: PLC0415
    from check_straps import find_netlist  # noqa: PLC0415
    netlist = netlist or find_netlist(pcb)
    if netlist is None:
        return [], {"netlist": None,
                    "nothing_to_judge": "no netlist beside the board"}
    nl = na.parse_netlist(netlist)
    cons = checklib.load_json(constraints, "constraints") if constraints \
        else {}
    volts = na._rail_volts(cons, nl.get("nets") or {})
    hits, facts = analyse(nl, volts, load_ioff() if ioff is None else ioff)
    viols = [checklib.violation(
        SCRIPT, h["severity"], None, None, h["net"], h["refs"], _msg(h),
        SOURCE, kind="backfeed", current_ua=(round(h["ua"], 1)
                                             if h["ua"] is not None
                                             else None),
        from_rail=h["from"], to_rail=h["to"], witness=h["witness"])
        for h in hits]
    facts["netlist"] = netlist.name
    return viols, facts


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcb", required=True)
    ap.add_argument("--netlist")
    ap.add_argument("--constraints")
    ap.add_argument("--out", help="write the JSON report here")
    args = ap.parse_args(argv)
    pcb = Path(args.pcb)
    if not pcb.is_file():
        raise checklib.CheckError(f"board not found: {pcb}")
    nl = Path(args.netlist) if args.netlist else None
    if nl is not None and not nl.is_file():
        raise checklib.CheckError(f"netlist not found: {nl}")
    cons = Path(args.constraints) if args.constraints else None
    viols, seen = check(pcb, nl, cons)
    return checklib.report(SCRIPT, pcb, viols, checked=[seen],
                           nothing_to_judge=seen.get("nothing_to_judge"),
                           not_judged=seen.get("not_judged")), args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    sys.exit(main())
