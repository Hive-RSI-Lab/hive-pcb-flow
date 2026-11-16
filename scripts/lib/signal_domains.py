"""signal_domains.py - netlist_audit's signal-level rules: a control
signal's active level across a connection, and an I/O pin's supply
domain against the level its net carries.

Active level (polarity):
 - polarity_mismatch: two dedicated control pins of one kind (resets,
   chip selects, output enables, interrupts, enables...) whose names say
   opposite active levels meet on one line: directly, through a series
   resistor or 0 ohm link (a net rename), through a non-inverting buffer
   or level translator channel, or on a connector whose pins are named.
   A reset output that asserts low reaching a reset input that asserts
   high holds the part in reset whenever the line is idle.
 - polarity_net_name: a line whose name says it asserts low (~{RST},
   RST#, nRST, RST_N) that reaches only control pins asserting high of
   that kind, or the other way about, with no pin of the named level on it.
 - idle_pull_asserted: a reset or hold input nothing on its line can
   drive, held by its pull resistors at the level that asserts it (an
   active-low reset pulled down, an open-drain reset line with only a
   pull-down). Enables are left out: one pulled on is an always-on strap.
 - latch_enable_held: a latch or shift register's latch-enable, strobe
   or storage clock held where its outputs never update: tied to a rail,
   or held by its only pull with nothing to drive it. The level comes
   from the family's datasheet (_LATCHES), not the symbol: 74x373/573 LE
   and 4094 STR are transparent while high (held high is a buffer, held
   low never updates), 74x259 LE while low; a 74x595 RCLK or a 74x165 ~PL
   has to move, so held either way is a fault. A pin named like one (LE,
   STB, STROBE, RCLK, XLAT...) on a part not in the table is not_judged.

A pin's active level comes from its name: KiCad's overbar ~{X}, a
leading ~ or /, a # or * suffix, an N, _N, _B or _L suffix, a lowercase n
or upper N prefix (nRST, NRST); with none of them it asserts high. A pin
with a port or GPIO name (PA3, GPIO12, IO5, P1.2) is set by firmware and
is never judged, nor is any pin of a part whose name says it inverts
(74x04, x14, x240, x540...), nor a multi-function name (RDY0/SLRD)
other than a reset role, nor a pin strapped to a rail or ground. A
select, strobe, interrupt or ready pin drawn plain counts as asserting
high only on a part whose symbol marks another of those active-low;
elsewhere the symbol just left the bars off. On a net name _B is not a
low marker (PG_B is channel B as often). Kinds that fold together: a power-good
output and an enable or run input; a reset and a run input (RP2040 RUN
is reset asserting low); shutdown and power-down are enables asserting
the other way.

Supply domain:
 - io_domain_overdrive (error): an output of an IC on one rail reaches,
   directly or through a series resistor of 1 kohm or less, an input of
   an IC whose datasheet row (reference/io_tolerance.yaml, else
   reference/pin_abs_max.yaml) says it takes less than that rail: a 5 V
   output into a 3.3 V part that isn't 5 V tolerant.
 - io_domain_underdrive (warning): an output whose high (its rail) is
   below the receiver's VIH from a row (io_tolerance.yaml, else
   pin_leakage.yaml): a 3.3 V output into a 5 V CMOS input that needs
   0.7 x VCC.
 - io_domain_pull_rail (error over, warning under): an open-drain line
   (an open-collector pin or I2C SDA/SCL) pulled up to a rail above a
   listed receiver's limit, or below its VIH.
A part's I/O rail is the io_levels.yaml driver row's supply, an
io_supply.yaml row, a row's own supply regex, the rails of pins named as
the I/O supply (VIO, VDDIO, VCCIO, IOVDD, OVDD, VCCO, VDDQ, VL), and on a
translator (VCCA and VCCB) the A or B side; else the one rail of known
volts on its other supply pins. A part whose supply pins sit on rails of
different volts, or of unknown volts, is not judged. A pair rule 19
(io_overvoltage) already judges, both parts in io_levels.yaml, is left to
it. A receiver with no row whose rail the driver's exceeds by more than
0.5 V is listed in not_judged, never reported as a fault.
"""
from __future__ import annotations

import re
from pathlib import Path

IO_TOLERANCE = Path(__file__).resolve().parents[2] / "reference" / \
    "io_tolerance.yaml"

# ---------------------------------------------------------------- polarity

# stem -> (kind, inverted): inverted is True when the plain (unmarked)
# name asserts its kind low: RUN is a reset asserting low, SHDN an enable
# asserting low (high shuts down), FAULT a power-good asserting low.
_STEMS: dict[str, tuple[str, bool]] = {}
for _s in ("RESET", "RST", "MR", "RESETDRV", "RESET_DRV", "RSTIN", "RSTOUT",
           "SYSRST", "PERST", "POR"):
    _STEMS[_s] = ("reset", False)
_STEMS["RUN"] = ("reset", True)
for _s in ("EN", "ENABLE", "ON"):
    _STEMS[_s] = ("enable", False)
for _s in ("SHDN", "SHUTDOWN", "PD", "PWDN", "PWRDN", "STBY", "STANDBY",
           "SLEEP", "SLP"):
    _STEMS[_s] = ("enable", True)
for _s in ("PG", "PGOOD", "PWRGD", "PGD", "POK", "PWROK", "PWR_GOOD",
           "PWR_OK", "PWRGOOD"):
    _STEMS[_s] = ("good", False)
for _s in ("FAULT", "FLT"):
    _STEMS[_s] = ("good", True)
for _s in ("CS", "SS", "CE", "CSEL"):
    _STEMS[_s] = ("select", False)
for _s in ("OE",):
    _STEMS[_s] = ("oe", False)
for _s in ("WE",):
    _STEMS[_s] = ("we", False)
for _s in ("RD",):
    _STEMS[_s] = ("rd", False)
for _s in ("WR",):
    _STEMS[_s] = ("wr", False)
