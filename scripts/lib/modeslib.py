"""modeslib - resolve a brief's build-mode token into scope + binding (U18).

`reference/build-modes.md` IS the registry. Its four tables are the machine
data - tokens, targets, scope tiers, binding levels - so the doc agents read
and the rule scripts enforce can never drift apart. Nothing here hard-codes a
target, a tier or a binding name; the tests pin the parsed table against the
doc, which is the plan's "test-pinned against build-modes.md".

The mode is ONE dial the brief sets (the target learning outcome) and two the
target derives:

    learning stage-placement:  ->  scope block-only, binding canonical, stage P6
    ultra bare bones design:   ->  the `block-basics` target (legacy alias)

`scope` bounds what goes ON the board: `excludes` is the closed vocabulary of
feature classes that are never a reviewer finding, `requires` the classes whose
ABSENCE is one. `binding` says whether the stated geometry is an INPUT to the
design or an OUTPUT of it - which is the bb-buck lesson made mechanical:

    geometry_is_output(binding)  ->  board_init REFUSES a fixed --outline WxH
                                     (place first, `board_edit --outline fit`
                                     after; U17's flow)

`geometry_plan()` is the pure arithmetic behind a relaxation: given the mode and
whatever size was stated, it says whether the stated size binds, what P5 should
be given, and - when the size loses - the `state.py decision` text that has to
be recorded for it. Relaxation is never silent, so the decision text is part of
the answer, not an afterthought.
"""
from __future__ import annotations

import re
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
SKILL = SCRIPTS.parent
DOC = SKILL / "reference" / "build-modes.md"

# The generic form in the tokens table; every other row is a literal alias.
GENERIC_TOKEN = "learning <target>:"
_TOKEN_RE = re.compile(r"^\s*learning\s+([a-z0-9][a-z0-9+-]*)\s*:", re.I)
_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:x|\*|by)\s*(\d+(?:\.\d+)?)", re.I)
_CAP_RE = re.compile(r"^\+(\d+(?:\.\d+)?)%$")
# Sections whose FIRST markdown table is machine data, and that table's key.
TABLES = {"Tokens": "token", "Targets": "target", "Scope tiers": "tier",
          "Binding levels": "binding"}
# What a mode may relax, and what it may never relax (build-modes.md).
RELAXES = ("size", "aspect", "outline", "cost", "packaging")
NEVER_RELAXED = ("electrical spec", "safety questions", "gates", "coverage",
                 "research", "DFM", "datasheet requirements")


class ModeError(Exception):
    """A token that does not resolve, or a doc that does not parse."""


# ---- the doc ------------------------------------------------------------
def _cells(line: str) -> list[str]:
    return [c.strip().strip("`").strip() for c in line.strip().strip("|").split("|")]


def _table(lines: list[str], start: int) -> list[list[str]]:
    """The first markdown table inside this section.

    Stops at the next `#`/`##` heading even when nothing was found - a section
    that lost its table must raise, not silently inherit the next one's.
    """
    rows: list[list[str]] = []
    for ln in lines[start:]:
        if re.match(r"^#{1,2}\s", ln) or (ln.startswith("#") and rows):
            break
        if not ln.lstrip().startswith("|"):
            if rows:
                break
            continue
        cells = _cells(ln)
        if all(set(c) <= set("-: ") and c for c in cells):
            continue                                   # |---|---| separator
        rows.append(cells)
    return rows


def _split_list(cell: str) -> list[str]:
    if cell in ("", "-", "none"):
        return []
    return [c.strip().strip("`") for c in cell.split(",") if c.strip()]


