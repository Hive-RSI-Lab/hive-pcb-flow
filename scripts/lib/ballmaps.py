#!/usr/bin/env python
"""ballmaps.py - a ball-grid part's ball map, from its datasheet's ball list
or the maker's pinout file, for land_batch.py (#1455).

fp_verify's ball-map check (#377) judges a ball-grid footprint against its
record's pinout when the pinout names its pins as balls (A1, B2 ...). This
library gets that pinout, from two sources in turn:

  from_pages(pages, balls)     the datasheet's own ball list: lines that
                               print a ball and its name ("A1 VSS ..."),
                               on pages whose text names balls
  curated(names, table)        the entry of reference/ball_map_urls.yaml
                               that names the part (fnmatch on MPN / Value)
  fetch_file(url, dest)        GET the maker's file through the research
                               allowlist, every hop checked
  parse(data, entry)           read it (lattice_csv | xilinx_pkg_txt)
  from_file(data, entry)       parse it and check it at the entry's count
  check(balls, want)           the deterministic checks, both sources

A map is kept only when every check holds: each ball once with one name,
the names a letter-row / number-column grid (fp_verify._ball_names), and
exactly the package's stated ball count (the table's `balls`, the digits of
the package name, a file's own total). Anything else is (None, why): the
part stays not_judged with the reason, never a partial map.

Pin types come from the name only: ground (GND, VSS*), power_in (VCC*,
VDD*), nc (NC), else unspecified. A map is checked against the datasheet,
not against the board, so a footprint fault can't hide its own map.

Not a CLI - library only.
"""
from __future__ import annotations

import csv
import fnmatch
import hashlib
import io
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
TABLE = HERE.parents[1] / "reference" / "ball_map_urls.yaml"

BALL = re.compile(r"^([A-Z]{1,2})(\d{1,3})$")
# one ball and its name in a datasheet's list: "A1 VSS", "B12  DQ[3] ..."
LIST_PAIR = re.compile(r"(?<![\w/])([A-Z]{1,2}\d{1,2})\s+"
                       r"([A-Za-z][A-Za-z0-9_/#+.\[\]-]{0,31})(?![\w])")
# the ball count in a package name: BGA-256, CABGA381, FTG256, WLCSP-25
PKG_BALLS = re.compile(r"(?:BGA|CSP|LGA|FTG|FGG|CSG|BG|MG)[-_ ]?(\d{2,4})(?!\d)",
                       re.I)
MAX_BYTES = 4_000_000
FORMATS = ("lattice_csv", "xilinx_pkg_txt")


def pin_type(name: str) -> str:
    n = name.strip().upper()
    if n in ("GND", "VSS") or n.startswith(("GND", "VSS")):
        return "ground"
    if n.startswith(("VCC", "VDD")):
        return "power_in"
    if n == "NC":
        return "nc"
    return "unspecified"


def ball_count(*packages: str) -> int | None:
    """The ball count a package name states (BGA-256 -> 256): one number
    across the names given, else None (none, or two that differ)."""
    got = {int(m.group(1)) for p in packages if p
           for m in PKG_BALLS.finditer(str(p))}
    return got.pop() if len(got) == 1 else None


def is_grid(balls) -> bool:
    """Letter-row / number-column names: six or more, three or more rows
    and columns, every name a ball (fp_verify._ball_names, all of them)."""
    names = [str(b) for b in balls]
    m = [BALL.match(n) for n in names]
    if not names or not all(m):
        return False
    return (len(names) >= 6 and len({x.group(1) for x in m}) >= 3
            and len({int(x.group(2)) for x in m}) >= 3)


