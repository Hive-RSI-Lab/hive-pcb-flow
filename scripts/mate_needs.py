"""mate_needs.py - what a shared bus or an off-board load needs beyond its
pinout (#1087, #1090), judged for check_pinouts.py from
reference/mate_needs.yaml:

 - wired_or_push_pull: a push-pull output tied to a wired-OR line, one
   every device pulls low against a pull-up (I2C SDA/SCL, UM10204 3.1.1;
   the Apple II slot's RDY, DMA, NMI, IRQ, RES and INH, Apple II Reference
   Manual Table 33). Drivers are known by value: a bus transceiver (74x245,
   74LVC8T245, 74LVC1T45) whose OE and DIR are tied so the side on the line
   always drives, or an auto-direction push-pull translator (TXB01xx) whose
   OE is not grounded. A driver whose OE or DIR is driven by logic is
   listed, not judged; open-drain buffers (74x07) are listed as such.
 - pulsed_load_no_bulk: a connector to a load that draws in pulses (a laser
   rangefinder, a GSM module), found by its value, footprint, symbol or pin
   net names, with no bulk capacitor (>= bulk_uf_min) on any of its named
   supply nets, or (with the board) none within bulk_mm of its supply pad.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

TABLE = Path(__file__).resolve().parent.parent / "reference" / "mate_needs.yaml"
CONN_RE = re.compile(r"^(J|P|CN|X|XS|XP)\d", re.I)
CAP_RE = re.compile(r"^C(?!N\d)", re.I)
SIDE_RE = re.compile(r"^([AB])(\d+)$")
OE_NAMES = {"OE", "CE", "G", "EN"}
DIR_NAMES = {"DIR", "A->B", "T/R", "AB"}


def load_table(path: Path = TABLE) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _fn(text: str) -> str:
    """A pin function without KiCad's overbar markup: ~{OE} -> OE."""
    return re.sub(r"[~{}]", "", text or "").strip().upper()


def _words(text: str) -> set[str]:
    return set(re.split(r"[^A-Z0-9]+", (text or "").upper())) - {""}


def _comp_text(comp: dict) -> str:
    lib = comp.get("libsource") or ()
    return " ".join([comp.get("value") or "", comp.get("footprint") or "",
                     *[str(x) for x in lib]])


def _src(row: dict) -> str:
    return re.split(r"[,(:]", row.get("source", ""))[0].strip()


# -- #1087 ------------------------------------------------------------------

def _lines(board, table: dict) -> dict[str, tuple[str, str]]:
    """net -> (line kind, how it was found) for every wired-OR line."""
    out: dict[str, tuple[str, str]] = {}
    lines = (table.get("wired_or") or {}).get("lines") or {}
    i2c = lines.get("i2c")
    if i2c:
        want = set(i2c.get("words") or ())
        for net, members in board.nets.items():
            if net.startswith("unconnected-"):
                continue
            hit = None
            for m in members:
                w = _words(m.get("pinfunction") or "") & want
                if w and not CONN_RE.match(m["ref"] or ""):
                    hit = f"{m['ref']} pin {m['pin']} ({sorted(w)[0]})"
                    break
            if hit is None and not net.startswith("Net-("):
                w = _words(net.rsplit("/", 1)[-1]) & want
                if w:
                    hit = f"its name ({sorted(w)[0]})"
            if hit:
                out[net] = ("i2c", hit)
    slot = lines.get("apple_ii_slot")
    if slot:
        rx = re.compile(slot["connector"], re.I)
        for ref, comp in board.comps.items():
            if ref not in board.pins or not rx.search(_comp_text(comp)):
                continue
            for pin, role in (slot.get("pins") or {}).items():
                net = board.pins[ref].get(str(pin))
                if net and not board.is_ground(net) and not net.startswith(
                        "unconnected-"):
                    out.setdefault(net, ("apple_ii_slot",
                                         f"{ref} pin {pin} ({role})"))
    return out


