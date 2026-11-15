"""mpn_decode.py - a part's package read from its MPN's ordering code.

The makers' ordering-code tables (TI's package designators, ST's pin-count
and package letters, Microchip's package codes, onsemi, Nexperia, Vishay
and Infineon order numbers) are in reference/mpn_packages.yaml, each row
cited to the maker's datasheet page it was read from (part_rows.py
--verify checks every quote against the PDF).

decode(mpn) -> None, or {package, desc, source, maker, cites} where desc is
what lib/pkgname.describe() returns ({mount, family, pins, pitch, width,
body, chip}), filled only with what the cited rows state. An MPN that no
entry matches in full, or whose code letter has no row, is None: the
caller says "not judged", never a guess.

Table entry (makers[]):
  name     the maker and series, for the message
  match    a regex matched in full against the upper-cased MPN with its
           spaces taken out; its named groups pick the rows
  groups   {group: {value: row}}
  row      a fixed row for every MPN the entry matches
  combos   [{when: {group: value}, part: optional regex on the MPN, ...row}]
           rows that hold only for that combination (a body size that
           depends on both the pin count and the package letter)
A row: package (the name), family (a pkgname family), mount (smd/tht),
pin_count, pitch, width (mm), width_in (inches), body ([a, b] mm), cite.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml

TABLE = Path(__file__).resolve().parents[2] / "reference" / "mpn_packages.yaml"
_KEYS = ("package", "family", "mount", "pin_count", "pitch", "width",
         "width_in", "body")


@lru_cache(maxsize=4)
def load(path: str | None = None) -> list[dict]:
    """The entries, each with its regex compiled."""
    data = yaml.safe_load(Path(path or TABLE).read_text(encoding="utf-8"))
    out = []
    for e in (data or {}).get("makers") or []:
        out.append({**e, "_rx": re.compile(e["match"])})
    return out


def norm(mpn: str) -> str:
    return re.sub(r"\s+", "", str(mpn or "")).upper()


def _merge(acc: dict, row: dict, cites: list) -> None:
    for k in _KEYS:
        if row.get(k) is not None:
            acc[k] = row[k]
    cites.extend(row.get("cite") or [])


def decode(mpn: str, path: str | None = None) -> dict | None:
    m_pn = norm(mpn)
    if not m_pn:
        return None
    for e in load(path):
        m = e["_rx"].fullmatch(m_pn)
        if not m:
            continue
        acc: dict = {}
        cites: list = []
        if e.get("row"):
            _merge(acc, e["row"], cites)
        got = {g: v for g, v in m.groupdict().items() if v is not None}
        for g, rows in (e.get("groups") or {}).items():
            if g not in got:
                continue
            row = rows.get(got[g])
            if row is None:
                return None  # a code letter the table has no row for
            _merge(acc, row, cites)
        for cb in e.get("combos") or []:
            if all(got.get(g) == v for g, v in cb["when"].items()) and (
                    not cb.get("part") or re.match(cb["part"], m_pn)):
                _merge(acc, cb, cites)
        if not (acc.get("family") or acc.get("mount")):
            return None
        return _record(e, acc, cites)
    return None


def _record(e: dict, acc: dict, cites: list) -> dict:
    width = acc.get("width")
    if width is None and acc.get("width_in") is not None:
        width = round(acc["width_in"] * 25.4, 2)
    body = tuple(acc["body"]) if acc.get("body") else None
    desc = {"mount": acc.get("mount"), "family": acc.get("family"),
            "pins": acc.get("pin_count"), "pitch": acc.get("pitch"),
            "width": width, "body": body, "chip": None}
    name = acc.get("package") or acc.get("family")
    if desc["pins"] and not re.search(r"\d", name):
        name = f"{name}-{desc['pins']}"
    docs = []
    for c in cites:
        ref = f"{c['doc']} p{c['page']}"
        if ref not in docs:
            docs.append(ref)
    return {"package": name, "desc": desc, "maker": e["name"],
            "cites": cites,
            "source": f"its ordering code, {e['name']} "
                      f"(reference/mpn_packages.yaml: {'; '.join(docs[:3])})"}