for _s in ("IRQ", "INT", "NMI", "ALERT", "DRDY"):
    _STEMS[_s] = ("irq", False)
for _s in ("HOLD",):
    _STEMS[_s] = ("hold", False)
for _s in ("WAIT", "BUSY"):
    _STEMS[_s] = ("ready", True)
for _s in ("READY", "RDY"):
    _STEMS[_s] = ("ready", False)

# kinds that are one signal seen from two sides (a supervisor's reset out
# into a regulator's enable; a power-good into an enable)
_GROUP = {"reset": "run", "enable": "run", "good": "run"}

# a suffix that marks a name active-low; "B" is left out for EN (ENB is
# often channel B's enable on a motor driver)
_LOW_SUFFIX = re.compile(r"^(?P<stem>.+?)(?:_N|_B|_L|_BAR|BAR|N|B)$")
_GPIO = re.compile(
    r"^(P[A-Z]\d{1,2}|P\d{1,2}[._]?\d{1,2}|GPIO_?\d+|GP\d+|IO_?\d+|"
    r"R[A-Z]\d{1,2}|PIO\d?_?\d+|D\d{1,2}|A\d{1,2}|MIO\d+|EMIO\d*|"
    r"PORT\w*|ADC\d*|AIN\d*|SPI\d*_\w+)$")
# part values that invert between input and output (never joined)
_INVERTING = re.compile(
    r"74\w*?(04|05|06|14|19|240|540|1G04|1G14|2G04|2G14|1G06|2G06|"
    r"1GU04|U04|00|02|10|20|30|132|1G00|1G02)\b", re.I)
# non-inverting buffers and translators whose A/B or A/Y channels pass a
# level through unchanged
_PASS = re.compile(
    r"(TXB0|TXS0|TXU0|LSF0|PCA9306|NVT20|GTL2|FXMA|FXLA|NTB0|NTS0|"
    r"74\w*?(07|17|34|125|126|244|245|541|1T45|2T45|4T245|8T245|16245|"
    r"1G07|1G17|1G34|1G125|1G126|2G07|2G17|2G34|2G125|3G07|3G17|3G34))",
    re.I)


def _segments(raw: str) -> list[tuple[str, bool]]:
    """(name, overbarred) for each role of a pin name: 'PC6/~{RESET}' ->
    [('PC6', False), ('RESET', True)]. A leading '/' or '~' marks the
    first role active-low."""
    s = (raw or "").strip()
    out = []
    lead_low = False
    if s.startswith("/") or (s.startswith("~") and not s.startswith("~{")):
        lead_low, s = True, s[1:]
    for i, part in enumerate(p for p in re.split(r"[/,]", s) if p.strip()):
        part = part.strip()
        bar = bool(re.fullmatch(r"~\{[^}]*\}\S*", part)) or (
            i == 0 and lead_low)
        name = re.sub(r"~\{([^}]*)\}", r"\1", part)
        out.append((name.strip(), bar))
    return out


def name_level(raw: str) -> tuple[str, str] | None:
    """(kind, asserted level 'low'|'high') a control name says, or None:
    not a control name, or a port/GPIO name set by firmware. 'low' when
    any marker says active-low."""
    full = name_level_full(raw)
    return full[:2] if full else None


def name_level_full(raw: str) -> tuple[str, str, bool] | None:
    """name_level plus whether the name carries an active-low marker
    (an overbar, #, an N or _N suffix...). A multi-function pin name
    (RDY0/SLRD, two roles neither a port name) is ambiguous: firmware
    picks the role, so only a reset role in it is judged."""
    segs = _segments(raw)
    if not segs:
        return None
    found = None
    gpio = False
    roles = 0
    for name, bar in segs:
        u = re.sub(r"\s+", "", name).upper()
        if _GPIO.match(u):
            gpio = True
            continue
        roles += 1
        lowmark = bar
        if u.endswith(("#", "*", "\\")):
            u, lowmark = u.rstrip("#*\\"), True
        u = u.strip("_")
        hit = _stem_of(u, name)
        if hit is None:
            continue
        stem, low2 = hit
        kind, inv = _STEMS[stem]
        low = lowmark ^ low2
        # asserted level of the KIND's sense: an unmarked stem asserts
        # high; an inverted stem's high is the kind's low
        level_low = low ^ inv
        if found is None:
            found = (kind, "low" if level_low else "high", low)
    # a port pin's other roles are firmware's, but a reset (ATmega
    # PC6/~{RESET}) is a fuse default
    if (gpio or roles > 1) and found and found[0] != "reset":
        return None
    return found


def _stem_of(u: str, orig: str) -> tuple[str, bool] | None:
    """(stem, marked-low) for an upper-cased name, digits after the stem
    allowed (CS1, ~{CE2}, INT1)."""
    base = re.sub(r"\d+$", "", u)
    if base in _STEMS:
        return base, False
    m = _LOW_SUFFIX.match(base)
    if m:
        st = m.group("stem").rstrip("_")
        tail = base[len(st):].lstrip("_")
        if st in _STEMS and not (st in ("EN", "ON") and tail in ("B",)) \
                and not (len(st) <= 2 and tail == "L"):
            return st, True
    # nRESET (lowercase n) or NRST / NCS (upper N before a stem of 2+)
    o = re.sub(r"\s+", "", orig).lstrip("~/").strip("_")
    if o[:1] == "n" and o[1:2].isupper():
        st = re.sub(r"\d+$", "", o[1:].upper().rstrip("#*")).strip("_")
        if st in _STEMS:
            return st, True
    if base.startswith("N") and len(base) >= 3:
        st = base[1:].lstrip("_")
        if st in _STEMS and st not in ("ON", "OE", "EN"):
            return st, True
    return None