def _reach(board, net: str) -> list[tuple[dict, str]]:
    """(member, via) on `net`, and on the far side of a 2-pin series
    resistor from it (not a pull-up: a far side on a rail or ground)."""
    from check_pinouts import _volts_from_name  # noqa: PLC0415
    out = [(m, "") for m in board.nets.get(net, ())]
    for m in board.nets.get(net, ()):
        ref = m["ref"] or ""
        if ref.upper().startswith("R") and len(board.pins.get(ref, {})) == 2:
            for p, far in board.pins[ref].items():
                if (p != str(m["pin"]) and far != net
                        and _volts_from_name(far) is None):
                    out += [(f, f" through {ref}") for f in board.nets.get(far, ())
                            if f["ref"] != ref]
    return out


def _level(board, net: str | None) -> str | None:
    """'L' for ground, 'H' for a named rail, None when logic drives it."""
    if net is None:
        return None
    if board.is_ground(net):
        return "L"
    from check_pinouts import _volts_from_name  # noqa: PLC0415
    v = _volts_from_name(net)
    return "H" if v is not None and v > 0 else None


def _ctrl(board, ref: str, names: set[str]) -> tuple[str | None, str | None]:
    """(pin, net) of the part's control pin named one of `names`."""
    for p, net in board.pins.get(ref, {}).items():
        if _fn(board.func.get((ref, p), "")) in names:
            return p, net
    return None, None


def judge_wired_or(board, table: dict):
    from check_pinouts import _viol  # noqa: PLC0415
    rows = (table.get("wired_or") or {}).get("drivers") or {}
    rx = {k: re.compile(r["match"], re.I) for k, r in rows.items()}
    lines = _lines(board, table)
    hits: dict[str, list] = {}
    judged: list[dict] = []
    for net, (kind, how) in sorted(lines.items()):
        for m, via in _reach(board, net):
            ref, pin = m["ref"] or "", str(m["pin"])
            val = (board.comps.get(ref) or {}).get("value") or ""
            name = next((k for k in rows if rx[k].search(val)), None)
            if name is None:
                continue
            row = rows[name]
            fn = _fn(board.func.get((ref, pin), ""))
            side = SIDE_RE.match(fn)
            if row["kind"] == "open_drain":
                judged.append({"ref": ref, "net": net, "line": kind,
                               "driver": name, "verdict": "open_drain"})
                continue
            if not side:
                continue
            oe_pin, oe_net = (_ctrl(board, ref, OE_NAMES)
                              if row.get("oe", True) else (None, None))
            oe = _level(board, oe_net) if oe_pin else "L"
            if row["kind"] == "push_pull_auto":
                drives = oe != "L"
                state = "drives" if drives else "disabled"
                why = (f"its OE is {oe_net or 'unconnected'}, not ground, so "
                       "its push-pull one-shots drive the line")
            else:
                dir_pin, dir_net = _ctrl(board, ref, DIR_NAMES)
                d = _level(board, dir_net)
                if oe == "H":
                    state, drives = "isolated", False
                elif oe is None or d is None:
                    state, drives = "switched", False
                else:
                    low = row.get("dir_low", "A")
                    out_side = low if d == "L" else ("B" if low == "A" else "A")
                    drives = out_side == side.group(1)
                    state = "drives" if drives else "receives"
                why = (f"OE {'tied' if oe_pin is None else 'on ' + str(oe_net)}, DIR on "
                       f"{dir_net}: its {side.group(1)} side drives")
            judged.append({"ref": ref, "net": net, "line": kind,
                           "driver": name, "pin": pin, "verdict": state})
            if drives:
                hits.setdefault(ref, []).append(
                    (net, kind, how, pin, fn, via, name, why))
    viols = []
    for ref, items in sorted(hits.items()):
        name = items[0][6]
        row = rows[name]
        nets = sorted({i[0] for i in items})
        pins = ", ".join(f"{i[4]} (pin {i[3]}) on {i[0]}{i[5]}" for i in items)
        line_src = (table["wired_or"]["lines"][items[0][1]]).get("source", "")
        msg = (f"{ref} ({board.comps[ref].get('value')}) is a push-pull driver "
               f"on wired-OR lines: {pins}; {items[0][7]}. A line every "
               f"device pulls low against a pull-up needs open-drain or "
               f"open-collector outputs ({re.split(r'[,(]', line_src)[0].strip()}; "
               f"{_src(row)}); a push-pull high fights the next device's low")
        viols.append(_viol("wired_or_push_pull", ref, nets[0], msg,
                           nets=nets, driver=name))
    return viols, judged


