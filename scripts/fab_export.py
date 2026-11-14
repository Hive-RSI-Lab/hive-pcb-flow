#!/usr/bin/env python
"""fab_export.py - JLCPCB fabrication package: gerbers + drill + IPC-D-356 + pos.

The P9 export step (SPEC 6.4). Drives the S2 kc.py wrappers to produce the
fab deliverables JLCPCB expects and bundles the gerbers + drill into one zip
(JLC's upload unit). BOM/CPL are bom_cpl.py's job; DFM is dfm_check.py's.

JLC guidance replicated here:
  - Curated layer set (copper x N + silk + mask + paste + edge cuts). We do NOT
    pass --subtract-soldermask: silk-over-pad must remain VISIBLE in the gerber
    so dfm_check (the independent geometry path) can catch it BEFORE the fab
    silently clips it.
  - Drill: Excellon, millimetres (kicad-cli defaults pos+drill sanely once the
    kc.py wrappers force units; LEARNINGS [kicad-cli]).
  - IPC-D-356 netlist `<name>.ipc` (kicad-cli pcb export ipcd356) in the
    gerber dir and zip: the fab's flying-probe E-test runs against it. A net
    joined only inside a part (jumper_pad_groups) gets one name per bare-board
    island (`USB_DP`, `USB_DP~J1`), else the tester reads it as open (#962).
    The nets a net tie joins (net_tie_pad_groups: a Bridged solder jumper)
    get one name where the copper joins them, else it reads them as shorted.
  - Pos (CPL source): both sides, csv, mm. bom_cpl.py reformats it into JLC's
    CPL with rotation corrections.
  - Gerbers use KiCad's standard extensions/X2, which current JLC accepts. No
    Protel-extension toggle exists in kicad-cli (it always uses standard ones).

Output: a JSON manifest {files, sha256, gerber_zip, layer_count, ...} to stdout
or --out. Exit 0 success / 2 error (an export has no "violations" state).

CLI:
  fab_export.py --pcb board.kicad_pcb --out-dir fab/ [--name NAME]
                [--layers F.Cu,B.Cu,...] [--no-zip] [--out manifest.json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import zipfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import kc  # noqa: E402
import gerblib  # noqa: E402
from lib import env  # noqa: E402

# Non-copper fab layers, in JLC upload order. Copper layers are inserted from
# the board's actual stackup (F.Cu, In1.Cu ... B.Cu).
_NONCOPPER = [
    "F.Silkscreen", "B.Silkscreen",
    "F.Mask", "B.Mask",
    "F.Paste", "B.Paste",
    "Edge.Cuts",
]


def copper_layers(pcb: Path) -> list[str]:
    """Ordered copper layer names from the board's (layers ...) block
    (F.Cu, In1.Cu, ..., B.Cu). Pure text scan - no SWIG, no kicad-cli."""
    text = pcb.read_text(encoding="utf-8", errors="replace")
    # (layers (0 "F.Cu" signal) (1 "In1.Cu" ...) (31 "B.Cu" signal) ...)
    import re
    names: list[tuple[int, str]] = []
    m = re.search(r"\(layers\b", text)
    if m:
        # scan balanced region after (layers
        depth = 0
        i = m.start()
        for j in range(m.start(), len(text)):
            c = text[j]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    i = j
                    break
        block = text[m.start():i + 1]
        for lm in re.finditer(r'\((\d+)\s+"([^"]+\.Cu)"', block):
            names.append((int(lm.group(1)), lm.group(2)))
    # Physical stackup order (top->bottom), NOT the file's raw layer-id order:
    # F.Cu, In1.Cu, In2.Cu, ..., B.Cu. board_init/SWIG can number B.Cu below the
    # inner layers, so sorting by raw id mis-orders inners vs B.Cu (matters for
    # dfm_check's outer-vs-inner copper-weight rules).
    def _phys(name: str) -> tuple[int, int]:
        if name == "F.Cu":
            return (0, 0)
        if name == "B.Cu":
            return (2, 0)
        m = re.match(r"In(\d+)\.Cu", name)
        return (1, int(m.group(1)) if m else 0)
    ordered = [n for _, n in sorted(names, key=lambda t: _phys(t[1]))]
    return ordered or ["F.Cu", "B.Cu"]


def jlc_layer_list(pcb: Path) -> list[str]:
    return copper_layers(pcb) + _NONCOPPER


def board_stackup_entry(pcb: Path) -> tuple[str, dict | None]:
    """(material family, the stackups.yaml entry the board was built from).
    The entry is the available one of that family whose dielectric
    thicknesses match the board's stackup block; None when none does."""
    import yaml
    import fabmaterial
    fam = fabmaterial.board_family(pcb)
    diel = [d["thickness_mm"] or 0.0 for d in fabmaterial.board_dielectrics(pcb)]
    stacks = yaml.safe_load((SCRIPTS.parent / "reference" / "stackups.yaml")
                            .read_text(encoding="utf-8"))
    for name, st in (stacks.get("stackups") or {}).items():
        if st.get("available") is False \
                or st.get("material_family", fabmaterial.FR4) != fam:
            continue
        mine = [float(ly["thickness_mm"]) for ly in st["stack"]
                if ly["type"] != "copper"]
        if len(mine) == len(diel) and all(
                abs(a - b) < 1e-4 for a, b in zip(mine, diel)):
            return fam, {"name": name, **st}
    return fam, None