def parse_doc(path: Path | str | None = None) -> dict:
    """Parse build-modes.md into {tokens, targets, scopes, bindings}."""
    path = Path(path or DOC)
    if not path.is_file():
        raise ModeError(f"build-modes doc not found: {path}")
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    heads = {ln.lstrip("# ").strip(): i for i, ln in enumerate(lines)
             if re.match(r"^##\s+\S", ln)}
    out: dict[str, dict] = {}
    for section, key in TABLES.items():
        if section not in heads:
            raise ModeError(f"{path.name}: no '## {section}' section")
        rows = _table(lines, heads[section] + 1)
        if len(rows) < 2:
            raise ModeError(f"{path.name}: '## {section}' has no table")
        header, body = rows[0], rows[1:]
        if header[0] != key:
            raise ModeError(f"{path.name}: '## {section}' table must start "
                            f"with a {key!r} column, found {header[0]!r}")
        out[section] = {r[0]: dict(zip(header, r)) for r in body}

    tokens, targets = out["Tokens"], out["Targets"]
    scopes, bindings = out["Scope tiers"], out["Binding levels"]
    for tok, row in tokens.items():
        if tok != GENERIC_TOKEN and row["target"] not in targets:
            raise ModeError(f"token {tok!r} names unknown target "
                            f"{row['target']!r}")
    for name, row in targets.items():
        if row["scope"] not in scopes:
            raise ModeError(f"target {name!r} names unknown scope "
                            f"{row['scope']!r}")
        if row["binding"] not in bindings:
            raise ModeError(f"target {name!r} names unknown binding "
                            f"{row['binding']!r}")
    for tier, row in scopes.items():
        row["excludes"] = _split_list(row["excludes"])
        row["requires"] = _split_list(row["requires"])
        overlap = set(row["excludes"]) & set(row["requires"])
        if overlap:
            raise ModeError(f"scope tier {tier!r} both excludes and requires "
                            f"{', '.join(sorted(overlap))}")
    for name, row in bindings.items():
        if row["geometry"] not in ("input", "output"):
            raise ModeError(f"binding {name!r}: geometry must be input|output,"
                            f" found {row['geometry']!r}")
        cap = row.get("cap", "-")
        if cap not in ("", "-"):
            m = _CAP_RE.match(cap)
            if not m:
                raise ModeError(f"binding {name!r}: cap must be '-' or "
                                f"'+N%', found {cap!r}")
            row["cap_factor"] = 1.0 + float(m.group(1)) / 100.0
        else:
            row["cap_factor"] = None
        row["also_binds"] = _split_list(row.get("also binds", "-"))
    return {"doc": str(path).replace("\\", "/"), "tokens": tokens,
            "targets": targets, "scopes": scopes, "bindings": bindings}


_CACHE: dict[str, dict] = {}


def load(path: Path | str | None = None) -> dict:
    key = str(Path(path or DOC))
    if key not in _CACHE:
        _CACHE[key] = parse_doc(path)
    return _CACHE[key]


# ---- tokens -------------------------------------------------------------
def detect(text: str, doc: dict | None = None) -> str | None:
    """The mode token a brief OPENS with, or None.

    Only the leading non-empty, non-heading lines are searched: a mode is a
    declaration at the top of the brief, not a word somewhere in its prose.
    A brief quoted as a markdown blockquote (the owner's words, verbatim) is
    read through its `>` markers: a quoted line that opens with a token
    declares it, and a quoted line that does not (a provenance note) is
    passed over, so only a plain line ends the search.
    """
    doc = doc or load()
    for raw in (text or "").splitlines():
        ln = raw.strip()
        if not ln or ln.startswith("#"):
            continue
        quoted = ln.startswith(">")
        if quoted:
            ln = ln.lstrip("> \t")
            if not ln or ln.startswith("#"):
                continue
        tok = _token_at(ln, doc)
        if tok is not None or not quoted:
            return tok                  # first plain line decides
    return None


def _token_at(ln: str, doc: dict) -> str | None:
    """The mode token line `ln` opens with, or None."""
    low = ln.lower()
    for tok in doc["tokens"]:
        if tok != GENERIC_TOKEN and low.startswith(tok.lower()):
            return tok
    m = _TOKEN_RE.match(ln)
    if m:
        return f"learning {m.group(1).lower()}:"
    return None


def resolve(token: str, doc: dict | None = None) -> dict:
    """Resolve a token into the full mode record. Raises ModeError on an
    unknown target - naming the known ones, because that message is the only
    thing standing between a typo and a silently mode-less run."""
    doc = doc or load()
    tok = (token or "").strip()
    target = None
    for known, row in doc["tokens"].items():
        if known != GENERIC_TOKEN and tok.lower() == known.lower():
            target, alias = row["target"], True
            break
    else:
        alias = False
        m = _TOKEN_RE.match(tok)
        if not m:
            raise ModeError(
                f"not a build-mode token: {token!r} (expected "
                f"'{GENERIC_TOKEN}' or one of "
                f"{', '.join(sorted(t for t in doc['tokens'] if t != GENERIC_TOKEN))})")
        target = m.group(1).lower()
    if target not in doc["targets"]:
        raise ModeError(f"unknown learning target {target!r}: known targets "
                        f"are {', '.join(sorted(doc['targets']))}")
    trow = doc["targets"][target]
    scope, binding = trow["scope"], trow["binding"]
    srow, brow = doc["scopes"][scope], doc["bindings"][binding]
    stage = trow.get("stage", "-")
    return {
        "token": tok, "alias": alias, "target": target, "scope": scope,
        "binding": binding, "stage": None if stage in ("", "-") else stage,
        "teaches": trow.get("teaches", ""),
        "excludes": list(srow["excludes"]), "requires": list(srow["requires"]),
        "geometry": brow["geometry"], "cap_factor": brow["cap_factor"],
        "also_binds": list(brow["also_binds"]),
        "geometry_is_output": brow["geometry"] == "output",
        "relaxes": list(RELAXES) if brow["geometry"] == "output" else [],
        "never_relaxed": list(NEVER_RELAXED),
    }