def net_name_level(net: str) -> tuple[str, str] | None:
    """The kind and level a net's own name says, only when it carries an
    explicit active-low marker or an inverted stem; the sheet path
    (/sub/NAME) is dropped. A plain name says nothing: nets are often
    named after the signal, not its level."""
    name = net.rsplit("/", 1)[-1] if not net.startswith("~") else net
    if not name or name.startswith("unconnected-") or name.startswith("Net-"):
        return None
    lv = name_level(name)
    if lv is None:
        return _compound_net_level(name)
    u = name.upper()
    explicit = bool(re.search(r"~|#|\*|_N$|_L$|N$|^N", u)) or \
        name[:1] == "n"
    return lv if explicit else None


def _compound_net_level(name: str) -> tuple[str, str] | None:
    """A net named for its signal and part ('~{PHY_RST}', 'nCODEC_RESET',
    'SD_CS_N', 'LCD_RST#'): an explicit low marker on the whole name,
    and a control stem as one of its '_' words (the last one wins)."""
    s = name.strip()
    low = False
    m = re.fullmatch(r"~\{([^}]*)\}", s)
    if m:
        s, low = m.group(1), True
    elif s[:1] in "~/" and len(s) > 1:
        s, low = s[1:], True
    if s.endswith(("#", "*")):
        s, low = s.rstrip("#*"), True
    # not _B: on a net it is as often channel B (PG_B) as a bar
    m = re.fullmatch(r"(.+?)_(N|L|BAR)", s, re.I)
    if m and m.group(2).isupper():
        s, low = m.group(1), True
    m = re.fullmatch(r"(?:n(?=[A-Z])|N_)(.+)", s)
    if m:
        s, low = m.group(1), True
    if not low:
        return None
    stem = None
    for w in s.upper().split("_"):
        w = re.sub(r"\d+$", "", w)
        if w in _STEMS:
            stem = w
    if stem is None:
        return None
    kind, inv = _STEMS[stem]
    return kind, "high" if inv else "low"


def _pin(nets, ref, pnum, comp_pins):
    net = comp_pins.get(ref, {}).get(pnum)
    return next((m for m in nets.get(net, []) if m["ref"] == ref
                 and m["pin"] == pnum), None)


def _joined_groups(nets, comps, comp_pins, rails, parse_value, part_kind,
                   is_ground, max_series=1000.0):
    """Union nets joined by a 2-pin resistor of max_series ohm or less (a
    series resistor or 0 ohm link), or by a channel of a non-inverting
    buffer or translator (A1<->B1, 1A<->1Y). Returns ({net: root},
    {root: [(how, ref)]})."""
    parent: dict[str, str] = {n: n for n in nets}
    how: dict[str, list[str]] = {}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def join(a, b, why):
        if a not in parent or b not in parent:
            return
        if a in rails or b in rails or is_ground(a) or is_ground(b):
            return
        if a.startswith("unconnected-") or b.startswith("unconnected-"):
            return
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra
            how.setdefault(ra, []).extend(how.pop(rb, []))
        how.setdefault(ra, []).append(why)

    for ref, pins in comp_pins.items():
        comp = comps.get(ref, {})
        if part_kind(ref) == "R" and len(pins) == 2:
            ohm = parse_value(comp.get("value"))
            if ohm is not None and float(ohm) <= max_series:
                a, b = list(pins.values())
                join(a, b, f"{ref} {comp.get('value')}")
            continue
        val = f"{comp.get('value') or ''} {(comp.get('libsource') or ('', ''))[1]}"
        if not _PASS.search(val) or _INVERTING.search(val):
            continue
        side: dict[tuple[str, str], str] = {}
        for pnum, net in pins.items():
            m = _pin(nets, ref, pnum, comp_pins) or {}
            f = re.sub(r"~\{([^}]*)\}", r"\1",
                       m.get("pinfunction") or "").upper()
            mm = (re.fullmatch(r"([AB])(\d+)", f)
                  or re.fullmatch(r"(\d+)([AYB])", f)
                  or re.fullmatch(r"([AY])(\d+)", f)
                  or re.fullmatch(r"(SDA|SCL)([12])", f))
            if not mm:
                continue
            g1, g2 = mm.group(1), mm.group(2)
            if g1.isdigit():
                ch, sd = g1, ("A" if g2 == "A" else "B")
            elif g1 in ("SDA", "SCL"):
                ch, sd = g1, ("A" if g2 == "1" else "B")
            else:
                ch, sd = g2, ("A" if g1 == "A" else "B")
            side[(ch, sd)] = net
        for (ch, sd), net in side.items():
            if sd == "A" and (ch, "B") in side:
                join(net, side[(ch, "B")],
                     f"{ref} ({comp.get('value')}) channel {ch}")
    groups = {n: find(n) for n in nets}
    return groups, how


def polarity(nets, comps, comp_pins, rails, h):
    """Yield polarity_mismatch / polarity_net_name / idle_pull_asserted
    findings; the last item is {'checked': n} (control pins judged). h
    holds netlist_audit's helpers (norm_pin, part_kind, parse_value,
    is_ground, base_type)."""
    groups, how = _joined_groups(nets, comps, comp_pins, rails,
                                 h["parse_value"], h["part_kind"],
                                 h["is_ground"])
    members: dict[str, list[tuple[str, dict]]] = {}
    for net, ms in nets.items():
        members.setdefault(groups[net], []).extend((net, m) for m in ms)
    # parts whose symbol marks a bus strobe or select active-low: there an
    # unmarked one asserts high; elsewhere (CS, RD, WR all drawn plain on
    # a part where they are active-low) the symbol just left bars off
    careful = set()
    for ms in nets.values():
        for m in ms:
            full = name_level_full(m.get("pinfunction") or "")
            if full and full[0] in _STROBE_KINDS and full[2]:
                careful.add(m["ref"])
    checked = 0
    for root, ms in sorted(members.items()):
        # a pin strapped to a rail or ground is configuration, not a line
        if any(n in rails or h["is_ground"](n) for n, _ in ms):
            continue
        pins = []
        for net, m in ms:
            if h["base_type"](m.get("pintype")) in ("power_in", "power_out"):
                continue
            ref = m["ref"]
            if h["part_kind"](ref) in ("R", "C", "L"):
                continue
            comp = comps.get(ref, {})
            val = f"{comp.get('value') or ''}"
            lv = name_level_full(m.get("pinfunction") or "")
            if lv is None:
                continue
            if lv[0] in _STROBE_KINDS and not lv[2] and ref not in careful:
                continue
            pins.append({"net": net, "ref": ref, "pin": m["pin"],
                         "func": m.get("pinfunction") or "",
                         "kind": lv[0], "level": lv[1], "value": val,
                         "type": h["base_type"](m.get("pintype"))})
        checked += len(pins)
        nets_here = sorted({n for n, _ in ms})
        yield from _pin_mismatch(root, pins, how.get(root, []), nets_here)
        yield from _name_mismatch(pins, nets_here)
    for hit in _idle_pull(nets, comps, comp_pins, rails, h):
        yield hit
    yield {"checked": checked}


