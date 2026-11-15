"""pair_order.py - netlist_audit rule 37: a differential or paired line's
P/N, and its pair-to-pair order, across a connector (#1217).

A connector pin's pair role comes from, first to last:
 - constraints connector_pinouts[] ({ref, pins: {pin: name}, source?});
 - the connector's own pin names (D+, TD-, RXIN0+, TTIP...);
 - a standard pinout for its footprint: USB-A, USB-B, mini-B and micro-B
   (pin 2 D-, pin 3 D+; USB 2.0 spec table 6-1), USB Type-C (A6/B6 D+,
   A7/B7 D-; USB Type-C spec table 3-4), a plain 8P8C jack (contacts 1/2,
   3/6, 4/5, 7/8 with + on 1, 3, 4, 7; TIA-568 / IEEE 802.3 clause 40 MDI).
From each paired connector pin the line is followed inward through
two-pin series parts (resistors, inductors and beads, series capacitors,
jumpers; never onto a rail or ground) to the first pins on another part
whose names carry a pair role of the same family. A part in the way that
names neither side (a common-mode choke, a transformer, a switch) stops
the walk; with no named pin reached the pair is not_judged, and so is a
named chip pair that reaches a connector whose pinout nothing states.

Findings, one per pair and far part:
 - pair_polarity_swap: both lines of one pair land on the far part's
   pair the other way round (D+ on D-, D- on D+). An error for USB 2.0
   and LVDS, whose receivers have no polarity correction; a warning for
   Ethernet (10BASE-T and 1000BASE-T PHYs correct it, 100BASE-TX's
   MLT-3 does not care) and E1/T1 (AMI line code), and for other indexed
   lanes (HDMI, DSI), where some bridges swap polarity in a register.
 - pair_split: the two lines of one connector pair land on two
   different pairs of the far part. An error: the pair's twist and the
   far end's termination no longer belong to one signal.
 - pair_lane_order: an LVDS pair lands on the far part's pair of another
   lane, both sides numbered from the same base. An error: a serializer
   sends bits in a fixed lane order.
Not judged: Ethernet TX meeting RX (a crossover, which an MDI-X port or
the naming side may intend), Ethernet and HDMI/DSI pair order (1000BASE-T
PHYs and many bridges correct a pair swap), lanes numbered from different
bases, and any pair whose far part names its pins ambiguously.
"""
from __future__ import annotations

import re

CONNECTOR_RE = re.compile(r"^(J|P|CN|X|CON)\d")

# ----------------------------------------------------------- pin roles

_POL = {"+": "P", "P": "P", "-": "N", "N": "N", "M": "N"}

_LVDS_STEMS = ("TXCLKOUT", "RXCLKIN", "TXOUT", "RXIN", "TXCLK", "RXCLK")
_ETH_LETTER = {"A": "0", "B": "1", "C": "2", "D": "3"}


def _clean(raw: str) -> str:
    s = re.sub(r"~\{([^}]*)\}", r"\1", raw or "")
    return re.sub(r"[\s]", "", s).upper()


def role(raw: str):
    """(family, key, pol) for one pin name, or None. family is usb, eth,
    lvds, lane or e1; key names the pair within it; pol is P or N. A name
    with several functions (PA12/USB_DP) takes its first pair role."""
    for seg in re.split(r"/", raw or ""):
        r = _role1(_clean(seg))
        if r:
            return r
    return None


