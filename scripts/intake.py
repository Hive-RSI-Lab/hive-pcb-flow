#!/usr/bin/env python
"""intake.py - import an EXTERNAL KiCad project into an hwde workspace (T9).

The "review this board" front door: point it at any KiCad project (a directory,
a .kicad_pro/.kicad_pcb/.kicad_sch) and it produces a normal hwde workspace -
copied-in design files at the pinned KiCad format, a v2 state.json, baseline
gate results, a netlist audit, renders, and a design-document digest - WITHOUT
ever writing to the source.

    intake.py --source DIR|FILE [--board NAME] [--pn PCB-NNNN-R]
              [--workspace DIR] [--project STEM]
              [--force [--keep-foreign]] [--no-upgrade] [--no-gates] [--no-renders] [--no-report]
              [--views top,bottom,iso] [--out report.json]

What it does, in order:
 1. DISCOVER the project (stem + .kicad_pro/.kicad_pcb/.kicad_sch), the
    schematic hierarchy (recursive Sheetfile properties), the lib tables and
    every ${KIPRJMOD}-relative library they point at.
 2. STAGE a copy into <workspace>.intake-tmp/kicad/ preserving the project's
    own relative layout (so ${KIPRJMOD} URIs keep resolving). The top-level
    stem files (.kicad_pro/.kicad_pcb/.kicad_sch/.kicad_dru) are renamed to the
    project name - <PN>_<board> when register.yaml has issued the board a
    number (a rev whose dir is spelled so, or --pn), else the board name;
    the workspace directory takes the same name. The pipeline resolves every
    artifact by stem (state kinds, kicad-cli sidecar lookup: LEARNINGS
    2026-08-06 [bench][kicad-cli]), and .kicad_pro's meta.filename is
    patched to match. Sub-sheets, .kicad_sym and
    .pretty dirs keep their names (they are referenced by name/URI).
    A lib URI that escapes the project dir is copied to kicad/imported_libs/
    and the URI is rewritten IN THE COPY, so the workspace is self-contained
    (LEARNINGS 2026-07-28 [librarian][kicad]: an escaping URI silently shares
    one library between boards).
 3. VERSION-PIN the copies: `kicad-cli pcb upgrade` / `sch upgrade` on every
    board and schematic (smoke-verified on this host: idempotent no-op when
    already current, "Successfully saved ... latest format" otherwise; it does
    NOT recurse into hierarchical children, so every sheet is upgraded).
    REFUSALS (exit 2, workspace never materialized): a file the pinned
    kicad-cli cannot load (newer format than the pin), or a project still
    carrying MIXED format versions within one file type afterwards.
 4. MATERIALIZE the workspace (atomic-ish directory rename), then state.py
    init (v2 scaffold) at the phase the design is actually in: P4 schematic
    only, P6 board without copper, P8 routed board.
 5. BASELINE: netlist export + netlist_audit, gates erc / drc_routed / verify /
    dfm (each recorded into state.json, which is what establishes the T7 input
    hashes), renders, schematic PDF, reports/intake-digest.md and report_gen's
    design document.

Exit codes follow SPEC section 6, with one deliberate reading: the BOARD's own
findings (ERC/DRC/verify/DFM violations) are the deliverable, not intake
failures - a foreign board with 40 DRC errors is a successful intake. Exit 1
("violations") is reserved for INTAKE-level problems: a gate that could not
run, an unresolvable library, a missing schematic, a degraded design document.
Exit 2 is a refusal: unusable source, a format the pin cannot read, a
mixed-version project, or a workspace that already exists (use --force).
--force replaces only the files intake wrote (listed in
reports/intake-files.json, plus state.json and reports/); it refuses, naming
them, when the workspace holds anything else (fw/, import/, kicad/gen/, a
README), unless --keep-foreign says to leave those in place (#536).

Gate results are recorded honestly: a `verify` pass on a project with no
constraints.json means "no check had inputs", so the digest and the report
name every skipped check.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "lib"))

import checklib  # noqa: E402
from checklib import CheckError  # noqa: E402
from lib import boardreg, env  # noqa: E402

SCRIPT = "intake"
SOURCE = "intake"
STAGE_SUFFIX = ".intake-tmp"
# A project is named by any of these; the top-level ones (plus .kicad_dru) are
# renamed to the board stem, since KiCad resolves project sidecars BY STEM.
# .kicad_prl is UI state and is not imported.
PROJECT_SUFFIXES = (".kicad_pro", ".kicad_pcb", ".kicad_sch")
DEFAULT_VIEWS = "top,bottom,iso"
# Baseline gates, in pipeline order. `drc` is the fallback when drc_routed
# refuses (it requires fresh zone fills - an imported board often has none).
BASELINE_GATES = ("erc", "drc_routed", "verify", "dfm")

_VERSION_RE = re.compile(r"\(version\s+(\d+)\s*\)")
_GENVER_RE = re.compile(r'\(generator_version\s+"([^"]*)"\)')
_SHEETFILE_RE = re.compile(r'\(property\s+"Sheetfile"\s+"([^"]+)"')
_LIBENTRY_RE = re.compile(
    r'\(name\s+"([^"]+)"\)(?:[^()]|\([^()]*\))*?\(uri\s+"([^"]+)"\)')
_SEGMENT_RE = re.compile(r"\((?:segment|arc)\s")
_ENVVAR_RE = re.compile(r"^\$[{(](\w+)[)}]")
# KiCad's built-in path variables (defined by KiCad itself, NOT by the OS
# environment) -> their subdirectory under <install>/share/kicad.
_KICAD_VAR_RE = re.compile(r"KICAD(\d*)_(FOOTPRINT|SYMBOL|3DMODEL|TEMPLATE)_DIR",
                           re.I)
_KICAD_VAR_DIRS = {"FOOTPRINT": "footprints", "SYMBOL": "symbols",
                   "3DMODEL": "3dmodels", "TEMPLATE": "template"}


# --------------------------------------------------------------- pure helpers
def sanitize_board(name: str) -> str:
    """Workspace/board name: the stem with anything but [A-Za-z0-9._-] folded
    to '-' (a board name is also a directory name and a file stem)."""
    clean = re.sub(r"[^A-Za-z0-9._-]+", "-", str(name)).strip("-._")
    if not clean:
        raise CheckError(f"cannot derive a board name from {name!r}")
    return clean


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def source_fingerprint(paths: list[Path]) -> dict[str, str]:
    """{file: sha256} over the source files intake reads, directories expanded
    to their members - the evidence that the source is untouched afterwards."""
    out: dict[str, str] = {}
    for p in paths:
        if p.is_dir():
            for f in sorted(x for x in p.rglob("*") if x.is_file()):
                out[str(f)] = sha256_file(f)
        elif p.is_file():
            out[str(p)] = sha256_file(p)
    return out


def read_head(path: Path, nbytes: int = 4096) -> str:
    with open(path, "rb") as fh:
        return fh.read(nbytes).decode("utf-8", errors="replace")


def file_format(path: Path) -> dict:
    """{version, generator_version} from a KiCad s-expression file header.
    Missing tokens are None - never an exception (a hand-rolled file must be
    reportable, not fatal)."""
    head = read_head(path)
    ver = _VERSION_RE.search(head)
    gen = _GENVER_RE.search(head)
    return {"version": int(ver.group(1)) if ver else None,
            "generator_version": gen.group(1) if gen else None}


def sheet_files(root_sch: Path) -> tuple[list[Path], list[str]]:
    """Every .kicad_sch in the hierarchy below (and including) root_sch, plus
    the names of sheet files the hierarchy references but that do not exist.
    Cycle-safe: KiCad allows the same sheet file on several sheets."""
    found: list[Path] = []
    missing: list[str] = []
    seen: set[str] = set()
    queue = [root_sch]
    while queue:
        cur = queue.pop(0)
        key = str(cur.resolve()).lower()
        if key in seen:
            continue
        seen.add(key)
        if not cur.is_file():
            missing.append(cur.name)
            continue
        found.append(cur)
        try:
            text = cur.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for rel in _SHEETFILE_RE.findall(text):
            queue.append((cur.parent / rel).resolve())
    return found, missing


def lib_table_entries(path: Path) -> list[dict]:
    """[{name, uri}] parsed from an fp-/sym-lib-table."""
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    return [{"name": n, "uri": u} for n, u in _LIBENTRY_RE.findall(text)]


def kicad_share_dir(cli: Path | None) -> Path | None:
    """<install>/share/kicad for the resolved kicad-cli - where KiCad's own
    ${KICAD<n>_FOOTPRINT_DIR} family points. None when it cannot be located
    (the URI is then reported unresolved, which is the conservative read)."""
    if cli is None:
        return None
    for cand in (cli.parent.parent / "share" / "kicad",
                 cli.parent.parent / "share",
                 cli.parent.parent.parent / "share" / "kicad"):
        if cand.is_dir():
            return cand
    return None


def classify_uri(uri: str, project_dir: Path,
                 kicad_share: Path | None = None) -> dict:
    """Where a lib-table URI points, from the workspace's point of view.

    kind: project   ${KIPRJMOD}-relative and inside the project dir -> copy in
          escaping  ${KIPRJMOD}-relative but climbing OUT of it -> copy+rewrite
          env       another ${VAR}: KiCad's own KICAD<n>_FOOTPRINT_DIR family
                    is resolved against the PINNED install's share dir (KiCad
                    defines those internally - they are not OS environment
                    variables, so os.path.expandvars alone reports every
                    stock library "missing"), anything else via the process
                    environment
          absolute  a plain filesystem path
    """
    if uri.startswith("${KIPRJMOD}") or uri.startswith("$(KIPRJMOD)"):
        rel = uri.split("}", 1)[-1].split(")", 1)[-1].lstrip("/\\")
        target = (project_dir / rel).resolve()
        try:
            inside = target.relative_to(project_dir.resolve())
        except ValueError:
            return {"kind": "escaping", "uri": uri, "rel": rel,
                    "target": target}
        return {"kind": "project", "uri": uri, "rel": inside.as_posix(),
                "target": target}
    m = _ENVVAR_RE.match(uri)
    if m:
        var = m.group(1)
        rest = uri[m.end():].lstrip("/\\").rstrip("}")
        rec = {"kind": "env", "uri": uri, "var": var, "target": None,
               "var_major": None}
        kv = _KICAD_VAR_RE.fullmatch(var)
        if kv and kicad_share is not None:
            rec["var_major"] = int(kv.group(1)) if kv.group(1) else None
            rec["target"] = kicad_share / _KICAD_VAR_DIRS[kv.group(2).upper()] \
                / rest
        else:
            expanded = os.path.expandvars(
                uri.replace("$(", "${").replace(")", "}"))
            if "$" not in expanded:
                rec["target"] = Path(expanded)
        return rec
    return {"kind": "absolute", "uri": uri, "target": Path(uri)}


def discover_project(source: Path, project: str | None = None) -> dict:
    """{dir, stem, pro, pcb, sch} for the project to import. Refuses an
    ambiguous directory (KiCad demos ship several projects side by side)
    rather than guessing."""
    src = Path(source)
    if not src.exists():
        raise CheckError(f"source does not exist: {src}")
    if src.is_file():
        if src.suffix not in PROJECT_SUFFIXES:
            raise CheckError(
                f"source file must be one of {', '.join(PROJECT_SUFFIXES)}, "
                f"got {src.suffix or src.name!r}")
        pdir, stem = src.parent, src.stem
    else:
        pdir = src
        if project:
            stem = project
            if not any((pdir / f"{stem}{sfx}").is_file()
                       for sfx in PROJECT_SUFFIXES):
                raise CheckError(
                    f"--project {project!r}: no {'/'.join(PROJECT_SUFFIXES)} "
                    f"with that stem in {pdir}")
        else:
            stems: list[str] = []
            for sfx in PROJECT_SUFFIXES:          # .kicad_pro wins, then pcb
                stems = sorted({p.stem for p in pdir.glob(f"*{sfx}")})
                if stems:
                    break
            if not stems:
                raise CheckError(
                    f"no KiCad project found in {pdir} (looked for "
                    f"{', '.join(PROJECT_SUFFIXES)})")
            if len(stems) > 1:
                raise CheckError(
                    f"{pdir} holds several projects ({', '.join(stems)}) - "
                    "name one with --project STEM")
            stem = stems[0]
    spec = {"dir": pdir.resolve(), "stem": stem}
    for key, sfx in (("pro", ".kicad_pro"), ("pcb", ".kicad_pcb"),
                     ("sch", ".kicad_sch"), ("dru", ".kicad_dru")):
        p = pdir / f"{stem}{sfx}"
        spec[key] = p.resolve() if p.is_file() else None
    if spec["pcb"] is None and spec["sch"] is None:
        raise CheckError(
            f"project {stem!r} in {pdir} has neither a .kicad_pcb nor a "
            ".kicad_sch - nothing to import")
    return spec


def board_phase(pcb: Path | None) -> str:
    """The phase an imported design is actually in: P4 (schematic only),
    P6 (board placed, no copper), P8 (routed board awaiting verification)."""
    if pcb is None:
        return "P4"
    return "P8" if _SEGMENT_RE.search(pcb.read_text(encoding="utf-8",
                                                    errors="replace")) \
        else "P6"


def plan_copy(spec: dict, board: str, kicad_share: Path | None = None,
              pin_major: int | None = None) -> tuple[list[dict], list[dict],
                                                     list[dict]]:
    """(copies, libs, findings).

    copies: [{src, dest}] with dest workspace-relative (kicad/...).
    libs:   one entry per lib-table URI with its resolution + disposition.
    findings: intake violations raised while planning (unresolvable libs,
              missing sheet files).
    """
    pdir: Path = spec["dir"]
    copies: list[dict] = []
    libs: list[dict] = []
    findings: list[dict] = []
    seen_dest: set[str] = set()
    dest_src: dict[str, str] = {}
    cross_gen: set[str] = set()   # ${KICAD<n>_*} vars from another generation

    def add(src: Path, dest: str) -> None:
        dest = dest.replace("\\", "/")
        if dest in seen_dest:
            return
        seen_dest.add(dest)
        dest_src[dest] = str(src)
        copies.append({"src": str(src), "dest": dest})

    def unique_dest(base: str, src: Path) -> str:
        """A destination that is free, or already claimed by THIS source.
        Two libraries called foo.pretty in different directories, or a
        stray sheet whose name matches an in-project one, must not silently
        overwrite each other (add() de-dups by destination)."""
        if dest_src.get(base) in (None, str(src)):
            return base
        stem, dot, ext = base.partition(".")
        n = 2
        while dest_src.get(f"{stem}_{n}{dot}{ext}") not in (None, str(src)):
            n += 1
        return f"{stem}_{n}{dot}{ext}"

    # 1. the stem files, renamed to the board
    for key, sfx in (("pro", ".kicad_pro"), ("pcb", ".kicad_pcb"),
                     ("dru", ".kicad_dru")):
        if spec.get(key):
            add(spec[key], f"kicad/{board}{sfx}")
    # 2. the schematic hierarchy: root renamed, sub-sheets keep their names
    #    (they are referenced BY NAME from the sheet symbols)
    if spec.get("sch"):
        sheets, missing = sheet_files(spec["sch"])
        for sheet in sheets:
            if sheet == spec["sch"]:
                add(sheet, f"kicad/{board}.kicad_sch")
                continue
            try:
                rel = sheet.relative_to(pdir).as_posix()
            except ValueError:            # a sheet outside the project dir
                rel = unique_dest(f"kicad/{sheet.name}", sheet)[len("kicad/"):]
                findings.append(checklib.violation(
                    "intake", "warning", None, None, None, [],
                    f"sub-sheet {sheet} lives outside the project directory - "
                    "imported flat into kicad/", source=SOURCE,
                    kind="sheet_outside_project"))
            add(sheet, f"kicad/{rel}")
        for name in missing:
            findings.append(checklib.violation(
                "intake", "error", None, None, None, [],
                f"schematic hierarchy references a missing sheet file: {name}",
                source=SOURCE, kind="sheet_missing"))
    # 3. lib tables + everything they point at
    for table in ("fp-lib-table", "sym-lib-table"):
        tpath = pdir / table
        if not tpath.is_file():
            continue
        add(tpath, f"kicad/{table}")
        for entry in lib_table_entries(tpath):
            info = classify_uri(entry["uri"], pdir, kicad_share)
            rec = {"table": table, "name": entry["name"], "uri": entry["uri"],
                   "kind": info["kind"], "disposition": None,
                   "rewritten_to": None}
            target = info.get("target")
            exists = bool(target and target.exists())
            if info["kind"] == "project":
                if exists:
                    add(target, f"kicad/{info['rel']}")
                    rec["disposition"] = "copied"
                else:
                    rec["disposition"] = "missing"
                    findings.append(checklib.violation(
                        "intake", "warning", None, None, None, [],
                        f"lib {entry['name']!r} ({table}) points at "
                        f"{entry['uri']} which does not exist in the source",
                        source=SOURCE, kind="lib_uri_unresolved"))
            elif info["kind"] == "escaping":
                # keep the workspace self-contained: import + rewrite the URI
                if exists:
                    dest_rel = unique_dest(
                        f"kicad/imported_libs/{target.name}",
                        target)[len("kicad/"):]
                    add(target, f"kicad/{dest_rel}")
                    rec["disposition"] = "copied_rewritten"
                    rec["rewritten_to"] = "${KIPRJMOD}/" + dest_rel
                    findings.append(checklib.violation(
                        "intake", "warning", None, None, None, [],
                        f"lib {entry['name']!r} ({table}) escapes the project "
                        f"dir ({entry['uri']}); imported to kicad/{dest_rel} "
                        "and the URI rewritten in the copy",
                        source=SOURCE, kind="lib_uri_rewritten"))
                else:
                    rec["disposition"] = "missing"
                    findings.append(checklib.violation(
                        "intake", "warning", None, None, None, [],
                        f"lib {entry['name']!r} ({table}) points outside the "
                        f"project at {entry['uri']}, which does not exist",
                        source=SOURCE, kind="lib_uri_unresolved"))
            else:                       # env / absolute: host-resolved
                rec["disposition"] = "external" if exists else "external_missing"
                rec["resolved"] = str(target) if target else None
                if not exists:
                    findings.append(checklib.violation(
                        "intake", "warning", None, None, None, [],
                        f"lib {entry['name']!r} ({table}) resolves outside "
                        f"the workspace ({entry['uri']}) and is not readable "
                        "on this host - footprint/symbol loads may fail",
                        source=SOURCE, kind="lib_uri_unresolved"))
                elif info.get("var_major") and pin_major \
                        and info["var_major"] != pin_major:
                    cross_gen.add(info["var"])
            libs.append(rec)
    # 4. hwde sidecars, when the source IS an hwde-shaped project (another
    #    workspace's kicad/ dir, a golden fixture): they are the design's own
    #    constraint set, and without them the verify gate can only skip. Human
    #    artifacts (verify-waivers.json) deliberately do NOT travel.
    for sidecar in ("constraints.json", "decoupling.json", "parts.json"):
        cand = pdir / sidecar
        if cand.is_file():
            add(cand, f"kicad/{sidecar}")
    # 5. page-layout sheets referenced by the project file
    if spec.get("pro"):
        try:
            pro = json.loads(spec["pro"].read_text(encoding="utf-8",
                                                   errors="replace"))
        except (OSError, json.JSONDecodeError):
            pro = {}
        for section in ("board", "pcbnew", "schematic"):
            wks = ((pro.get(section) or {}) if isinstance(pro.get(section), dict)
                   else {}).get("page_layout_descr_file")
            if wks and not _ENVVAR_RE.match(str(wks)):
                cand = (pdir / str(wks))
                if cand.is_file():
                    add(cand, f"kicad/{Path(str(wks)).as_posix()}")
    if cross_gen:
        findings.append(checklib.violation(
            "intake", "warning", None, None, None, [],
            "lib tables reference another KiCad generation's path variables "
            f"({', '.join(sorted(cross_gen))}); resolved against the pinned "
            f"KiCad {pin_major} libraries",
            source=SOURCE, kind="lib_uri_cross_generation"))
    return copies, libs, findings


def rewrite_lib_uris(table_path: Path, libs: list[dict]) -> None:
    """Apply the planned URI rewrites to a COPIED lib table."""
    if not table_path.is_file():
        return
    text = table_path.read_text(encoding="utf-8", errors="replace")
    changed = False
    for rec in libs:
        if rec.get("rewritten_to") and rec["table"] == table_path.name:
            if rec["uri"] in text:
                text = text.replace(f'(uri "{rec["uri"]}")',
                                    f'(uri "{rec["rewritten_to"]}")')
                changed = True
    if changed:
        table_path.write_text(text, encoding="utf-8")


def patch_pro_filename(pro: Path) -> bool:
    """.kicad_pro records its own file name in meta.filename; the rename has
    to travel with it."""
    try:
        doc = json.loads(pro.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return False
    meta = doc.get("meta")
    if not isinstance(meta, dict) or meta.get("filename") == pro.name:
        return False
    meta["filename"] = pro.name
    pro.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return True


def upgrade_file(cli: Path, path: Path) -> dict:
    """`kicad-cli {pcb,sch} upgrade` on ONE file (it does not recurse into
    hierarchical sheets). Returns {file, before, after, updated, stdout}."""
    import kc  # noqa: PLC0415  (sibling wrapper; deferred for pure imports)
    section = "pcb" if path.suffix == ".kicad_pcb" else "sch"
    before = file_format(path)
    cp = kc.run_cli(cli, [section, "upgrade", str(path)])
    if cp.returncode != 0:
        raise CheckError(
            f"{path.name}: the pinned kicad-cli cannot load this file "
            f"(format version {before['version']}, written by KiCad "
            f"{before['generator_version']}) - it is newer than the pin, or "
            f"corrupt. kicad-cli said: "
            f"{(cp.stderr or cp.stdout or '').strip()[-300:]}")
    after = file_format(path)
    return {"file": path.name, "before": before["version"],
            "after": after["version"],
            "generator_version": before["generator_version"],
            "updated": before["version"] != after["version"],
            "stdout": (cp.stdout or "").strip()[:200]}


def check_version_consistency(formats: list[dict],
                              upgraded: bool = True) -> None:
    """Refuse a project that still mixes format versions within one file type.

    Upgrading is exactly the fix for a mixed SOURCE (KiCad 10's own demos ship
    a 2024 board next to a 2025 schematic), so the check runs AFTER the
    upgrade pass: what cannot be unified is what we refuse. Two schematics at
    different versions is a mix; a .kicad_pcb and a .kicad_sch at different
    versions is NORMAL (the formats are versioned independently).
    """
    by_type: dict[str, dict[int, list[str]]] = {}
    for rec in formats:
        suffix = Path(rec["file"]).suffix
        by_type.setdefault(suffix, {}).setdefault(rec["after"], []).append(
            rec["file"])
    for suffix, versions in sorted(by_type.items()):
        if len(versions) > 1:
            detail = "; ".join(f"{v}: {', '.join(sorted(files))}"
                               for v, files in sorted(
                                   versions.items(),
                                   key=lambda kv: (kv[0] is None, kv[0])))
            where = ("after the upgrade pass" if upgraded
                     else "and --no-upgrade was given")
            raise CheckError(
                f"mixed-version project: {suffix} files disagree on format "
                f"version {where} ({detail}). Re-run without --no-upgrade, or "
                "open the project in KiCad 10 once and save it.")


# ------------------------------------------------------- workspace ownership
# --force replaces only what intake writes (#536: a rerun used to rmtree the
# whole workspace and took fw/, import/, kicad/gen/ and the README with it).
# Intake's own files are: the ones its previous run listed in MANIFEST_REL,
# the ones this run is about to copy in, state.json (and its lock), the
# netlist, KiCad's regenerable UI state, and everything under reports/ (the
# generated gate, render and design-document outputs). Anything else is
# foreign.
MANIFEST_REL = "reports/intake-files.json"
OWNED_DIRS = ("reports",)
_KICAD_UI_STATE = (".kicad_prl",)
_KICAD_CACHES = ("fp-info-cache",)


def workspace_files(ws: Path) -> list[str]:
    """Every file (and symlink) under `ws`, as sorted POSIX paths relative to
    it. Empty directories are not listed: intake recreates its scaffold and a
    foreign empty directory is left where it is."""
    out: list[str] = []
    for root, dirs, files in os.walk(ws):
        rel_root = Path(root).relative_to(ws)
        for d in dirs:
            if (Path(root) / d).is_symlink():      # os.walk does not descend
                out.append((rel_root / d).as_posix())
        out.extend((rel_root / f).as_posix() for f in files)
    return sorted(out)


def read_manifest(ws: Path) -> set[str]:
    """The files the previous intake run wrote (empty for a workspace made
    before the manifest existed, or one whose manifest is unreadable)."""
    try:
        doc = json.loads((ws / MANIFEST_REL).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    files = doc.get("files") if isinstance(doc, dict) else None
    return {str(f) for f in files or [] if isinstance(f, str)}


def intake_owned(copies: list[dict], project: str,
                 manifest: set[str]) -> Callable[[str], bool]:
    """A predicate: is this workspace-relative POSIX path one intake writes?"""
    files = set(manifest) | {"state.json", "state.json.lock",
                             f"kicad/{project}.net"}
    dirs = list(OWNED_DIRS)
    for c in copies:
        dest = Path(c["dest"]).as_posix()
        if Path(c["src"]).is_dir():
            dirs.append(dest)
        else:
            files.add(dest)

    def owned(rel: str) -> bool:
        if rel in files or any(rel == d or rel.startswith(d + "/")
                               for d in dirs):
            return True
        p = Path(rel)
        return (p.parent.as_posix() == "kicad"
                and (p.suffix in _KICAD_UI_STATE or p.name in _KICAD_CACHES))
    return owned


def summarize_paths(paths: list[str], everything: list[str]) -> list[str]:
    """Fold `paths` for a message: a directory all of whose files are in
    `paths` is named once (`fw/ (5 files)`), the rest file by file."""
    def prefixes(rel: str) -> list[str]:
        parts = rel.split("/")
        return ["/".join(parts[:i]) + "/" for i in range(1, len(parts))]
    total: dict[str, int] = {}
    chosen: dict[str, int] = {}
    for rel in everything:
        for pre in prefixes(rel):
            total[pre] = total.get(pre, 0) + 1
    for rel in paths:
        for pre in prefixes(rel):
            chosen[pre] = chosen.get(pre, 0) + 1
    out: list[str] = []
    seen: set[str] = set()
    for rel in sorted(paths):
        top = next((pre for pre in prefixes(rel)
                    if chosen.get(pre) == total.get(pre)), None)
        if top is None:
            out.append(rel)
        elif top not in seen:
            seen.add(top)
            n = total[top]
            out.append(f"{top} ({n} file{'s' if n != 1 else ''})")
    return out


def merge_into(ws: Path, stage: Path, owned: Callable[[str], bool]) -> None:
    """Replace intake's files in `ws` with the staged ones and leave every
    other file where it is: delete the owned files, then move the stage in
    file by file. A crash part-way leaves the foreign files untouched."""
    present = workspace_files(ws)
    staged = workspace_files(stage)
    clash = sorted(set(staged) & {r for r in present if not owned(r)})
    if clash:                      # cannot happen: every staged path is owned
        raise CheckError(f"intake would overwrite files it did not write in "
                         f"{ws}: {', '.join(clash[:5])}")
    for rel in present:
        if owned(rel):
            (ws / rel).unlink()
    for rel in staged:
        dest = ws / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(stage / rel, dest)
    shutil.rmtree(stage)


# ------------------------------------------------------------- baseline gates
def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")


def run_baseline_gate(name: str, ws: Path, sch: Path | None,
                      pcb: Path | None) -> tuple[dict, dict | None]:
    """(gate_result, raw_report). Raises on a gate that cannot run."""
    import gate  # noqa: PLC0415
    gates = gate.load_gates(gate.DEFAULT_GATES)
    if name not in gates:
        raise CheckError(f"unknown gate {name!r}")
    gdef = gates[name]
    target = sch if gdef.get("tool") == "erc" else pcb
    if target is None:
        raise CheckError(f"gate {name}: no input artifact in this project")
    if gdef.get("tool") == "verify":
        # run verify_all ourselves so the per-check reports persist for review
        import verify_all  # noqa: PLC0415
        argv = ["--pcb", str(target),
                "--reports-dir", str(ws / "reports" / "checks")]
        for fname, flag in (("constraints.json", "--constraints"),
                            ("decoupling.json", "--decoupling")):
            f = target.parent / fname
            if f.exists():
                argv += [flag, str(f)]
        raw, _ = verify_all.run(argv)
        if raw.get("status") == "error":
            bad = [n for n, c in raw.get("checks", {}).items()
                   if c.get("status") == "error"]
            raise CheckError(f"verification could not run (checks errored: "
                             f"{bad})")
        raw["input"] = str(target)
        _write_json(ws / "reports" / "verify_all.json", raw)
    else:
        raw = gate.run_report_for_gate(gdef, target)
        if gdef.get("tool") == "dfm":
            _write_json(ws / "reports" / "dfm_check.json", raw)
        raw.setdefault("input", str(target))
    result = gate.evaluate(name, gdef, raw)
    _write_json(ws / "reports" / f"gate-{name}.json", result)
    return result, raw


# -------------------------------------------------------------------- digest
def _fmt_gate_row(name: str, rec: dict) -> str:
    if rec.get("status") == "error":
        return f"| {name} | ERROR | - | - | {rec.get('error', '')[:90]} |"
    if rec.get("status") == "skipped":
        return f"| {name} | SKIPPED | - | - | {rec.get('reason', '')} |"
    counts = rec.get("counts") or {}
    return (f"| {name} | {rec.get('status', '?').upper()} | "
            f"{rec.get('failing_count', '?')} | {counts.get('total', '?')} | "
            f"{rec.get('report', '')} |")


def build_digest(payload: dict) -> str:
    """The human 'review this board' summary (reports/intake-digest.md)."""
    b = payload["baseline"]
    lines = [f"# Intake digest - {payload['board']}", "",
             f"- Source: `{payload['source']['dir']}` "
             f"(project `{payload['source']['project']}`, "
             f"{payload['source']['files']} files, source unmodified: "
             f"{str(payload['source']['verified_unmodified']).lower()})",
             f"- Workspace: `{short_ws(payload['workspace'])}` (phase "
             f"{payload['phase']})",
             f"- Toolchain: kicad-cli {payload['toolchain']['version']}",
             f"- Imported: {payload['imported']}", ""]

    lines += ["## Format", "", "| file | version in | version out | written by |",
              "|---|---|---|---|"]
    for rec in payload["formats"]:
        lines.append(f"| {rec['file']} | {rec['before']} | {rec['after']} | "
                     f"KiCad {rec.get('generator_version') or '?'} |")
    if not payload["formats"]:
        lines.append("| (no upgrade pass run) | - | - | - |")
    lines.append("")

    lines += ["## Baseline gates", "",
              "| gate | status | failing | total | report |", "|---|---|---|---|---|"]
    for name, rec in b["gates"].items():
        lines.append(_fmt_gate_row(name, rec))
    lines.append("")

    skipped = [n for n, s in (b.get("verify_checks") or {}).items()
               if s == "skipped"]
    if skipped:
        lines += ["> `verify` ran with no constraints/decoupling sidecars: "
                  f"{len(skipped)} of {len(b['verify_checks'])} checks were "
                  f"SKIPPED ({', '.join(sorted(skipped))}). A pass here means "
                  "'no check had inputs', not 'the board is verified'.", ""]

    na = b.get("netlist_audit")
    if na:
        lines += ["## Netlist audit", "",
                  f"- status: {na['status']} "
                  f"({na['counts'].get('total', 0)} findings; "
                  f"report `{na['report']}`)"]
        for v in na.get("top", []):
            lines.append(f"  - [{v['severity']}] {v.get('kind', '')}: "
                         f"{v['msg']}")
        lines.append("")

    findings = payload.get("violations") or []
    lines += ["## Intake findings", ""]
    if findings:
        for v in findings:
            lines.append(f"- [{v['severity']}] {v.get('kind', '')}: {v['msg']}")
    else:
        lines.append("- none")
    lines.append("")

    if payload.get("libs"):
        lines += ["## Libraries", "", "| table | nickname | uri | disposition |",
                  "|---|---|---|---|"]
        for rec in payload["libs"]:
            uri = rec.get("rewritten_to") or rec["uri"]
            lines.append(f"| {rec['table']} | {rec['name']} | `{uri}` | "
                         f"{rec['disposition']} |")
        lines.append("")

    lines += ["## Next actions", ""]
    for act in payload.get("next_actions", []) or ["- (none)"]:
        lines.append(f"- {act}" if not act.startswith("-") else act)
    lines.append("")
    return "\n".join(lines)


def short_ws(workspace: str) -> str:
    """The workspace as the operator types it: absolute (boards live in
    their own repo, so a repo-relative path no longer resolves from hwde),
    with the home directory written as ~ to keep it short."""
    try:
        return "~/" + Path(workspace).resolve().relative_to(
            Path.home().resolve()).as_posix()
    except (ValueError, OSError):
        return workspace


def next_actions(payload: dict) -> list[str]:
    out: list[str] = []
    b = payload["baseline"]
    ws = short_ws(payload["workspace"])
    for name, rec in b["gates"].items():
        if rec.get("status") == "fail":
            out.append(f"`{name}` fails with {rec.get('failing_count')} "
                       f"finding(s) - cluster them "
                       f"(`cluster_violations.py --report {ws}/reports/"
                       f"gate-{name}.json`) and dispatch fixes")
        elif rec.get("status") == "error":
            out.append(f"`{name}` could not run: {rec.get('error', '')[:120]}")
    if any(s == "skipped" for s in (b.get("verify_checks") or {}).values()):
        out.append(f"author `{ws}/kicad/constraints.json` (+ decoupling.json) "
                   "to switch the skipped verify checks on - see "
                   "reference/constraints_schema.md")
    if not (payload["source"].get("has_sch")):
        out.append("no schematic in the source: ERC, netlist audit and DRC "
                   "parity are unavailable for this board")
    out.append(f"review `{ws}/reports/intake-digest.md` and the design "
               "document, then pick a task (fix-finding / dfm-check / order)")
    return out


# ----------------------------------------------------------------------- run
def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", required=True,
                    help="external project dir, .kicad_pro, .kicad_pcb or "
                         ".kicad_sch (never written to)")
    ap.add_argument("--project", help="project stem, when the source dir "
                                      "holds several projects")
    ap.add_argument("--board", help="board/workspace name (default: the "
                                    "source project stem)")
    ap.add_argument("--workspace", help="workspace dir (default: "
                                        "<boards root>/<PN>_<board> when "
                                        "register.yaml issued the board a "
                                        "number, else <boards root>/<board>; "
                                        "HWDE_BOARDS_ROOT, default ~/dev/boards)")
    ap.add_argument("--pn", help="the board's part number PCB-NNNN-R; the "
                                 "workspace takes that rev's `dir` from "
                                 "register.yaml (it must be listed there)")
    ap.add_argument("--force", action="store_true",
                    help="replace the files intake wrote in an existing "
                         "workspace (refuses a directory that is not one, "
                         "and one holding files intake did not write)")
    ap.add_argument("--keep-foreign", action="store_true",
                    help="with --force: leave the files intake did not write "
                         "(fw/, import/, kicad/gen/, ...) in place and "
                         "replace only intake's own")
    ap.add_argument("--no-upgrade", action="store_true",
                    help="do not run kicad-cli upgrade on the copies")
    ap.add_argument("--no-gates", action="store_true",
                    help="skip the baseline gate run")
    ap.add_argument("--no-renders", action="store_true")
    ap.add_argument("--no-report", action="store_true",
                    help="skip report_gen (design document)")
    ap.add_argument("--views", default=DEFAULT_VIEWS)
    ap.add_argument("--width", type=int, default=1600)
    ap.add_argument("--out", help="write the JSON report here")
    args = ap.parse_args(argv)

    started = time.strftime("%Y-%m-%dT%H:%M:%S")
    spec = discover_project(Path(args.source), args.project)
    board = sanitize_board(args.board or spec["stem"])
    # The KiCad project (and the workspace directory) is <PN>_<board> once
    # the register has issued the board a number, the bare board name before.
    if args.workspace:
        ws = Path(args.workspace)
        pn, human = boardreg.split_dir(ws.name)
        project = ws.name if pn and human == board else board
    else:
        try:
            project = boardreg.new_dir(board, env.boards_root(), args.pn)
        except ValueError as exc:
            raise CheckError(str(exc)) from exc
        ws = env.boards_root() / project
    ws = ws.resolve()

    if ws.exists() and any(ws.iterdir()):
        if not args.force:
            raise CheckError(f"workspace {ws} already exists and is not empty "
                             "(use --force to replace it)")
        if not (ws / "state.json").is_file():
            raise CheckError(
                f"--force refuses to delete {ws}: it has no state.json, so it "
                "is not an hwde workspace")

    # Toolchain pin (plan guardrail: "version-pin checks via lib/env.py").
    # env.find_kicad_cli() validates the pin itself and returns None when
    # nothing usable is installed; only the pure-copy modes can proceed then.
    cli = env.find_kicad_cli()
    cli_version = ".".join(str(x) for x in env.kicad_cli_version(cli)) \
        if cli else None
    needs_cli = not (args.no_upgrade and args.no_gates and args.no_renders)
    if cli is None and needs_cli:
        raise CheckError(
            "kicad-cli not found (the pipeline pins KiCad 10; see "
            "check_env.py). Re-run with --no-upgrade --no-gates --no-renders "
            "to copy the project in without touching the toolchain, or set "
            "HWDE_KICAD_CLI.")
    pin_major = env.kicad_cli_version(cli)[0] if cli else None

    copies, libs, findings = plan_copy(spec, project, kicad_share_dir(cli),
                                       pin_major)

    # ---- what --force may replace (#536) ----------------------------------
    owned = intake_owned(copies, project, read_manifest(ws)) \
        if ws.is_dir() else None
    present = workspace_files(ws) if owned else []
    foreign = [r for r in present if not owned(r)] if owned else []
    if foreign and not args.keep_foreign:
        listed = summarize_paths(foreign, present)
        more = f" and {len(listed) - 20} more" if len(listed) > 20 else ""
        raise CheckError(
            f"--force refuses to replace {ws}: it holds files intake did not "
            f"write: {', '.join(listed[:20])}{more}. Move them out, or re-run "
            "with --force --keep-foreign to replace only intake's own files "
            "and leave these in place")
    src_hashes = source_fingerprint([Path(c["src"]) for c in copies])

    # ---- stage ------------------------------------------------------------
    stage = ws.parent / (ws.name + STAGE_SUFFIX)
    if stage.exists():
        shutil.rmtree(stage)
    payload_formats: list[dict] = []
    try:
        for c in copies:
            dest = stage / c["dest"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            src = Path(c["src"])
            if src.is_dir():
                shutil.copytree(src, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dest)
        for table in ("fp-lib-table", "sym-lib-table"):
            rewrite_lib_uris(stage / "kicad" / table, libs)
        pro_copy = stage / "kicad" / f"{project}.kicad_pro"
        if pro_copy.is_file():
            patch_pro_filename(pro_copy)

        # ---- version pin --------------------------------------------------
        if not args.no_upgrade:
            targets = sorted((stage / "kicad").rglob("*.kicad_pcb")) + \
                sorted((stage / "kicad").rglob("*.kicad_sch"))
            for path in targets:
                payload_formats.append(upgrade_file(cli, path))
            check_version_consistency(payload_formats)
        else:
            for path in (sorted((stage / "kicad").rglob("*.kicad_pcb"))
                         + sorted((stage / "kicad").rglob("*.kicad_sch"))):
                fmt = file_format(path)
                payload_formats.append(
                    {"file": path.name, "before": fmt["version"],
                     "after": fmt["version"],
                     "generator_version": fmt["generator_version"],
                     "updated": False, "stdout": "(--no-upgrade)"})
            check_version_consistency(payload_formats, upgraded=False)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise

    # ---- materialize ------------------------------------------------------
    if foreign:                    # --keep-foreign: swap intake's files only
        merge_into(ws, stage, owned)
    else:
        if ws.exists():
            shutil.rmtree(ws)
        ws.parent.mkdir(parents=True, exist_ok=True)
        os.replace(stage, ws)

    pcb = ws / "kicad" / f"{project}.kicad_pcb"
    sch = ws / "kicad" / f"{project}.kicad_sch"
    pcb = pcb if pcb.is_file() else None
    sch = sch if sch.is_file() else None
    phase = board_phase(pcb)

    import state as statemod  # noqa: PLC0415
    st = statemod.State.init(ws, board, phase, force=True)
    st.add_decision(
        what=f"imported external KiCad project {spec['stem']!r} from "
             f"{spec['dir']}",
        why="intake.py: copy-in review workspace (source never modified)")

    if sch is None:
        findings.append(checklib.violation(
            "intake", "warning", None, None, None, [],
            "project has no schematic: ERC, netlist audit and DRC parity are "
            "unavailable", source=SOURCE, kind="no_schematic"))
    if pcb is None:
        findings.append(checklib.violation(
            "intake", "warning", None, None, None, [],
            "project has no board: DRC, verify, DFM and renders are "
            "unavailable", source=SOURCE, kind="no_board"))

    baseline: dict = {"gates": {}, "verify_checks": {}, "netlist_audit": None}
    renders: list[dict] = []
    deliverables: dict = {}

    # ---- netlist + audit --------------------------------------------------
    if sch is not None and not args.no_gates:
        import kc  # noqa: PLC0415
        import netlist_audit  # noqa: PLC0415
        netlist = ws / "kicad" / f"{project}.net"
        res = kc.export_netlist(cli, sch, netlist)
        if res.get("status") != "pass":
            findings.append(checklib.violation(
                "intake", "error", None, None, None, [],
                f"netlist export failed: {res.get('stderr_tail', '')[:200]}",
                source=SOURCE, kind="netlist_export_failed"))
        else:
            st.set_artifact("netlist", f"kicad/{project}.net")
            # netlist_audit needs a constraints file; an EMPTY one from a temp
            # dir keeps the netlist-intrinsic findings (dangling nets, unpaired
            # diff pairs, pins on no net) without planting a constraints.json
            # in kicad/ - which would turn verify's honest "skipped" into a
            # vacuous "pass".
            with tempfile.TemporaryDirectory(prefix="aiee_intake_") as td:
                stub = Path(td) / "constraints.json"
                stub.write_text("{}", encoding="utf-8")
                rel = "reports/netlist_audit.json"
                audit, _ = netlist_audit.run_audit(netlist, stub, None, None)
            _write_json(ws / rel, audit)
            baseline["netlist_audit"] = {
                "status": audit["status"], "counts": audit["counts"],
                "report": rel,
                "top": [{"severity": v["severity"], "kind": v.get("kind"),
                         "msg": v["msg"]} for v in audit["violations"][:10]]}

    # ---- baseline gates ---------------------------------------------------
    if not args.no_gates:
        for name in BASELINE_GATES:
            # a gate whose input the project does not have is SKIPPED, not an
            # intake failure (a board-only import has no ERC to run); the
            # no_schematic / missing-board findings already say so once
            if (name == "erc" and sch is None) or (name != "erc"
                                                   and pcb is None):
                baseline["gates"][name] = {
                    "status": "skipped",
                    "reason": "no schematic" if name == "erc" else "no board"}
                continue
            try:
                result, raw = run_baseline_gate(name, ws, sch, pcb)
            except Exception as exc:                       # noqa: BLE001
                msg = f"{type(exc).__name__}: {exc}"
                baseline["gates"][name] = {"status": "error", "error": msg}
                findings.append(checklib.violation(
                    "intake", "error", None, None, None, [],
                    f"baseline gate {name} could not run: {msg[:300]}",
                    source=SOURCE, kind="gate_error"))
                if name == "drc_routed" and pcb is not None:
                    try:                       # stale fills: plain DRC still
                        result, raw = run_baseline_gate("drc", ws,
                                                        sch, pcb)
                    except Exception as exc2:              # noqa: BLE001
                        baseline["gates"]["drc"] = {
                            "status": "error",
                            "error": f"{type(exc2).__name__}: {exc2}"}
                        continue
                    baseline["gates"]["drc"] = {
                        "status": result["status"],
                        "failing_count": result["failing_count"],
                        "counts": result["counts"],
                        "report": "reports/gate-drc.json"}
                    st.record_gate("drc", result, result.get("phase"))
                continue
            baseline["gates"][name] = {
                "status": result["status"],
                "failing_count": result["failing_count"],
                "counts": result["counts"],
                "report": f"reports/gate-{name}.json"}
            if name == "verify":
                baseline["verify_checks"] = {
                    n: c.get("status") for n, c in
                    (raw.get("checks") or {}).items()}
            st.record_gate(name, result, result.get("phase"))

    # ---- renders + schematic pdf -----------------------------------------
    if pcb is not None and not args.no_renders:
        import render as rendermod  # noqa: PLC0415
        views = rendermod.parse_views(args.views)
        rres = rendermod.render_views(pcb, views, ws / "reports",
                                      width=args.width, height=900,
                                      quality="high")
        renders = rres["outputs"]
        if rres["status"] != "pass":
            findings.append(checklib.violation(
                "intake", "warning", None, None, None, [],
                "one or more board renders failed", source=SOURCE,
                kind="render_failed"))
    if sch is not None and not args.no_renders:
        import kc  # noqa: PLC0415
        pdf = kc.export_sch_pdf(cli, sch, ws / "reports" / "schematic.pdf")
        if pdf.get("status") != "pass":
            findings.append(checklib.violation(
                "intake", "warning", None, None, None, [],
                "schematic PDF export failed", source=SOURCE,
                kind="sch_pdf_failed"))

    # ---- source integrity -------------------------------------------------
    modified = [p for p, sha in src_hashes.items()
                if not Path(p).is_file() or sha256_file(Path(p)) != sha]
    if modified:
        findings.append(checklib.violation(
            "intake", "error", None, None, None, [],
            f"SOURCE WAS MODIFIED during intake: {', '.join(modified[:5])}",
            source=SOURCE, kind="source_modified"))

    st.save()

    payload = checklib.report(
        SCRIPT, board, findings,
        imported=started,
        source={"dir": str(spec["dir"]).replace("\\", "/"),
                "project": spec["stem"], "files": len(copies),
                "has_sch": sch is not None, "has_pcb": pcb is not None,
                "verified_unmodified": not modified},
        workspace=str(ws).replace("\\", "/"),
        toolchain={"kicad_cli": str(cli) if cli else None,
                   "version": cli_version},
        phase=phase,
        formats=payload_formats,
        copied=[c["dest"] for c in copies],
        libs=libs,
        baseline=baseline,
        renders=renders,
        deliverables=deliverables,
    )
    payload["next_actions"] = next_actions(payload)

    # ---- deliverables -----------------------------------------------------
    # written BEFORE report_gen so the digest appears in the design document's
    # artifact index, and rewritten after so it carries the report's own
    # findings too (report_gen never reads it).
    digest_rel = "reports/intake-digest.md"
    (ws / digest_rel).parent.mkdir(parents=True, exist_ok=True)
    (ws / digest_rel).write_text(build_digest(payload), encoding="utf-8")
    deliverables["digest"] = digest_rel

    if not args.no_report:
        import report_gen  # noqa: PLC0415
        try:
            rg, rc = report_gen.run(str(ws))
            deliverables["design_doc"] = rg.get("pdf") or rg.get("tex")
            deliverables["design_doc_status"] = rg.get("status")
            if rc != 0:
                findings.append(checklib.violation(
                    "intake", "warning", None, None, None, [],
                    "design document is degraded: missing "
                    f"{rg.get('missing') or []}, warnings "
                    f"{len(rg.get('warnings') or [])} (see "
                    f"reports/design_doc/)", source=SOURCE,
                    kind="report_degraded"))
        except Exception as exc:                           # noqa: BLE001
            findings.append(checklib.violation(
                "intake", "warning", None, None, None, [],
                f"report_gen failed: {type(exc).__name__}: {exc}",
                source=SOURCE, kind="report_failed"))

    # recompute status/counts after the deliverable stage added findings
    payload["counts"] = checklib.summarize(findings)
    payload["violations"] = findings
    payload["deliverables"] = deliverables
    payload["status"] = ("violations"
                         if any(v["severity"] == "error" for v in findings)
                         else "pass")
    (ws / digest_rel).write_text(build_digest(payload), encoding="utf-8")
    _write_json(ws / "reports" / "intake.json", payload)
    st.set_artifact("intake_report", digest_rel)
    st.save()
    # what this run wrote, so the next --force knows what is intake's
    kept = set(foreign)
    _write_json(ws / MANIFEST_REL, {
        "note": "files intake wrote; --force replaces only these, "
                "state.json and reports/",
        "files": [r for r in workspace_files(ws)
                  if r not in kept and r != MANIFEST_REL]})
    return payload, args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    sys.exit(main())