# -- #1090 ------------------------------------------------------------------

def _farads(value) -> float | None:
    from check_decoupling import parse_farads  # noqa: PLC0415
    s = str(value or "")
    if not re.match(r"\s*\d+(?:[.,]\d+)?\s*(?:[pnuµ]\d*F?|F\b)", s, re.I):
        return None
    return parse_farads(s)


def judge_pulsed(board, table: dict, pcb: str | None = None):
    from check_pinouts import _viol, _volts_from_name  # noqa: PLC0415
    spec = table.get("pulsed_loads") or {}
    rows = spec.get("kinds") or {}
    floor = float(spec.get("bulk_uf_min", 10)) * 1e-6
    reach = float(spec.get("bulk_mm", 30))
    bg = None
    if pcb:
        try:
            import geom  # noqa: PLC0415
            bg = geom.load_board(pcb)
        except Exception:  # noqa: BLE001 - no board: judge by net alone
            bg = None
    viols, judged = [], []
    for ref in sorted(board.pins):
        if not CONN_RE.match(ref):
            continue
        comp = board.comps.get(ref) or {}
        pins = board.pins[ref]
        text = " ".join([_comp_text(comp),
                         *[n.rsplit("/", 1)[-1] for n in pins.values()]])
        name = next((k for k, r in rows.items()
                     if re.search(r["match"], text, re.I)), None)
        if name is None:
            continue
        row = rows[name]
        supplies = sorted({n for n in pins.values()
                           if (_volts_from_name(n) or 0) > 0})
        if not supplies:
            judged.append({"ref": ref, "load": name, "verdict": "no_named_supply"})
            continue
        best, near = None, None
        for net in supplies:
            for m in board.nets.get(net, ()):
                c = m["ref"] or ""
                if not CAP_RE.match(c) or CONN_RE.match(c):
                    continue
                f = _farads((board.comps.get(c) or {}).get("value"))
                if f is None or f < floor:
                    continue
                best = max(best or 0, f)
                if bg is not None:
                    from checklib import manhattan  # noqa: PLC0415
                    a = bg.pads_of(net=net, ref=ref)
                    b = bg.pads_of(net=net, ref=c)
                    if a and b:
                        d = min(manhattan(p.center, q.center) for p in a for q in b)
                        near = d if near is None else min(near, d)
        far = bg is not None and best is not None and near is not None and near > reach
        verdict = "no_bulk" if best is None else ("far" if far else "ok")
        judged.append({"ref": ref, "load": name, "supplies": supplies,
                       "bulk_uf": None if best is None else round(best * 1e6, 3),
                       "verdict": verdict})
        if verdict == "ok":
            continue
        want = (f"its maker asks for {row['vendor_uf']:g} uF "
                f"({_src(row)})" if row.get("vendor_uf") else
                f"its maker gives no figure; the bulk class starts at "
                f"{floor * 1e6:g} uF")
        where = (f"no capacitor of {floor * 1e6:g} uF or more on "
                 f"{', '.join(supplies)}" if verdict == "no_bulk" else
                 f"its nearest bulk capacitor is {near:.0f} mm away "
                 f"(more than {reach:g} mm)")
        msg = (f"{ref} ({comp.get('value')}) feeds an off-board {name} load "
               f"that draws in pulses, with {where}: the pulse current comes "
               f"down the cable and dips the rail for the rest of the board; "
               f"{want}")
        viols.append(_viol("pulsed_load_no_bulk", ref, supplies[0], msg,
                           load=name, supplies=supplies,
                           vendor_uf=row.get("vendor_uf")))
    return viols, judged