def _role1(s: str):
    if not s:
        return None
    # USB 2.0: D+, D-, DP, DM, DN, USB_DP, USB0_D+, USBDM; hub ports DP1
    m = re.fullmatch(r"(?:USB\d?_?(?:OTG_?|HS_?|FS_?)?)?D_?([+\-PMN])(\d?)", s)
    if m and (s.startswith("USB") or not m.group(2) or s[:2] in ("DP", "DM")):
        return "usb", m.group(2), _POL[m.group(1)]
    # E1/T1 line interface: TTIP/TRING, RTIP/RRING
    m = re.fullmatch(r"([TR])(TIP|RING)(\d?)", s)
    if m:
        return ("e1", ("TX" if m.group(1) == "T" else "RX") + m.group(3),
                "P" if m.group(2) == "TIP" else "N")
    # LVDS: TXOUT0+, RXIN2-, TXCLKOUT+, LVDS_D0P
    for stem in _LVDS_STEMS:
        m = re.fullmatch(rf"(?:LVDS_?)?{stem}_?(\d?)_?([+\-PMN])", s)
        if m:
            key = ("CLK" if "CLK" in stem else "") + m.group(1)
            if not key:
                return None
            return "lvds", key, _POL[m.group(2)]
    m = re.fullmatch(r"LVDS_?(D|CLK|CK)(\d?)_?([+\-PMN])", s)
    if m and (m.group(2) or m.group(1) != "D"):
        key = ("CLK" if m.group(1) != "D" else "") + m.group(2)
        return "lvds", key, _POL[m.group(3)]
    # Ethernet 10/100: TD+, TX-, RXP, RXN, TXM, RD_P
    m = re.fullmatch(r"(?:ETH_?|PHY_?)?(TX|TD|RX|RD)_?([+\-PMN])", s)
    if m:
        return "eth", "TX" if m.group(1) in ("TX", "TD") else "RX", \
            _POL[m.group(2)]
    # Ethernet 1000BASE-T MDI: MDI0+, MDIP0, MDI_0_N, TRD1-, TRXP2, MX3+,
    # BI_DA+, TXRXP_A, TD_M_B
    for pat in (r"(?:MDI|TRD|TRX|MX|TXRX|TX_RX)_?(\d)_?([+\-PMN])",
                r"(?:MDI|TRD|TRX|MX|TXRX|TX_RX)_?([+\-PMN])_?(\d)",
                r"BI_?D([A-D])_?([+\-PMN])",
                r"(?:TXRX|TD|MDI)_?([PM])_?([A-D])"):
        m = re.fullmatch(pat, s)
        if m:
            a, b = m.groups()
            if a in _POL and b not in _POL:
                a, b = b, a
            return "mdi", _ETH_LETTER.get(a, a), _POL[b]
    # indexed lanes (HDMI D0+/CK-, DSI D1P/CLKN, TMDS_D2+)
    m = re.fullmatch(r"(?:[A-Z]+_)?(D|CK|CLK|DATA|LANE)(\d?)_?([+\-PMN])", s)
    if m and (m.group(2) or m.group(1) in ("CK", "CLK")):
        key = ("CLK" if m.group(1) in ("CK", "CLK") else "D") + m.group(2)
        return "lane", key, _POL[m.group(3)]
    return None


def _same_family(a: str, b: str) -> bool:
    eth = ("eth", "mdi")
    return a == b or (a in eth and b in eth)


# ------------------------------------------------------ standard pinouts

_USB_STD = (r"USB_(A|B|Micro-B|Mini-B)(?![A-Za-z0-9])", {"2": "D-", "3": "D+"},
            "USB 2.0 spec table 6-1: pin 2 D-, pin 3 D+")
_USB_C = (r"USB_C_", {"A6": "D+", "B6": "D+", "A7": "D-", "B7": "D-"},
          "USB Type-C spec table 3-4: A6/B6 D+, A7/B7 D-")
_RJ45 = (r"RJ45|8P8C", {"1": "BI_DA+", "2": "BI_DA-", "3": "BI_DB+",
                        "6": "BI_DB-", "4": "BI_DC+", "5": "BI_DC-",
                        "7": "BI_DD+", "8": "BI_DD-"},
         "IEEE 802.3 clause 40 MDI: contacts 1/2, 3/6, 4/5, 7/8")
# a jack with magnetics or LEDs inside numbers its PHY side, not contacts
_MAGJACK = re.compile(r"LED|MAG|TRANSF|INTEGRATED|HR9|7499|J0011|J1011|"
                      r"ARJ|RJMG|SI-|LPJ|HY9|0826|JXD|HFJ", re.I)


def standard_pinout(comp: dict):
    """({pin: name}, source) the connector's footprint implies, or None."""
    fp = comp.get("footprint") or ""
    name = fp.split(":")[-1]
    for pat, pins, src in (_USB_C, _USB_STD):
        if re.search(pat, name, re.I):
            return pins, src
    if re.search(_RJ45[0], name, re.I) and not (
            _MAGJACK.search(name) or _MAGJACK.search(comp.get("value") or "")):
        return _RJ45[1], _RJ45[2]
    return None


# ----------------------------------------------------------- the walk

def _pins_of(nets, net):
    return nets.get(net, [])