def check(pairs: list[tuple[str, str]], want: int | None
          ) -> tuple[list[dict] | None, str]:
    """(pinout, "") when the (ball, name) pairs are a whole ball map: each
    ball once, with one name, on a grid, exactly `want` balls. Else (None,
    why)."""
    if not want:
        return None, "no ball count stated for the package"
    names: dict[str, str] = {}
    for ball, name in pairs:
        ball, name = ball.strip().upper(), name.strip()
        if not name:
            return None, f"ball {ball} has no name"
        if ball in names:
            return None, (f"ball {ball} is listed twice ({names[ball]}, {name})"
                          if names[ball] != name else f"ball {ball} is listed twice")
        names[ball] = name
    if not is_grid(names):
        bad = [b for b in names if not BALL.match(b)][:3]
        return None, ("the balls are not a letter-row / number-column grid"
                      + (f" ({', '.join(bad)})" if bad else ""))
    if len(names) != want:
        return None, f"{len(names)} balls listed, the package has {want}"
    return [{"pin": b, "name": n, "type": pin_type(n)}
            for b, n in sorted(names.items(), key=lambda kv: _order(kv[0]))], ""


def _order(ball: str) -> tuple:
    m = BALL.match(ball)
    return (len(m.group(1)), m.group(1), int(m.group(2))) if m else (9, ball, 0)


# --- the datasheet's ball list ----------------------------------------------------

def from_pages(pages: list[dict], want: int | None
               ) -> tuple[list[dict] | None, str, list[int]]:
    """The datasheet's ball list: on each page whose text says "ball", every
    ball-and-name pair (a name that is itself a ball, as in a drawn ball
    map's rows, is not a name). (pinout, "", pages used) when they pass
    check(), else (None, why, [])."""
    pairs: list[tuple[str, str]] = []
    used: list[int] = []
    for i, p in enumerate(pages):
        text = p.get("text") or ""
        if "ball" not in text.lower():
            continue
        got = [(b, n) for line in text.splitlines()
               for b, n in LIST_PAIR.findall(line) if not BALL.match(n)]
        if len(got) >= 6:
            pairs += got
            used.append(i + 1)
    if not pairs:
        return None, "no ball list (ball and name on a line) in the text", []
    pin, why = check(_dedup(pairs), want)
    return (pin, "", used) if pin else (None, f"the text's ball list: {why}", [])


