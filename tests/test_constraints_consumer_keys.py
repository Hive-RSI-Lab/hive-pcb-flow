"""Every key the checkers read from constraints.json is a key the lint knows.

constraints_lint.py warns about any key it does not know, and its shapes
were a hand-kept copy of what the checkers read. The copy fell behind: one
real board's constraints.json drew 30 warnings, each about a key some
checker reads. This test takes the list from the code instead.
tests/_constraint_reads.py scans scripts/**/*.py (statically; it runs
nothing) for every constant key read off a constraints document:
d["k"], d.get("k"), "k" in d, through helpers and across modules. Then:

  - tests/fixtures/constraints_all_keys.json holds every path read, and
    the lint passes it with no error and no warning. A new .get("key") in a
    checker fails here until the key is in the fixture, in the lint's
    shapes and in reference/constraints_schema.md;
  - every key name read is in constraints_schema.md;
  - SKIP lists reads that are not keys of the file: the scan sees them on
    values the code builds itself. Each says why, and an entry the scan no
    longer reports fails as stale;
  - DYNAMIC lists the sites that read a key the scan cannot name (d[name]
    on a map keyed by refdes or net, a list index). A new site fails until
    someone looks at it and lists it; a stale one fails too;
  - CANARIES pin a few reads to their source line, so a scan that quietly
    stops following some code fails by name;
  - UNSEEN lists keys read where the scan cannot follow (a value kept on
    an object attribute), so the fixture and the schema page hold them too.

Escapes (a document value handed to code the scan cannot see: a method, an
attribute, a callback) are not asserted. Each is a scalar leaf, an output
(a report, a JSON dump), or a value whose keys are read elsewhere too
(place_anneal's _class_sets alias reads power[].net). The one exception is
placement.edges[].pos, in UNSEEN.

The scan takes about 13 s, once per module. It is not marked slow, on
purpose: bin/hwde test deselects slow, and this test has to run on every
PR, or the lint falls behind again.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))
import constraints_lint  # noqa: E402
import _constraint_reads as reads  # noqa: E402

FIXTURE = REPO / "tests" / "fixtures" / "constraints_all_keys.json"
SCHEMA_MD = REPO / "reference" / "constraints_schema.md"

# CPU seconds the scan may take: about 13 on the tower, so this fails only
# when the fixpoint blows up, never on a busy box
SCAN_CPU_LIMIT_S = 120

_MARK = ("check_current marks a power entry it derives or infers itself "
         "and reads the mark back; a constraints file never carries it")
_TODO = ("route_critical's one `for kind, specs in todo:` loop hands "
         "diff_pairs entries and the power and bus specs it builds through "
         "one name, so route_power_item, normalize_power_widths, sense_pairs "
         "and route_bus_item reads show up as diff_pairs[] reads")
_DDR = ("check_ddr_timing reads it off the ddr_timing.yaml controller row "
        "merged with controller_terms; controller_terms itself takes only "
        "CTRL_TERMS and stops on any other key")
# read paths that are not keys of a constraints file (path -> why)
SKIP = {
    "power[].derived": _MARK,
    "power[].inferred": _MARK,
    "diff_pairs[].net": _TODO,
    "diff_pairs[].nets": _TODO,
    "diff_pairs[].name": _TODO,
    "diff_pairs[].current_a": _TODO,
    "diff_pairs[].ipc_min_mm": _TODO,
    "diff_pairs[].width_mm": _TODO,
    "diff_pairs[].sense_pads": _TODO,
    "ddr.controller_terms.name": _DDR,
    "ddr.controller_terms.leveling_verified": _DDR,
}

_REF = "a map keyed by refdes"
_NET = "a map keyed by net name"
_INDEX = "a list taken by index"
# sites that read a key the scan cannot name ((file, path) -> why)
DYNAMIC = {
    ("scripts/check_antenna.py", "antennas"): _REF,
    ("scripts/check_cap_load.py", "cable_pf"): _REF,
    ("scripts/check_ldo_esr.py", "ldo_caps"): _REF,
    ("scripts/check_rail_budget.py", "rail_budget.sources"): _REF,
    ("scripts/check_rail_budget.py", "rail_budget.series"): _REF,
    ("scripts/check_rail_budget.py", "rail_budget.loads"): _REF,
    ("scripts/check_isolation.py", "isolation.parts"): _REF,
    ("scripts/check_isolation.py", "isolation.parts.*"):
        "over(key, ...) with each rating key; check_isolation checks the "
        "values itself",
    ("scripts/check_return_path.py", "high_speed[].reference"):
        "a reference map keyed by copper layer",
    ("scripts/check_return_path.py", "si.nets.*.reference"):
        "a reference map keyed by copper layer",
    ("scripts/check_si.py", "si.nets"): _NET,
    ("scripts/check_si.py", "si.victims"): _NET,
    ("scripts/check_surge.py", "surge.abs_max"): "keyed by REF.PAD or REF",
    ("scripts/check_surge.py", "surge.ports"): _REF,
    ("scripts/check_surge.py", "surge.clamps"): _REF,
    ("scripts/check_testpoints.py", "testpoints"):
        "`for k in DEFAULTS`: the probe rules, each in the lint's testpoints "
        "branch",
    ("scripts/check_transformer.py", "transformers"): _REF,
    ("scripts/lib/cablelib.py", "mating_pairs[].cable.ends"): _REF,
    ("scripts/lib/knowledgelib.py", "blocks[].operating_point"):
        "keyed by the dimensions a knowledge record's envelope lists",
    ("scripts/lib/rfmatch.py", "rf_match.*.band_hz"): _INDEX,
    ("scripts/lib/rfmatch.py", "rf_match[].band_hz"): _INDEX,
    ("scripts/planes_gen.py", "planes"): _INDEX,
    ("scripts/route_cleanup.py", ""):
        "a local entries(key), called with diff_pairs, length_match, "
        "length_groups, rf and high_speed",
    ("scripts/route_tune.py", "length_groups"): _INDEX,
    ("scripts/rules_gen.py", "isolation.domains"): "keyed by domain name",
}

# path -> (file, a snippet on the line that reads it): the scan must report
# the read at one of the lines that hold the snippet
CANARIES = {
    "power[].sense_pads": ("scripts/check_kelvin.py",
                           '(e.get("sense_pads") or {})'),
    "power[].source": ("scripts/check_current.py", 'entry.get("source")'),
    "power[].loads": ("scripts/check_current.py", 'entry.get("loads")'),
    "power_paths": ("scripts/check_kelvin.py", 'cons.get("power_paths")'),
    "placement.backside_decoupling": (
        "scripts/place_seed.py", 'placement.get("backside_decoupling")'),
}

# keys read where the scan cannot follow (path -> (file, snippet)): place_
# seed and place_anneal read an edge entry off a cluster's .edge attribute
UNSEEN = {
    "placement.edges[].pos": ("scripts/place_seed.py", 'c.edge.get("pos")'),
}


@pytest.fixture(scope="module")
def scan():
    wall, cpu = time.perf_counter(), time.process_time()
    s = reads.scan(REPO)
    s.wall_s = time.perf_counter() - wall
    s.cpu_s = time.process_time() - cpu
    return s


def _read_paths(scan) -> dict[str, set]:
    """{"power[].net": {(file, line)}} for every read the scan found"""
    out: dict[str, set] = {}
    for path, sites in scan.reads.items():
        out.setdefault(reads.fmt(path), set()).update(sites)
    return out


def _tokens(path: str) -> list[str]:
    return re.findall(r"\[\]|[^.\[\]]+", path)


def _wanted(scan) -> list[str]:
    """the read paths a constraints file may carry: no SKIP, no '_' key"""
    return sorted(p for p in set(_read_paths(scan)) | set(UNSEEN)
                  if p not in SKIP
                  and not any(t.startswith("_") for t in _tokens(p)))


def _reaches(doc, path: str) -> bool:
    """some node of doc sits at path ("[]": a list element; "*": a map
    member or a list element; a key: that key of a map)"""
    nodes = [doc]
    for t in _tokens(path):
        nxt = []
        for n in nodes:
            if t == "[]":
                if isinstance(n, list):
                    nxt += n
            elif t == "*":
                if isinstance(n, dict):
                    nxt += n.values()
                elif isinstance(n, list):
                    nxt += n
            elif isinstance(n, dict) and t in n:
                nxt.append(n[t])
        nodes = nxt
    return bool(nodes)


def _lines(file: str, snippet: str) -> set:
    text = (REPO / file).read_text(encoding="utf-8").splitlines()
    return {(file, i) for i, line in enumerate(text, 1) if snippet in line}


def test_scan_cost(scan):
    assert scan.cpu_s < SCAN_CPU_LIMIT_S, (
        f"the scan took {scan.cpu_s:.0f} s of CPU ({scan.wall_s:.0f} s wall, "
        f"{scan.rounds} rounds) against a limit of {SCAN_CPU_LIMIT_S} s")
    assert len(scan.reads) > 300, "the scan lost most of its reads"


def test_fixture_holds_every_key_the_checkers_read(scan):
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    sites = _read_paths(scan)

    def where(p):
        if p in UNSEEN:
            return UNSEEN[p][0]
        return "%s:%d" % min(sites[p])
    missing = [f"{p}  ({where(p)})"
               for p in _wanted(scan) if not _reaches(doc, p)]
    assert not missing, (
        "the checkers read these, but tests/fixtures/constraints_all_keys"
        ".json does not hold them. Add each to the fixture, to the lint's "
        "shapes (scripts/constraints_lint.py) and to reference/"
        "constraints_schema.md:\n  " + "\n  ".join(missing))


def test_lint_knows_every_key_in_the_fixture():
    violations, _ = constraints_lint.lint_file(FIXTURE)
    assert not violations, "\n".join(
        f"{v['severity']} {v['kind']}: {v['msg']}" for v in violations)


def test_schema_page_names_every_key_the_checkers_read(scan):
    md = SCHEMA_MD.read_text(encoding="utf-8")
    missing = sorted({t for p in _wanted(scan) for t in _tokens(p)
                      if t not in ("[]", "*") and not re.search(
                          r"(?<![A-Za-z0-9_])" + re.escape(t)
                          + r"(?![A-Za-z0-9_])", md)})
    assert not missing, ("reference/constraints_schema.md does not name: "
                         + ", ".join(missing))


def test_skip_entries_are_still_read(scan):
    stale = sorted(set(SKIP) - set(_read_paths(scan)))
    assert not stale, f"the scan no longer reports these; drop them from SKIP: {stale}"


def test_dynamic_sites_are_listed(scan):
    got = {(f, p) for (f, _line), paths in scan.dynamic.items() for p in paths}
    lines = {}
    for (f, line), paths in scan.dynamic.items():
        for p in paths:
            lines.setdefault((f, p), []).append(line)
    new = sorted(got - set(DYNAMIC))
    stale = sorted(set(DYNAMIC) - got)
    assert not new, (
        "these sites read a key the scan cannot name. Check what keys the "
        "code can take there, that the lint and the schema page know them, "
        "then list the site in DYNAMIC with why:\n  " + "\n  ".join(
            f"{f}:{min(lines[(f, p)])}  {p or '(the document)'}"
            for f, p in new))
    assert not stale, f"no longer read by name; drop them from DYNAMIC: {stale}"


@pytest.mark.parametrize("path", sorted(CANARIES))
def test_canary_reads_are_seen(scan, path):
    file, snippet = CANARIES[path]
    want = _lines(file, snippet)
    assert want, f"{file} no longer holds {snippet!r}: update CANARIES"
    got = _read_paths(scan).get(path, set())
    assert got & want, (
        f"the scan does not see the read of {path} at "
        f"{', '.join(f'{f}:{n}' for f, n in sorted(want))}")


@pytest.mark.parametrize("path", sorted(UNSEEN))
def test_unseen_reads_are_still_unseen(scan, path):
    file, snippet = UNSEEN[path]
    assert _lines(file, snippet), (
        f"{file} no longer holds {snippet!r}: update UNSEEN")
    assert path not in _read_paths(scan), (
        f"the scan now sees {path}: drop it from UNSEEN")
