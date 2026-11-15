"""teardrop_swig - BUNDLED-python SWIG worker for route_teardrops.py (#480).

Same job/result-file protocol as route_swig (routelib.run_worker):
  python teardrop_swig.py job.json
job: {"verb": "enable", "board": pcb, "out": pcb, "result": r.json,
      "vias": [uuid, ...], "pads": [[ref, number, x, y], ...],
      "params": {length_ratio, max_length_mm, width_ratio, max_width_mm,
                 filter_ratio, curved}}
Sets each named via's / pad's teardrop parameters and enables them, then
saves. It makes no copper: KiCad builds the teardrop zones on the next zone
refill (route_teardrops runs `kicad-cli pcb drc --refill-zones --save-board`).
Result: {"ok": true, "vias": n, "pads": n, "missing": [...]}.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# teardroplib.ITEM_PARAMS's values (that module needs the venv, this one runs
# in KiCad's python; test_teardrops keeps the two equal): what a new via that
# add_via asks teardrops for takes when a key is left out (#1138)
ITEM_DEFAULTS = {"length_ratio": 0.5, "max_length_mm": 1.0,
                 "width_ratio": 1.0, "max_width_mm": 2.0,
                 "filter_ratio": 0.9, "curved": False,
                 "two_tracks": True, "prefer_zones": True}


def set_params(item, p: dict, mm) -> None:
    """Set a via's or pad's teardrop parameters (teardroplib.ITEM_PARAMS
    names; mm converts mm to IU) and enable its teardrops. A ratio or size
    left out takes route_teardrops' default; two_tracks / prefer_zones are
    set only when given, else the item keeps its own. route_swig's add_via
    calls this too (#1138)."""
    item.SetTeardropBestLengthRatio(float(p.get("length_ratio", 0.5)))
    item.SetTeardropMaxLength(mm(float(p.get("max_length_mm", 1.0))))
    item.SetTeardropBestWidthRatio(float(p.get("width_ratio", 1.0)))
    item.SetTeardropMaxWidth(mm(float(p.get("max_width_mm", 2.0))))
    item.SetTeardropMaxTrackWidth(float(p.get("filter_ratio", 0.9)))
    item.SetTeardropCurved(bool(p.get("curved", False)))
    if "two_tracks" in p:
        item.SetTeardropAllowSpanTwoTracks(bool(p["two_tracks"]))
    if "prefer_zones" in p:
        item.SetTeardropPreferZoneConnections(bool(p["prefer_zones"]))
    item.SetTeardropsEnabled(True)


def get_params(item, to_mm) -> dict:
    """The item's teardrop settings as set_params takes them, plus
    "enabled" (to_mm converts IU to mm)."""
    return {"enabled": bool(item.GetTeardropsEnabled()),
            "length_ratio": float(item.GetTeardropBestLengthRatio()),
            "max_length_mm": to_mm(item.GetTeardropMaxLength()),
            "width_ratio": float(item.GetTeardropBestWidthRatio()),
            "max_width_mm": to_mm(item.GetTeardropMaxWidth()),
            "filter_ratio": float(item.GetTeardropMaxTrackWidth()),
            "curved": bool(item.GetTeardropCurved()),
            "two_tracks": bool(item.GetTeardropAllowSpanTwoTracks()),
            "prefer_zones": bool(item.GetTeardropPreferZoneConnections())}


def run(job: dict) -> dict:
    import pcbnew
    import safelib
    mm = pcbnew.FromMM
    with safelib.board_locks(job):
        b = pcbnew.LoadBoard(job["board"])
        want_v = set(job.get("vias") or [])
        nv = 0
        for t in b.GetTracks():
            if t.GetClass() != "PCB_VIA":
                continue
            if t.m_Uuid.AsString() in want_v:
                set_params(t, job.get("params") or {}, mm)
                want_v.discard(t.m_Uuid.AsString())
                nv += 1
        want_p = {(r, n, round(x, 3), round(y, 3))
                  for r, n, x, y in job.get("pads") or []}
        np_ = 0
        for fp in b.GetFootprints():
            ref = fp.GetReference()
            for pad in fp.Pads():
                pos = pad.GetPosition()
                key = (ref, pad.GetNumber(), round(pcbnew.ToMM(pos.x), 3),
                       round(pcbnew.ToMM(pos.y), 3))
                if key in want_p:
                    set_params(pad, job.get("params") or {}, mm)
                    want_p.discard(key)
                    np_ += 1
        missing = sorted(want_v) + [f"{r}.{n}@{x},{y}" for r, n, x, y in
                                    sorted(want_p)]
        if missing:
            return {"ok": False, "error": f"items not found: {missing[:10]}",
                    "missing": missing}
        b.Save(job["out"])
    return {"ok": True, "vias": nv, "pads": np_, "missing": []}


def main() -> int:
    job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    try:
        res = run(job)
    except Exception as exc:  # noqa: BLE001 - result file carries it
        res = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    Path(job["result"]).write_text(json.dumps(res), encoding="utf-8")
    return 0 if res.get("ok") else 3


if __name__ == "__main__":
    sys.exit(main())