def write_order_notes(pcb: Path, out_dir: Path) -> Path | None:
    """fab/ORDER-NOTES.md for a board off FR-4: the material, thickness,
    finish and solved lines to select on JLC's quote page, so the board
    built is the board designed (SI-09). None on FR-4 (nothing to choose
    beyond the defaults order_submit already lists)."""
    import fabmaterial
    fam, entry = board_stackup_entry(pcb)
    if fam == fabmaterial.FR4 or fabmaterial.not_offered(fam):
        return None
    text = fabmaterial.order_notes(
        fam, entry, pcb.stem,
        impedance=(entry or {}).get("controlled_impedance"))
    if entry is None:
        text += ("\n> The board's stackup block matches no stackups.yaml "
                 "entry: check the thickness by hand.\n")
    path = out_dir / "ORDER-NOTES.md"
    path.write_text(text, encoding="utf-8")
    return path


def via_fill_facts(pcb: Path) -> dict:
    """What the board asks of the via process: vias with filling/capping set
    (board default or per via) and vias landing inside an SMD pad
    (via-in-pad, whose barrel drains paste unless filled). Reuses
    check_wicking's reader."""
    import sexpdata
    import check_wicking as cw
    root = sexpdata.loads(pcb.read_text(encoding="utf-8"))
    dflt = cw.board_defaults(root)
    lyr = cw._kid(root, "layers")
    cu = [str(cw._tok(n[1])) for n in (lyr[1:] if lyr is not None else [])
          if cw.geom._is_node(n) and len(n) > 1 and str(cw._tok(n[1])).endswith(".Cu")]
    cu = sorted(set(cu) | {"F.Cu", "B.Cu"},
                key=lambda n: (0 if n == "F.Cu" else 2 if n == "B.Cu" else 1,
                               int(n[2:-3]) if n.startswith("In") else 0))
    vias = cw.read_vias(root, dflt, cu)
    pads = [p for ps in cw.read_pads(root, dflt).values() for p in ps
            if p["kind"] == "smd"]
    in_pad = []
    for v in vias:
        pt = cw.Point(v["at"])
        for p in pads:
            if p["copper"].contains(pt):
                in_pad.append({"ref": p["ref"], "pad": p["number"],
                               "at": [round(v["at"][0], 3), round(v["at"][1], 3)],
                               "filled": bool(v["filled"] or v["capped"])})
                break
    return {"filled_vias": sum(1 for v in vias if v["filled"] or v["capped"]),
            "in_pad": in_pad}


def write_via_fill_note(pcb: Path, out_dir: Path) -> tuple[Path, dict] | None:
    """fab/VIA-FILL.md when the board has filled/capped or via-in-pad vias:
    the gerber job file carries no via-fill field, so the request would be
    lost at order time (#684). dfm_check requires this note in the package."""
    f = via_fill_facts(pcb)
    if not f["filled_vias"] and not f["in_pad"]:
        return None
    lines = [f"# Via process for {pcb.stem}", "",
             f"- Vias with filling/capping requested: {f['filled_vias']}",
             f"- Vias inside SMD pads (via-in-pad): {len(f['in_pad'])}", "",
             "Order option: select **via fill / plugged vias (epoxy filled, "
             "capped)** on the quote page. Standard tented vias drain paste "
             "from these joints.", ""]
    if f["in_pad"]:
        lines += ["| Ref | Pad | x (mm) | y (mm) | Filled in board |", "|---|---|---|---|---|"]
        lines += [f"| {e['ref']} | {e['pad']} | {e['at'][0]} | {e['at'][1]} | "
                  f"{'yes' if e['filled'] else 'NO'} |" for e in f["in_pad"]]
    path = out_dir / "VIA-FILL.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path, {"filled_vias": f["filled_vias"], "in_pad": len(f["in_pad"])}


