#!/usr/bin/env python
"""lib_pin_types.py - retype pulled symbol pin electrical types from datasheet JSON.

easyeda2kicad emits `unspecified` (and a few arbitrary `input`) electrical
types on nearly every pin; kicad-cli ERC --severity-all then floods
`pin_to_pin` warnings plus FALSE `pin_not_driven` errors, so the P4 gate
(errors+warnings == 0) cannot pass on an untouched pulled lib (LEARNINGS
2026-07-27 [easyeda2kicad][erc]). Fix at the SOURCE - the symbol library the
schematic embeds - keyed by each symbol's "LCSC Part" property against the
datasheet-extract JSONs (parts/<lcsc>.json pinout types):

  power_in | ground -> power_in    supplies and grounds
  power_out         -> power_out   regulator outputs; a DUPLICATE pin with the
                                   same NAME (a SOT-223 tab) stays passive, or
                                   ERC raises power_out<->power_out
  everything else   -> passive     kills the Unspecified pin_to_pin flood

Symbols with no matching extraction get a default the symbol itself decides:
an IC (Reference U or IC) gets power_in on the pins NAMED as a supply or ground
(VCC, VDD, AVDD, VDDIO, GND, AGND, VSS, VEE, V+, V-, with a number or _suffix),
every other pin and every other part (connectors, switches, LEDs, discretes)
passive. The junk types are library-inherent, not part-specific (the
field-proven boards/stm32-blinky recipe this script productionizes). An IC's
power_in pins then need a driver on their net (a regulator's power_out or a
PWR_FLAG), which is what ERC should ask of them.

Power symbols (a `(power)` flag: GND, +3V3, PWR_FLAG ...) are left as they
are: their power_in / power_out pins are ERC's rail model, not junk.

--lib also takes a .kicad_sch: its embedded lib_symbols block is what ERC
reads, and an imported schematic (Eagle via eda_io import-sch types every
pin `io`) needs the same retype there (#528). The symbols are named
"LIB:SYM" there and their units "SYM_1_1"; only that block is rewritten.

lib_pull runs this on the symbols it pulls, with the extracts beside
parts.json when there are any; --symbols limits a run to named symbols the
same way, so types set from an extract earlier are not reverted.

Idempotent text surgery: only the electrical-type token of each `(pin ...`
node is rewritten; nothing else is reformatted. Re-run after any lib_pull
refresh (a re-pull restores the junk types).

Contract (SPEC section 6): argparse, JSON to stdout or --out, exit 0/1/2.
  0 = retype ran (changes applied or nothing to change)
  2 = unreadable library/JSON, internal error

Examples:
  lib_pin_types.py --lib lib/aiee.kicad_sym --datasheet-json parts/C8734.json
  lib_pin_types.py --lib lib/aiee.kicad_sym --datasheet-json "parts/*.json" --dry-run
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS / "lib"))
import fplib  # noqa: E402
import sexpdata  # noqa: E402

DEFAULT = "passive"

# The reference implementation's proven scan: the electrical type is the first
# token after `(pin`; the graphic style (line/inverted/clock/...) follows it.
# Only a real electrical type counts: a library's Description text can hold
# "(pin number consists of ...", which must not be rewritten.
PIN_RE = re.compile(
    r"\(pin\s+(input|output|bidirectional|tri_state|passive|free|unspecified"
    r"|power_in|power_out|open_collector|open_emitter|no_connect)\s+([a-z_]+)")
SYM_RE = re.compile(r'\(symbol\s+"([^"]+)"')
NUM_RE = re.compile(r'\(number\s+"([^"]+)"')
NAME_RE = re.compile(r'\(name\s+"([^"]*)"')

# Supply and ground pin names on an IC with no extract (case-insensitive):
# VCC VDD VSS VEE and GND families, an A/D/P/S prefix or a block's name and
# an underscore before it (USB_VDD, ADC_VDD on an RP2040, #528), a number or
# suffix after it.
POWER_NAME = re.compile(
    r"^(?:(?:[A-Z0-9]+_)?[ADPS]?(?:VCC|VDD|VSS|VEE|GND)(?:[A-Z]{0,2}|IO)?\d*"
    r"(?:_\w+)?|V[+-])$",
    re.IGNORECASE)
GROUND_NAME = re.compile(r"GND|VSS", re.IGNORECASE)
IC_REF = re.compile(r"^(?:U|IC)$", re.IGNORECASE)


KEEP = "keep"      # a rule value: leave the pin's type as it is


def symbol_index(path: Path) -> tuple[list[dict], tuple[int, int] | None]:
    """The top-level symbols of a .kicad_sym, or of a .kicad_sch's lib_symbols
    block, as fplib.symbol_index gives them plus "power" (the symbol carries
    the `(power)` flag); and for a schematic the text span of that block."""
    if path.suffix != ".kicad_sch":
        index = fplib.symbol_index(path)
        names = set()
        root = fplib._symbol_lib_root(path)
        for child in (root[1:] if root is not None else []):
            if _power(child):
                names.add(str(fplib._tok(child[1])))
        for e in index:
            e["power"] = e["name"] in names
        return index, None
    text = path.read_text(encoding="utf-8", errors="replace")
    root = sexpdata.loads(text)
    if not fplib._is_node(root) or fplib._head(root) != "kicad_sch":
        raise RuntimeError(f"not a KiCad schematic: {path}")
    block = next((c for c in root[1:] if fplib._head(c) == "lib_symbols"), [])
    out = []
    for child in block[1:]:
        if not (fplib._head(child) == "symbol" and len(child) > 1):
            continue
        props = {str(fplib._tok(c[1])): str(fplib._tok(c[2]))
                 for c in child[2:]
                 if fplib._head(c) == "property" and len(c) >= 3}
        out.append({"name": str(fplib._tok(child[1])),
                    "lcsc": props.get("LCSC Part", ""),
                    "footprint": props.get("Footprint", ""),
                    "reference": props.get("Reference", ""),
                    "power": _power(child)})
    m = re.search(r"\(lib_symbols\b", text)
    if not m:
        return out, (0, 0)
    depth, i = 0, m.start()
    while i < len(text):        # the block's closing paren, strings skipped
        ch = text[i]
        if ch == '"':
            i += 1
            while i < len(text) and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                break
        i += 1
    return out, (m.start(), i + 1)


def _power(node) -> bool:
    return fplib._is_node(node) and any(
        fplib._head(c) == "power" for c in node[2:])


def default_rules(entry: dict) -> dict[str, str]:
    """The rules for a symbol with no extract: power_in on an IC's supply and
    ground names, passive everywhere else."""
    if IC_REF.match(entry.get("reference", "").strip()):
        return {"*": DEFAULT, "name:power": "power_in"}
    return {"*": DEFAULT}


def build_rules(index: list[dict], extracts: dict[str, dict]) -> tuple[dict, list[str]]:
    """(symbol name -> {pin number -> kicad type}, matched lcsc ids)."""
    rules: dict[str, dict[str, str]] = {}
    matched: list[str] = []
    for entry in index:
        if entry.get("power") or entry["name"].split(":")[-1] == "PWR_FLAG":
            rules[entry["name"]] = {"*": KEEP}
            continue
        ds = extracts.get(entry["lcsc"])
        if ds is None:
            rules[entry["name"]] = default_rules(entry)
            continue
        matched.append(entry["lcsc"])
        pin_map: dict[str, str] = {}
        out_names: set[str] = set()
        for row in ds.get("pinout", []):
            t = row.get("type", "")
            if t in ("power_in", "ground"):
                want = "power_in"
            elif t == "power_out":
                name = row.get("name", "")
                if name in out_names:
                    want = "passive"      # duplicate output pin (tab)
                else:
                    out_names.add(name)
                    want = "power_out"
            else:
                want = "passive"
            pin_map[str(row.get("pin", ""))] = want
        rules[entry["name"]] = pin_map
    return rules, matched


def _owner(names: list[str], rules: dict) -> str:
    """The symbol a pin belongs to, from the symbol names opened before it,
    nearest last: a unit "SYM_1_1" belongs to "SYM", or in a schematic's
    lib_symbols to "LIB:SYM"."""
    for name in reversed(names):
        if name in rules:
            return name
        base = re.sub(r"_\d+_\d+$", "", name)
        if base in rules:
            return base
        if base != name:
            for key in rules:
                if key.endswith(":" + base):
                    return key
    return re.sub(r"_\d+_\d+$", "", names[-1]) if names else "?"


def retype(text: str, rules: dict[str, dict[str, str]],
           only: set[str] | None = None,
           span: tuple[int, int] | None = None) -> tuple[str, list[dict]]:
    """Rewrite every pin's electrical type; unruled symbols/pins -> passive,
    a symbol ruled KEEP (a power symbol) as it is. With `only`, symbols not
    named in it keep their pins as they are; with `span`, only pins inside
    that slice of the text are touched."""
    changes: list[dict] = []
    out: list[str] = []
    pos = 0
    lo, hi = span if span else (0, len(text))
    syms = [(s.start(), s.group(1)) for s in SYM_RE.finditer(text, lo, hi)]
    for m in PIN_RE.finditer(text, lo, hi):
        symbol = _owner([n for at, n in syms if at < m.start()], rules)
        num_m = NUM_RE.search(text, m.end())
        number = num_m.group(1) if num_m else "?"
        if only is not None and symbol not in only:
            continue
        sym_rules = rules.get(symbol, {})
        want = sym_rules.get(number)
        if want is None:
            name_m = NAME_RE.search(text, m.end())
            pin_name = name_m.group(1) if name_m else ""
            if "name:power" in sym_rules and POWER_NAME.match(pin_name):
                # a supply-named pin its library types power_out is an
                # internal regulator's output (DVDD on a DRV8434S, VCC on an
                # LM5158): keep it; a ground never is one
                if m.group(1) == "power_out" and not GROUND_NAME.search(pin_name):
                    want = KEEP
                else:
                    want = sym_rules["name:power"]
            else:
                want = sym_rules.get("*", DEFAULT)
        if want == KEEP:
            continue
        have, style = m.group(1), m.group(2)
        out.append(text[pos:m.start()])
        out.append(f"(pin {want} {style}")
        pos = m.end()
        if have != want:
            changes.append({"symbol": symbol, "pin": number,
                            "from": have, "to": want})
    out.append(text[pos:])
    return "".join(out), changes


def _load_extracts(paths: list[str]) -> dict[str, dict]:
    files: list[Path] = []
    for raw in paths:
        if any(ch in raw for ch in "*?["):
            files += [Path(p) for p in sorted(glob.glob(raw))]
        else:
            files.append(Path(raw))
    extracts: dict[str, dict] = {}
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"cannot read datasheet JSON {f}: {exc}") from exc
        lcsc = (data.get("lcsc") or "").strip()
        if lcsc:
            extracts[lcsc] = data
    return extracts


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--lib", required=True, help="the pulled .kicad_sym library, or a .kicad_sch whose "
                         "lib_symbols to retype")
    ap.add_argument("--datasheet-json", nargs="+", default=[],
                    help="datasheet-extract JSON file(s); globs accepted")
    ap.add_argument("--symbols", nargs="+",
                    help="retype only these symbols (default: every symbol)")
    ap.add_argument("--dry-run", action="store_true", help="report, do not write")
    ap.add_argument("--out", help="write JSON here instead of stdout")
    args = ap.parse_args(argv)

    try:
        lib = Path(args.lib)
        if not lib.exists():
            raise RuntimeError(f"symbol library not found: {lib}")
        text = lib.read_text(encoding="utf-8")
        index, span = symbol_index(lib)
        extracts = _load_extracts(args.datasheet_json)
        rules, matched = build_rules(index, extracts)
        new, changes = retype(text, rules,
                              set(args.symbols) if args.symbols else None,
                              span)
        if changes and not args.dry_run:
            lib.write_text(new, encoding="utf-8")
        lib_ids = {e["lcsc"] for e in index}
        payload = {
            "script": "lib_pin_types",
            "status": "pass",
            "lib": str(lib),
            "dry_run": bool(args.dry_run),
            "symbols": len(index),
            "extracts_matched": sorted(matched),
            # e.g. a part later removed by amendment: informational, not fatal
            "extracts_unmatched": sorted(set(extracts) - lib_ids),
            "changed": len(changes),
            "changes": changes,
        }
    except Exception as exc:  # noqa: BLE001 - contract: any error -> exit 2
        print(json.dumps({"script": "lib_pin_types", "status": "error",
                          "error": f"{type(exc).__name__}: {exc}"}))
        return 2

    out_text = json.dumps(payload, indent=1, ensure_ascii=True)
    if args.out:
        Path(args.out).write_text(out_text, encoding="utf-8")
    else:
        print(out_text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