def resolve_text(text: str, doc: dict | None = None) -> dict | None:
    """detect() + resolve() over a brief's text. None = no mode declared."""
    tok = detect(text, doc)
    return None if tok is None else resolve(tok, doc)


# ---- geometry -----------------------------------------------------------
def parse_size(text: str) -> tuple[float, float] | None:
    """The first `W x H` in a string, or None."""
    m = _SIZE_RE.search(text or "")
    return (float(m.group(1)), float(m.group(2))) if m else None


def geometry_plan(mode: dict | None, stated: tuple[float, float] | None = None,
                  earned: tuple[float, float] | None = None) -> dict:
    """What P5 is given, and what happens to a stated size.

    `stated` is whatever size the brief or a checkpoint named; `earned` is what
    the placement actually needs (`board_edit --outline fit`), known only after
    P6. The answer is deliberately the same shape before and after: callers ask
    at P5 with earned=None and again after the fit.
    """
    if not mode:
        return {"binds": bool(stated), "board_init_outline":
                _fmt(stated) if stated else "auto",
                "flow": "fixed" if stated else "auto",
                "relaxed": False, "stated": list(stated) if stated else None,
                "earned": list(earned) if earned else None,
                "final": list(stated or earned) if (stated or earned) else None,
                "decision": None}
    out: dict = {"binding": mode["binding"], "relaxed": False,
                 "stated": list(stated) if stated else None,
                 "earned": list(earned) if earned else None}
    if not mode["geometry_is_output"]:
        out.update(binds=bool(stated), flow="fixed",
                   board_init_outline=_fmt(stated) if stated else "auto",
                   final=list(stated) if stated else
                   (list(earned) if earned else None), decision=None)
        return out
    # geometry is an OUTPUT: P5 gets room, the placement earns the size.
    out.update(binds=False, flow="fit-after-place", board_init_outline="auto")
    if earned is None:
        out["final"] = None
        out["decision"] = None if stated is None else _decision(mode, stated,
                                                                None, None)
        out["relaxed"] = stated is not None
        return out
    final, why = list(earned), None
    cap = mode["cap_factor"]
    if stated:
        fits = stated[0] >= earned[0] and stated[1] >= earned[1]
        if fits and cap is not None and (stated[0] <= earned[0] * cap
                                         and stated[1] <= earned[1] * cap):
            final, why = list(stated), "within cap"      # bounded: keep it
        elif fits and cap is None:
            why = "stated size is larger than the layout needs"
        else:
            why = ("stated size is smaller than the layout needs"
                   if not fits else "stated size exceeds the binding's cap")
    out["final"] = final
    out["relaxed"] = bool(stated) and final != list(stated)
    out["kept_stated"] = bool(stated) and final == list(stated)
    out["decision"] = (_decision(mode, stated, earned, why)
                       if out["relaxed"] else None)
    return out


def _fmt(size: tuple[float, float] | None) -> str:
    if not size:
        return "auto"
    return f"{_num(size[0])}x{_num(size[1])}"


def _num(v: float) -> str:
    return f"{v:g}"


def _decision(mode: dict, stated, earned, why) -> dict:
    """The `state.py decision` a relaxation owes. Never silent (build-modes)."""
    what = f"geometry relaxed under binding {mode['binding']}: "
    what += (f"ignoring {_fmt(stated)}" if stated else "no size stated")
    if earned:
        what += f"; the layout earned {_fmt(earned)}"
    else:
        what += "; the outline is an OUTPUT - board_init --outline auto, then "
        what += "board_edit --outline fit after the place gate"
    return {
        "what": what[:400],
        "why": (f"target {mode['target']} binds {mode['binding']}: "
                f"{why or 'geometry follows the layout, not the brief'}. "
                f"A mode relaxes {', '.join(RELAXES)} only - "
                f"{', '.join(NEVER_RELAXED[:3])} are untouched."),
        "phase": "P2" if earned is None else "P6",
    }


# ---- scope --------------------------------------------------------------
def feature_verdict(mode: dict | None, feature: str) -> str:
    """`excluded` (never a finding) | `required` (absence IS a finding) |
    `normal` (judge it on engineering merit)."""
    if not mode:
        return "normal"
    f = (feature or "").strip().lower()
    if f in mode["excludes"]:
        return "excluded"
    if f in mode["requires"]:
        return "required"
    return "normal"


def summary(mode: dict | None) -> str:
    """One ASCII line for a checkpoint, a digest or a decision `what`."""
    if not mode:
        return "no build mode"
    bits = [f"mode {mode['target']}", f"scope {mode['scope']}",
            f"binding {mode['binding']}"]
    if mode["stage"]:
        bits.append(f"stage under study {mode['stage']}")
    return "; ".join(bits)