def aux_origin(pcb: Path) -> tuple[float, float] | None:
    """The board's aux axis (drill/place) origin, when it isn't (0, 0)."""
    m = re.search(r"\(aux_axis_origin ([-\d.]+) ([-\d.]+)\)", pcb.read_text(encoding="utf-8"))
    if m and (float(m.group(1)), float(m.group(2))) != (0.0, 0.0):
        return float(m.group(1)), float(m.group(2))
    return None


def name_jumper_islands(pcb: Path, ipc_file: Path, gerber_dir: Path,
                        bg=None) -> list[dict]:
    """#962: a net a part joins only inside itself (the footprint's
    `jumper_pad_groups`, e.g. a flow-through USBLC6's pins 1-6 and 3-4) is
    two copper islands on the bare board, but kicad-cli names both alike, so
    the fab's E-test reads a good board as open. Give each island its own
    D-356 name, from the gerbers' own connectivity. -> the renames."""
    import geom
    import ipcd356
    pcb_text = pcb.read_text(encoding="utf-8", errors="replace")
    jumpers = ipcd356.jumper_groups(pcb_text)
    if not jumpers:
        return []
    # bytes, so a CRLF file stays CRLF
    text = ipc_file.read_bytes().decode("utf-8", errors="replace")
    d = ipcd356.parse(text)
    bg = bg if bg is not None else geom.load_board(pcb)
    ipcd356.resolve(d, bg)
    res = ipcd356.compare(gerblib.open_fab(gerber_dir), d,
                          ipcd356.net_tie_classes(pcb_text, bg), jumpers)
    new, renames = ipcd356.split_jumper_islands(text, res["jumper_splits"])
    if renames:
        # beside the file, then one rename: a failed write ships no half file
        part = ipc_file.with_name(ipc_file.name + ".part")
        part.write_text(new, encoding="utf-8", newline="")
        os.replace(part, ipc_file)
    return renames


def name_net_ties(pcb: Path, ipc_file: Path, gerber_dir: Path,
                  bg=None) -> list[dict]:
    """A net tie (the footprint's `net_tie_pad_groups`: a Bridged solder
    jumper, a NetTie) joins two nets with its own copper on purpose, but
    kicad-cli names each net apart, so the fab's E-test reads the good board
    as a short between them. Give the nets of each tie one D-356 name where
    the gerbers' copper joins them (ipcd356.merge_net_ties); a tie whose
    copper is cut keeps its names. Runs after name_jumper_islands, on the
    file as it left. -> the renames."""
    import geom
    import ipcd356
    pcb_text = pcb.read_text(encoding="utf-8", errors="replace")
    if "net_tie_pad_groups" not in pcb_text:
        return []
    bg = bg if bg is not None else geom.load_board(pcb)
    ties = ipcd356.net_tie_classes(pcb_text, bg)
    if not ties:
        return []
    # bytes, so a CRLF file stays CRLF
    text = ipc_file.read_bytes().decode("utf-8", errors="replace")
    d = ipcd356.parse(text)
    ipcd356.resolve(d, bg)
    res = ipcd356.compare(gerblib.open_fab(gerber_dir), d, ties,
                          ipcd356.jumper_groups(pcb_text))
    new, renames = ipcd356.merge_net_ties(text, res["tie_joins"])
    if renames:
        part = ipc_file.with_name(ipc_file.name + ".part")
        part.write_text(new, encoding="utf-8", newline="")
        os.replace(part, ipc_file)
    return renames


