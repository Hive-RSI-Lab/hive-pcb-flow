"""Teardrops on vias and PTH pads, and the ring-breakout DFM rule (#480).

Fixture: tests/fixtures/teardrops/led.kicad_pcb (hwde-led-test, 2 GND vias,
J1 2-pin THT header, SMD LED/R/C) and led_dropped.kicad_pcb, the same board
after `route_teardrops --force` (KiCad 10.0.6 refill). Known answers, by
hand from the copper:
  via@18.831,20.753  GND: two F.Cu tracks touch it, one wholly inside the
                     via copper (a 0.29 mm stub) -> 1 join; B.Cu is GND pour
  via@13.200,21.740  GND: 1 F.Cu join; B.Cu pour
  J1.1 (+5V, PTH)    1 F.Cu join, no pour
  J1.2 (GND, PTH)    only the B.Cu pour -> nothing to drop
  SMD pads           out of scope by default
  => 3 items, 3 teardrop zones; a second run adds none.
A via's teardrop settings read back as route_edit's add_via takes them
(params_of, same_params; #1138), and read(area=) for part_repair's local
check.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

import pytest
from shapely.geometry import Point

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))
import dfm_check  # noqa: E402
import route_teardrops as rtd  # noqa: E402
import teardroplib as tdl  # noqa: E402

FIX = REPO / "tests" / "fixtures" / "teardrops"
RAW = FIX / "led.kicad_pcb"
DROPPED = FIX / "led_dropped.kicad_pcb"
VIA_A = "via@18.831,20.753"
VIA_B = "via@13.200,21.740"
UUID_A = "7a3b7045-1c37-463a-9e27-6b82ec68a88e"
UUID_B = "d150125a-0f47-42a1-b4eb-36069d276772"
CAPS = {"board": {"drill_wander_mm": 0.075, "min_ring_abs_mm": 0.05}}

FLEX_AREA = """\t(zone
\t\t(net 0)
\t\t(net_name "")
\t\t(layer "F.Cu")
\t\t(name "FLEX_TAIL")
\t\t(hatch edge 0.5)
\t\t(connect_pads (clearance 0))
\t\t(min_thickness 0.25)
\t\t(keepout (tracks allowed) (vias not_allowed) (pads allowed)
\t\t\t(copperpour allowed) (footprints allowed))
\t\t(polygon (pts (xy 0 0) (xy 1 0) (xy 1 1)))
\t)
"""


def _copy(tmp: Path, src: Path, name: str = "b.kicad_pcb") -> Path:
    out = tmp / name
    shutil.copy2(src, out)
    return out


def _edit(pcb: Path, fn) -> Path:
    pcb.write_text(fn(pcb.read_text(encoding="utf-8")), encoding="utf-8")
    return pcb


def _shrink_via_b(text: str) -> str:
    """Plant a 0.5/0.3 via (ring 0.1 < 0.05 + 0.075) at VIA_B."""
    new, n = re.subn(r"(\(at 13\.2 21\.74\)\s*\(size )0\.6", r"\g<1>0.5",
                     text)
    assert n == 1
    return new


def _drop_zones(text: str) -> list[tuple[int, int]]:
    out = []
    for m in re.finditer(r"\n\t\(zone\n", text):
        end = text.index("\n\t)\n", m.start()) + 4
        if "(teardrop" in text[m.start():end]:
            out.append((m.start(), end))
    return out


def _strip_drop_of(text: str, x: float, y: float) -> str:
    """Delete the teardrop zone anchored at (x, y)."""
    for a, b in _drop_zones(text):
        if f"(xy {x:g} {y:g}" in text[a:b] or \
                re.search(rf"\(xy {x:g}\d* {y:g}\d*\)", text[a:b]):
            return text[:a] + "\n" + text[b:]
    raise AssertionError("no teardrop zone at the point")


def _add_flex(text: str) -> str:
    i = text.rstrip().rindex(")")
    return text[:i] + FLEX_AREA + text[i:]


# ------------------------------------------------------------- reading

def test_known_answer_items_joins_and_scope():
    b = tdl.read(RAW)
    by = {it.name: it for it in b.items}
    assert by[VIA_A].joins == {"F.Cu": 1}      # the inside stub is no join
    assert by[VIA_A].held == {"B.Cu"}
    assert by[VIA_B].joins == {"F.Cu": 1}
    assert by["J1.1"].joins == {"F.Cu": 1} and not by["J1.1"].held
    assert by["J1.2"].wanted() == 0
    assert by["J1.1"].ring_mm == pytest.approx(0.4, abs=1e-4)
    assert by[VIA_A].ring_mm == pytest.approx(0.15)
    todo, done = tdl.select(b, tdl.config(None))
    assert sorted(it.name for it in todo) == sorted([VIA_A, VIA_B, "J1.1"])
    assert done == [] and b.drop_count == 0
    # SMD scope on: the SMD pads join in, the pads alone
    todo_smd, _ = tdl.select(b, tdl.config({"teardrops": {"smd": True}}))
    assert {it.kind for it in todo_smd} == {"via", "pth", "smd"}
    # net filter
    only5, _ = tdl.select(b, tdl.config({"teardrops": {"nets": ["+5V"]}}))
    assert [it.name for it in only5] == ["J1.1"]


def test_dropped_board_every_item_has_its_drop_and_nothing_left_to_do():
    b = tdl.read(DROPPED)
    assert b.drop_count == 3
    by = {it.name: it for it in b.items}
    for n in (VIA_A, VIA_B, "J1.1"):
        assert by[n].enabled and by[n].drops == {"F.Cu": 1}
        assert by[n].missing() == []
    todo, done = tdl.select(b, tdl.config(None))
    assert todo == [] and len(done) == 3     # already dropped: untouched


def test_item_params_read_from_the_via_and_compared():
    """A via's (teardrops ...) block as route_edit's add_via takes it
    (#1138): led_dropped's vias are at the defaults, led's have none."""
    import sexpdata
    got = tdl.via_params(sexpdata.loads(DROPPED.read_text(encoding="utf-8")))
    assert got == {UUID_A: dict(tdl.ITEM_PARAMS, enabled=True),
                   UUID_B: dict(tdl.ITEM_PARAMS, enabled=True)}
    assert tdl.via_params(sexpdata.loads(RAW.read_text(encoding="utf-8"))) \
        == {UUID_A: None, UUID_B: None}
    a = got[UUID_A]
    assert tdl.same_params(tdl.ITEM_PARAMS, a)
    assert tdl.same_params({"length_ratio": 0.50004}, a)
    assert not tdl.same_params({"length_ratio": 0.6}, a)
    assert not tdl.same_params({"curved": True}, a)
    assert not tdl.same_params({}, None)
    assert not tdl.same_params({}, dict(a, enabled=False))
    # a key the block lacks reads as ITEM_PARAMS
    assert tdl.same_params({"two_tracks": True}, {"enabled": True})
    assert not tdl.same_params({"two_tracks": False}, {"enabled": True})


def test_read_an_area_keeps_its_items_and_their_drops():
    b = tdl.read(DROPPED, None, area=Point(18.8312, 20.7525).buffer(0.3))
    assert [it.name for it in b.items] == [VIA_A]
    assert b.items[0].drops == {"F.Cu": 1} and not b.items[0].missing()
    assert b.drop_count == 1


def test_config_refuses_unknown_keys():
    with pytest.raises(ValueError, match="unknown keys"):
        tdl.config({"teardrops": {"enable": True}})


def test_required_on_flex_area_or_constraints(tmp_path):
    flex = _edit(_copy(tmp_path, RAW), _add_flex)
    b = tdl.read(flex)
    assert b.flex == ["FLEX_TAIL"]
    assert tdl.required(tdl.config(None), b.flex)
    assert not tdl.required(tdl.config({"teardrops": {"required": False}}),
                            b.flex)
    assert tdl.required(tdl.config({"teardrops": {"required": True}}), [])
    assert not tdl.required(tdl.config(None), [])


# ------------------------------------------------------------- ring rule

def test_standard_vias_pass_without_teardrops():
    # 0.6/0.3 (ring 0.15) and the 0.4 mm header ring clear 0.125
    assert tdl.ring_findings(tdl.read(RAW), req=False) == []


def test_planted_thin_ring_flagged_without_and_passes_with_teardrop(tmp_path):
    raw = _edit(_copy(tmp_path, RAW, "raw.kicad_pcb"), _shrink_via_b)
    f = tdl.ring_findings(tdl.read(raw), req=False)
    assert [(x["kind"], x["item"], x["severity"], x["layers"]) for x in f] \
        == [("dfm_ring_breakout", VIA_B, "warning", ["F.Cu"])]
    assert f[0]["ring_mm"] == pytest.approx(0.1)
    assert f[0]["limit_mm"] == pytest.approx(0.125)
    dropped = _edit(_copy(tmp_path, DROPPED, "dr.kicad_pcb"), _shrink_via_b)
    assert tdl.ring_findings(tdl.read(dropped), req=False) == []


def test_mutant_teardrop_deleted_is_caught(tmp_path):
    """The dropped board with VIA_B's teardrop zone cut out: the thin ring
    is a breakout again, and on a flex board the bare join is an error."""
    mut = _edit(_copy(tmp_path, DROPPED),
                lambda t: _strip_drop_of(_shrink_via_b(t), 13.2, 21.7))
    b = tdl.read(mut)
    assert b.drop_count == 2
    f = tdl.ring_findings(b, req=False)
    assert [(x["kind"], x["item"]) for x in f] == \
        [("dfm_ring_breakout", VIA_B)]
    f = tdl.ring_findings(b, req=True)
    assert [(x["kind"], x["severity"]) for x in f] == \
        [("dfm_ring_breakout", "error")]


def test_flex_board_requires_drops_on_every_join(tmp_path):
    raw = _edit(_copy(tmp_path, RAW, "raw.kicad_pcb"), _add_flex)
    vios: list = []
    facts = dfm_check.check_teardrops(raw, CAPS, vios)
    assert facts["required"] and facts["flex_areas"] == ["FLEX_TAIL"]
    assert sorted((v["kind"], v["item"], v["severity"]) for v in vios) == [
        ("dfm_teardrop_missing", "J1.1", "error"),
        ("dfm_teardrop_missing", VIA_B, "error"),
        ("dfm_teardrop_missing", VIA_A, "error")]
    dropped = _edit(_copy(tmp_path, DROPPED, "dr.kicad_pcb"), _add_flex)
    vios = []
    facts = dfm_check.check_teardrops(dropped, CAPS, vios)
    assert vios == [] and facts["drops"] == 3 \
        and facts["items_with_drops"] == 3


def test_dfm_family_and_caps_keys():
    assert dfm_check.DFM_FAMILIES["teardrop"] == (
        "dfm_ring_breakout", "dfm_teardrop_missing")
    caps = dfm_check.load_capabilities(dfm_check.CAPABILITIES)
    assert caps["board"]["drill_wander_mm"] == 0.075
    assert caps["board"]["min_ring_abs_mm"] == 0.05


# ------------------------------------------------------------- the step

def _report(argv) -> dict:
    payload, _ = rtd.run(argv)
    return payload


def test_off_unless_enabled_or_required(tmp_path):
    pcb = _copy(tmp_path, RAW)
    before = pcb.read_bytes()
    r = _report(["--pcb", str(pcb)])
    assert r["enabled_by"] is None and "skipped" in r
    assert pcb.read_bytes() == before
    r = _report(["--pcb", str(pcb), "--dry-run", "--force"])
    assert sorted(r["planned"]) == sorted([VIA_A, VIA_B, "J1.1"])
    assert pcb.read_bytes() == before
    (tmp_path / "constraints.json").write_text(
        json.dumps({"teardrops": {"enabled": True}}))
    r = _report(["--pcb", str(pcb), "--dry-run"])
    assert r["enabled_by"] == "constraints"
    flex = _edit(_copy(tmp_path, RAW, "flex.kicad_pcb"), _add_flex)
    (tmp_path / "constraints.json").unlink()
    r = _report(["--pcb", str(flex), "--dry-run"])
    assert r["enabled_by"] == "required" and r["required"]


def test_dropped_board_is_a_no_op(tmp_path):
    pcb = _copy(tmp_path, DROPPED)
    before = pcb.read_bytes()
    r = _report(["--pcb", str(pcb), "--force"])
    assert r["planned"] == [] and r["already"] == 3
    assert r["drops_after"] == 3 and pcb.read_bytes() == before


def test_drc_regression_leaves_the_board(tmp_path, monkeypatch):
    """Mutant: the refill after enabling grows a DRC type -> the staged
    board is thrown away and the run fails."""
    import kc
    import routelib
    pcb = _copy(tmp_path, RAW)
    before = pcb.read_bytes()
    calls = []

    def fake_drc(cli, staged, **kw):
        calls.append(staged)
        v = [] if len(calls) == 1 else [{"severity": "error",
                                          "source": "drc",
                                          "check": "clearance"}]
        return {"violations": v}

    monkeypatch.setattr(kc, "run_drc", fake_drc)
    monkeypatch.setattr(routelib, "run_worker",
                        lambda *a, **k: {"ok": True, "vias": 2, "pads": 1})
    import env
    monkeypatch.setattr(env, "find_kicad_cli", lambda: Path("/bin/true"))
    monkeypatch.setattr(env, "find_kicad_python", lambda cli: Path("/bin/true"))
    r = _report(["--pcb", str(pcb), "--force"])
    assert [v["kind"] for v in r["violations"]][0] == "teardrop_regression"
    assert r["violations"][0]["grown"] == {"drc:clearance": [0, 1]}
    assert pcb.read_bytes() == before and not r.get("written")


# ------------------------------------------------------------- live (slow)

def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_live_teardrops_led_count_drc_idempotent(tmp_path):
    import env
    if env.find_kicad_cli() is None:
        pytest.skip("kicad-cli not found")
    pcb = _copy(tmp_path, RAW, "led.kicad_pcb")
    for ext in (".kicad_pro", ".kicad_dru"):
        shutil.copy2(FIX / f"led{ext}", tmp_path / f"led{ext}")
    r = _report(["--pcb", str(pcb), "--force"])
    assert r["status"] == "pass", r["violations"]
    assert r["enabled_items"] == {"via": 2, "pth": 1, "smd": 0}
    assert r["drops_after"] == 3 == r["drops_wanted"]
    assert r["drc_after"] == r["drc_before"] == {}
    b = tdl.read(pcb)
    assert all(not it.drops for it in b.items if it.kind == "smd")
    sha = _sha(pcb)
    r2 = _report(["--pcb", str(pcb), "--force"])
    assert r2["planned"] == [] and r2["already"] == 3
    assert r2["drops_after"] == 3 and _sha(pcb) == sha
    shrunk = _edit(pcb, _shrink_via_b)
    assert tdl.ring_findings(tdl.read(shrunk), req=True) == []


def test_live_teardrops_bb_adc_count(tmp_path):
    import env
    if env.find_kicad_cli() is None:
        pytest.skip("kicad-cli not found")
    src = REPO / "tests" / "fixtures" / "bb_adc"
    for f in src.iterdir():
        if f.suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru"):
            shutil.copy2(f, tmp_path / f.name)
    pcb = tmp_path / "bb-adc.kicad_pcb"
    r = _report(["--pcb", str(pcb), "--force"])
    assert r["status"] == "pass", r["violations"]
    assert r["enabled_items"] == {"via": 12, "pth": 5, "smd": 0}
    assert r["drops_after"] == 19 == r["drops_wanted"]
    assert r["drc_after"] == r["drc_before"]