_STROBE_KINDS = {"select", "oe", "we", "rd", "wr", "irq", "ready"}


def _sense(p):
    """(group, run-sense level) of a pin: in the 'run' group, the level
    at which the pin means 'run' (reset low-asserted -> run high)."""
    grp = _GROUP.get(p["kind"], p["kind"])
    if grp != "run":
        return grp, p["level"], False
    asserted = p["level"]
    if p["kind"] == "reset":
        run = "high" if asserted == "low" else "low"
    else:
        run = asserted
    return grp, run, True


def _pin_mismatch(root, pins, joins, nets_here):
    by_kind: dict[str, list[dict]] = {}
    for p in pins:
        by_kind.setdefault(p["kind"], []).append(p)
    done = set()
    for kind, ps in sorted(by_kind.items()):
        lows = [p for p in ps if p["level"] == "low"]
        highs = [p for p in ps if p["level"] == "high"]
        if not lows or not highs:
            continue
        # one part's pins (a chip with CE and ~{CE2}) are its own business
        if {p["ref"] for p in lows} == {p["ref"] for p in highs} and \
                len({p["ref"] for p in ps}) == 1:
            continue
        refs = {p["ref"] for p in ps}
        if len(refs) < 2:
            continue
        done.update(p["ref"] + p["pin"] for p in ps)
        yield {"rule": "polarity_mismatch", "kind": kind, "group": kind,
               "lows": lows, "highs": highs, "joins": joins,
               "nets": nets_here, "cross": False}
    # across kinds of one group (a reset output into an enable)
    run = [p for p in pins if _GROUP.get(p["kind"]) == "run"
           and p["ref"] + p["pin"] not in done]
    if len({p["kind"] for p in run}) >= 2:
        outs = [p for p in run if p["type"] in ("output", "open_collector",
                                                "open_emitter", "tri_state")
                and p["kind"] in ("good", "reset")]
        ins = [p for p in run if p["type"] == "input"
               and p["kind"] in ("enable", "reset")]
        for o in outs:
            _, o_run, _ = _sense(o)
            bad = [i for i in ins if i["ref"] != o["ref"]
                   and i["kind"] != o["kind"] and _sense(i)[1] != o_run]
            if bad:
                yield {"rule": "polarity_mismatch", "kind": "run",
                       "group": "run", "lows": [o], "highs": bad,
                       "joins": joins, "nets": nets_here, "cross": True}


def _name_mismatch(pins, nets_here):
    for net in nets_here:
        lv = net_name_level(net)
        if lv is None:
            continue
        kind, level = lv
        same = [p for p in pins if p["kind"] == kind]
        if not same or any(p["level"] == level for p in same):
            continue
        # only pins on this net itself, a renamed net beyond a link is
        # named for its own side
        own = [p for p in same if p["net"] == net]
        if not own:
            continue
        yield {"rule": "polarity_net_name", "kind": kind, "net": net,
               "level": level, "pins": own}


# not enable: an enable tied on through a pull (EN to VIN) is the usual
# always-on strap, not a fault
_IDLE_KINDS = {"reset", "hold"}
_DRIVE_TYPES = {"output", "bidirectional", "tri_state", "power_out",
                "passive", "unspecified", "free", "open_emitter"}
_EXTERNAL = re.compile(r"^(J|P|CN|CON|X|SW|S|K|BT|TP|JP|SJ|Q|T|D|LED|U\d*A?"
                       r"|Y|F)\d")


def _idle_pull(nets, comps, comp_pins, rails, h):
    """A control input that nothing can drive, held asserted by its pulls."""
    for net, ms in sorted(nets.items()):
        if net in rails or h["is_ground"](net) or net.startswith(
                "unconnected-"):
            continue
        targets, ods, pulls_up, pulls_dn, other = [], [], [], [], []
        for m in ms:
            ref = m["ref"]
            k = h["part_kind"](ref)
            if k == "C":
                continue
            if k == "R":
                rp = comp_pins.get(ref, {})
                if len(rp) != 2:
                    other.append(m)
                    continue
                far = next((n for p, n in rp.items() if p != m["pin"]), None)
                if far and h["is_ground"](far):
                    pulls_dn.append((ref, far))
                elif far in rails and rails[far] > 0:
                    pulls_up.append((ref, far))
                else:
                    other.append(m)
                continue
            t = h["base_type"](m.get("pintype"))
            lv = name_level(m.get("pinfunction") or "")
            if t == "input" and lv and lv[0] in _IDLE_KINDS:
                targets.append((m, lv))
            elif t == "open_collector":
                ods.append(m)
            elif t == "input":
                continue
            else:
                other.append(m)
        if not targets or other:
            continue
        if pulls_up and pulls_dn:
            continue
        if pulls_dn and not pulls_up:
            idle = "low"
        elif pulls_up and not pulls_dn and not ods:
            idle = "high"
        else:
            continue
        for m, (kind, level) in targets:
            if level != idle:
                continue
            yield {"rule": "idle_pull_asserted", "net": net, "kind": kind,
                   "level": level, "pin": m,
                   "pulls": pulls_dn if idle == "low" else pulls_up,
                   "ods": ods}