def _dedup(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """One pair kept per (ball, name): a table that runs on to the next page
    repeats its last rows; a ball with two names stays two pairs."""
    seen: set = set()
    out = []
    for b, n in pairs:
        if (b, n) not in seen:
            seen.add((b, n))
            out.append((b, n))
    return out


# --- the maker's pinout file -------------------------------------------------------

def load_table(table: Path | None = None) -> list[dict]:
    import yaml
    doc = yaml.safe_load(Path(table or TABLE).read_text(encoding="utf-8"))
    return list((doc or {}).get("parts") or [])


def curated(names: list[str], table: Path | None = None
            ) -> tuple[dict | None, str]:
    """(entry, "") for the one entry whose `match` covers one of the part's
    names (MPN, listing MPN, Value), case aside; else (None, why)."""
    names = [n.strip().upper() for n in names if n and n.strip()]
    hits = [e for e in load_table(table)
            if any(fnmatch.fnmatchcase(n, str(pat).upper())
                   for pat in e.get("match") or [] for n in names)]
    if len(hits) > 1:
        return None, f"reference/ball_map_urls.yaml names it in {len(hits)} entries"
    if not hits:
        return None, "no entry in reference/ball_map_urls.yaml"
    return hits[0], ""


def fetch_file(url: str, dest: Path, transport=None,
               domains: dict | None = None) -> dict:
    """GET the maker's file into dest: {status: fetched|refused|error, url,
    final_url, hops, sha256, bytes}. Every hop must pass the research
    allowlist; a host that refuses or a PDF/HTML page in place of the file
    is refused. transport is researchlib.http_transport unless injected."""
    import researchlib
    out = {"url": url, "file": None}
    ck = researchlib.check_url(url, domains)
    if not ck["ok"]:
        return {**out, "status": "refused", "error": ck["reason"]}
    try:
        resp = (transport or researchlib.http_transport)(url)
    except Exception as exc:  # noqa: BLE001 - transport failure is a result
        return {**out, "status": "error",
                "error": f"transport failure: {type(exc).__name__}: {exc}"}
    out["hops"] = resp.get("hops") or [resp.get("final_url") or url]
    for hop in out["hops"]:
        hk = researchlib.check_url(hop, domains)
        if not hk["ok"]:
            return {**out, "status": "refused",
                    "error": f"redirect to {hop} refused: {hk['reason']}"}
    blk = researchlib.host_block(resp, url)
    if blk:
        return {**out, "status": "refused", "blocked_by_host": blk,
                "error": f"blocked by host: {blk['host']} answered HTTP "
                         f"{blk['http_status']}"}
    if int(resp.get("status") or 0) >= 400:
        return {**out, "status": "error", "error": f"HTTP {resp.get('status')}"}
    data = resp.get("body") or b""
    head = data[:512].lstrip().lower()
    if (not data or len(data) > MAX_BYTES or data.startswith(b"%PDF")
            or head.startswith((b"<!doctype", b"<html")) or b"\0" in data[:4096]):
        return {**out, "status": "refused",
                "error": f"not a text pinout file: {len(data)} bytes starting "
                         f"{data[:60]!r}"}
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return {**out, "status": "fetched", "file": str(dest),
            "final_url": resp.get("final_url") or url, "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest()}


def parse(data: bytes, entry: dict) -> tuple[list[tuple[str, str]] | None,
                                             int | None, str]:
    """(pairs, the file's own ball total or None, "") from the maker's file
    in the entry's format, else (None, None, why)."""
    fmt = entry.get("format")
    text = data.decode("utf-8", errors="replace")
    if fmt == "lattice_csv":
        return _lattice_csv(text, str(entry.get("column") or ""))
    if fmt == "xilinx_pkg_txt":
        return _xilinx_txt(text)
    return None, None, f"format {fmt!r} is not one of {', '.join(FORMATS)}"


def from_file(data: bytes, entry: dict, names: list[str] = ()
              ) -> tuple[list[dict] | None, str]:
    """(pinout, "") from the maker's file when it reads in the entry's
    format and passes check() at the entry's `balls`, which must also agree
    with the file's own total and with any count the entry's package or the
    part's names state (BG381 -> 381). Else (None, why)."""
    want = entry.get("balls")
    pairs, total, why = parse(data, entry)
    if pairs is None:
        return None, why
    if total is not None and total != want:
        return None, f"the file totals {total} balls, the entry says {want}"
    for n in [entry.get("package") or "", *names]:
        stated = ball_count(n)
        if stated is not None and stated != want:
            return None, f"{n} states {stated} balls, the entry says {want}"
    return check(pairs, want)


def _lattice_csv(text: str, column: str):
    rows = list(csv.reader(io.StringIO(text)))
    head = next((i for i, r in enumerate(rows)
                 if r and r[0].strip().upper() == "PAD"), None)
    if head is None or len(rows[head]) < 2 \
            or rows[head][1].strip() != "Pin/Ball Function":
        return None, None, "no PAD, Pin/Ball Function header row"
    cols = [c.strip().upper() for c in rows[head]]
    if column.upper() not in cols:
        return None, None, f"no {column} column in the header"
    c = cols.index(column.upper())
    pairs = [(r[c].strip(), r[1].strip()) for r in rows[head + 1:]
             if len(r) > c and r[c].strip() not in ("", "-")]
    if not pairs:
        return None, None, f"no ball in the {column} column"
    return pairs, None, ""


def _xilinx_txt(text: str):
    lines = text.splitlines()
    head = next((i for i, ln in enumerate(lines)
                 if ln.split("\t")[0].strip() == "Pin"
                 and "Pin Description" in ln), None)
    if head is None:
        return None, None, "no Pin ... Pin Description header row"
    pairs, total = [], None
    for ln in lines[head + 1:]:
        m = re.match(r"\s*Total Number of Pins generated,\s*(\d+)", ln)
        if m:
            total = int(m.group(1))
            break
        f = [x.strip() for x in ln.split("\t")]
        if len(f) >= 2 and f[0]:
            pairs.append((f[0], f[-1]))
    if total is None:
        return None, None, "no Total Number of Pins line: the file is cut short"
    return pairs, total, ""
