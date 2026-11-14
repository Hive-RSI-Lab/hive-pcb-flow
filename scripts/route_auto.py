"""route_auto - autoroute the remainder via Freerouting (S11, SPEC P7.3).

Flow (all on a STAGED copy in the work dir; the real board is swapped only on
success): refill zones (a stale/unfilled pour exports wrong) -> Specctra DSN
export via the SWIG worker (wx-suppressed; plane layers marked LT_POWER so the
DSN carries "(type power)") -> Freerouting CLI over an escalation ladder
(the DSN first carries each net's .kicad_dru width/clearance floor as its own
class, routelib.dsn_apply_net_rules, the board's hole rules the export
drops, _dsn_hole_rules, and each pre-routed wire chain as one path,
routelib.dsn_merge_wires, as split wires overflow Freerouting's stack;
routelib.DEFAULT_LADDER; deterministic flags, per-rung process timeout, score
logging) -> import the best rung's SES (Freerouting's pin neckdowns in a
courtyard neck zone set to the neck width: it lays them a hair under the
pad's width, _snap_neckdowns, #998) -> refill (imported tracks stale every
pour they cross - S11-verified: 33 clearance violations before refill, 0
after) -> final `kicad-cli pcb drc --schematic-parity --all-track-errors`.

Existing copper is protected by construction: the DSN exporter emits existing
tracks/vias as guide wires which Freerouting echoes back "(type protect)" -
pre-routed critical nets (route_critical.py) survive without an explicit lock.
Zones export as "(plane NET ...)"; Freerouting connects pads to planes itself
(including dropping stitch vias to reach an inner/back plane).

After the ladder, KRT's mop-up (never on a net a filled zone carries, see
_plane_carried; its under-width tracks on power nets are
stripped, not kept; a power net with a courtyard neck rule goes to KRT at
the neck width and keeps it only inside its neck zone, #937), then route_finish's maze pass: it rips the tracks and
vias DRC names in a short/clearance/crossing error and routes them again,
then whatever DRC still reports unconnected - grid A* at the board's own
rules, pour pieces joined by a via, walled-in pads freed by ripping the
fewest signal nets in the way - kept only if the board ends better
(--no-maze-finish skips it).

Freerouting's own success signal is NEVER trusted (LEARNINGS [freerouting]):
completion comes from the log's unrouted counts and the gate is kicad-cli DRC.

Contract:
  route_auto.py --pcb B.kicad_pcb [--exclude-classes Power,...]
      [--power-layers auto|"In1.Cu,In2.Cu"|none] [--max-rungs N]
      [--timeout-s S] [--work-dir DIR] [--out-report r.json] [--tries N]
      [--probe [--probe-passes N]] [--no-krt-finish] [--no-maze-finish]
  Freerouting runs single-threaded and sequential, so one board file routes
  the same way every time; a rebuilt board (fresh uuids, another DSN order)
  may not. --tries N also routes N-1 shuffled-order DSNs at the best rung,
  imports each on its own copy, keeps the one DRC grades best and reports
  the spread (facts tries, graded, variance).
  exit 0 = 100% routed AND DRC clean; 1 = incomplete or violations
  (board still updated - the fix loop owns the remainder); 2 = error
  (original board untouched).

--probe: capped-effort routability probe (S10 place_anneal feedback): stage,
export, ONE short Freerouting run; report {completion} and touch nothing.
route_probe() is the importable form.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import checklib  # noqa: E402
import env  # noqa: E402
import geom  # noqa: E402
import hdilib  # noqa: E402
import kc  # noqa: E402
import necklib  # noqa: E402  (#937 neck zones)
import route_edit  # noqa: E402  (finish pass: rip sliver vias by uuid)
import routelib  # noqa: E402
import zrules  # noqa: E402  (#489 per-layer impedance widths)
from checklib import CheckError  # noqa: E402


def _tools():
    cli = env.find_kicad_cli()
    bp = env.find_kicad_python(cli) if cli else None
    java = env.find_java()
    jar = env.find_freerouting_jar()
    missing = [n for n, v in (("kicad-cli", cli), ("bundled python", bp),
                              ("java 25", java), ("freerouting jar", jar))
               if not v]
    if missing:
        raise CheckError("missing tools: " + ", ".join(missing)
                         + " (check_env.py --full has remediation)")
    return cli, bp, java[0], jar


def _stage_board(pcb: Path, work: Path) -> Path:
    """Copy the board + every same-stem sidecar (pro/prl/dru/sch) to work."""
    work.mkdir(parents=True, exist_ok=True)
    staged = work / pcb.name
    for src in pcb.parent.glob(pcb.stem + ".*"):
        if src.is_file() and not src.name.endswith(".lck"):
            shutil.copy2(src, work / src.name)
    if not staged.is_file():
        raise CheckError(f"staging failed: {staged}")
    return staged


def _dsn_net_rules(staged: Path, dsn: Path) -> list[str]:
    """Write the staged board's per-net .kicad_dru track_width/clearance
    floors into the exported DSN; returns the nets given their own class."""
    import route_critical as rc
    dru = staged.with_suffix(".kicad_dru")
    text, moved = routelib.dsn_apply_net_rules(
        dsn.read_text(encoding="utf-8"),
        rc.dru_net_floors(dru, "track_width"),
        rc.dru_net_floors(dru, "clearance"))
    if moved:
        dsn.write_text(text, encoding="utf-8")
    return moved


def _dsn_barriers(staged: Path, dsn: Path) -> list[dict]:
    """Write the staged board's barrier rules (rules_gen's aiee_iso_*, #595)
    into the DSN as pair clearances, so Freerouting stays out of the gap; run
    last of the class edits, since it splits the classes they make."""
    ddru = staged.with_suffix(".kicad_dru")
    if not ddru.is_file():
        return []
    text, pairs = routelib.dsn_apply_barriers(
        dsn.read_text(encoding="utf-8"),
        routelib.dru_barriers(ddru.read_text(encoding="utf-8")))
    if pairs:
        dsn.write_text(text, encoding="utf-8")
    return pairs


def _dsn_layer_widths(staged: Path, dsn: Path) -> list[str]:
    """Write rules_gen's per-layer impedance widths (stripline layers, #489:
    zrules) into the DSN as class layer_rules, so Freerouting draws an inner
    run at the width DRC grades it against; returns the nets given them."""
    geo = zrules.board_layer_geometry(staged)
    text, done = routelib.dsn_apply_layer_widths(
        dsn.read_text(encoding="utf-8"),
        {net: zrules.layer_widths(geo, [net]) for net in geo})
    if done:
        dsn.write_text(text, encoding="utf-8")
    return done


def _dsn_merge_wires(dsn: Path) -> int:
    """Join the exported DSN's split pre-routed wires (Freerouting 2.2.4
    overflows its stack on them); returns the wires folded together."""
    text, merged = routelib.dsn_merge_wires(dsn.read_text(encoding="utf-8"))
    if merged:
        dsn.write_text(text, encoding="utf-8")
    return merged


def _pro_rules(staged: Path) -> dict:
    """board.design_settings.rules of the staged board's .kicad_pro."""
    try:
        pro = json.loads(staged.with_suffix(".kicad_pro")
                         .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return dict(((pro.get("board") or {}).get("design_settings") or {})
                .get("rules") or {})


# --offer-spans (#477): give Freerouting the HDI build's spans. Off until a run
# shows Freerouting uses them and SES import restores their type.
OFFER_SPANS = False


def _dsn_hole_rules(staged: Path, dsn: Path, bg: geom.BoardGeom) -> dict:
    """Carry the board's hole rules, which KiCad's DSN export drops, into
    the DSN: via padstacks grown so copper clearance keeps different-net
    vias min_hole_to_hole apart (routelib.dsn_hole_floor; undone in the SES
    by _ses_restore before import), and a keepout over every NPTH hole
    grown by max(min_hole_clearance, min_copper_edge_clearance)
    (routelib.dsn_npth_keepouts), and a keepout band along every board
    edge as wide as min_copper_edge_clearance (routelib.dsn_edge_keepouts).
    Returns the facts, with "grown" (the padstack map _ses_restore
    needs)."""
    rules = _pro_rules(staged)
    h2h = float(rules.get("min_hole_to_hole") or 0.25)
    margin = max(float(rules.get("min_hole_clearance") or 0.25),
                 float(rules.get("min_copper_edge_clearance") or 0.0))
    text = dsn.read_text(encoding="utf-8")
    build, entry = hdilib.board_build(bg)
    added: list[str] = []
    if build:
        caps = hdilib.load_hdi_caps()
    if build and OFFER_SPANS:
        spans = hdilib.spans_of(entry, bg.copper_layers, caps=caps)
        text, added = routelib.dsn_add_spans(
            text, [(s.i, s.j, s.size, s.drill) for s in spans])
    text, grown = routelib.dsn_hole_floor(
        text, h2h, float(caps["hole_to_hole_diff_net_mm"]) if build else None)
    holes = [{"at": list(p.center), "drill": list(p.drill), "angle": p.angle}
             for p in bg.npth]
    text, kept_out = routelib.dsn_npth_keepouts(text, holes, margin)
    edge_m = float(rules.get("min_copper_edge_clearance") or 0.0)
    rings = []
    for face in bg.outline_faces:
        rings.append(list(face.exterior.coords))
        rings += [list(r.coords) for r in face.interiors]
    text, edge_out = routelib.dsn_edge_keepouts(
        text, routelib.edge_segments(rings), edge_m)
    dsn.write_text(text, encoding="utf-8")
    return {"hole_to_hole_mm": h2h, "grown": grown,
            "via_padstacks_grown": {k: list(v) for k, v in grown.items()},
            "npth_keepouts": kept_out, "npth_margin_mm": margin,
            "edge_keepouts": edge_out, "edge_margin_mm": edge_m,
            **({"hdi_build": build, "via_spans_offered": added} if build else {})}


def _ses_restore(ses: Path, grown: dict) -> None:
    """Put the real via diameters back into a session before import."""
    if grown and ses.is_file():
        text, n = routelib.ses_restore_vias(
            ses.read_text(encoding="utf-8"), grown)
        if n:
            ses.write_text(text, encoding="utf-8")


def _auto_power_layers(bg: geom.BoardGeom) -> list[str]:
    """Inner copper layers that carry a zone fill and no tracks -> planes."""
    out = []
    for layer in bg.copper_layers[1:-1]:
        has_fill = any(z.fills.get(layer) for z in bg.zones_of())
        has_tracks = bool(bg.tracks_of(layer=layer))
        if has_fill and not has_tracks:
            out.append(layer)
    return out


def _routable_nets(bg: geom.BoardGeom) -> set[str]:
    """Nets with >= 2 pads (the autorouter's actual workload)."""
    counts: dict[str, int] = {}
    for p in bg.pads_of():
        if p.net:
            counts[p.net] = counts.get(p.net, 0) + 1
    return {n for n, c in counts.items() if c >= 2}


def _plane_carried(bg: geom.BoardGeom) -> set[str]:
    """Nets a filled zone carries. The KRT finish leaves them alone: the
    pour is their trunk, and the maze finish (a via per pour piece),
    stitch_vias and plane_repair join their pads. KRT joined such pads
    with traces across another net's
    thermal pour (bb-ldo: 25.6 mm of GND on F.Cu through the +3V3 pour,
    -2.6 % of the effective area)."""
    return {z.net for z in bg.zones_of() if z.net and z.filled}


def _finish_targets(unrouted: list[str], rip: list[tuple[str, str]],
                    skip: set[str]) -> tuple[list[str], list, list[str]]:
    """What the KRT finish works on: (nets, vias to rip, plane nets left
    for stitch_vias). A skipped net's vias stay: nothing would re-route
    what ripping them cut."""
    rip = [(u, n) for u, n in rip if n not in skip]
    every = set(unrouted) | {n for _, n in rip}
    left = sorted(n for n in unrouted if n in skip)
    return sorted(every - skip), rip, left


def _ses_import_check(fr_facts: dict | None, drc: dict) -> dict | None:
    """Nets the board shows unrouted right after the SES import although
    Freerouting reported none unrouted: the import dropped them (ultra-
    front: fr_completion 1.0, three connections missing)."""
    if not fr_facts or fr_facts.get("unrouted") != 0:
        return None
    lost = _drc_unrouted(drc)
    if not lost:
        return None
    return {"dropped_nets": lost,
            "note": "Freerouting reported every connection routed; these "
                    "were unrouted after the SES import"}


def _drc_unrouted(report: dict) -> list[str]:
    nets = {v.get("net") for v in report["violations"]
            if v.get("source") == "unconnected"}
    return sorted(n for n in nets if n)


_fresh_work_dir = routelib.fresh_work_dir
_swap_in = routelib.swap_in


def _placement_adjust_request(bg: geom.BoardGeom, unrouted: list[str],
                              rungs_tried: int) -> dict:
    """The P7 -> P6 backward edge (SPEC P7.5): a machine-readable placement
    micro-adjust request for the nets Freerouting could not complete. The
    orchestrator (S13) hands this to the placement agent, which may spread
    the named clusters / free the region and re-run P6 stage 3 - the ONLY
    sanctioned backward edge in the pipeline."""
    refs: set[str] = set()
    xs: list[float] = []
    ys: list[float] = []
    for net in unrouted:
        for p in bg.pads_of(net=net):
            refs.add(p.ref)
            xs.append(p.center[0])
            ys.append(p.center[1])
    region = [round(min(xs), 3), round(min(ys), 3),
              round(max(xs), 3), round(max(ys), 3)] if xs else None
    return {
        "request": "placement_adjust",
        "nets": unrouted,
        "refs": sorted(refs),
        "region": region,
        "reason": f"unrouted after {rungs_tried} freerouting rungs",
        "suggestions": [
            "spread the named clusters apart / off the congested region",
            "clear a ~3 mm escape ring around the anchor IC",
            "consider more board area or more layers if congestion persists",
        ],
    }


def _krt_finish(cli: Path, staged: Path, work: Path, drc: dict, *,
                power_layers: list[str] | None = None,
                skip_nets: set[str] = frozenset(),
                refill: bool, parity: bool, timeout_s: int) -> dict | None:
    """Deterministic post-Freerouting mop-up via the vendored
    KiCadRoutingTools (S11 acceptance: FR leaves 0.5 mm-pitch LQFP GND pins
    unconnected - no fanout capability - and drops the odd via a few 10s of
    microns inside the hole-clearance floor). Steps, all on the staged board
    with a backup for full revert:
      1. remove vias implicated in hole_clearance/clearance/hole_to_hole
         DRC ERRORS
         (route_edit remove by uuid from the report's raw items);
      2. run KRT route.py per affected net (grid 0.05, board floors,
         --no-fix-drc-settings) - KRT only routes what is disconnected;
      3. refill + re-DRC; keep only if strictly better (fewer errors +
         no new error types), else restore the backup.
    Nets in skip_nets (plane-carried, see _plane_carried) are left out.
    Returns facts (kept, nets, ripped_vias, drc) or None when KRT is absent
    or there is nothing to fix."""
    unrouted = _drc_unrouted(drc)
    via_re = re.compile(r"^Via \[([^\]]+)\]")
    rip = []            # (uuid, net)
    for v in drc["violations"]:
        if v.get("source") != "drc" or v.get("severity") != "error":
            continue
        if v.get("check") not in ("hole_clearance", "clearance",
                                  "hole_to_hole"):
            continue
        for item in v.get("items", []):
            m = via_re.match(item.get("msg", ""))
            if m and item.get("uuid"):
                rip.append((item["uuid"], m.group(1)))
    nets, rip, left = _finish_targets(unrouted, rip, set(skip_nets))
    if not nets:
        return {"kept": False, "nets": [], "plane_nets_left": left} \
            if left else None
    krt = env.find_krt()
    if krt is None:
        return {"kept": False, "nets": nets, "plane_nets_left": left,
                "note": "KRT not vendored; finish pass skipped"}
    import route_critical as rc

    backup = work / ("prefinish_" + staged.name)
    shutil.copy2(staged, backup)
    dru_widths = rc.dru_net_floors(staged.with_suffix(".kicad_dru"),
                                   "track_width")
    necks = necklib.read_necks(staged)
    before_uuids = {t.uuid for t in
                    geom.BoardGeom.from_file(staged).tracks_of()}
    narrow: list = []
    try:
        if rip:
            route_edit.apply_ops(staged, [
                {"op": "remove", "uuid": u} for u, _ in rip])
        floors = rc.grading_floors(work / (staged.stem + ".kicad_pro"))
        fab = rc.write_fab_overrides(work, floors)
        # one batched invocation - KRT only routes what is disconnected, and
        # per-net runs pay the full startup/parse cost ~40x on a fallback
        out = work / "krt_finish.kicad_pcb"
        # a net with a .kicad_dru width floor (rules_gen's aiee_pwr_width_*)
        # is routed at it: KRT knows only netclass widths, and on lite-main
        # (hifi-receiver-2) it closed VBUS/PVDD taps at 0.2-0.3 mm against a
        # 0.37 mm rule, three track_width errors the gate failed on
        wide = {n: w for n, w in dru_widths.items()
                if n in nets and w > floors["track_width"] + 1e-6}
        # a necked power net (#937) goes to KRT at its neck width, so it can
        # leave a fine-pitch pad; _narrow_new_tracks then keeps the neck
        # only inside its zone and strips the rest, which the maze finish
        # lays at the full width
        wide = krt_widths(wide, necks)
        extra = ["--nets", *nets]
        if power_layers:
            # KRT defaults to every copper layer: without this it runs signals
            # across a plane the caller named (bldc-drive: 128 mm on In1 GND)
            all_layers = geom.BoardGeom.from_file(staged).copper_layers
            extra += ["--layers", *[ly for ly in all_layers
                                    if ly not in power_layers]]
        if wide:
            extra += ["--power-nets", *sorted(wide), "--power-nets-widths",
                      *[f"{wide[n]:g}" for n in sorted(wide)],
                      "--no-power-tap-neckdown"]
        rc.run_krt(krt, "route.py", staged, out, extra,
                   floors, fab, 0.05, timeout_s)
        os.replace(out, staged)
        # KRT lays every net at its own default width: a power net comes out
        # under its .kicad_dru width rule (50-199 track_width errors on a
        # dense board, and the whole pass discarded). Strip what it laid
        # below the floor; the maze finish routes those at the rule width.
        narrow = _narrow_new_tracks(staged, dru_widths, before_uuids,
                                    necks)
        if narrow:
            route_edit.apply_ops(staged, [{"op": "remove", "uuid": u}
                                          for u in narrow])
        # KRT can leave sub-grid crumbs (e.g. a 0.017 mm dangling stub) that
        # KiCad flags track_dangling but sit below route_cleanup's touch
        # tolerance. Removal is connectivity-safe: neighbouring round caps
        # overlap far beyond the crumb length.
        crumb_ops = [
            {"op": "remove", "uuid": m.group(1)}
            for m in rc._SEG_BLOCK_RE.finditer(
                staged.read_text(encoding="utf-8"))
            if _seg_len(m.group(0)) < 0.05]
        if crumb_ops:
            route_edit.apply_ops(staged, crumb_ops)
        if refill:
            kc.run_drc(cli, staged, refill=True, save_board=True)
        after = kc.run_drc(cli, staged, parity=parity, all_track_errors=True)
        b_err = {k: v for k, v in _err_counts(drc).items()}
        a_err = _err_counts(after)
        # KRT knows no barrier (net-wide clearances only): a finish that puts
        # copper into one isn't better, however many nets it connects
        better = (sum(a_err.values()) < sum(b_err.values())
                  and not (set(a_err) - set(b_err))
                  and _barrier_errors(after) <= _barrier_errors(drc))
        if not better:
            shutil.copy2(backup, staged)
            return {"kept": False, "nets": nets, "plane_nets_left": left,
                    "ripped_vias": len(rip), "narrow_stripped": len(narrow),
                    "errors_before": b_err, "errors_after": a_err}
        return {"kept": True, "nets": nets, "plane_nets_left": left,
                "ripped_vias": len(rip),
                "narrow_stripped": len(narrow),
                "errors_before": b_err, "errors_after": a_err, "drc": after}
    except CheckError as exc:
        shutil.copy2(backup, staged)
        return {"kept": False, "nets": nets, "plane_nets_left": left,
                "error": str(exc)[:300]}


def krt_widths(wide: dict[str, float], necks: dict) -> dict[str, float]:
    """The width KRT routes each wide net at: its neck width where it has a
    neck zone (rules_gen's aiee_pwr_neck, #937; the narrowest, so it leaves
    every necked pad), else its .kicad_dru floor."""
    out = dict(wide)
    for n in wide:
        ws = [w for zs in (necks.get(n) or {}).values() for _, w in zs]
        if ws:
            out[n] = min(out[n], min(ws))
    return out


def _narrow_new_tracks(staged: Path, widths: dict[str, float],
                       before: set, necks: dict | None = None) -> list[str]:
    """uuids of tracks not on the board before that sit under their net's
    width rule. A track wholly in its net's neck zone at no less than the
    neck width keeps (#937): DRC's courtyard-held rule passes it."""
    out = []
    for t in geom.BoardGeom.from_file(staged).tracks_of():
        if t.uuid in before or not t.uuid:
            continue
        if t.width >= widths.get(t.net, 0.0) - 1e-4:
            continue
        nw = necklib.neck_width(necks or {}, t.net, t.layer, t.shape)
        if nw is None or t.width < nw - 1e-4:
            out.append(t.uuid)
    return out


NECKDOWN_TOL_MM = 0.005   # #998: Freerouting's pin neckdown runs this far
#                           under the pad's width (0.2498 for a 0.25 pad)


def _snap_neckdowns(staged: Path, before: set) -> list[str]:
    """Widen Freerouting's pin neckdowns to their neck width (#998).

    Freerouting takes one width per net, but it necks a trace down where
    it meets a pin narrower than the trace (its own neckdown), so a
    fine-pitch power pad does get out. It lays that stub a hair under the
    pin's width, though, and a courtyard neck rule (necklib, rules_gen's
    aiee_pwr_neck) sized to the pad then fails DRC by 0.2 um. A new track
    wholly in its net's neck zone and under the neck width by no more than
    NECKDOWN_TOL_MM is set to the neck width. Returns the uuids it widened.
    """
    necks = necklib.read_necks(staged)
    if not necks:
        return []
    import route_critical as rc
    want = {}
    for t in geom.BoardGeom.from_file(staged).tracks_of():
        if t.uuid in before or not t.uuid or t.net not in necks:
            continue
        nw = necklib.neck_width(necks, t.net, t.layer, t.shape)
        if nw is not None and nw - NECKDOWN_TOL_MM <= t.width < nw - 1e-6:
            want[t.uuid] = nw
    if not want:
        return []
    # A text edit, not route_edit's set_width: that skips a change under
    # its 1 um position tolerance, and this one is 0.2 um.
    done = []

    def widen(m):
        nw = want.get(m.group(1))
        if nw is None:
            return m.group(0)
        done.append(m.group(1))
        return rc._SEG_FIELD_RES["width"].sub(f"(width {nw:g})", m.group(0),
                                              count=1)
    text = staged.read_text()
    staged.write_text(rc._SEG_BLOCK_RE.sub(widen, text))
    return done


def _seg_len(block: str) -> float:
    import route_critical as rc
    try:
        sx, sy = map(float, rc._SEG_FIELD_RES["start"].search(block).groups())
        ex, ey = map(float, rc._SEG_FIELD_RES["end"].search(block).groups())
    except AttributeError:
        return float("inf")
    return ((ex - sx) ** 2 + (ey - sy) ** 2) ** 0.5


def _variance(graded: list[dict]) -> dict:
    """[min, max] of each grade over the candidates --tries imported."""
    return {k: [min(g[k] for g in graded), max(g[k] for g in graded)]
            for k in ("drc_unrouted_nets", "drc_errors", "drc_total")}


def _barrier_errors(report: dict) -> int:
    return sum("aiee_iso_" in str(v.get("description") or v.get("msg") or "")
               for v in report.get("violations", []))


def _err_counts(report: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in report["violations"]:
        if v.get("severity") == "error":
            key = f"{v.get('source')}:{v.get('check')}"
            out[key] = out.get(key, 0) + 1
    return out


def _oom_error(facts: dict, what: str, work: Path) -> CheckError:
    heap = facts.get("heap_mb")
    cap = f"its {heap} MB heap" if heap else "Java's default heap"
    return CheckError(
        f"Freerouting ran out of {cap} on {what}; give the job more memory "
        "(a bigger heavy budget) or set HWDE_FR_HEAP_MB, see routelib "
        f"FR_HEAP_SHARE; logs in {work} (board untouched)")


def route_probe(pcb: Path, *, passes: int = 4, timeout_s: int = 180,
                work_dir: Path | None = None) -> dict:
    """Capped-effort routability probe. Never modifies the board.

    Returns {"completion": float|None, "unrouted": int|None, ...}. For
    place_anneal wiring see place_anneal.anneal(route_probe=...) - S10.
    """
    pcb = Path(pcb).resolve()
    cli, bp, java, jar = _tools()
    # .resolve(): Freerouting runs with cwd=dsn.parent and KRT with
    # cwd=<plugins dir>, so relative work paths break silently
    # (LEARNINGS 1318; same fix as route_critical).
    work = _fresh_work_dir(Path(work_dir).resolve() if work_dir
                           else pcb.parent / "route_probe")
    staged = _stage_board(pcb, work)
    bg = geom.BoardGeom.from_file(staged)
    if bg.zones_of() and any(not z.filled for z in bg.zones_of()):
        kc.run_drc(cli, staged, refill=True, save_board=True)
        bg = geom.BoardGeom.from_file(staged)  # power layers need fresh fills
    dsn, ses = work / "probe.dsn", work / "probe.ses"
    routelib.run_worker(bp, {
        "verb": "export_dsn", "board": str(staged), "dsn": str(dsn),
        "layer_types": {ly: "power" for ly in _auto_power_layers(bg)}}, work)
    _dsn_net_rules(staged, dsn)
    _dsn_layer_widths(staged, dsn)
    _dsn_hole_rules(staged, dsn, bg)
    merged = _dsn_merge_wires(dsn)
    _dsn_barriers(staged, dsn)
    facts = routelib.run_freerouting(
        java, jar, dsn, ses, rung={"mp": passes}, timeout=timeout_s,
        log_file=work / "probe.log")
    if facts.get("oom"):
        raise _oom_error(facts, "the probe", work)
    facts["completion"] = routelib.completion_fraction(facts)
    facts["dsn_wires_merged"] = merged
    return facts


def run(argv: list[str] | None = None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcb", required=True)
    ap.add_argument("--exclude-classes", default=None,
                    help="net classes Freerouting must not route (-inc)")
    ap.add_argument("--power-layers", default="auto",
                    help='"auto" | none | comma list, e.g. "In1.Cu,In2.Cu"')
    ap.add_argument("--max-rungs", type=int, default=len(routelib.DEFAULT_LADDER))
    ap.add_argument("--timeout-s", type=int, default=3600,
                    help="per-rung Freerouting HARD wall-clock cap")
    ap.add_argument("--stall-s", type=int, default=900,
                    help="kill a rung with no output/.ses growth this long "
                         "(a 4-layer pass takes ~5 min)")
    ap.add_argument("--work-dir", default=None)
    ap.add_argument("--out-report", default=None)
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--probe-passes", type=int, default=4)
    ap.add_argument("--tries", type=int, default=1,
                    help="also run the best rung on N-1 DSNs with shuffled "
                         "component/net order, import every result on its "
                         "own copy and keep the one DRC grades best (facts "
                         "tries, graded, variance); 1 = the ladder only")
    ap.add_argument("--offer-spans", action="store_true",
                    help="HDI build: offer Freerouting each via span (#477)")
    ap.add_argument("--no-krt-finish", action="store_true",
                    help="skip the KRT mop-up pass after the ladder")
    ap.add_argument("--no-maze-finish", action="store_true",
                    help="skip route_finish's maze pass after KRT")
    ap.add_argument("--finish-plane-nets", action="store_true",
                    help="let the KRT finish route nets a filled zone "
                         "carries (default: left to the maze finish's "
                         "vias and to stitch_vias)")
    args = ap.parse_args(argv)
    global OFFER_SPANS
    OFFER_SPANS = args.offer_spans

    pcb = Path(args.pcb).resolve()
    if not pcb.is_file():
        raise CheckError(f"board not found: {pcb}")

    if args.probe:
        facts = route_probe(pcb, passes=args.probe_passes,
                            timeout_s=min(args.timeout_s, 600),
                            work_dir=Path(args.work_dir) if args.work_dir else None)
        payload = {"script": "route_auto", "status": "pass", "board": str(pcb),
                   "probe": True,
                   "facts": {k: facts.get(k) for k in
                             ("completion", "unrouted", "started_unrouted",
                              "final_score", "session_completed", "timed_out")}}
        return payload, args.out_report

    cli, bp, java, jar = _tools()
    # .resolve(): see route_probe - relative work paths break KRT/Freerouting
    work = _fresh_work_dir(Path(args.work_dir).resolve() if args.work_dir
                           else pcb.parent / "route")
    staged = _stage_board(pcb, work)

    # 1. fresh fills before export (unfilled/stale pours export wrong),
    #    THEN parse - power-layer auto-detection and routable-net counting
    #    must see the refilled state (review finding).
    has_zones = bool(geom.BoardGeom.from_file(staged).zones_of())
    if has_zones:
        kc.run_drc(cli, staged, refill=True, save_board=True)
    bg = geom.BoardGeom.from_file(staged)
    routable = _routable_nets(bg)
    tracks_before = len(bg.tracks_of())

    # 2. DSN export (plane layers marked power)
    if args.power_layers == "auto":
        power_layers = _auto_power_layers(bg)
    elif args.power_layers in ("none", ""):
        power_layers = []
    else:
        power_layers = [s.strip() for s in args.power_layers.split(",") if s.strip()]
    dsn = work / (pcb.stem + ".dsn")
    routelib.run_worker(bp, {
        "verb": "export_dsn", "board": str(staged), "dsn": str(dsn),
        "layer_types": {ly: "power" for ly in power_layers}}, work)
    dsn_net_rules = _dsn_net_rules(staged, dsn)
    dsn_layer_widths = _dsn_layer_widths(staged, dsn)
    hole_rules = _dsn_hole_rules(staged, dsn, bg)
    grown = hole_rules.pop("grown")
    dsn_wires_merged = _dsn_merge_wires(dsn)
    dsn_barriers = _dsn_barriers(staged, dsn)

    # 3. Freerouting escalation ladder
    ladder = routelib.DEFAULT_LADDER[:max(1, args.max_rungs)]
    rungs = []
    candidates = []  # (sort key, rung index, ses_path, facts)
    for i, rung in enumerate(ladder, 1):
        rung = dict(rung)
        if args.exclude_classes:
            rung["inc"] = args.exclude_classes
        ses = work / f"rung{i}.ses"
        facts = routelib.run_freerouting(
            java, jar, dsn, ses, rung=rung, timeout=args.timeout_s,
            stall_s=args.stall_s,
            log_file=work / f"rung{i}.log")
        entry = {"rung": i, "options": rung,
                 "unrouted": facts.get("unrouted"),
                 "started_unrouted": facts.get("started_unrouted"),
                 "final_score": facts.get("final_score"),
                 "passes": len(facts.get("passes", [])),
                 "timed_out": facts.get("timed_out"),
                 "kill_reason": facts.get("kill_reason"),
                 "ses_written": facts.get("ses_written")}
        rungs.append(entry)
        if facts.get("oom"):
            # Out of its heap cap (#897): a later rung asks more of the same
            # DSN, so it would run out too. Keep what earlier rungs routed.
            entry["oom"] = True
            if not candidates:
                raise _oom_error(facts, f"rung {i}", work)
            break
        if facts.get("ses_written") and facts.get("unrouted") is not None:
            candidates.append(((facts["unrouted"],
                                -(facts.get("final_score") or 0.0)), i, ses,
                               facts))
            if facts["unrouted"] == 0:
                break
        if facts.get("timed_out") and not facts.get("passes"):
            # The DSN wedges Freerouting's reader (S11-verified: KRT guide
            # wires can drive PolylineTrace.combine into infinite recursion
            # before pass 1). Every rung parses the same DSN - skip the rest.
            entry["wedged"] = True
            break

    sch = work / (pcb.stem + ".kicad_sch")
    imported = None
    best_i = None
    best_facts: dict = {}
    fr_ok = False
    # 3b. --tries: the same rung on DSNs whose component and net order is
    # shuffled (Freerouting's sequential router is deterministic for one
    # DSN, but its result turns on that order: lite-main routed 98-100 %
    # with 0-6 leftovers as its board was rebuilt)
    tries = []
    if args.tries > 1 and candidates:
        base_i = sorted(candidates)[0][1]
        base_rung = dict(ladder[base_i - 1])
        if args.exclude_classes:
            base_rung["inc"] = args.exclude_classes
        text = dsn.read_text(encoding="utf-8")
        for t in range(2, args.tries + 1):
            tdsn, tses = work / f"try{t}.dsn", work / f"try{t}.ses"
            tdsn.write_text(routelib.dsn_shuffle(text, seed=t),
                            encoding="utf-8")
            facts = routelib.run_freerouting(
                java, jar, tdsn, tses, rung=base_rung, timeout=args.timeout_s,
                stall_s=args.stall_s, log_file=work / f"try{t}.log")
            tries.append({"try": t, "seed": t, "rung": base_i,
                          "unrouted": facts.get("unrouted"),
                          "final_score": facts.get("final_score"),
                          "timed_out": facts.get("timed_out")})
            if facts.get("oom"):
                tries[-1]["oom"] = True
                break
            if facts.get("ses_written") and facts.get("unrouted") is not None:
                candidates.append(((facts["unrouted"],
                                    -(facts.get("final_score") or 0.0)),
                                   f"try{t}", tses, facts))

    sch = work / (pcb.stem + ".kicad_sch")
    imported = None
    best_i = None
    best_facts: dict = {}
    fr_ok = False
    dedup_facts = {"removed": 0}
    drc = None
    # 4. SES import - best rung first, next-best on a truncated/corrupt SES;
    # with --tries every candidate is imported on its own copy and graded by
    # DRC (unrouted nets, then errors), and the best board is kept.
    pristine = work / ("preimport_" + staged.name)
    shutil.copy2(staged, pristine)
    uuids_before = {t.uuid for t in bg.tracks_of() if t.uuid}
    graded = []
    for key, i, ses, facts in sorted(candidates, key=lambda c: c[0]):
        _ses_restore(ses, grown)
        trial = staged
        if args.tries > 1:
            # its own dir, same file name: DRC and refill read the board's
            # .kicad_pro/.kicad_dru beside it by stem (a renamed copy was
            # graded at KiCad's defaults, hundreds of false clearance errors)
            cdir = work / f"cand_{i}"
            cdir.mkdir(exist_ok=True)
            for side in work.glob(staged.stem + ".*"):
                if side.is_file() and side != staged:
                    shutil.copy2(side, cdir / side.name)
            trial = cdir / staged.name
            shutil.copy2(pristine, trial)
        try:
            imp = routelib.run_worker(bp, {
                "verb": "import_ses", "board": str(trial), "ses": str(ses),
                "out": str(trial)}, work, timeout=600)
        except CheckError:
            for e in rungs:
                if e["rung"] == i:
                    e["ses_import_failed"] = True
            for e in tries:
                if f"try{e['try']}" == i:
                    e["ses_import_failed"] = True
            continue
        # 4a. KiCad's import calls every non-through via "buried"; an HDI
        # board's vias get their span's real type back (#477).
        hdi_build, hdi_entry = hdilib.board_build(bg)
        if hdi_build:
            spans = hdilib.spans_of(hdi_entry, bg.copper_layers,
                                    caps=hdilib.load_hdi_caps())
            routelib.run_worker(bp, {
                "verb": "retype_vias", "board": str(trial), "out": str(trial),
                "spans": [[s.top, s.bot, s.kind] for s in spans]},
                work, timeout=300)
        # 4b. dedup: the SES echoes pre-session guide-wire copper (critical-
        # net trunks) back through ImportSpecctraSES as EXACT same-net
        # duplicates - invisible to DRC/gerbers (S14: run (a) shipped 45
        # echoed segments).
        dd = routelib.run_worker(bp, {
            "verb": "dedup_copper", "board": str(trial),
            "out": str(trial)}, work, timeout=300)
        # 4c. Freerouting's pin neckdowns a hair under a neck rule (#998)
        dd["neckdowns_snapped"] = len(_snap_neckdowns(trial, uuids_before))
        if args.tries <= 1:
            imported, dedup_facts = imp, dd
            best_i, best_facts, fr_ok = i, facts, True
            break
        # 5. (per candidate) refill + DRC
        if has_zones:
            kc.run_drc(cli, trial, refill=True, save_board=True)
        d = kc.run_drc(cli, trial, parity=sch.is_file(), all_track_errors=True)
        grade = (len(_drc_unrouted(d)), sum(_err_counts(d).values()),
                 d["counts"]["total"])
        graded.append({"candidate": i, "fr_unrouted": facts.get("unrouted"),
                       "drc_unrouted_nets": grade[0], "drc_errors": grade[1],
                       "drc_total": grade[2]})
        if drc is None or grade < best_grade:
            best_grade, drc = grade, d
            imported, dedup_facts = imp, dd
            best_i, best_facts, fr_ok, best_trial = i, facts, True, trial
    if fr_ok and args.tries > 1:
        shutil.copy2(best_trial, staged)
    if not fr_ok:
        shutil.copy2(pristine, staged)
    if not fr_ok and (args.no_krt_finish or env.find_krt() is None):
        raise CheckError("no Freerouting rung produced an importable session "
                         f"and the KRT fallback is unavailable; see {work} "
                         "logs (board untouched)")

    # 5. refill (post-SES fills are stale) + DRC of the current state
    if drc is None:
        if has_zones:
            kc.run_drc(cli, staged, refill=True, save_board=True)
        drc = kc.run_drc(cli, staged, parity=sch.is_file(),
                         all_track_errors=True)

    ses_check = _ses_import_check(best_facts if fr_ok else None, drc)
    plane_skip = set() if args.finish_plane_nets else _plane_carried(bg)

    # 5b. KRT pass: with a Freerouting result this is the mop-up (fine-pitch
    # pads FR cannot fan out, sliver vias inside the hole-clearance floor);
    # with NO usable Freerouting result it is the FALLBACK autorouter for
    # the whole remainder. Kept only if the DRC strictly improves.
    finish_facts = None
    if not args.no_krt_finish:
        finish_facts = _krt_finish(cli, staged, work, drc,
                                   power_layers=power_layers,
                                   skip_nets=plane_skip,
                                   refill=has_zones,
                                   parity=sch.is_file(),
                                   timeout_s=min(args.timeout_s, 600))
        if finish_facts and finish_facts.get("kept"):
            drc = finish_facts.pop("drc")
    if not fr_ok and not (finish_facts and finish_facts.get("kept")):
        raise CheckError("Freerouting produced nothing usable and the KRT "
                         f"fallback did not improve the board; see {work} "
                         "(board untouched)")

    # 5c. maze finish: what the ladder and KRT left unconnected (pads boxed
    # in by neighbouring tracks, pour pieces with no via), routed one
    # connection at a time at the board's rules; kept only if it helps.
    maze_facts = None
    if not args.no_maze_finish and (_drc_unrouted(drc) or _err_counts(drc)):
        import route_finish
        maze_facts = route_finish.finish_staged(
            cli, staged, drc, parity=sch.is_file(), refill=has_zones,
            rip_errors=True,
            layers=(",".join(ly for ly in bg.copper_layers
                             if ly not in power_layers)
                    if power_layers else "auto"))
        if maze_facts.get("kept"):
            drc = maze_facts.pop("drc")
        else:
            maze_facts.pop("drc", None)

    # 6. sanity + swap the routed board in
    bg2 = geom.BoardGeom.from_file(staged)
    tracks_after = len(bg2.tracks_of())
    if fr_ok and tracks_after <= tracks_before \
            and best_facts.get("started_unrouted"):
        raise CheckError("SES import added no tracks (board untouched); "
                         f"see {work}")
    _swap_in(staged, pcb)

    unrouted_nets = _drc_unrouted(drc)
    completion = (1.0 - len(unrouted_nets) / len(routable)) if routable else 1.0
    fully_routed = not unrouted_nets
    clean = drc["counts"]["total"] == 0
    status = "pass" if (fully_routed and clean) else "violations"
    adjust_request = None
    if unrouted_nets:
        adjust_request = _placement_adjust_request(bg2, unrouted_nets,
                                                   len(rungs))
    payload = {
        "script": "route_auto", "status": status, "board": str(pcb),
        "counts": drc["counts"], "violations": drc["violations"],
        "facts": {
            "completion": round(completion, 4),
            "routable_nets": len(routable),
            "unrouted_nets": unrouted_nets,
            "tracks_before": tracks_before, "tracks_after": tracks_after,
            "ses_items_added": (imported["tracks_after"]
                                - imported["tracks_before"])
            if imported else 0,
            "best_rung": best_i, "rungs": rungs,
            "tries": tries, "graded": graded,
            "variance": _variance(graded) if graded else None,
            "fr_completion": routelib.completion_fraction(best_facts)
            if fr_ok else None,
            "power_layers": power_layers,
            "dsn_net_rules": dsn_net_rules,
            "dsn_layer_widths": dsn_layer_widths,
            "dsn_barriers": dsn_barriers,
            "dsn_hole_rules": hole_rules,
            "dsn_wires_merged": dsn_wires_merged,
            "krt_finish": finish_facts,
            "ses_import_check": ses_check,
            "maze_finish": maze_facts,
            "ses_echo_dups_removed": dedup_facts.get("removed", 0),
            "neckdowns_snapped": dedup_facts.get("neckdowns_snapped", 0),
            "work_dir": str(work),
        },
    }
    if adjust_request:
        payload["facts"]["placement_adjust_request"] = adjust_request
    return payload, args.out_report


def main(argv: list[str] | None = None) -> int:
    return checklib.cli_wrap("route_auto", lambda: run(argv))


if __name__ == "__main__":
    sys.exit(main())