# ------------------------------------------------------- latch enables

# latch and shift-register parts and the pins that move data to their
# outputs, from each family's datasheet: (part, pin names, mode). Mode
# 'high' or 'low' is the level at which the latch is transparent (or
# loads); held at the other level it never takes new data. 'toggle' is a
# pin that has to move: an edge-triggered storage clock (74HC595 RCLK), a
# parallel load a shift register needs both ways (74HC165 ~PL, 74HC166
# ~PE: held low it never shifts, held high it never loads).
_FAM = r"(?:SN|MC|CD|HEF|NLV|NL|M|IDT|QS)?74[A-Z]{0,6}"
_LATCHES = (
    (re.compile(_FAM + r"(?:16|162|1G)?(?:373|573|533|563|580|841)"
                r"(?![0-9])"), {"LE", "C", "G"}, "high"),
    (re.compile(_FAM + r"(?:259|4724)(?![0-9])"), {"LE", "E"}, "low"),
    (re.compile(_FAM + r"(?:595|594|596|599|6595)(?![0-9])|"
                r"[NT]PIC6[A-Z]?59[56]"),
     {"RCLK", "RCK", "STCP", "LATCH"}, "toggle"),
    (re.compile(_FAM + r"(?:597|589)(?![0-9])"),
     {"RCLK", "RCK", "STCP", "PL", "SLOAD", "LD"}, "toggle"),
    (re.compile(_FAM + r"(?:165|166|674)(?![0-9])"),
     {"PL", "PE", "LD", "LOAD", "SLOAD"}, "toggle"),
    (re.compile(r"(?<![0-9])(?:74[A-Z]{0,6}|CD|HEF|MC1|NLV)?4[07]94"
                r"(?![0-9])"), {"STR", "STROBE", "STB", "ST"}, "high"),
    (re.compile(r"STP(?:08|16)C|TLC59(?:16|17|25|26|27)(?![0-9])"),
     {"LE"}, "high"),
    (re.compile(r"TLC59(?:40|41|47)(?![0-9])"), {"XLAT"}, "high"),
    (re.compile(r"MAX72(?:19|21)(?![0-9])"), {"LOAD", "CS", "LD"},
     "toggle"),
)
# names a latch or strobe pin goes by on a part not in the table above:
# its active level isn't known, so a held one is listed in not_judged
_LATCH_NAMES = {"LE", "STB", "STR", "STROBE", "LATCH", "LAT", "RCLK",
                "RCK", "STCP", "XLAT"}


def _latch_family(comp):
    val = " ".join(str(x or "") for x in (
        comp.get("value"), (comp.get("libsource") or ("", ""))[1])).upper()
    for pat, names, mode in _LATCHES:
        if pat.search(val):
            return names, mode
    return None


def _latch_stem(func, names):
    """The pin name in NAMES a pin's name says, markers and digits off
    ('~{LE}', '1LE', 'nLE', 'LE_N', 'ST_CP', 'SH/~{LD}' -> LD)."""
    for name, _bar in _segments(func):
        u = re.sub(r"\s+", "", name)
        if u[:1] == "n" and u[1:2].isupper():
            u = u[1:]
        u = u.upper().rstrip("#*\\").strip("_")
        u = re.sub(r"^\d+|\d+$", "", u).replace("_", "")
        for c in (u, re.sub(r"(?:N|B)$", "", u),
                  u[1:] if u.startswith("N") else u):
            if c in names:
                return c
    return None


def _held(net, ms, rails, h, comp_pins):
    """(level, how, pulls) a net sits at with nothing able to drive it:
    tied to a rail or ground, or held by pull resistors to one side with
    only inputs besides; None when something can drive it or it floats."""
    if h["is_ground"](net):
        return "low", "tied", []
    if (net in rails and rails[net] > 0) or any(
            h["base_type"](m.get("pintype")) in ("power_in", "power_out")
            for m in ms):
        return "high", "tied", []
    if net.startswith("unconnected-"):
        return None
    up, dn = [], []
    for m in ms:
        ref = m["ref"]
        k = h["part_kind"](ref)
        if k == "C":
            continue
        if k == "R":
            rp = comp_pins.get(ref, {})
            far = next((n for p, n in rp.items() if p != m["pin"]), None)
            if len(rp) == 2 and far and h["is_ground"](far):
                dn.append((ref, far))
            elif len(rp) == 2 and far in rails and rails[far] > 0:
                up.append((ref, far))
            else:
                return None
            continue
        if h["base_type"](m.get("pintype")) != "input":
            return None
    if up and not dn:
        return "high", "pulled", up
    if dn and not up:
        return "low", "pulled", dn
    return None


def latch_enables(nets, comps, comp_pins, rails, h):
    """Yield latch_enable_held findings: a latch or shift register's
    latch-enable, strobe or storage clock held where its outputs never
    update. {'not_judged': msg} for a held latch-named pin on a part whose
    active level isn't known; the last item is {'checked': n}."""
    checked = 0
    for net, ms in sorted(nets.items()):
        bad, unknown = [], []
        for m in ms:
            ref = m["ref"]
            if h["part_kind"](ref) in ("R", "C", "L"):
                continue
            if h["base_type"](m.get("pintype")) in ("power_in",
                                                    "power_out"):
                continue
            func = m.get("pinfunction") or ""
            fam = _latch_family(comps.get(ref, {}))
            if fam is None:
                if _latch_stem(func, _LATCH_NAMES):
                    unknown.append(m)
                continue
            stem = _latch_stem(func, fam[0])
            if stem is None:
                continue
            checked += 1
            bad.append((m, stem, fam[1]))
        if not bad and not unknown:
            continue
        held = _held(net, ms, rails, h, comp_pins)
        if held is None:
            continue
        level, how, pulls = held
        for m in unknown:
            val = comps.get(m["ref"], {}).get("value")
            yield {"not_judged":
                   f"latch_enable_held on {net}: {m['ref']} ({val}) "
                   f"{m.get('pinfunction')} (pin {m['pin']}) is {how} "
                   f"{level}; no row says the part's active level, so "
                   f"whether its outputs ever update isn't known"}
        pins = [(m, stem, mode) for m, stem, mode in bad
                if mode == "toggle" or mode != level]
        if pins:
            yield {"rule": "latch_enable_held", "net": net, "level": level,
                   "how": how, "pulls": pulls, "pins": pins}
    yield {"checked": checked}


