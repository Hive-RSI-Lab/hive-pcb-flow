"""#1412: the Specctra export route_auto hands Freerouting depends on the
design alone, not on the order the board file lists its footprints in.

KiCad saves footprints sorted by UUID and gives a footprint written
without one a random UUID per load, so the same board comes back from a
save in another order; the DSN followed it (component order, which of two
same-geometry footprints names their shared image, and net numbers), and
Freerouting's result follows the DSN order. The pure half (nets by name)
is in test_routelib_holes; this runs the export live.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))
sys.path.insert(0, str(REPO / "tests"))
import env  # noqa: E402
import route_auto  # noqa: E402
import routelib  # noqa: E402
from test_route_escape import _fp, _pad  # noqa: E402

HEAD = """(kicad_pcb
  (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (1 "F.Mask" user)
    (13 "F.Paste" user) (5 "F.SilkS" user "F.Silkscreen")
    (31 "F.CrtYd" user "F.Courtyard") (25 "Edge.Cuts" user))
  (setup)
  (gr_rect (start 0 0) (end 30 30) (stroke (width 0.1)) (fill no)
    (layer "Edge.Cuts"))
"""


def _parts():
    # J1 and J2 share one geometry, so one DSN image, named after
    # whichever of them the export meets first
    def conn(ref, nets):
        return "".join(_pad(str(k + 1), k * 1.0, 0.0, n, 1.0, "rect", 0.6)
                       for k, n in enumerate(nets))
    u1 = "".join(_pad(str(k + 1), (k % 2) * 1.0, (k // 2) * 1.0, n, 0.5)
                 for k, n in enumerate(["/A", "/B", "/C", "/D"]))
    return {"U1": _fp("U1", 15.0, 10.0, u1, (1.5, 1.5)),
            "J1": _fp("J1", 8.0, 22.0, conn("J1", ["/C", "/A"]), (2, 1)),
            "J2": _fp("J2", 22.0, 22.0, conn("J2", ["/D", "/B"]), (2, 1))}


def _board(tmp_path, name, order):
    parts = _parts()
    p = tmp_path / f"{name}.kicad_pcb"
    p.write_text(HEAD + "".join(parts[r] for r in order) + ")\n",
                 encoding="utf-8")
    return p


def _body(dsn: Path) -> str:
    # the first line names the file
    return dsn.read_text(encoding="utf-8").split("\n", 1)[1]


def test_export_dsn_is_the_same_whatever_the_footprint_order(tmp_path):
    cli = env.find_kicad_cli()
    bp = env.find_kicad_python(cli) if cli else None
    if bp is None:
        pytest.skip("the export runs in KiCad's python")
    sorted_, raw = {}, {}
    for name, order in (("a", ["J2", "U1", "J1"]), ("b", ["J1", "U1", "J2"])):
        pcb = _board(tmp_path, name, order)
        work = tmp_path / f"w-{name}"
        work.mkdir()
        dsn = tmp_path / f"{name}.dsn"
        route_auto._export_dsn(bp, pcb, dsn, [], work)
        sorted_[name] = _body(dsn)
        plain = tmp_path / f"{name}-plain.dsn"
        routelib.run_worker(bp, {"verb": "export_dsn", "board": str(pcb),
                                 "dsn": str(plain)}, work)
        raw[name] = _body(plain)
    assert raw["a"] != raw["b"]                 # the bug: order leaks in
    assert sorted_["a"] == sorted_["b"]
    text = sorted_["a"]
    places = [text.index(f"(place {r} ") for r in ("J1", "J2", "U1")]
    assert places == sorted(places)
    nets = [text.index(f"(net {n}") for n in ("/A", "/B", "/C", "/D")]
    assert nets == sorted(nets)