def _far_pins(net, conn, nets, comps, comp_pins, rails, h, depth=4):
    """Walk from NET away from connector CONN through two-pin series
    parts. Returns (named, blocked, walked): named is
    [(ref, pin, pinfunction, role, net)] on parts other than connectors,
    blocked the refs of unnamed multi-pin parts met on the way."""
    is_ground, part_kind = h["is_ground"], h["part_kind"]
    seen, frontier = {net}, [net]
    named, blocked = [], set()
    for _ in range(depth):
        nxt = []
        for n in frontier:
            for m in _pins_of(nets, n):
                ref = m["ref"]
                if ref == conn or CONNECTOR_RE.match(ref or ""):
                    continue
                comp = comps.get(ref) or {}
                if comp.get("dnp"):
                    continue
                r = role(m.get("pinfunction") or "")
                if r:
                    named.append((ref, m["pin"], m.get("pinfunction"), r, n))
                    continue
                pins = comp_pins.get(ref) or {}
                kind = part_kind(ref)
                if len(pins) == 2 and (kind in ("R", "L", "C")
                                       or re.match(r"^(JP|SJ)\d", ref)):
                    other = next((x for p, x in pins.items()
                                  if p != m["pin"]), None)
                    if (other and other not in seen and other not in rails
                            and not is_ground(other)
                            and not other.startswith("unconnected-")):
                        seen.add(other)
                        nxt.append(other)
                    continue
                if len(pins) >= 3 and _passes_unnamed(ref, comp):
                    blocked.add(ref)
        frontier = nxt
    return named, blocked, seen


def _passes_unnamed(ref: str, comp: dict) -> bool:
    """A part a pair may pass through without naming its pins: a
    common-mode choke, a transformer, an analog switch or a module."""
    return not re.match(r"^(D|TVS|ESD|TP|C|R)\d", ref or "")


# ------------------------------------------------------------- judging

def pair_order(nets, comps, comp_pins, rails, h, stated=None):
    """Yield findings as dicts {rule, sev, family, conn, far, key, ...}
    and not_judged notes {'not_judged': text}; the last item is
    {'checked': n} (connector pairs judged against a far part)."""
    stated = {e["ref"]: e for e in stated or []
              if isinstance(e, dict) and e.get("ref")
              and isinstance(e.get("pins"), dict)}
    checked = 0
    unstated_seen: set[tuple[str, str]] = set()
    for conn in sorted(comps):
        if not CONNECTOR_RE.match(conn) or comps[conn].get("dnp"):
            continue
        pins = comp_pins.get(conn) or {}
        std = standard_pinout(comps[conn])
        groups: dict[tuple[str, str], dict[str, list]] = {}
        src = {}
        for pin, net in sorted(pins.items()):
            if net.startswith("unconnected-") or net in rails \
                    or h["is_ground"](net):
                continue
            m = next((x for x in nets.get(net, []) if x["ref"] == conn
                      and x["pin"] == pin), {})
            r, how = None, None
            if conn in stated and str(pin) in {str(k) for k in
                                               stated[conn]["pins"]}:
                name = {str(k): v for k, v in
                        stated[conn]["pins"].items()}[str(pin)]
                r = role(str(name))
                how = ("constraints connector_pinouts"
                       + (f" ({stated[conn]['source']})"
                          if stated[conn].get("source") else ""))
            if r is None and conn not in stated:
                r = role(m.get("pinfunction") or "")
                how = f"{conn}'s pin names"
                if r is None and std and pin in std[0]:
                    r, how = role(std[0][pin]), std[1]
            if r is None:
                continue
            fam, key, pol = r
            groups.setdefault((fam, key), {"P": [], "N": []})[pol].append(
                (pin, net))
            src[(fam, key)] = how
        for (fam, key), side in sorted(groups.items()):
            if not side["P"] or not side["N"]:
                continue
            yield from _judge(conn, fam, key, side, src[(fam, key)], nets,
                              comps, comp_pins, rails, h, groups)
            checked += 1
        # a named chip pair on a connector whose pinout nothing states
        if not groups and conn not in stated:
            for pin, net in sorted(pins.items()):
                named, _, _ = _far_pins(net, conn, nets, comps, comp_pins,
                                        rails, h, depth=2)
                for ref, fpin, func, (fam, k, _), _n in named:
                    if fam in ("usb", "lvds", "eth", "mdi") and \
                            (conn, ref + fam + k) not in unstated_seen:
                        unstated_seen.add((conn, ref + fam + k))
                        yield {"not_judged":
                               f"pair_order on {conn}: {ref}'s {func} "
                               f"(pin {fpin}) reaches {conn} pin {pin}, "
                               f"but {conn}'s pins are not named, its "
                               f"footprint has no standard pinout and "
                               f"constraints connector_pinouts says "
                               f"nothing, so its pair order can't be "
                               f"checked"}
    yield {"checked": checked}


