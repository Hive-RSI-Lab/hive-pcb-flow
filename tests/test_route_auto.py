"""S11a tests: routing spine (routelib, route_edit, route_auto).

Pure tests (no toolchain, unmarked): Freerouting log parsing / completion
math / command builder, route_edit op-schema validation, route_auto helpers
against the committed golden boards.

`smoke` tests drive the live toolchain end-to-end: route_edit op roundtrip
(add / idempotent re-apply / remove / rollback) and the full route_auto flow
(placed-unrouted blinky2 + B.Cu GND pour -> DSN -> Freerouting -> SES ->
refill -> DRC), plus the capped-effort probe (S10 feedback hook).
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
GOLDEN = REPO / "tests" / "golden"

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))
import env  # noqa: E402
import geom  # noqa: E402
import route_auto  # noqa: E402
import route_edit  # noqa: E402
import routelib  # noqa: E402
from checklib import CheckError  # noqa: E402

FR_LOG_COMPLETE = """
2026-07-23 INFO  Freerouting v2.2.4 (build-date: 2026-05-13)
2026-07-23 INFO  [X] Auto-router pass #1 on board 'aaa' was completed in 1.51 seconds with the score of 891.34 (9 unrouted), using 1.02 CPU seconds
2026-07-23 INFO  [X] Auto-router pass #2 on board 'bbb' was completed in 0.64 seconds with the score of 977.53 (2 unrouted), using 1.98 CPU seconds
2026-07-23 INFO  [X] Auto-router pass #3 on board 'ccc' was completed in 0.23 seconds with the score of 996.29, using 1.98 CPU seconds
2026-07-23 INFO  [X] Auto-router session completed: started with 43 unrouted nets, completed in 2.79 seconds, final score: 996.29, using 1.98 total CPU seconds
"""

FR_LOG_PARTIAL = """
INFO  Auto-router pass #1 on board 'aaa' was completed in 9 seconds with the score of 100.00 (30 unrouted), using 9 CPU seconds
INFO  Auto-router pass #2 on board 'bbb' was completed in 9 seconds with the score of 120.00 (7 unrouted), using 9 CPU seconds
INFO  Auto-router session completed: started with 40 unrouted nets, completed in 140 seconds, final score: 120.00 (7 unrouted), using 100 total CPU seconds
"""


# ---------------------------------------------------------------- pure: parse

def test_parse_fr_log_complete_run():
    facts = routelib.parse_fr_log(FR_LOG_COMPLETE)
    assert facts["session_completed"] is True
    assert facts["started_unrouted"] == 43
    assert facts["final_score"] == pytest.approx(996.29)
    # last pass line has NO "(N unrouted)" parenthetical -> 0 left
    assert facts["unrouted"] == 0
    assert [p["unrouted"] for p in facts["passes"]] == [9, 2, 0]


def test_parse_fr_log_partial_run_prefers_session_line():
    facts = routelib.parse_fr_log(FR_LOG_PARTIAL)
    assert facts["unrouted"] == 7
    assert facts["started_unrouted"] == 40


def test_parse_fr_log_empty_is_unknown():
    facts = routelib.parse_fr_log("JVM crashed before doing anything")
    assert facts["unrouted"] is None
    assert facts["session_completed"] is False
    assert facts["passes"] == []


def test_completion_fraction():
    cf = routelib.completion_fraction
    assert cf({"started_unrouted": 43, "unrouted": 0,
               "session_completed": True}) == 1.0
    assert cf({"started_unrouted": 42, "unrouted": 1,
               "session_completed": True}) == pytest.approx(1 - 1 / 42)
    assert cf({"started_unrouted": 0, "unrouted": None,
               "session_completed": True}) == 1.0
    assert cf({"started_unrouted": None, "unrouted": None,
               "session_completed": False}) is None
    assert cf({"started_unrouted": 40, "unrouted": None,
               "session_completed": False}) is None


def test_build_fr_cmd_flags():
    cmd = routelib.build_fr_cmd(Path("java"), Path("fr.jar"),
                                Path("in.dsn"), Path("out.ses"),
                                {"mp": 60, "oit": 0.05, "us": "global",
                                 "inc": "Power"})
    s = " ".join(cmd)
    # determinism/safety trio + no phoning home + no debug log litter
    assert "--gui.enabled=false" in s
    assert "-mt 1" in s and "-is sequential" in s and "-da" in s
    assert "--logging.file.enabled=false" in s
    assert "-mp 60" in s and "-oit 0.05" in s and "-us global" in s
    assert "-inc Power" in s
    # design in / session out come last
    assert cmd[-4:] == ["-de", "in.dsn", "-do", "out.ses"]


def test_default_ladder_escalates():
    mps = [r["mp"] for r in routelib.DEFAULT_LADDER]
    assert mps == sorted(mps) and len(set(mps)) == len(mps)


# ---------------------------------------------------------------- pure: ops

def _ops(ops):
    return {"version": 1, "ops": ops}


def test_validate_ops_accepts_all_kinds():
    ops = route_edit.validate_ops(_ops([
        {"op": "add_track", "start": [1, 2], "end": [3, 4], "width": 0.25,
         "layer": "F.Cu", "net": "GND"},
        {"op": "add_via", "at": [5, 6], "size": 0.6, "drill": 0.3,
         "net": "GND"},
        {"op": "remove", "uuid": "aaaa-bbbb"},
    ]))
    assert len(ops) == 3


@pytest.mark.parametrize("doc", [
    {"ops": [{"op": "remove", "uuid": "x"}]},                      # no version
    _ops([]),                                                       # empty
    _ops([{"op": "teleport"}]),                                     # unknown op
    _ops([{"op": "add_via", "at": [1, 2], "size": 0.6,
           "drill": 0.3}]),                                         # missing net
    _ops([{"op": "remove", "uuid": "x", "extra": 1}]),              # unknown key
    _ops([{"op": "add_track", "start": [1], "end": [3, 4],
           "width": 0.25, "layer": "F.Cu", "net": "G"}]),           # bad point
    _ops([{"op": "add_via", "at": [1, 2], "size": 0.3, "drill": 0.6,
           "net": "G"}]),                                           # drill>=size
    _ops([{"op": "add_track", "start": [1, 2], "end": [3, 4],
           "width": -1, "layer": "F.Cu", "net": "G"}]),             # neg width
    _ops([{"op": "remove", "uuid": ""}]),                           # empty uuid
])
def test_validate_ops_rejects(doc):
    with pytest.raises(CheckError):
        route_edit.validate_ops(doc)


# ------------------------------------------------------- pure: golden helpers

def test_auto_power_layers_on_golden_usbbuck4():
    bg = geom.BoardGeom.from_file(GOLDEN / "usbbuck4" / "usbbuck4.kicad_pcb")
    assert route_auto._auto_power_layers(bg) == ["In1.Cu", "In2.Cu"]


def test_krt_finish_leaves_plane_carried_nets():
    # bb-ldo: KRT joined GND's SMD pads with F.Cu traces through the +3V3
    # thermal pour; a net a filled zone carries is left to the vias
    bg = geom.BoardGeom.from_file(GOLDEN / "usbbuck4" / "usbbuck4.kicad_pcb")
    planes = route_auto._plane_carried(bg)
    assert "GND" in planes and "/MCO" not in planes
    nets, rip, left = route_auto._finish_targets(
        ["GND", "/MCO"], [("u1", "GND"), ("u2", "/SWDIO")], planes)
    assert nets == ["/MCO", "/SWDIO"]
    assert rip == [("u2", "/SWDIO")]
    assert left == ["GND"]
    nets, rip, _ = route_auto._finish_targets(["GND"], [("u1", "GND")], set())
    assert nets == ["GND"] and rip == [("u1", "GND")]


def test_ses_import_check_names_what_the_import_dropped():
    drc = {"violations": [
        {"source": "unconnected", "net": "GND"},
        {"source": "unconnected", "net": "+3V3"},
        {"source": "drc", "net": "X", "check": "clearance"}]}
    got = route_auto._ses_import_check({"unrouted": 0}, drc)
    assert got["dropped_nets"] == ["+3V3", "GND"]
    # Freerouting said some were left: not a drop
    assert route_auto._ses_import_check({"unrouted": 2}, drc) is None
    assert route_auto._ses_import_check(None, drc) is None
    assert route_auto._ses_import_check({"unrouted": 0},
                                        {"violations": []}) is None


def test_routable_nets_and_adjust_request_on_golden():
    bg = geom.BoardGeom.from_file(GOLDEN / "usbbuck4" / "usbbuck4.kicad_pcb")
    nets = route_auto._routable_nets(bg)
    assert "/MCO" in nets and "GND" in nets
    req = route_auto._placement_adjust_request(bg, ["/MCO"], 3)
    assert req["request"] == "placement_adjust"
    assert req["refs"] == ["J3", "U1"]
    x1, y1, x2, y2 = req["region"]
    assert x1 < x2 and y1 < y2
    assert "3 freerouting rungs" in req["reason"]


# ---------------------------------------------------------------- smoke setup

@pytest.fixture(scope="session")
def cli() -> Path:
    c = env.find_kicad_cli()
    if c is None:
        pytest.skip("kicad-cli not installed")
    return c


@pytest.fixture(scope="session")
def bundled_python(cli) -> Path:
    bp = env.find_kicad_python(cli)
    if bp is None:
        pytest.skip("KiCad bundled python not found")
    return bp


@pytest.fixture(scope="session")
def fr_tools() -> tuple[Path, Path]:
    java = env.find_java()
    jar = env.find_freerouting_jar()
    if java is None or jar is None:
        pytest.skip("freerouting toolchain not vendored")
    return java[0], jar


@pytest.fixture(scope="session")
def seeded_poured_blinky(cli, bundled_python, tmp_path_factory) -> Path:
    """blinky2 netlist -> board_init(2 layer) -> place_seed --apply ->
    B.Cu GND zone (route_swig add_zones) -> refill. The S11 route input."""
    import board_init
    import kc
    import place_seed

    d = tmp_path_factory.mktemp("routeinit")
    rc = board_init.main([
        "--netlist", str(REPO / "tests" / "s7_regen" / "blinky2" / "kicad"
                         / "blinky2.net"),
        "--name", "blinky2r", "--out", str(d / "kicad"), "--layers", "2",
        "--schematic", str(GOLDEN / "blinky2" / "blinky2.kicad_sch")])
    assert rc == 0
    pcb = d / "kicad" / "blinky2r.kicad_pcb"
    for name in ("constraints.json", "decoupling.json"):
        shutil.copy2(GOLDEN / "blinky2" / name, pcb.parent / name)
    payload, _ = place_seed.run([
        "--pcb", str(pcb), "--ops-out", str(d / "seed_ops.json"), "--apply"])
    assert payload["status"] == "pass"

    bg = geom.BoardGeom.from_file(pcb)
    minx, miny, maxx, maxy = bg.outline.bounds
    stage = d / "zonestage"
    stage.mkdir()
    routelib.run_worker(bundled_python, {
        "verb": "add_zones", "board": str(pcb), "out": str(pcb),
        "zones": [{"net": "GND", "layer": "B.Cu",
                   "rect": [minx + 1, miny + 1, maxx - 1, maxy - 1]}]}, stage)
    import kc as _kc
    _kc.run_drc(cli, pcb, refill=True, save_board=True)
    bg = geom.BoardGeom.from_file(pcb)
    assert any(z.filled for z in bg.zones_of(net="GND"))
    return pcb


def _copy_board(src: Path, dst_dir: Path) -> Path:
    dst_dir.mkdir(parents=True, exist_ok=True)
    for f in src.parent.glob(src.stem + ".*"):
        if f.is_file() and not f.name.endswith(".lck"):
            shutil.copy2(f, dst_dir / f.name)
    return dst_dir / src.name


# ---------------------------------------------------------------- smoke: edit

@pytest.mark.smoke
def test_route_edit_roundtrip(seeded_poured_blinky, tmp_path):
    pcb = _copy_board(seeded_poured_blinky, tmp_path / "edit")
    ops = [{"op": "add_via", "at": [95.0, 95.0], "size": 0.6, "drill": 0.3,
            "net": "GND"},
           {"op": "add_track", "start": [95.0, 95.0], "end": [97.0, 95.0],
            "width": 0.25, "layer": "F.Cu", "net": "GND"}]
    results = route_edit.apply_ops(pcb, ops)
    assert [r["status"] for r in results] == ["added", "added"]
    uuids = [r["uuid"] for r in results]

    # idempotent re-application
    results2 = route_edit.apply_ops(pcb, ops)
    assert [r["status"] for r in results2] == ["exists", "exists"]

    # removal by uuid + absent uuid is a no-op
    rm = [{"op": "remove", "uuid": u} for u in uuids]
    rm.append({"op": "remove", "uuid": "0000-not-there"})
    results3 = route_edit.apply_ops(pcb, rm)
    assert [r["status"] for r in results3] == ["removed", "removed", "absent"]
    text = pcb.read_text(encoding="utf-8")
    assert uuids[0] not in text and uuids[1] not in text


@pytest.mark.smoke
def test_route_edit_rejects_and_rolls_back(seeded_poured_blinky, tmp_path):
    pcb = _copy_board(seeded_poured_blinky, tmp_path / "editbad")
    before = pcb.read_bytes()
    with pytest.raises(CheckError, match="not on board"):
        route_edit.apply_ops(pcb, [
            {"op": "add_track", "start": [1, 1], "end": [2, 2], "width": 0.2,
             "layer": "F.Cu", "net": "NO_SUCH_NET"}])
    with pytest.raises(CheckError, match="not a copper layer"):
        route_edit.apply_ops(pcb, [
            {"op": "add_track", "start": [1, 1], "end": [2, 2], "width": 0.2,
             "layer": "In1.Cu", "net": "GND"}])  # 2-layer board
    assert pcb.read_bytes() == before


# --------------------------------------------------------------- smoke: probe

@pytest.mark.smoke
def test_route_probe_leaves_board_untouched(seeded_poured_blinky, fr_tools,
                                            tmp_path):
    pcb = _copy_board(seeded_poured_blinky, tmp_path / "probe")
    before = pcb.read_bytes()
    facts = route_auto.route_probe(pcb, passes=3, timeout_s=300,
                                   work_dir=tmp_path / "probework")
    assert facts["completion"] is not None
    assert 0.0 <= facts["completion"] <= 1.0
    assert pcb.read_bytes() == before


# ---------------------------------------------------------------- smoke: auto

@pytest.mark.smoke
def test_route_auto_full_flow(seeded_poured_blinky, fr_tools, tmp_path):
    pcb = _copy_board(seeded_poured_blinky, tmp_path / "auto")
    payload, _ = route_auto.run(["--pcb", str(pcb),
                                 "--work-dir", str(tmp_path / "work")])
    facts = payload["facts"]
    # blinky2 fully routes on rung 1; the only tolerated leftover is the
    # B.Cu GND pour island (plane_repair's job), which surfaces as GND
    assert payload["status"] in ("pass", "violations")
    assert facts["tracks_after"] > facts["tracks_before"]
    assert facts["completion"] >= 0.9
    # Freerouting's result varies run to run on the same input (it left one
    # signal net, /OSC_OUT or /NRST, unrouted twice in CI; both passed on a
    # re-run), and the flow has no later rung that targets a single net. So
    # allow at most ONE unrouted signal net besides the tolerated GND island;
    # every power net must be routed, and the flow must report the leftover
    # (placement_adjust request below). A second miss or any power miss fails.
    leftover = set(facts["unrouted_nets"]) - {"GND"}
    assert len(leftover) <= 1, f"more than one signal net unrouted: {leftover}"
    assert not leftover & {"+3V3", "+5V"}, f"power net unrouted: {leftover}"
    # Rung 1 alone must suffice on blinky2 - but only when Freerouting actually
    # got its wall-clock budget. On a contended host (parallel sessions, or the
    # full suite next to another build) the rung can hit the per-rung timeout
    # with partial passes, which reads as a routing regression and is not one
    # (LEARNINGS 2026-08-13 [tests][freerouting]; the ladder recovers on rung 2).
    rung1 = facts["rungs"][0]
    if not rung1.get("timed_out"):
        assert rung1["unrouted"] <= 1, \
            f"rung 1 no longer routes blinky2 (<=1 net left): {rung1}"
    assert (tmp_path / "work" / "blinky2r.dsn").is_file()
    assert (tmp_path / "work" / "rung1.ses").is_file()
    assert (tmp_path / "work" / "rung1.log").is_file()
    if facts["unrouted_nets"]:
        req = facts["placement_adjust_request"]
        assert req["request"] == "placement_adjust"
        assert req["nets"] == facts["unrouted_nets"]
    # the routed board replaced the input atomically
    bg = geom.BoardGeom.from_file(pcb)
    assert len(bg.tracks_of()) == facts["tracks_after"]


@pytest.mark.smoke
def test_dedup_copper_removes_exact_echoes(tmp_path):
    """S14 (#27): FR's SES echoes pre-session copper back through import as
    exact same-net duplicates - invisible to DRC. The dedup verb removes the
    echo, keeps one of each, and is idempotent."""
    import shutil as _sh
    import sys as _sys
    _sys.path.insert(0, str(SCRIPTS / "lib"))
    import env as _env
    import routelib as _rl
    import geom as _geom

    cli = _env.find_kicad_cli()
    if cli is None:
        pytest.skip("kicad-cli not installed")
    bp = _env.find_kicad_python(cli)

    src = GOLDEN / "blinky2" / "blinky2.kicad_pcb"
    pcb = tmp_path / "dup.kicad_pcb"
    text = src.read_text(encoding="utf-8")
    # duplicate the FIRST (segment ...) block verbatim with a fresh uuid
    import re as _re
    m = _re.search(r"\t\(segment[\s\S]*?\n\t\)\n", text)
    assert m, "no segment found in golden"
    dup = m.group(0)
    du = _re.search(r'\(uuid "([0-9a-f-]+)"\)', dup)
    dup2 = dup.replace(du.group(1), "aaaaaaaa-bbbb-cccc-dddd-eeeeffff0000")
    text = text.replace(dup, dup + dup2, 1)
    pcb.write_text(text, encoding="utf-8")

    def dup_count(p):
        bg = _geom.BoardGeom.from_file(p)
        from collections import Counter
        c = Counter()
        for t in bg.tracks_of():
            c[(t.net, t.layer, round(t.width, 4),
               tuple(round(v, 4) for v in t.shape.coords[0]),
               tuple(round(v, 4) for v in t.shape.coords[-1]))] += 1
        return sum(n - 1 for n in c.values() if n > 1)

    assert dup_count(pcb) == 1
    work = tmp_path / "work"
    work.mkdir()
    r = _rl.run_worker(bp, {"verb": "dedup_copper", "board": str(pcb),
                            "out": str(pcb)}, work)
    assert r["removed"] == 1 and r["changed"] is True
    assert dup_count(pcb) == 0
    # idempotent
    r2 = _rl.run_worker(bp, {"verb": "dedup_copper", "board": str(pcb),
                             "out": str(pcb)}, work)
    assert r2["removed"] == 0 and r2["changed"] is False


_DSN_CLASS = '''(pcb x
  (resolution um 10)
  (network
    (class kicad_default /RF GND "Net-(C1-Pad2)"
      (circuit
        (use_via "Via[0-1]_600:300_um")
      )
      (rule
        (width 200)
        (clearance 200)
      )
    )
  )
)
'''


def test_dsn_apply_net_rules_moves_a_floored_net_to_its_own_class():
    """rf-term's /RF: the .kicad_dru's 0.9392 mm width and 0.8 mm HV
    clearance never reached Freerouting, which laid it at 0.2 mm."""
    import routelib
    text, moved = routelib.dsn_apply_net_rules(
        _DSN_CLASS, {"/RF": 0.9392, "GND": 0.1}, {"/RF": 0.8})
    assert moved == ["/RF"]
    assert "(class kicad_default GND \"Net-(C1-Pad2)\"" in text
    rf = text[text.index("(class aiee__RF /RF"):]
    assert "(width 939.2)" in rf and "(clearance 800)" in rf
    assert '(use_via "Via[0-1]_600:300_um")' in rf
    # the suppressed case: GND's 0.1 mm floor is below its class's 0.2 mm,
    # so it stays put, and the default class keeps its own rule
    default = text[text.index("(class kicad_default"):text.index("aiee__RF")]
    assert "(width 200)" in default and "GND" in default
    # nothing floored -> the DSN comes back unchanged
    assert routelib.dsn_apply_net_rules(_DSN_CLASS, {}, {}) == (_DSN_CLASS, [])


# --- Freerouting heap cap (#897) ----------------------------------------------

def test_fr_heap_is_a_clamped_share_of_the_budget(monkeypatch):
    monkeypatch.delenv("HWDE_FR_HEAP_MB", raising=False)
    assert routelib.fr_heap_mb(3000) == int(3000 * routelib.FR_HEAP_SHARE)
    assert routelib.fr_heap_mb(200) == routelib.FR_HEAP_MIN_MB
    assert routelib.fr_heap_mb(64 * 1024) == routelib.FR_HEAP_MAX_MB
    monkeypatch.setattr(routelib.loadlib, "memory_budget_mb", lambda: None)
    assert routelib.fr_heap_mb() == routelib.FR_HEAP_MAX_MB


def test_fr_heap_env_override(monkeypatch):
    monkeypatch.setenv("HWDE_FR_HEAP_MB", "1500")
    assert routelib.fr_heap_mb(64 * 1024) == 1500
    monkeypatch.setenv("HWDE_FR_HEAP_MB", "0")
    assert routelib.fr_heap_mb(3000) is None
    monkeypatch.setenv("HWDE_FR_HEAP_MB", "2g")
    with pytest.raises(CheckError, match="HWDE_FR_HEAP_MB"):
        routelib.fr_heap_mb(3000)


def test_fr_cmd_caps_the_heap_before_the_jar(monkeypatch):
    monkeypatch.setenv("HWDE_FR_HEAP_MB", "777")
    cmd = routelib.build_fr_cmd(Path("java"), Path("t/freerouting-2.5.0.jar"),
                                Path("i.dsn"), Path("o.ses"))
    j = cmd.index("-jar")    # JVM options only count before the jar
    assert cmd.index("-Xmx777m") < j and cmd.index(routelib.FR_OOM_EXIT) < j
    monkeypatch.setenv("HWDE_FR_HEAP_MB", "0")
    cmd = routelib.build_fr_cmd(Path("java"), Path("t/freerouting-2.5.0.jar"),
                                Path("i.dsn"), Path("o.ses"))
    assert not any(a.startswith("-Xmx") for a in cmd)
    assert routelib.FR_OOM_EXIT in cmd


_OOM_BODY = ("import sys\n"
             "print('Terminating due to java.lang.OutOfMemoryError: Java heap space')\n"
             "sys.exit({rc})\n")


def test_run_freerouting_reports_out_of_heap(tmp_path):
    cmd, dsn, ses = _fake_fr(tmp_path, _OOM_BODY.format(rc=3))
    facts = routelib.run_freerouting(None, None, dsn, ses,
                                     cmd=cmd + ["-Xmx64m"], timeout=30, stall_s=10)
    assert facts["oom"] is True and facts["heap_mb"] == 64


def test_oom_text_in_a_clean_or_killed_run_is_not_oom(tmp_path):
    # planted near-misses: the words in a run that exited 0, and in one the
    # stall timer killed, are not a heap failure
    cmd, dsn, ses = _fake_fr(tmp_path, _OOM_BODY.format(rc=0))
    assert routelib.run_freerouting(None, None, dsn, ses, cmd=cmd,
                                    timeout=30, stall_s=10)["oom"] is False
    cmd, dsn, ses = _fake_fr(tmp_path, (
        "import time\nprint('java.lang.OutOfMemoryError')\ntime.sleep(60)\n"))
    facts = routelib.run_freerouting(None, None, dsn, ses, cmd=cmd,
                                     timeout=30, stall_s=1)
    assert facts["timed_out"] and facts["oom"] is False


def test_route_probe_out_of_heap_is_a_clean_error(tmp_path, monkeypatch):
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text("(kicad_pcb)")
    before = pcb.read_bytes()
    monkeypatch.setattr(route_auto, "_tools", lambda: (None, None, None, None))
    monkeypatch.setattr(routelib, "run_worker", lambda *a, **k: None)
    monkeypatch.setattr(route_auto.geom.BoardGeom, "from_file",
                        classmethod(lambda cls, f: type(
                            "BG", (), {"zones_of": lambda self: []})()))
    for name in ("_auto_power_layers", "_dsn_net_rules", "_dsn_layer_widths",
                 "_dsn_hole_rules", "_dsn_merge_wires", "_dsn_barriers"):
        monkeypatch.setattr(route_auto, name, lambda *a, **k: [])
    monkeypatch.setattr(routelib, "run_freerouting", lambda *a, **k: {
        "oom": True, "heap_mb": 512, "rc": 3, "timed_out": False})
    with pytest.raises(CheckError, match="ran out of its 512 MB heap"):
        route_auto.route_probe(pcb, work_dir=tmp_path / "w")
    assert pcb.read_bytes() == before


# --- run_freerouting: stall-based timeout (2026-10-06) -----------------------

def _fake_fr(tmp_path, body):
    script = tmp_path / "fake_fr.py"
    script.write_text(body, encoding="utf-8")
    dsn = tmp_path / "b.dsn"
    dsn.write_text("x", encoding="utf-8")
    return [sys.executable, "-u", str(script)], dsn, tmp_path / "b.ses"


def test_run_freerouting_slow_but_progressing_is_not_killed(tmp_path):
    # 4 passes 0.5 s apart = 2 s total, longer than the 1 s stall budget but
    # never silent for that long: must finish, not be killed.
    cmd, dsn, ses = _fake_fr(tmp_path, (
        "import time\n"
        "for i in range(1, 5):\n"
        "    time.sleep(0.5)\n"
        "    print(f'INFO  [X] Auto-router pass #{i} on board b was completed"
        " in 0.5 seconds with the score of {900+i}.0 ({5-i} unrouted)')\n"
        "print('INFO  [X] Auto-router session completed: started with 5 "
        "unrouted nets, completed in 2 seconds, final score: 905.0')\n"
        "open('b.ses', 'w').write('ses')\n"))
    facts = routelib.run_freerouting(None, None, dsn, ses, cmd=cmd,
                                     timeout=30, stall_s=1)
    assert not facts["timed_out"] and facts["rc"] == 0
    assert facts["ses_written"] and len(facts["passes"]) == 4


def test_run_freerouting_silent_hang_is_killed_on_stall(tmp_path):
    cmd, dsn, ses = _fake_fr(tmp_path, (
        "import time\nprint('start')\ntime.sleep(60)\n"))
    facts = routelib.run_freerouting(None, None, dsn, ses, cmd=cmd,
                                     timeout=30, stall_s=1)
    assert facts["timed_out"] and facts["kill_reason"] == "stall"


def test_run_freerouting_hard_cap_ends_a_chatty_run(tmp_path):
    cmd, dsn, ses = _fake_fr(tmp_path, (
        "import time\nfor _ in range(50):\n    print('tick')\n    time.sleep(0.2)\n"))
    facts = routelib.run_freerouting(None, None, dsn, ses, cmd=cmd,
                                     timeout=2, stall_s=60)
    assert facts["timed_out"] and facts["kill_reason"] == "hard"


def test_run_freerouting_silent_but_growing_ses_is_not_killed(tmp_path):
    # Silent for 5 s in all, longer than the 3 s stall budget, but the .ses
    # grows every 0.5 s: must finish. The 2.5 s of slack per gap absorbs a
    # slow interpreter start on a loaded machine (a 1 s budget did not).
    cmd, dsn, ses = _fake_fr(tmp_path, (
        "import time\n"
        "for i in range(1, 11):\n"
        "    open('b.ses', 'a').write('x' * i)\n"
        "    time.sleep(0.5)\n"))
    facts = routelib.run_freerouting(None, None, dsn, ses, cmd=cmd,
                                     timeout=60, stall_s=3)
    assert not facts["timed_out"] and facts["rc"] == 0


def test_run_freerouting_interrupt_kills_the_process(tmp_path, monkeypatch):
    cmd, dsn, ses = _fake_fr(tmp_path, "import time\ntime.sleep(60)\n")
    procs = []
    real = routelib.subprocess.Popen

    def spy(*a, **k):
        p = real(*a, **k)
        procs.append(p)
        return p

    def boom(_s):
        raise KeyboardInterrupt

    monkeypatch.setattr(routelib.subprocess, "Popen", spy)
    monkeypatch.setattr(routelib.time if hasattr(routelib, "time") else
                        __import__("time"), "sleep", boom)
    with pytest.raises(KeyboardInterrupt):
        routelib.run_freerouting(None, None, dsn, ses, cmd=cmd,
                                 timeout=30, stall_s=5)
    assert procs[0].poll() is not None and procs[0].stdout.closed