# ----------------------------------------------------------- supply domain

_IO_SUPPLY_NAME = re.compile(
    r"^(VIO|V_?IO|VDDIO\w*|VDD_IO\w*|VCCIO\w*|VCC_IO\w*|IOVDD\w*|OVDD\w*|"
    r"VCCO\w*|VDDQ\w*|VL|VLOGIC|VDDL|VDD_?LOGIC)$")
_SUPPLY_NAME = re.compile(r"^(V(CC|DD|IN|S|\+|BAT|USB|BUS|CORE|REF)\w*|"
                          r"[AD]?V(CC|DD)\w*|VDD\d*|VCC\d*|\+?\d+V\d*|3V3|5V)$")
_NOT_IO_SUPPLY = re.compile(
    r"^(AVDD\w*|AVCC\w*|VDDA\w*|VCCA\w*|VREF\w*|VBAT\w*|VBACKUP|VBUS\w*|"
    r"VUSB\w*|VDD_?USB\w*|VDD33_?USB|VCORE\w*|VDD_?CORE\w*|VCCINT\w*|"
    r"VCCAUX\w*|VCCBRAM|VCCPLL\w*|VDDPLL\w*|VCAP\w*|VPP|VLCD\w*|VDDR|"
    r"VIN\w*|VS\w*|VCP|VGH|VGL|VDDD|VDD_?RTC|VRTC|VDD_?SPI|VDD_?SDIO|"
    r"VDDCPU|VDD3P3_?RTC\w*|VDDA\d*|VREG\w*|VOUT\w*|V\+|V-|VEE|VSS\w*|"
    r"VM|VMOT\w*|VBB|VDRV|VCC_?RF|VDD_?RF\w*|VDDIF)$")
_REGULATOR_PIN = re.compile(r"^(SW\d*|LX\d*|FB|VFB|BOOT|BST|VOUT\d*|OUT)$")
_ANALOG_OR_DIFF = re.compile(
    r"(XTAL|XIN|XOUT|XI$|XO$|OSC|CLKIN|VREF|AIN|ADC|DAC|AOUT|AREF|USB|"
    r"^D[+-]$|^DP$|^DM$|^D[PN]\d*$|CANH|CANL|[+-]$|_[PN]$|TMDS|LVDS|MDI|"
    r"^TX[PN]|^RX[PN]|^TD[+-]|^RD[+-]|SENSE|ISNS|^CS[+-]|COMP|^SS$|"
    r"^TR$|^CT$|^RT$|^ILIM|^IMON|^PROG$|^ISET|^RSET|^REXT|^BIAS)")
_OD_NAME = re.compile(r"^(SDA\d*|SCL\d*|I2C\d*_?(SDA|SCL)|SMB(DAT|CLK)\w*|"
                      r"TWI\w*_?(SDA|SCL)|\w*_SDA|\w*_SCL)$")
_IC = re.compile(r"^(U|IC|A|M|MOD|MD|N)\d")
_LOGIC_OUT = {"output", "tri_state", "bidirectional"}
_LOGIC_IN = {"input", "bidirectional"}


def load_io_tolerance(path: Path = IO_TOLERANCE) -> list[dict]:
    """reference/io_tolerance.yaml's receivers (its header says the form)."""
    import yaml  # noqa: PLC0415
    import checklib  # noqa: PLC0415
    try:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise checklib.CheckError(f"io tolerance {path}: {exc}") from exc
    return doc.get("receivers") or []


def _pinfunc(nets, ref, pnum, net, norm):
    m = next((x for x in nets.get(net, []) if x["ref"] == ref
              and x["pin"] == pnum), {})
    return norm(m.get("pinfunction")), m


def io_rail(ref, nets, comps, comp_pins, rails, h, tables,
            func=None) -> tuple[float, str] | None:
    """(volts, why) of the rail REF's I/O swings to, for the pin whose
    function is FUNC on a translator; None when the design doesn't say."""
    comp = comps.get(ref, {})
    pins = comp_pins.get(ref, {})
    funcs = {}
    for pnum, net in pins.items():
        f, m = _pinfunc(nets, ref, pnum, net, h["norm_pin"])
        funcs[pnum] = (f, net, h["base_type"](m.get("pintype")))
    if any(_REGULATOR_PIN.match(f) for f, _, _ in funcs.values()):
        return None

    def by(rx, why):
        vs = {rails[n] for f, n, _ in funcs.values()
              if re.search(rx, f, re.I) and n in rails and rails[n] > 0}
        if len(vs) == 1:
            return (vs.pop(), why)
        return None

    for drow in (tables.get("io_levels") or {}).get("drivers") or []:
        if h["spec_matches"](drow, comp):
            r = by(drow["supply"], f"io_levels.yaml {drow['mpn']} supply")
            if r:
                return r
    for row in tables.get("io_supply") or []:
        if h["spec_matches"](row, comp):
            r = by(row["supply_pins"], "io_supply.yaml")
            if r:
                return r
            if row.get("volts"):
                return float(row["volts"]), "io_supply.yaml volts"
    for row in (tables.get("pin_abs_max") or {}).get("receivers") or []:
        if not h["spec_matches"](row, comp):
            continue
        if row.get("vdd") is not None:
            return float(row["vdd"]), f"pin_abs_max.yaml {row['mpn']} vdd"
        if row.get("supply"):
            r = by(row["supply"], f"pin_abs_max.yaml {row['mpn']} supply")
            if r:
                return r
        break
    names = {f for f, _, _ in funcs.values()}
    # a translator: A-side pins on VCCA, B-side on VCCB
    if {"VCCA", "VCCB"} <= names or {"VA", "VB"} <= names:
        if func is None:
            return None
        side = "A" if re.match(r"^(A\d|\d+A$|DIR|OE|SDA1|SCL1)", func) \
            else "B" if re.match(r"^(B\d|\d+B$|SDA2|SCL2)", func) else None
        if side is None:
            return None
        return by(rf"^(VCC{side}|V{side})$", f"VCC{side}")
    if {"VREF1", "VREF2"} <= names:
        return None
    io = by(r"^" + _IO_SUPPLY_NAME.pattern.lstrip("^"), "I/O supply pin")
    if io:
        return io
    if any(_IO_SUPPLY_NAME.match(f) for f, _, _ in funcs.values()):
        return None  # I/O pins on rails of unknown or several volts
    vs = set()
    unknown = False
    for f, n, t in funcs.values():
        if h["is_ground"](n) or n.startswith("unconnected-"):
            continue
        if not (t == "power_in" or _SUPPLY_NAME.match(f)):
            continue
        if _NOT_IO_SUPPLY.match(f) and not re.match(r"^V(CC|DD)\d*$", f):
            continue
        if n in rails:
            if rails[n] > 0:
                vs.add(rails[n])
        elif t == "power_in" or _SUPPLY_NAME.match(f):
            unknown = True
    if len(vs) == 1 and not unknown:
        return vs.pop(), "its supply pins"
    return None


