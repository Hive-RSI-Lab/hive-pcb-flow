"""checklib.py - shared plumbing for the S4/S5 verification checks.

Every check script emits the S2 normalized violation schema (kc.py shape):
    {check, severity, pos: [x, y] | None, layer, net, refs, msg, source, items}
plus check-specific extra keys (kind, distances, polygons ...) that downstream
consumers (cluster_violations.py, fixer agents) may read but must not require.

Report payload shape (mirrors kc.py's run_erc/run_drc reports so gate.py and
S5's verify_all.py can treat kicad-cli gates and custom checks uniformly):
    {script, status: pass|violations|error, board, counts{total, by_severity,
     by_source}, violations: [...], ...check-specific facts}

Exit contract (SPEC.md section 6): 0 pass, 1 violations, 2 error.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


class CheckError(RuntimeError):
    """The check cannot run (bad inputs, unusable board). CLI exit 2."""


# Version of the report payload shape gate.py validates (U2, codex C4). Bump
# when a field gate.py's --report validation relies on changes meaning.
REPORT_SCHEMA = 1

# Version of the CHECKER SEMANTICS behind the reports (U5, codex H9). Bump on
# any change to what a check finds or how findings are keyed - durable waivers
# bind this value, so a bump invalidates every recorded waiver until a human
# re-approves it under the new checkers. Coarse on purpose: one bump point,
# conservative direction.
CHECKER_VERSION = 1


def _statelib():
    """Import lib/statelib regardless of whether checklib was imported as
    `checklib` (lib on sys.path) or `lib.checklib` (scripts dir on sys.path)."""
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    import statelib
    return statelib


# Leading words that mark an approval as not given yet: a waiver or
# not-applicable entry whose `approved` opens with one of these is a request,
# not a human sign-off ("PENDING", "TBD - ask owner", "N/A", "not approved").
_NOT_APPROVED = frozenset((
    "pending", "tbd", "tba", "todo", "fixme", "xxx", "awaiting", "waiting",
    "unapproved", "notapproved", "notyet", "needsapproval", "needsreview",
    "requested", "none", "null", "nil", "na", "no", "false", "unknown",
    "rejected", "denied", "who"))


def approval_problem(value) -> str | None:
    """Why an `approved` value (who/when) is not a real approval, or None.
    Missing, empty, non-text, punctuation-only, template (`<who> <when>`) and
    placeholder values (PENDING, TBD, TODO, awaiting..., N/A, none ...) are
    not approvals - waivers are human artifacts and an unsigned one must not
    pass a gate."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return "'approved' is missing or empty"
    if not isinstance(value, str):
        return f"'approved' {value!r} is not text naming who and when"
    text = value.strip()
    words = re.findall(r"[a-z0-9]+", text.lower())
    if not words:
        return f"'approved' {text!r} names no one"
    if re.search(r"<[^>]*>", text) or words[0] in _NOT_APPROVED \
            or "".join(words[:2]) in _NOT_APPROVED:
        return (f"'approved' {text!r} is a placeholder, not a sign-off - "
                "a human must approve it (who and when)")
    return None


def stamp(payload: dict, input_path) -> dict:
    """Attach the provenance fields gate.py --report validates (U2, codex C4):
    schema version, generation time (UTC), input path and its NORMALIZED
    design digest (statelib norms - byte hashes churn on UUID/EOL noise).
    Digest failures degrade to None (the report stays usable standalone;
    gate.py refuses a report without a digest)."""
    from datetime import datetime, timezone
    payload["report_schema"] = REPORT_SCHEMA
    payload["checker_version"] = CHECKER_VERSION
    payload["generated_at"] = datetime.now(timezone.utc).isoformat(
        timespec="seconds")
    if input_path is not None:
        p = Path(input_path)
        payload["input"] = str(p)
        try:
            sl = _statelib()
            payload["input_digest"] = sl.hash_artifact(p, sl.norm_for_path(p))
        except Exception:
            payload["input_digest"] = None
    return payload


def utf8_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def data_keys(d: dict) -> set:
    """The keys of a constraints object that carry data: a key starting
    with "_" is a comment (`_why`, `_note`), which constraints_lint passes,
    so a consumer's unknown-key check must pass it too (#1097)."""
    return {k for k in d if not (isinstance(k, str) and k.startswith("_"))}


def rnd(v: float, nd: int = 4) -> float:
    return round(float(v), nd)


def poly_coords(poly, nd: int = 3) -> list[list[float]]:
    """Exterior ring of a shapely polygon as rounded [x, y] pairs."""
    return [[rnd(x, nd), rnd(y, nd)] for x, y in poly.exterior.coords]


