"""A diode's reverse-voltage rating read from its part number (#1323).

reference/diode_vr.yaml lists the common families whose part number states
the rating (1N4007 = 1000 V, SS14 = 40 V, SMBJ24A = 24 V standoff), each
row cited to one maker's datasheet. check_ratings.diode_reverse falls back
to it when a 2-terminal diode has no datasheet read and no distributor
reverse-voltage attribute. A TVS or ESD diode is rated by its standoff
(VRWM), never its clamp; a part whose family or code has no row stays
unrated.
"""
from __future__ import annotations

import functools
import re
from pathlib import Path

import yaml

DIODE_YAML = (Path(__file__).resolve().parents[2] / "reference"
              / "diode_vr.yaml")


def load_families(path: Path = DIODE_YAML) -> list[dict]:
    """The diode families, each with its regex compiled."""
    if not path.is_file():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [{**e, "_rx": re.compile(e["match"])}
            for e in data.get("families") or []]


@functools.lru_cache(maxsize=1)
def _families() -> tuple[dict, ...]:
    return tuple(load_families())


def mpn_reverse(mpn: str, table: list[dict] | None = None) -> dict | None:
    """{v, code, bi, rating, kind, family, cite} when the part number matches
    a family in full and its code has a row; else None (a code with no row
    is never guessed). bi: the part blocks both ways (a C/CA TVS, a
    bidirectional ESD diode), so the rating holds for either polarity."""
    key = re.sub(r"\s+", "", str(mpn or "")).upper()
    if not key:
        return None
    for e in _families() if table is None else table:
        m = e["_rx"].fullmatch(key)
        if not m:
            continue
        gd = m.groupdict()
        code = (gd.get("k") or "") + (gd.get("ka") or "")
        v = {str(c): x for c, x in (e.get("codes") or {}).items()}.get(code)
        if v is None:
            return None
        return {"v": float(v), "code": code,
                "bi": bool(gd.get("bi") or e.get("bidirectional")),
                "rating": e.get("rating", "VR"), "kind": e.get("kind", ""),
                "family": e["name"], "cite": e["cite"]}
    return None
