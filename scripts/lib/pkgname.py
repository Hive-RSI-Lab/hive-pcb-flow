"""pkgname.py - what a package name says: mount, family, pins, pitch, body.

One reader for both sides of check_fp_package: a KiCad footprint name
(`Package_SO:SOIC-8_3.9x4.9mm_P1.27mm`, `Diode_SMD:D_SOD-123F`) and a
part's package as JLC, LCSC or EasyEDA writes it (`SOIC-8`, `SOD-123FL`,
`QFN-32(5x5)`, `SOIC-8_L4.9-W3.9-P1.27-LS6.0-BL`, `0402`).

describe(name) -> {mount, family, pins, pitch, width, body, chip} with None
for whatever the name does not say. Nothing is guessed from a part's kind;
the only defaults are the ones the family name fixes by JEDEC convention:
- a plain SOIC/SOP/SO is 1.27 mm pitch, and 3.9 mm wide up to 16 pins (a
  208 mil or 300 mil body is written so: `SOP-8_208mil`, `SOIC-16W`);
- a plain TSSOP is 0.65 mm pitch;
- SOT-23 without a count has 3 pins, SC-70 / SOT-323 too; SOT-563 and
  SOT-666 have 6, SOT-553 5;
- a PowerPAK 1212-8 is a 3.3x3.3 mm, 0.65 mm pitch no-lead body.

compare(part, fp) -> the list of ways the two disagree, judged only on
what both sides state, so an unknown on either side is never a mismatch.
"""
from __future__ import annotations

import re

CHIPS = ("01005", "0201", "0402", "0603", "0805", "1206", "1210", "1812",
         "2010", "2512")
_METRIC = {"0402": "01005", "0603": "0201", "1005": "0402", "1608": "0603",
           "2012": "0805", "3216": "1206", "3225": "1210", "4532": "1812",
           "5025": "2010", "6332": "2512"}

# family -> (regex on the upper-cased name, mount, default pins). Order
# matters: the first match wins, so a longer name comes before its prefix.
_FAMILIES: list[tuple[str, str, str, int | None]] = [
    ("SOD-123F", r"SOD-?123F", "smd", 2),
    ("SOD-123", r"SOD-?123(?![0-9])", "smd", 2),
    ("SOD-323", r"SOD-?323", "smd", 2),
    ("SOD-523", r"SOD-?523", "smd", 2),
    ("SOD-923", r"SOD-?923", "smd", 2),
    ("MINIMELF", r"MINI-?MELF|SOD-?80(?![0-9])|LL-?34|DO-?213AA", "smd", 2),
    ("MELF", r"(?<!MINI)(?<!MINI-)MELF|LL-?41|DO-?213AB", "smd", 2),
    ("SMA", r"(?<![A-Z])SMAF?(?![A-Z])|DO-?214AC", "smd", 2),
    ("SMB", r"(?<![A-Z])SMBF?(?![A-Z])|DO-?214AA", "smd", 2),
    ("SMC", r"(?<![A-Z])SMCF?(?![A-Z])|DO-?214AB", "smd", 2),
    ("DO-41", r"DO-?41|DO-?204AL", "tht", 2),
    ("DO-35", r"DO-?35|DO-?204AH", "tht", 2),
    ("DO-15", r"DO-?15(?![0-9])|DO-?204AC", "tht", 2),
    ("DO-201", r"DO-?201", "tht", 2),
    # PowerPAK 1212-8 is a 3.3x3.3 mm 0.65 mm-pitch no-lead body, not the
    # 5x6 PowerPAK SO-8 (Vishay SiS412DN p1, p7)
    ("PPAK1212", r"(?:POWER[-_]?)?PAK[-_]?1212", "smd", 8),
    # power 5x6 flat-lead packages are no SOIC whatever their name says
    ("POWER-SO8", r"SO-?8-?FL|POWERPAK|DFN-?5X6|TDSON|LFPAK|SON-?5X6",
     "smd", None),
    ("SOT-223", r"SOT-?223", "smd", None),
    ("SOT-89", r"SOT-?89", "smd", None),
    ("SOT-143", r"SOT-?143", "smd", 4),
    ("SOT-23", r"(?<![A-Z])T?SOT-?23(?:-?(\d))?(?![0-9])|SOT-?2([56])(?![0-9])"
     r"|SOT-?753|SOT-?457|SC-?74A?(?![0-9])|TSOP-?[56](?![0-9])", "smd", 3),
    ("SOT-563", r"SOT-?(?:5[56]3|666|5X3)(?![0-9])", "smd", 6),
    ("SC-70", r"SC-?70(?:-?(\d))?|SOT-?3([2-6])3|SC-?88A?", "smd", 3),
    ("TO-252", r"TO-?252|DPAK(?!2)", "smd", None),
    ("TO-263", r"TO-?263|D2PAK", "smd", None),
    ("TO-220", r"TO-?220", "tht", None),
    ("TO-92", r"TO-?92", "tht", 3),
    ("TO-247", r"TO-?247", "tht", None),
    ("DIP", r"(?<![A-Z])P?DIP-?(\d+)", "tht", None),
    ("QFP", r"(?<![A-Z])[LTPHV]?QFP-?(\d+)", "smd", None),
    ("NOLEAD", r"(?<![A-Z])[VWUXTLH]?(?:QFN|DFN|SON)-?(\d+)", "smd", None),
    ("SO", r"(?<![A-Z])(?:HTS|TS|VS|HS|QS|HT|E|H|T|V|M|S|Q|U)?SO(?:IC|P)?-?(\d+)", "smd",
     None),
    ("BGA", r"(?<![A-Z])[FTUV]?BGA-?(\d+)", "smd", None),
]
_ALIASES = {"SOT-25": 5, "SOT-753": 5, "SC-74A": 5, "SOT-26": 6,
            "SOT-457": 6, "SC-74": 6, "SOT-323": 3, "SOT-353": 5,
            "SOT-363": 6, "SC-88": 6, "SC-88A": 5}