def _tol_row(rows, comp, m, norm, spec_matches, base_type):
    if base_type(m.get("pintype")) in ("power_in", "power_out"):
        return None
    func = norm(m.get("pinfunction"))
    for r in rows:
        if not spec_matches(r, comp):
            continue
        if r.get("pins") and not re.search(r["pins"], func, re.I):
            continue
        if r.get("pintypes") and not re.search(r["pintypes"],
                                               m.get("pintype") or "", re.I):
            continue
        return r
    return None


def _limits(ref, m, vr, nets, comps, comp_pins, rails, h, tables):
    """(vmax, vmax_row, vih, vih_row) of receiver pin M of REF with its rail
    VR (None when unknown); a field None when no row gives it."""
    comp = comps.get(ref, {})
    vmax = vih = None
    vrow = hrow = None
    tol = _tol_row(tables.get("io_tolerance") or [], comp, m,
                   h["norm_pin"], h["spec_matches"], h["base_type"])
    if tol:
        if tol.get("vin_max") is not None:
            vmax, vrow = float(tol["vin_max"]), tol
        elif tol.get("vin_max_vdd") is not None and vr is not None:
            vmax, vrow = vr + float(tol["vin_max_vdd"]), tol
        if tol.get("vih") is not None:
            vih, hrow = float(tol["vih"]), tol
            if tol.get("vih_vdd") and vr is not None:
                vih = vih * vr / float(tol["vih_vdd"])
        elif tol.get("vih_frac") is not None and vr is not None:
            vih, hrow = float(tol["vih_frac"]) * vr, tol
    if vmax is None:
        ab = h["abs_row"](tables.get("pin_abs_max") or {}, comp, m)
        if ab:
            if ab.get("vmax") is not None:
                vmax, vrow = float(ab["vmax"]), ab
            elif ab.get("vmax_vdd") is not None:
                v = vr
                if ab.get("vdd") is not None:
                    v = float(ab["vdd"])
                elif ab.get("supply"):
                    v = _supply_v(ref, ab["supply"], nets, comp_pins,
                                  rails, h) or vr
                if v is not None:
                    vmax, vrow = v + float(ab["vmax_vdd"]), ab
    if vih is None:
        lk = h["leak_row"](tables.get("pin_leakage") or [], comp, m)
        if lk:
            spec, prow = lk
            if prow.get("vih") is not None:
                vih, hrow = float(prow["vih"]), spec
            elif prow.get("vih_frac") is not None and vr is not None:
                vih, hrow = float(prow["vih_frac"]) * vr, spec
    return vmax, vrow, vih, hrow


def _supply_v(ref, rx, nets, comp_pins, rails, h):
    """The one rail of known volts REF's pins matching RX sit on, or None."""
    vs = set()
    for pnum, net in comp_pins.get(ref, {}).items():
        f, _ = _pinfunc(nets, ref, pnum, net, h["norm_pin"])
        if re.search(rx, f, re.I) and net in rails and rails[net] > 0:
            vs.add(rails[net])
    return vs.pop() if len(vs) == 1 else None


def _reach(net, nets, comps, comp_pins, rails, h, max_series=1000.0):
    """[(net, series ohm, series ref)]: NET itself and the nets one 2-pin
    resistor of max_series ohm or less away (not rails or ground)."""
    out = [(net, 0.0, None)]
    for m in nets.get(net, []):
        rp = comp_pins.get(m["ref"], {})
        if h["part_kind"](m["ref"]) != "R" or len(rp) != 2:
            continue
        far = next((n for p, n in rp.items() if p != m["pin"]), None)
        ohm = h["parse_value"](comps.get(m["ref"], {}).get("value"))
        if (far and far != net and ohm is not None
                and float(ohm) <= max_series and far not in rails
                and not h["is_ground"](far)
                and not far.startswith("unconnected-")):
            out.append((far, float(ohm), m["ref"]))
    return out


def _od_net(ms, h):
    return any(h["base_type"](m.get("pintype")) == "open_collector"
               or _OD_NAME.match(h["norm_pin"](m.get("pinfunction")))
               for m in ms)


def _pullups(net, nets, comps, comp_pins, rails, h):
    """[(ref, rail, volts)] of 2-pin resistors from NET to a rail of known
    volts above 0."""
    out = []
    for m in nets.get(net, []):
        rp = comp_pins.get(m["ref"], {})
        if h["part_kind"](m["ref"]) != "R" or len(rp) != 2:
            continue
        far = next((n for p, n in rp.items() if p != m["pin"]), None)
        if far in rails and rails[far] > 0:
            out.append((m["ref"], far, rails[far]))
    return out


