"""#628: route_auto --power-layers names the layers closed to signals.

The DSN marks a power layer "(type power)" and Freerouting runs no signal
on it. An explicit list used to be passed on as {layer: "power"} only, so
a layer the board itself types power stayed power in the export: closed
to Freerouting though the caller left it out, while the KRT and maze
finishes, which take "every layer not in the list", routed on it. Now a
list (or none) is the whole set and every other copper layer exports as
signal; auto keeps the board's own power layers closed for every pass and
warns about one auto would not have picked.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))
sys.path.insert(0, str(REPO / "tests"))
import env  # noqa: E402
import geom  # noqa: E402
import route_auto  # noqa: E402
import routelib  # noqa: E402
from checklib import CheckError  # noqa: E402
from test_route_escape import _fp, _pad  # noqa: E402

GOLDEN = REPO / "tests" / "golden"


def _head(in2: str) -> str:
    return f"""(kicad_pcb
  (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (4 "In1.Cu" signal) (6 "In2.Cu" {in2})
    (2 "B.Cu" signal) (1 "F.Mask" user) (13 "F.Paste" user)
    (5 "F.SilkS" user "F.Silkscreen") (31 "F.CrtYd" user "F.Courtyard")
    (25 "Edge.Cuts" user))
  (setup)
  (gr_rect (start 0 0) (end 30 30) (stroke (width 0.1)) (fill no)
    (layer "Edge.Cuts"))
"""


def _board(tmp_path, in2: str) -> Path:
    u1 = "".join(_pad(str(k + 1), (k % 2) * 1.0, (k // 2) * 1.0, n, 0.5)
                 for k, n in enumerate(["/A", "/B"]))
    j1 = "".join(_pad(str(k + 1), k * 1.0, 0.0, n, 1.0, "rect", 0.6)
                 for k, n in enumerate(["/A", "/B"]))
    p = tmp_path / f"b-{in2}" / "b.kicad_pcb"
    p.parent.mkdir(parents=True)
    p.write_text(_head(in2) + _fp("U1", 15.0, 10.0, u1, (1.5, 1.5))
                 + _fp("J1", 8.0, 22.0, j1, (2, 1)) + ")\n",
                 encoding="utf-8")
    return p


class _Stop(Exception):
    pass


def _export_job(tmp_path, monkeypatch, in2, spec):
    """run() up to the DSN export; -> the export_dsn job it sends."""
    pcb = _board(tmp_path, in2)
    jobs = []

    def worker(bp, job, work, *a, **k):
        jobs.append(job)
        raise _Stop

    monkeypatch.setattr(route_auto, "_tools", lambda: ("cli", "bp", "j", "jar"))
    monkeypatch.setattr(routelib, "run_worker", worker)
    with pytest.raises(_Stop):
        route_auto.run(["--pcb", str(pcb), "--power-layers", spec,
                        "--work-dir", str(tmp_path / f"w-{in2}-{spec}")])
    (job,) = [j for j in jobs if j["verb"] == "export_dsn"]
    return job


ALL = ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")


def test_a_board_power_layer_left_out_of_the_list_exports_signal(
        tmp_path, monkeypatch):
    # the fault: In2 typed power on the board, the caller closes In1 only
    job = _export_job(tmp_path, monkeypatch, "power", "In1.Cu")
    assert job["layer_types"] == {"F.Cu": "signal", "In1.Cu": "power",
                                  "In2.Cu": "signal", "B.Cu": "signal"}


def test_none_opens_every_layer_even_a_board_power_layer(
        tmp_path, monkeypatch):
    job = _export_job(tmp_path, monkeypatch, "power", "none")
    assert job["layer_types"] == {ly: "signal" for ly in ALL}


def test_quiet_a_list_on_a_signal_typed_board_closes_just_the_list(
        tmp_path, monkeypatch):
    # (a layer the job leaves out keeps the board's type, signal here)
    job = _export_job(tmp_path, monkeypatch, "signal", "In1.Cu,In2.Cu")
    assert {ly for ly, t in job["layer_types"].items()
            if t == "power"} == {"In1.Cu", "In2.Cu"}


def test_board_layer_types_reads_the_layers_section():
    text = _head("power") + ")\n"
    assert route_auto._board_layer_types(text) == {
        "F.Cu": "signal", "In1.Cu": "signal", "In2.Cu": "power",
        "B.Cu": "signal"}


def test_auto_keeps_a_board_power_layer_closed_for_every_pass_and_warns():
    # usbbuck4: In1 and In2 carry fills and no tracks, so auto picks both;
    # say the board types F.Cu power (it has tracks): auto keeps it closed
    # (the DSN would anyway) and the list KRT and the maze finish read
    # leaves it out too, with a warning that names it
    bg = geom.BoardGeom.from_file(GOLDEN / "usbbuck4" / "usbbuck4.kicad_pcb")
    closed, types, warns = route_auto._closed_layers(
        "auto", bg, {"F.Cu": "power", "In1.Cu": "signal"})
    assert closed == ["F.Cu", "In1.Cu", "In2.Cu"]
    assert types["F.Cu"] == "power" and types["B.Cu"] == "signal"
    assert len(warns) == 1 and warns[0].startswith("F.Cu: typed power")


def test_quiet_auto_on_a_board_with_no_power_types_is_the_plane_pick():
    bg = geom.BoardGeom.from_file(GOLDEN / "usbbuck4" / "usbbuck4.kicad_pcb")
    closed, types, warns = route_auto._closed_layers("auto", bg, {})
    assert closed == route_auto._auto_power_layers(bg) == ["In1.Cu", "In2.Cu"]
    assert types == {ly: ("power" if ly in closed else "signal")
                     for ly in bg.copper_layers}
    assert warns == []


def test_a_list_naming_a_layer_the_board_lacks_is_an_error():
    bg = geom.BoardGeom.from_file(GOLDEN / "usbbuck4" / "usbbuck4.kicad_pcb")
    with pytest.raises(CheckError, match="In5.Cu is not a copper layer"):
        route_auto._closed_layers("In1.Cu,In5.Cu", bg, {})


def _dsn_types(dsn: Path) -> dict[str, str]:
    return dict(re.findall(r"\(layer\s+(\S+)\s+\(type\s+(\w+)\)",
                           dsn.read_text(encoding="utf-8")))


@pytest.mark.parametrize("in2,spec,want", [
    ("power", "In1.Cu", "signal"),      # the fault
    ("signal", "In1.Cu", "signal"),     # quiet twin
])
def test_live_dsn_export_types_in2_as_the_list_says(tmp_path, monkeypatch,
                                                    in2, spec, want):
    cli = env.find_kicad_cli()
    bp = env.find_kicad_python(cli) if cli else None
    if bp is None:
        pytest.skip("the export runs in KiCad's python")
    pcb = _board(tmp_path, in2)
    work = tmp_path / "w"
    monkeypatch.setattr(route_auto, "_tools", lambda: (cli, bp, "j", "jar"))

    def stop(*a, **k):
        raise _Stop
    monkeypatch.setattr(route_auto, "_dsn_net_rules", stop)
    with pytest.raises(_Stop):
        route_auto.run(["--pcb", str(pcb), "--power-layers", spec,
                        "--work-dir", str(work)])
    types = _dsn_types(work / "b.dsn")
    assert types["In1.Cu"] == "power"
    assert types["In2.Cu"] == want
    assert types["F.Cu"] == types["B.Cu"] == "signal"