def export_ipc_absolute(cli: Path, pcb: Path, ipc_file: Path,
                        gerber_dir: Path | None = None, bg=None) -> dict:
    """The netlist, then (with `gerber_dir`, the gerbers it ships beside)
    each jumper island named apart (name_jumper_islands) and the nets of
    each net tie given one name (name_net_ties). If a naming fails, the
    file stays as it was before it (a later netlist compare reports the
    jumper open or the tie short) and `jumper_rename_error` or
    `tie_rename_error` says why."""
    res = _export_ipc_absolute(cli, pcb, ipc_file)
    if gerber_dir is not None and res.get("status") == "pass":
        try:
            res["jumper_renames"] = name_jumper_islands(pcb, ipc_file,
                                                        gerber_dir, bg)
        except Exception as exc:  # noqa: BLE001 (the export itself stands)
            res["jumper_rename_error"] = f"{type(exc).__name__}: {exc}"
        try:
            res["tie_renames"] = name_net_ties(pcb, ipc_file, gerber_dir, bg)
        except Exception as exc:  # noqa: BLE001 (the export itself stands)
            res["tie_rename_error"] = f"{type(exc).__name__}: {exc}"
    return res


def _export_ipc_absolute(cli: Path, pcb: Path, ipc_file: Path) -> dict:
    """IPC-D-356 in the gerbers' coordinates. kicad-cli writes it from the
    board's aux (drill/place) origin when the board has one, while gerbers
    and drill are absolute: an Altium import keeps Altium's origin there, and
    every netlist point then lands on no copper (each one an open at the
    fab's tester). Such a board's netlist comes from a copy with the aux
    origin at (0, 0)."""
    if not aux_origin(pcb):
        return kc.export_ipcd356(cli, pcb, ipc_file)
    import shutil
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        for ext in (".kicad_pro", ".kicad_dru"):
            if pcb.with_suffix(ext).exists():
                shutil.copy(pcb.with_suffix(ext), Path(td) / (pcb.stem + ext))
        copy = Path(td) / pcb.name
        copy.write_text(re.sub(r"\(aux_axis_origin [-\d.]+ [-\d.]+\)", "(aux_axis_origin 0 0)",
                               pcb.read_text(encoding="utf-8"), count=1), encoding="utf-8")
        return kc.export_ipcd356(cli, copy, ipc_file)


def _hits(path: Path) -> list[str]:
    return [ln.strip() for ln in path.read_text(encoding="utf-8", errors="replace")
            .splitlines() if re.match(r"X-?[\d.]+Y-?[\d.]+", ln.strip())]


def tidy_backdrill_files(drill_dir: Path) -> list[Path]:
    """KiCad writes each back-drill span twice, `<name>_Backdrills_Drill_a_b.drl`
    and `<name>-<layer>-<side>-backdrill.drl`, with the same hits. A fab given
    both can drill twice or ask, so one of them ships."""
    files = sorted(p for p in drill_dir.glob("*.drl") if gerblib.is_backdrill(p))
    named = {tuple(_hits(p)) for p in files if p.name.endswith("-backdrill.drl")}
    keep = []
    for p in files:
        if not p.name.endswith("-backdrill.drl") and tuple(_hits(p)) in named:
            p.unlink()
        else:
            keep.append(p)
    return keep


def write_backdrill_note(pcb: Path, files: list[Path], drill_dir: Path,
                         name: str) -> Path:
    """`<name>-backdrill.md` in the zip: what each pass is, in the terms a fab
    asks for (start side, must-cut layer, must-not-cut layer, depth, tool).
    Markdown, since gerblib reads any .txt as a drill file."""
    import sexpdata
    import geom
    import viastub
    bg = geom.load_board(pcb)
    st = viastub.Stack(bg.stackup)
    drills = viastub.read_backdrills(sexpdata.loads(pcb.read_text(encoding="utf-8")))
    rows = {}
    for v in bg.vias_of():
        here = drills.get((round(v.at[0], 4), round(v.at[1], 4)))
        if not here:
            continue
        used = viastub.used_layers(bg, v) or [v.layers[0]]
        for row in viastub.stubs(st, v, used, here):
            if "backdrill" not in row:
                continue
            d = row["backdrill"]
            key = (d["start"], d["end"], d["size"], row["exit"], row["depth_mm"],
                   row["barrel_left_mm"], row["length_mm"])
            rows[key] = rows.get(key, 0) + 1
    lines = [f"# {name}: back-drill (controlled-depth drilling)", "",
             "Request back-drilling on the order; it is a priced option at the "
             "fab, and not every fab offers it.",
             f"Board thickness (copper + dielectric): {st.thickness:.3f} mm.",
             "Depth is from the start layer's outer face to the middle of the "
             "end layer.", "",
             "| Drill from | Must cut to | Must NOT cut | Tool mm | Holes | "
             "Depth mm | Barrel left mm | Stub past signal mm |",
             "|---|---|---|---|---|---|---|---|"]
    for (start, end, size, exit_, depth, left, stub), n in sorted(rows.items()):
        lines.append(f"| {start} | {end} | {exit_} | {size:.3f} | {n} | {depth:.3f} "
                     f"| {left:.3f} | {stub:.3f} |")
    lines += ["", "Drill files: " + ", ".join(p.name for p in files), ""]
    path = drill_dir / f"{name}-backdrill.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# Gates whose recorded `pcb` digest must still match the board being exported.