def domains(nets, comps, comp_pins, rails, h, tables):
    """Yield io_domain_* findings and not-judged notes (rule 'unrated');
    the last item is {'checked': n} (IC-to-IC pin pairs with both rails
    known)."""
    rail_cache: dict[tuple[str, str | None], tuple | None] = {}

    def rail(ref, func):
        key = (ref, func if _is_translator(ref) else None)
        if key not in rail_cache:
            rail_cache[key] = io_rail(ref, nets, comps, comp_pins, rails, h,
                                      tables, func)
        return rail_cache[key]

    trans: dict[str, bool] = {}

    def _is_translator(ref):
        if ref not in trans:
            fs = set()
            for pnum, net in comp_pins.get(ref, {}).items():
                fs.add(_pinfunc(nets, ref, pnum, net, h["norm_pin"])[0])
            trans[ref] = {"VCCA", "VCCB"} <= fs or {"VA", "VB"} <= fs
        return trans[ref]

    io_rows = tables.get("io_levels") or {}

    def rule19_pair(dref, rref):
        dr = any(h["spec_matches"](r, comps.get(dref, {}))
                 for r in io_rows.get("drivers") or [])
        rr = any(h["spec_matches"](r, comps.get(rref, {}))
                 for r in io_rows.get("receivers") or [])
        return dr and rr

    checked = 0
    pairs: dict[tuple, dict] = {}
    for net, ms in sorted(nets.items()):
        if (net in rails or h["is_ground"](net)
                or net.startswith("unconnected-")):
            continue
        if _od_net(ms, h):
            continue
        for d in ms:
            dref = d["ref"]
            if not _IC.match(dref):
                continue
            dt = h["base_type"](d.get("pintype"))
            dfunc = h["norm_pin"](d.get("pinfunction"))
            if dt not in _LOGIC_OUT or _ANALOG_OR_DIFF.search(dfunc):
                continue
            dr = rail(dref, dfunc)
            if dr is None:
                continue
            for rnet, rs, sref in _reach(net, nets, comps, comp_pins, rails,
                                         h):
                if rnet != net and _od_net(nets.get(rnet, []), h):
                    continue
                for r in nets.get(rnet, []):
                    rref = r["ref"]
                    if rref == dref or not _IC.match(rref):
                        continue
                    rt = h["base_type"](r.get("pintype"))
                    rfunc = h["norm_pin"](r.get("pinfunction"))
                    if rt in ("power_in", "power_out", "output",
                              "tri_state", "open_collector", "no_connect"):
                        continue
                    if _ANALOG_OR_DIFF.search(rfunc):
                        continue
                    rr = rail(rref, rfunc)
                    vr = rr[0] if rr else None
                    vmax, vrow, vih, hrow = _limits(
                        rref, r, vr, nets, comps, comp_pins, rails, h,
                        tables)
                    if vr is None and vmax is None and vih is None:
                        continue
                    checked += 1
                    if rule19_pair(dref, rref):
                        continue
                    vd = dr[0]
                    key = None
                    if vmax is not None and vd > vmax + 1e-6:
                        key = ("io_domain_overdrive", dref, rref)
                        lim, row = vmax, vrow
                    elif (vih is not None and vd < vih - 1e-6
                          and rt in ("input", "bidirectional", "passive",
                                     "unspecified")):
                        key = ("io_domain_underdrive", dref, rref)
                        lim, row = vih, hrow
                    elif (vmax is None and vr is not None
                          and vd > vr + 0.5 + 1e-6):
                        key = ("unrated", dref, rref)
                        lim, row = vr, None
                    if key is None:
                        continue
                    hit = pairs.setdefault(key, {
                        "rule": key[0], "driver": dref, "receiver": rref,
                        "vd": vd, "dwhy": dr[1], "vr": vr, "limit": lim,
                        "row": row, "nets": [], "pins": [], "series": []})
                    if rnet not in hit["nets"]:
                        hit["nets"].append(rnet)
                        hit["pins"].append((dfunc or d["pin"],
                                            rfunc or r["pin"]))
                    if sref and sref not in hit["series"]:
                        hit["series"].append(sref)
    yield from pairs.values()
    # open-drain lines: the pull-up's rail against each pin on the line
    for net, ms in sorted(nets.items()):
        if (net in rails or h["is_ground"](net)
                or net.startswith("unconnected-") or not _od_net(ms, h)):
            continue
        ups = _pullups(net, nets, comps, comp_pins, rails, h)
        if not ups:
            continue
        vp = max(v for _, _, v in ups)
        pref = next(r for r, _, v in ups if v == vp)
        prail = next(n for _, n, v in ups if v == vp)
        for r in ms:
            rref = r["ref"]
            if not _IC.match(rref):
                continue
            rt = h["base_type"](r.get("pintype"))
            rfunc = h["norm_pin"](r.get("pinfunction"))
            if rt in ("power_in", "power_out", "no_connect") or \
                    _ANALOG_OR_DIFF.search(rfunc):
                continue
            rr = rail(rref, rfunc)
            vr = rr[0] if rr else None
            vmax, vrow, vih, hrow = _limits(rref, r, vr, nets, comps,
                                            comp_pins, rails, h, tables)
            if vr is None and vmax is None and vih is None:
                continue
            checked += 1
            kind = None
            if vmax is not None and vp > vmax + 1e-6:
                kind, lim, row = "over", vmax, vrow
            elif vih is not None and vp < vih - 1e-6 and rt != "output":
                kind, lim, row = "under", vih, hrow
            elif vmax is None and vr is not None and vp > vr + 0.5 + 1e-6:
                kind, lim, row = "unrated", vr, None
            if kind:
                yield {"rule": "io_domain_pull_rail", "how": kind,
                       "net": net, "pull": pref, "rail": prail, "vp": vp,
                       "receiver": rref, "rfunc": rfunc or r["pin"],
                       "vr": vr, "limit": lim, "row": row}
    yield {"checked": checked}