def manhattan(a: tuple[float, float], b: tuple[float, float]) -> float:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def violation(check: str, severity: str, pos, layer, net, refs, msg: str,
              source: str, **extras) -> dict:
    """Build one normalized violation (kc.py schema + check-specific extras)."""
    p = [rnd(pos[0]), rnd(pos[1])] if pos is not None else None
    v = {
        "check": check,
        "severity": severity,
        "pos": p,
        "layer": layer,
        "net": net,
        "refs": sorted(set(refs or [])),
        "msg": msg,
        "source": source,
        "items": [{"msg": msg, "pos": p}],
    }
    v.update(extras)
    return v


def zone_fill_facts(violations: list[dict], fill) -> dict:
    """Report facts for a geom.fresh_board() fill note (None: nothing).
    Refilled: a zone_fill fact says the board was judged on a refilled
    scratch copy. Not refilled: the reason goes to not_judged, and an error
    on a net with an unfilled zone (or on no net: a part's copper may be one
    of those zones) is cut to a warning, its copper being undercounted."""
    if not fill:
        return {}
    import geom  # noqa: E402  (lib/ is on sys.path for every check)
    out = {"zone_fill": dict(fill, note=geom.fill_note_text(fill))}
    if fill["refilled"]:
        return out
    nets = {z["net"] for z in fill["zones"]}
    for v in violations:
        if v.get("severity") == "error" and (v.get("net") is None
                                             or v.get("net") in nets):
            v["severity"] = "warning"
            v["msg"] += ("; zone fill not current (unfilled zones on this "
                         "board could not be refilled), copper undercounted")
            for it in v.get("items", []):
                it["msg"] = v["msg"]
    out["not_judged"] = [out["zone_fill"]["note"]]
    return out


def summarize(violations: list[dict]) -> dict:
    counts = {"total": len(violations)}
    by_sev: dict[str, int] = {}
    by_src: dict[str, int] = {}
    for v in violations:
        by_sev[v["severity"]] = by_sev.get(v["severity"], 0) + 1
        by_src[v["source"]] = by_src.get(v["source"], 0) + 1
    counts["by_severity"] = by_sev
    counts["by_source"] = by_src
    return counts


def report(script: str, board, violations: list[dict], **facts) -> dict:
    """The check's JSON. Two optional facts feed verify_all's coverage:
    nothing_to_judge (why the check had nothing it could judge: not a pass)
    and not_judged (what it had to leave out while judging the rest)."""
    for key in ("nothing_to_judge", "not_judged"):
        if not facts.get(key):
            facts.pop(key, None)
    payload = {
        "script": script,
        "status": "violations" if violations else "pass",
        "board": Path(board).name if board else None,
        "counts": summarize(violations),
        "violations": violations,
        **facts,
    }
    return stamp(payload, board)


def load_json(path, what: str) -> dict:
    p = Path(path)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CheckError(f"cannot read {what} {p}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise CheckError(f"{what} {p} is not valid JSON: {exc}") from exc


SNAPSHOT_DIRS = ("state_snapshots", "snapshots")


def find_constraints(pcb) -> Path | None:
    """The constraints.json that governs a board file: the one beside it, or,
    for a copy kept under a state_snapshots/ (or snapshots/) folder, the
    nearest one above it, up to the board folder that holds the snapshots
    (checking each level and its kicad/). A board outside a snapshot folder
    gets only its sibling: walking up from an ordinary board could pick up
    another board's file. The caller still checks that the file's nets are on
    the copy (check_current: constraints_mismatch)."""
    p = Path(pcb).resolve()
    own = p.parent / "constraints.json"
    if own.is_file():
        return own
    parts = p.parent.parts
    idx = [i for i, n in enumerate(parts) if n in SNAPSHOT_DIRS]
    if not idx:
        return None
    top = Path(*parts[:idx[-1]])        # the board folder holding the snapshots
    d = p.parent
    while True:
        for c in (d / "constraints.json", d / "kicad" / "constraints.json"):
            if c.is_file():
                return c
        if d == top or d.parent == d:
            return None
        d = d.parent


def emit(payload: dict, out: str | None) -> int:
    """Write/print the report; return the SPEC exit code for its status."""
    text = json.dumps(payload, indent=1)
    if out:
        # a report path into a directory that does not exist yet must not
        # crash AFTER the script has already changed the board
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(text, encoding="utf-8")
    else:
        print(text)
    return {"pass": 0, "violations": 1}.get(payload.get("status"), 2)


def cli_wrap(script: str, fn) -> int:
    """Run fn() -> (payload, out_path); map any exception to the exit-2
    error JSON contract (geom.py CLI pattern)."""
    utf8_stdout()
    try:
        payload, out = fn()
    except Exception as exc:  # noqa: BLE001  (contract: any error -> exit 2)
        print(json.dumps({"script": script, "status": "error",
                          "error": f"{type(exc).__name__}: {exc}"}))
        return 2
    return emit(payload, out)