# `place`/`drc` are earlier-stage gates that a routed board legitimately
# outgrows (drc_routed supersedes drc).
RELEASE_GATES = ("drc_routed", "verify", "dfm", "verify_release", "dfm_release")


class StaleBoardError(RuntimeError):
    """The board changed after the gates passed (#684)."""


def stale_gates(pcb: Path) -> dict:
    """{board_digest, workspace, checked, stale} for `pcb`. Compares the pcb
    digest each passed release gate recorded in the workspace's state.json
    with the board on disk. No workspace above the board (a corpus board, a
    scratch export) or no state.json gate record = nothing to compare."""
    import statelib
    ws = statelib.find_workspace(pcb)
    out = {"board_digest": statelib.hash_artifact(pcb, statelib.norm_for_path(pcb)),
           "workspace": str(ws) if ws else None, "checked": [], "stale": {}}
    if ws is None:
        return out
    try:
        data = json.loads((ws / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    for g in RELEASE_GATES:
        entry = (data.get("gates") or {}).get(g) or {}
        last = entry.get("last") or {}
        rec = (last.get("inputs") or {}).get("pcb")
        if entry.get("status") != "pass" or not rec:
            continue
        out["checked"].append(g)
        if rec != out["board_digest"]:
            out["stale"][g] = rec
    return out


def run(pcb: Path, out_dir: Path, name: str | None = None,
        layers: list[str] | None = None, make_zip: bool = True,
        allow_stale: bool = False) -> dict:
    if not pcb.exists():
        raise FileNotFoundError(f"board not found: {pcb}")
    fresh = stale_gates(pcb)
    if fresh["stale"] and not allow_stale:
        raise StaleBoardError(
            f"{pcb.name} changed after gate(s) {', '.join(sorted(fresh['stale']))} "
            f"passed (recorded pcb digest differs from the board on disk): "
            f"re-run those gates, or pass --allow-stale to export anyway")
    cli = kc.resolve_cli()
    name = name or pcb.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    gerber_dir = out_dir / "gerbers"
    drill_dir = out_dir / "gerbers"          # JLC wants drill alongside gerbers
    layer_names = layers or jlc_layer_list(pcb)
    coppers = copper_layers(pcb)

    # a back-drill the board no longer has must not ship from the last export
    if gerber_dir.is_dir():
        for old in gerber_dir.glob("*.drl"):
            if gerblib.is_backdrill(old):
                old.unlink()
        (gerber_dir / f"{name}-backdrill.md").unlink(missing_ok=True)
    g = kc.export_gerbers(cli, pcb, gerber_dir, layers=",".join(layer_names))
    if g.get("status") != "pass":
        raise RuntimeError(f"gerber export failed: {g.get('stderr') or g}")
    d = kc.export_drill(cli, pcb, drill_dir, fmt="excellon")
    if d.get("status") != "pass":
        raise RuntimeError(f"drill export failed: {d.get('stderr') or d}")
    backdrill_files = tidy_backdrill_files(drill_dir)
    backdrill_note = (write_backdrill_note(pcb, backdrill_files, drill_dir, name)
                      if backdrill_files else None)
    # The bare-board tester's netlist, in the zip beside the gerbers: JLC,
    # PCBWay and Eurocircuits E-test against it when one is supplied, else
    # against a netlist they extract from the same gerbers (so an export
    # defect would test good). dfm_check compares the gerbers with it.
    ipc_file = gerber_dir / f"{name}.ipc"
    n = export_ipc_absolute(cli, pcb, ipc_file, gerber_dir)
    if n.get("status") != "pass":
        raise RuntimeError(f"ipcd356 export failed: {n.get('stderr_tail') or n}")
    if n.get("jumper_rename_error"):
        print(f"fab_export: jumper islands not named apart in {ipc_file.name} "
              f"({n['jumper_rename_error']}); dfm_check will report the "
              "E-test open", file=sys.stderr)
    if n.get("tie_rename_error"):
        print(f"fab_export: net ties not given one name in {ipc_file.name} "
              f"({n['tie_rename_error']}); dfm_check will report the "
              "E-test short", file=sys.stderr)
    pos_file = out_dir / f"{name}-pos.csv"
    p = kc.export_pos(cli, pcb, pos_file, side="both", fmt="csv", units="mm")
    if p.get("status") != "pass":
        raise RuntimeError(f"pos export failed: {p.get('stderr') or p}")

    # Files that belong in the fab zip: everything in gerber_dir (gerbers +
    # job + drill) - NOT the pos file (CPL is a separate JLC upload).
    notes = write_order_notes(pcb, out_dir)
    via_fill = write_via_fill_note(pcb, out_dir)

    fab_files = sorted(f for f in gerber_dir.iterdir() if f.is_file())
    files_meta = [{"name": f.name, "sha256": sha256(f), "bytes": f.stat().st_size}
                  for f in fab_files]

    zip_path = None
    zip_sha = None
    if make_zip:
        zip_path = out_dir / f"{name}_gerbers.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in fab_files:
                zf.write(f, arcname=f.name)
        zip_sha = sha256(zip_path)

    return {
        "script": "fab_export",
        "status": "pass",
        "board": pcb.name,
        "name": name,
        "layer_count": len(coppers),
        "copper_layers": coppers,
        "layers_exported": layer_names,
        "out_dir": str(out_dir),
        "gerber_dir": str(gerber_dir),
        "pos_file": str(pos_file),
        "ipc_netlist": str(ipc_file),
        **({"ipc_jumper_renames": n["jumper_renames"]}
           if n.get("jumper_renames") else {}),
        **({"ipc_jumper_rename_error": n["jumper_rename_error"]}
           if n.get("jumper_rename_error") else {}),
        **({"ipc_tie_renames": n["tie_renames"]}
           if n.get("tie_renames") else {}),
        **({"ipc_tie_rename_error": n["tie_rename_error"]}
           if n.get("tie_rename_error") else {}),
        "files": files_meta,
        "gerber_zip": str(zip_path) if zip_path else None,
        "gerber_zip_sha256": zip_sha,
        "board_digest": fresh["board_digest"],
        "gates_checked": fresh["checked"],
        **({"stale_gates": sorted(fresh["stale"])} if fresh["stale"] else {}),
        **({"order_notes": str(notes)} if notes else {}),
        **({"via_fill": {"note": via_fill[0].name, **via_fill[1]}}
           if via_fill else {}),
        **({"backdrill": {"files": [p.name for p in backdrill_files],
                          "note": backdrill_note.name}}
           if backdrill_files else {}),
    }


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcb", required=True, help="input .kicad_pcb")
    ap.add_argument("--out-dir", required=True, help="fab output directory")
    ap.add_argument("--name", help="basename for pos/zip (default: board stem)")
    ap.add_argument("--layers", help="override the JLC layer set (comma list)")
    ap.add_argument("--no-zip", action="store_true", help="skip the gerber zip")
    ap.add_argument("--allow-stale", action="store_true",
                    help="export even if a passed gate recorded a different "
                         "board digest (the manifest lists stale_gates)")
    ap.add_argument("--out", help="write JSON manifest here instead of stdout")
    args = ap.parse_args(argv)

    try:
        rep = run(Path(args.pcb), Path(args.out_dir), name=args.name,
                  layers=args.layers.split(",") if args.layers else None,
                  make_zip=not args.no_zip, allow_stale=args.allow_stale)
    except Exception as exc:  # noqa: BLE001 (SPEC: any error -> exit 2)
        err = {"script": "fab_export", "status": "error",
               "error": f"{type(exc).__name__}: {exc}"}
        text = json.dumps(err, indent=1)
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
        else:
            print(text)
        return 2

    text = json.dumps(rep, indent=1)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