def _count(tok: str) -> int | None:
    """Pins of a SOT-23 / SC-70 name: `SOT-23-5` 5, `SOT-353` 5, `SC-70` 3."""
    m = re.match(r"TSOP-?([56])$", tok)
    if m:
        return int(m.group(1))
    tok = re.sub(r"^TSOT", "SOT", tok)
    tok = re.sub(r"^(SOT|SC)-?", r"\1-", tok)
    if tok in _ALIASES:
        return _ALIASES[tok]
    m = re.match(r"(?:SOT-23|SC-70)-?(\d)$", tok)
    return int(m.group(1)) if m else None


def _chip(name: str) -> str | None:
    """An imperial chip size, only where the name is a chip passive's: the
    size leads the name (`0402`, `R0603`, `C_0805_2012Metric`, `LED_0603`)
    or is its metric code (`1608Metric`)."""
    m = re.search(r"(\d{4})METRIC", name)
    if m and m.group(1) in _METRIC:
        return _METRIC[m.group(1)]
    m = re.match(r"(?:SMD[-_]?)?(?:[RCLDF]|LED|CP|FB)?[-_]?(\d{4,5})(?![0-9])"
                 r"(?:[-_]|$)", name)
    return m.group(1) if m and m.group(1) in CHIPS else None


def _mm(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def describe(name: str | None) -> dict:
    """What the package name says (see the module doc)."""
    out = {"mount": None, "family": None, "pins": None, "pitch": None,
           "width": None, "body": None, "chip": None}
    raw = str(name or "").strip()
    if not raw:
        return out
    n = raw.split(":")[-1].upper().replace(" ", "")
    n = re.sub(r"^(?:D|R|C|L|CP|LED|F|Q|U|IC)_(?=[A-Z0-9])", "", n)
    out["chip"] = _chip(n)
    if out["chip"]:
        out.update(mount="smd", family="CHIP", pins=2)
        return out
    if re.search(r"PLUGIN|(?<![A-Z])THT(?![A-Z])|THROUGH-?HOLE", n):
        out["mount"] = "tht"
    elif re.search(r"(?<![A-Z])SMD(?![A-Z])|SMT(?![A-Z])", n):
        out["mount"] = "smd"
    for fam, rx, mount, pins in _FAMILIES:
        m = re.search(rx, n)
        if not m:
            continue
        if fam in ("SMA", "SMB", "SMC") and _RF.search(n):
            continue  # an SMA coax connector, not the DO-214 diode case
        if fam == "MELF" and re.search(r"0204(?![0-9])", n):
            fam = "MINIMELF"  # MELF 0204 is the MiniMELF (SOD-80) body
        out["family"], out["mount"] = fam, mount
        if fam == "PPAK1212":
            out.update(family="NOLEAD", mount=mount, pins=pins, pitch=0.65,
                       body=(3.3, 3.3))
            break
        if fam == "SOT-563" and "553" in m.group(0):
            pins = 5
        groups = [g for g in m.groups() if g]
        if fam in ("SOT-23", "SC-70"):
            out["pins"] = _count(m.group(0)) or pins
        elif groups and fam in ("DIP", "QFP", "NOLEAD", "SO", "BGA"):
            out["pins"] = int(groups[0])
        else:
            out["pins"] = pins
        if fam == "SO":
            pre = re.match(r"(HTS|TS|VS|HS|QS|HT|E|H|T|V|M|S|Q|U)?SO",
                           m.group(0))
            kind = (pre.group(1) or "") if pre else ""
            if kind in ("", "E", "H"):
                out["pitch"] = 1.27
            elif kind in ("TS", "HTS"):
                out["pitch"] = 0.65
        break
    # pitch: KiCad `_P0.5mm`, EasyEDA `-P1.27`
    m = re.search(r"(?:^|[_-])P(\d+(?:\.\d+)?)(?:MM)?(?=[_-]|$)", n)
    if m and _mm(m.group(1)) and _mm(m.group(1)) < 3:
        out["pitch"] = _mm(m.group(1))
    # body: KiCad `_3.9x4.9mm`, JLC `(5x5)`, EasyEDA `-L4.9-W3.9`
    m = re.search(r"(\d+(?:\.\d+)?)X(\d+(?:\.\d+)?)(?:MM)?(?![0-9])", n)
    if m:
        a, b = _mm(m.group(1)), _mm(m.group(2))
        out["body"] = (a, b)
        if out["family"] == "SO":
            out["width"] = a
    lw = re.search(r"[-_]L(\d+(?:\.\d+)?)-W(\d+(?:\.\d+)?)", n)
    if lw:
        out["body"] = (_mm(lw.group(2)), _mm(lw.group(1)))
        if out["family"] == "SO":
            out["width"] = _mm(lw.group(2))
    mil = re.search(r"(150|208|300)MIL", n)
    if mil and out["family"] == "SO":
        out["width"] = {"150": 3.9, "208": 5.3, "300": 7.5}[mil.group(1)]
    if out["family"] == "SO" and re.search(r"SO(?:IC|P)?-?\d+W(?![A-Z])", n):
        out["width"] = 7.5
    return out


def width_class(w: float | None) -> str | None:
    if w is None:
        return None
    if 3.4 <= w <= 4.5:
        return "150 mil (3.9 mm)"
    if 4.9 <= w <= 5.7:
        return "208 mil (5.3 mm)"
    if 7.0 <= w <= 7.9:
        return "300 mil (7.5 mm)"
    return f"{w:g} mm"


def implied_width(d: dict) -> float | None:
    """A plain SOIC/SOP's body width by JEDEC MS-012: 3.9 mm up to 16
    pins at 1.27 mm. None when the name says nothing or the part is not
    one."""
    if d["width"] is not None:
        return d["width"]
    if d["family"] == "SO" and d["pitch"] == 1.27 and d["pins"] \
            and d["pins"] <= 16:
        return 3.9
    return None


def compare(part: dict, fp: dict, fp_pads: int | None = None) -> list[str]:
    """The ways a part's package (described) and a footprint (described,
    with its count of numbered pads) disagree. Each check runs only when
    both sides say the thing."""
    why: list[str] = []
    if part["mount"] and fp["mount"] and part["mount"] != fp["mount"]:
        why.append(f"the part is {_mount(part['mount'])}, the footprint "
                   f"{_mount(fp['mount'])}")
        return why
    if part["chip"] and fp["chip"]:
        if part["chip"] != fp["chip"]:
            why.append(f"the part is a {part['chip']} chip, the footprint "
                       f"{fp['chip']}")
        return why
    if part["family"] and fp["family"]:
        if _same_land(part, fp):
            return why
        if part["family"] != fp["family"]:
            if {part["family"], fp["family"]} & {"CHIP"}:
                return why  # a chip LED or fuse name read loosely: no call
            why.append(f"the part is {part['family']}, the footprint "
                       f"{fp['family']}")
            return why
        fpins = fp["pins"] if fp["pins"] is not None else fp_pads
        if part["pins"] and fpins and part["pins"] != fpins:
            exposed = fp["pins"] is None and fpins == part["pins"] + 1
            if not exposed and part["family"] not in ("SOT-223", "SOT-89",
                                                       "TO-252", "TO-263"):
                why.append(f"the part has {part['pins']} pins, the "
                           f"footprint {fpins}")
        if part["pitch"] and fp["pitch"] \
                and abs(part["pitch"] - fp["pitch"]) > 0.03:
            why.append(f"the part's pitch is {part['pitch']:g} mm, the "
                       f"footprint's {fp['pitch']:g} mm")
        if part["family"] == "SO":
            pw, fw = implied_width(part), fp["width"]
            if pw and fw and abs(pw - fw) > 0.8:
                why.append(f"the part's body is {width_class(pw)} wide, the "
                           f"footprint's {width_class(fw)}")
        if part["family"] in ("NOLEAD", "QFP") and part["body"] \
                and fp["body"]:
            a, b = sorted(part["body"]), sorted(fp["body"])
            if any(x is None for x in a + b) or \
                    max(abs(a[0] - b[0]), abs(a[1] - b[1])) > 0.3:
                if not any(x is None for x in a + b):
                    why.append(f"the part's body is {a[0]:g}x{a[1]:g} mm, "
                               f"the footprint's {b[0]:g}x{b[1]:g} mm")
    return why


# SMA-K/J (bare or hyphened) and SMA-F/M (hyphened only) name coax
# jacks; a bare "SMAF" is JLC's name for the flat-lead SMA diode body
# (1N4007 is sold in it), so it is read as the SMA family.
_RF = re.compile(r"CONN|COAX|JACK|PLUG|BWSMA|WIRELESS|EDGE|RF|"
                 r"SMA(?:-?(?:K|J)|-(?:F|M))(?:W?E|HD|Y|B)?(?![A-Z])")


def _same_land(part: dict, fp: dict) -> bool:
    """Two families with one land pattern: SOD-123 leads and SOD-123F's
    flat ones span the same pads, and SO-8FL (PowerPAK, LFPAK, 5x6 power
    SON) is the 5x6 DFN-5 land by another name."""
    fams = {part["family"], fp["family"]}
    if fams == {"SOD-123", "SOD-123F"}:
        return True
    if fams == {"POWER-SO8", "NOLEAD"}:
        dfn = part if part["family"] == "NOLEAD" else fp
        body = sorted(dfn["body"]) if dfn["body"] and None not in dfn[
            "body"] else None
        return body is None or (abs(body[0] - 5.0) <= 0.4
                                and abs(body[1] - 6.0) <= 0.4)
    return False


def _mount(m: str) -> str:
    return {"smd": "surface mount", "tht": "through hole"}[m]