def _judge(conn, fam, key, side, how, nets, comps, comp_pins, rails, h,
           groups):
    # far roles per part: {ref: {"P": {(fam,key,pol,pin,func)}, "N": ...}}
    far: dict[str, dict[str, set]] = {}
    blocked_all, walked_all = set(), set()
    for pol in ("P", "N"):
        for _pin, net in side[pol]:
            named, blocked, walked = _far_pins(net, conn, nets, comps,
                                               comp_pins, rails, h)
            blocked_all |= blocked
            walked_all |= walked
            for ref, fpin, func, r, _n in named:
                if _same_family(r[0], fam):
                    far.setdefault(ref, {"P": set(), "N": set()})[pol].add(
                        (r[0], r[1], r[2], fpin, func))
    cpins = "/".join(p for pol in ("P", "N") for p, _ in side[pol])
    cnets = sorted({n for pol in ("P", "N") for _, n in side[pol]})
    label = _label(fam, key)
    if not far:
        if blocked_all:
            yield {"not_judged":
                   f"pair_order on {conn} {label} (pins {cpins}, "
                   f"{'/'.join(cnets)}): the line passes "
                   f"{', '.join(sorted(blocked_all))}, whose pins name no "
                   f"pair, before any named pin, so its order can't be "
                   f"followed"}
        return
    for ref in sorted(far):
        ps, ns = far[ref]["P"], far[ref]["N"]
        if not ps or not ns:
            continue
        rp = {(f, k, pol) for f, k, pol, _, _ in ps}
        rn = {(f, k, pol) for f, k, pol, _, _ in ns}
        if len(rp) != 1 or len(rn) != 1:
            yield {"not_judged":
                   f"pair_order on {conn} {label}: {ref} names the lines "
                   f"ambiguously ({', '.join(sorted(x[4] for x in ps | ns))}"
                   f"), so their order can't be judged"}
            continue
        (fp, kp, pp), (fn, kn, pn) = next(iter(rp)), next(iter(rn))
        funcs = {"P": sorted(x[4] for x in ps)[0],
                 "N": sorted(x[4] for x in ns)[0]}
        fpins = {"P": sorted(x[3] for x in ps)[0],
                 "N": sorted(x[3] for x in ns)[0]}
        base = {"conn": conn, "far": ref, "family": fam, "key": key,
                "label": label, "p_net": side["P"][0][1],
                "n_net": side["N"][0][1], "p_pin": side["P"][0][0],
                "n_pin": side["N"][0][0], "far_key": kp,
                "nets": cnets, "how": how, "far_pins": fpins,
                "far_funcs": funcs, "walked": sorted(walked_all)}
        if kp != kn:
            yield {**base, "rule": "pair_split", "sev": "error"}
            continue
        if pp == pn:
            continue  # both lines on one polarity: another rule's short
        swapped = pp == "N"
        if fam in ("eth", "mdi") and fp in ("eth", "mdi"):
            if fam == "eth" and fp == "eth" and kp != key:
                yield {"not_judged":
                       f"pair_order on {conn}: its {key} pair meets "
                       f"{ref}'s {kp} pair, a crossover; an MDI-X port or "
                       f"the side the names were written from may intend "
                       f"it"}
                continue
        elif fam == "lvds" and kp != key:
            mine = {k for f, k in groups if f == "lvds"}
            theirs = _lvds_keys(ref, nets, comp_pins)
            if mine and mine == theirs:
                yield {**base, "rule": "pair_lane_order", "sev": "error",
                       "swapped": swapped}
            else:
                yield {"not_judged":
                       f"pair_order on {conn}: LVDS {key} meets {ref}'s "
                       f"{kp}, but the two sides number their lanes "
                       f"differently ({sorted(mine)} against "
                       f"{sorted(theirs)}), so the order can't be judged"}
            continue
        elif fam == "lane" and kp != key:
            yield {"not_judged":
                   f"pair_order on {conn}: lane {key} meets {ref}'s "
                   f"{kp}; many bridges swap lanes in a register"}
            continue
        if swapped:
            sev = "error" if fam in ("usb", "lvds") else "warning"
            yield {**base, "rule": "pair_polarity_swap", "sev": sev}


_FAMILY = {"usb": "USB 2.0", "eth": "Ethernet", "mdi": "Ethernet MDI",
           "lvds": "LVDS", "lane": "lane", "e1": "E1/T1"}


def _label(fam, key):
    return f"{_FAMILY[fam]} pair{(' ' + key) if key else ''}"


def _lvds_keys(ref, nets, comp_pins):
    keys = set()
    for pin, net in (comp_pins.get(ref) or {}).items():
        m = next((x for x in nets.get(net, []) if x["ref"] == ref
                  and x["pin"] == pin), {})
        r = role(m.get("pinfunction") or "")
        if r and r[0] == "lvds":
            keys.add(r[1])
    return keys
