"""fpwaivers.py - per-footprint DRC floors for vendor geometry (#813).

reference/footprint_waivers.yaml lists the footprints whose OWN pads sit under
a KiCad default the fab does not require (the MC-311D USB-C receptacle's pads
are 0.18 / 0.21 mm from its locating pegs; KiCad 10.0.5+ holds them to its
0.25 mm min_hole_clearance). Each entry becomes one .kicad_dru rule scoped to
that footprint's own items, so nothing else on the board is relaxed:

  A.memberOfFootprint('*:USB-C-SMD_MC-311D') && B.memberOfFootprint(...)
  && (A.Pad_Type == 'NPTH, mechanical' || B.Pad_Type == 'NPTH, mechanical')

rules_gen emits them after its fab floors (rules_gen.build); board_init merges
the entries for its board's footprints into the .kicad_dru its setup DRC reads
(merge_dru), replacing only rules named PREFIX*.
"""
from __future__ import annotations

import fnmatch
import re
from pathlib import Path

import yaml

WAIVER_FILE = Path(__file__).resolve().parents[2] / "reference" / "footprint_waivers.yaml"
PREFIX = "aiee_fpwaive_"
CONSTRAINTS = {"hole_clearance"}
BETWEEN = {"own_npth"}
_FPID = re.compile(r"[A-Za-z0-9_.*\-]+:[A-Za-z0-9_.*\-]+")
_NPTH = "(A.Pad_Type == 'NPTH, mechanical' || B.Pad_Type == 'NPTH, mechanical')"


class WaiverError(ValueError):
    pass


def load(path: Path | None = None) -> list[dict]:
    """The validated entries. A bad entry raises: a waiver is never guessed."""
    data = yaml.safe_load(Path(path or WAIVER_FILE).read_text(encoding="utf-8")) or {}
    out = []
    for i, e in enumerate(data.get("waivers") or []):
        where = f"footprint_waivers[{i}]"
        fp = str(e.get("footprint", ""))
        if not _FPID.fullmatch(fp):
            raise WaiverError(f"{where}: footprint {fp!r} is not lib:name")
        if e.get("constraint") not in CONSTRAINTS:
            raise WaiverError(f"{where}: constraint {e.get('constraint')!r} "
                              f"not in {sorted(CONSTRAINTS)}")
        if e.get("between") not in BETWEEN:
            raise WaiverError(f"{where}: between {e.get('between')!r} "
                              f"not in {sorted(BETWEEN)}")
        try:
            mn = float(e["min_mm"])
        except (KeyError, TypeError, ValueError):
            raise WaiverError(f"{where}: min_mm missing or not a number") from None
        if not 0.0 < mn <= 1.0:
            raise WaiverError(f"{where}: min_mm {mn} outside (0, 1] mm")
        for key in ("fab", "reason"):
            if not str(e.get(key) or "").strip():
                raise WaiverError(f"{where}: {key} is required (cite the fab figure "
                                  "and why the vendor geometry stands)")
        out.append({**e, "min_mm": mn})
    return out


def rule_name(entry: dict) -> str:
    name = entry["footprint"].split(":", 1)[1]
    return PREFIX + entry["constraint"] + "_" + re.sub(r"[^A-Za-z0-9]", "_", name).strip("_")


def condition(entry: dict) -> str:
    fp = entry["footprint"]
    cond = f"A.memberOfFootprint('{fp}') && B.memberOfFootprint('{fp}')"
    if entry["between"] == "own_npth":
        cond += " && " + _NPTH
    return cond


def matches(entry: dict, fpid: str) -> bool:
    """Does a board footprint 'lib:name' fall under the entry (as KiCad matches it)?"""
    return fnmatch.fnmatchcase(fpid, entry["footprint"])


def rules(entries: list[dict] | None = None,
          footprints: list[str] | None = None) -> list[dict]:
    """One rule dict per entry (name, constraint, min_mm, condition, comment);
    with `footprints` (the board's lib IDs) only the entries on the board."""
    entries = load() if entries is None else entries
    out = []
    for e in entries:
        if footprints is not None and not any(matches(e, f) for f in footprints):
            continue
        out.append({"name": rule_name(e), "constraint": e["constraint"],
                    "min_mm": e["min_mm"], "condition": condition(e),
                    "footprint": e["footprint"], "issue": e.get("issue"),
                    "comment": (f"{e['footprint']}'s own pads to its own NPTH pegs "
                                f"(#{e.get('issue', '?')}): vendor geometry, "
                                "reference/footprint_waivers.yaml")})
    return out


def render(rule: dict) -> str:
    return "\n".join([
        f"# {rule['comment']}",
        f'(rule "{rule["name"]}"',
        f"\t(constraint {rule['constraint']} (min {rule['min_mm']:.4f}mm))",
        f'\t(condition "{rule["condition"]}")',
        ")"])


def _blocks(text: str):
    """(start, end, name) of each top-level (rule "...") block, a run of '#'
    comment lines just above it included."""
    for m in re.finditer(r'^\(rule "([^"]+)"', text, re.M):
        depth, j = 0, m.start()
        while j < len(text):
            if text[j] == "(":
                depth += 1
            elif text[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        start = m.start()
        while True:
            prev = text.rfind("\n", 0, start - 1) + 1 if start > 0 else 0
            if start > 0 and text[prev:start].startswith("#"):
                start = prev
            else:
                break
        end = j + 1
        if text[end:end + 1] == "\n":
            end += 1
        yield start, end, m.group(1)


def merge_dru(text: str | None, new_rules: list[dict]) -> str:
    """The .kicad_dru text with every PREFIX* rule replaced by new_rules, put
    after the last aiee_*_floor rule (so later design rules still win), or
    after the version line."""
    text = text if text and text.strip() else "(version 1)\n"
    for start, end, name in reversed(list(_blocks(text))):
        if name.startswith(PREFIX):
            text = text[:start] + text[end:]
    if not new_rules:
        return text
    block = "".join(render(r) + "\n" for r in new_rules)
    at = None
    for _, end, name in _blocks(text):
        if name.startswith("aiee_") and name.endswith("_floor"):
            at = end
    if at is None:
        m = re.search(r"^\(version [^)]*\)\n?", text, re.M)
        at = m.end() if m else 0
        if text[at - 1:at] != "\n":
            block = "\n" + block
    return text[:at] + block + text[at:]
