"""bin/score.py's parts that need no KiCad: the pins and the charges
(docs/score.md). The gates themselves run in `bin/hwde score`."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
sys.path.insert(0, str(ROOT / "scripts"))

import score  # noqa: E402


def test_every_pin_matches_its_digest_and_classifies_its_scripts():
    pins = score.pins()
    assert {"hwde-led-test", "lite-main", "core"} <= {p.name for p in pins}
    assert set(score.PINS) == {p.name for p in pins}
    for p in pins:
        assert score.problems(p) == [], p.name


def test_hifi_pins_name_a_commit():
    for p in score.pins():
        if p.project == "hifi-receiver-2":
            assert len(p.commit) == 40 and p.dir == ROOT / "pinned" / p.name


@pytest.fixture
def pin(tmp_path, monkeypatch):
    d = tmp_path / "b"
    (d / "kicad" / "gen").mkdir(parents=True)
    (d / "state.json").write_text("{}")
    (d / "kicad" / "gen" / "root.py").write_text("")
    (d / "kicad" / "gen" / "fix.py").write_text("")
    pf = tmp_path / "b.json"
    pf.write_text(json.dumps({"board": "b", "dir": str(d), "design": ["kicad/gen/root.py"],
                              "stand_in": {"kicad/gen/fix.py": "hwde can't"}}))
    monkeypatch.setattr(score, "ROOT", tmp_path)
    p = score.read_pin(pf)
    monkeypatch.setitem(score.PINS, "b", score.digest(p))
    return p


def test_a_clean_pin_has_no_problems_and_charges_its_stand_ins(pin):
    assert score.problems(pin) == []
    assert score.script_charges(pin) == ["kicad/gen/fix.py"]


def test_an_edited_board_stops_the_score(pin):
    (pin.dir / "state.json").write_text('{"gates": {}}')
    assert any("digest" in m for m in score.problems(pin))


def test_a_new_script_is_charged_until_classified(pin):
    (pin.dir / "kicad" / "gen" / "new.sh").write_text("")
    msgs = score.problems(pin)
    assert any("new.sh is in the board but not classified" in m for m in msgs)


def test_a_dropped_stand_in_row_is_not_a_free_point(pin):
    # taking the row out of the pin without the script leaves it unclassified
    pin.stand_in.clear()
    assert any("fix.py is in the board but not classified" in m for m in score.problems(pin))


def test_gate_charges_count_failing_and_unwaived_warnings():
    w = {"check": "check_ratings", "net": "+5V", "reason": "r", "approved": "x 2026-10-29"}
    result = {
        "failing": [{"check": "check_thermal", "net": "GND"}],
        "warnings": [{"check": "check_ratings", "net": "+5V"},
                     {"check": "check_route_style", "net": "SDA"}],
        "warnings_count": 4,
    }
    c = score.gate_charges(result, [w])
    assert c == {"check_thermal": 1, "check_route_style": 1, "(warnings past the cap)": 2}
    assert sum(score.gate_charges({"failing": [], "warnings": [], "warnings_count": 0}).values()) == 0


def test_an_info_note_costs_nothing():
    # gate.evaluate ships everything below the fail bar as `warnings`, info
    # included. mlcc_dcbias's 50 % screen on a rail it left not_judged (no
    # floor stated, #1067) is info: it must not charge as an unjudged finding
    note = {"check": "mlcc_dcbias", "kind": "cap_dc_bias_low", "severity": "info",
            "net": "+5V", "refs": ["C10"]}
    warn = dict(note, severity="warning", refs=["C11"])
    result = {"failing": [], "warnings": [note, warn], "warnings_count": 2}
    assert score.gate_charges(result) == {"mlcc_dcbias": 1}
    fs, past = score.gate_findings("verify", result)
    assert [f["refs"] for f in fs] == [["C11"]] and past == 0
    j = score.judge(fs, [], {"verify"})
    assert j.charges == 1 and [f["refs"] for f in j.unjudged] == [["C11"]]


def test_a_failed_model_fetch_is_not_judged_and_costs_nothing():
    # dfm's cpl_fetch_failed says EasyEDA didn't answer (rate limit,
    # network): the check could not judge those parts. It must charge 0 and
    # be no catch. A part EasyEDA has no model for (cpl_no_model) is a real
    # finding and still charges as before.
    fetch = {"check": "dfm", "kind": "cpl_fetch_failed", "severity": "error",
             "refs": ["L3"], "reason": "HTTP 429"}
    nomodel = {"check": "dfm", "kind": "cpl_no_model", "severity": "error",
               "refs": ["U2"]}
    result = {"failing": [fetch, nomodel], "warnings": [], "warnings_count": 0}
    fs, past = score.gate_findings("dfm", result)
    assert past == 0 and len(fs) == 2          # both still reported
    j = score.judge(fs, [], {"dfm"})
    assert j.charges == 1
    assert [f["refs"] for f in j.not_judged] == [["L3"]]
    assert [f["refs"] for f in j.unjudged] == [["U2"]]
    assert score.gate_charges(result) == {"dfm": 1}
    # a ledger entry, even one marked real, can't turn it into a catch
    real = dict(score.finding_of("dfm", fetch), verdict="real", reason="r")
    j = score.judge(fs, [real], {"dfm"})
    assert j.real == [] and j.charges == 1 and len(j.not_judged) == 1
    # and the old scorer (no NOT_JUDGED) charged it
    old = score.NOT_JUDGED
    try:
        score.NOT_JUDGED = {}
        assert score.judge(fs, [], {"dfm"}).charges == 2
    finally:
        score.NOT_JUDGED = old


def test_not_judged_says_why_and_score_gates_run_offline(monkeypatch, tmp_path):
    fetch = {"check": "dfm", "kind": "cpl_fetch_failed", "severity": "error",
             "refs": ["L3"], "reason": "offline (HWDE_EASYEDA_OFFLINE): C844983"}
    f = score.finding_of("dfm", fetch)
    assert f["why"] == fetch["reason"]
    # a judged finding carries no why: the ledger match is unchanged
    assert "why" not in score.finding_of("dfm", {**fetch, "kind": "cpl_no_model"})
    # every gate a score runs is offline: the network can't move a score
    seen = {}

    class Done:
        returncode = 0

    def run(cmd, **kw):
        seen.update(kw["env"])
        Path(cmd[cmd.index("--out") + 1]).write_text(
            json.dumps({"failing": [fetch], "warnings": []}))
        return Done()
    monkeypatch.setattr(score.subprocess, "run", run)
    monkeypatch.delenv("HWDE_EASYEDA_OFFLINE", raising=False)
    monkeypatch.delenv("HWDE_STOCK_OFFLINE", raising=False)
    monkeypatch.setattr(score, "board_file", lambda ws, n, ext: ws / f"b{ext}")
    pin = type("P", (), {"name": "b"})()
    fs, _ = score.run_gate(pin, tmp_path, "dfm", tmp_path)
    assert seen["HWDE_EASYEDA_OFFLINE"] == "1" and seen["HWDE_STOCK_OFFLINE"] == "1"
    assert fs[0]["why"].startswith("offline")


def test_gate_charges_take_a_plain_string_warning():
    # a gate may report a warning (or a failing finding) as a plain string
    result = {"failing": ["board has no outline"],
              "warnings": ["3 3D model(s) missing", {"check": "check_thermal"}],
              "warnings_count": 3}
    w = {"check": "check_thermal", "net": None, "reason": "r", "approved": "x 2026-10-29"}
    assert score.gate_charges(result, [w]) == {"?": 2, "(warnings past the cap)": 1}


def test_a_finding_is_named_by_where_it_is_not_its_wording():
    f = score.finding_of("verify", {"check": "check_current", "net": "+3V3", "refs": ["R2", "C1"],
                                    "pos": [10.00004, 20.5], "msg": "0.3 mm carries 0.8 A",
                                    "kind": "undersized_track"})
    assert f == {"gate": "verify", "check": "check_current", "kind": "undersized_track",
                 "net": "+3V3", "refs": ["C1", "R2"], "pos": [10.0, 20.5]}
    # two findings at one place differ by kind
    g = score.finding_of("verify", {"check": "check_current", "net": "+3V3", "refs": ["R2", "C1"],
                                    "pos": [10.0, 20.5], "kind": "neck"})
    assert not score.matches({**f, "verdict": "real"}, g)
    assert score.finding_of("verify", {"check": "verify_all", "msg": "twin drift"})["message"] \
        == "twin drift"
    # a plain string, or a finding with nowhere to be, goes by its message
    assert score.finding_of("erc", "pin types: 3 untyped")["message"] == "pin types: 3 untyped"


def _f(**kw):
    return score.finding_of(kw.pop("gate", "verify"), {"check": "check_route_style", **kw})


def test_real_findings_cost_nothing_false_and_unjudged_cost_one_missed_real_costs_one():
    real = {**_f(net="SDA", pos=[1, 2]), "verdict": "real", "reason": "a staircase"}
    gone = {**_f(net="SCL", pos=[3, 4]), "verdict": "real", "reason": "a staircase"}
    false = {**_f(net="GND", pos=[5, 6]), "verdict": "false", "reason": "a pour neck"}
    found = [_f(net="SDA", pos=[1.01, 2]), _f(net="GND", pos=[5, 6]), _f(net="X", pos=[0, 0])]
    j = score.judge(found, [real, gone, false])
    assert (len(j.real), len(j.missed), len(j.false), len(j.unjudged)) == (1, 1, 1, 1)
    assert j.charges == 3
    # a sharper check that finds a new real defect, once it is in the ledger, costs nothing
    new = {**found[2], "verdict": "real", "reason": "r"}
    assert score.judge(found, [real, gone, false, new]).charges == 2
    # a looser check that stops reporting known defects is charged for each
    assert score.judge([], [real, gone, false]).charges == 2


def test_one_entry_is_one_finding_and_a_crashed_gate_misses_nothing():
    e = {**_f(net="SDA"), "verdict": "real", "reason": "r"}
    assert score.judge([_f(net="SDA"), _f(net="SDA")], [e]).charges == 1
    erc = {**score.finding_of("erc", "x"), "verdict": "real", "reason": "r"}
    assert score.judge([], [erc], ran={"verify"}).charges == 0
    assert score.judge([], [erc], ran={"erc"}).charges == 1


def test_the_ledger_is_in_the_digest_and_a_verdict_needs_a_reason(pin):
    k = score.known_path(pin)
    k.parent.mkdir(parents=True)
    k.write_text(json.dumps({"known": [{**_f(net="A"), "verdict": "real", "reason": ""}]}))
    msgs = score.problems(pin)
    assert any("digest" in m for m in msgs) and any("no reason" in m for m in msgs)
    k.write_text(json.dumps({"known": [{**_f(net="A"), "verdict": "maybe"}]}))
    assert any("verdict of" in m for m in score.problems(pin))


def test_draft_known_adds_only_what_the_ledger_lacks(pin):
    assert score.draft_known(pin, [_f(net="A"), _f(net="B")]) == 2
    known = score.read_known(pin)
    assert [e["verdict"] for e in known] == ["?", "?"]
    known[0].update(verdict="real", reason="r")
    score.known_path(pin).write_text(json.dumps({"known": known}))
    assert score.draft_known(pin, [_f(net="A"), _f(net="B"), _f(net="C")]) == 1
    assert [e["verdict"] for e in score.read_known(pin)] == ["real", "?", "?"]


def test_digest_ignores_kicad_local_state(tmp_path):
    """kicad-cli leaves an ignored hwde-led-test.kicad_prl (and lock/autosave
    files) beside any board it opens; a pin's digest must not move for them."""
    (tmp_path / "a.kicad_pcb").write_text("x", encoding="utf-8")
    before = score.files(tmp_path)
    for name in ("a.kicad_prl", "~a.kicad_pcb.lck", "_autosave-a.kicad_pcb",
                 "fp-info-cache"):
        (tmp_path / name).write_text("local", encoding="utf-8")
    assert score.files(tmp_path) == before == ["a.kicad_pcb"]


def test_digest_ignores_the_learnings_log_but_not_the_design(tmp_path):
    """#966 ruled rows in pinned/ultra-front/learnings/ and every score on
    main stopped on a digest mismatch. The learnings log is not the design;
    a file under a design folder must still move the digest."""
    (tmp_path / "a.kicad_pcb").write_text("x", encoding="utf-8")
    before = score.files(tmp_path)
    (tmp_path / "learnings").mkdir()
    (tmp_path / "learnings" / "queue.yaml").write_text("- row", encoding="utf-8")
    assert score.files(tmp_path) == before
    (tmp_path / "kicad" / "learnings").mkdir(parents=True)
    (tmp_path / "kicad" / "learnings" / "x.kicad_sch").write_text("s", encoding="utf-8")
    assert score.files(tmp_path) == ["a.kicad_pcb", "kicad/learnings/x.kicad_sch"]


def _parts(pin, rows):
    (pin.dir / "parts").mkdir(exist_ok=True)
    (pin.dir / "parts" / "parts.json").write_text(json.dumps({"parts": rows}))


def test_a_pin_with_bare_parts_rows_is_refused(pin):
    """Planted fault (#834): core's parts.json rows once carried empty
    attributes, so check_ratings could rate no cap and called 139 parts a
    coverage gap. A pinned board with such rows must stop the score."""
    _parts(pin, [{"refdes": ["C1"], "lcsc": "C1525", "attributes": {"Voltage Rated": "50V"}},
                 {"refdes": ["C2"], "lcsc": "C2286", "attributes": {}},
                 {"refdes": ["C3"], "lcsc": "C9"}])
    assert score.bare_rows(pin) == ["C2286", "C9"]
    assert any("C2286" in m and "parts_enrich" in m for m in score.problems(pin))


def test_a_bare_row_the_pin_names_with_a_reason_is_allowed(pin):
    _parts(pin, [{"refdes": ["U1"], "lcsc": "C42", "attributes": {}}])
    assert score.bare_rows(pin) == ["C42"]
    pin.bare_parts["C42"] = "JLC search has no listing for this code"
    assert score.bare_rows(pin) == []
    pin.bare_parts["C42"] = " "
    assert score.bare_rows(pin) == ["C42"]


# ------------------------------------------------- foreign boards (#403)

def _foreign_pins():
    return [p for p in sorted((ROOT / "pinned").glob("*.json"))
            if "source" in json.loads(p.read_text(encoding="utf-8"))]


def test_foreign_pins_name_their_open_source_and_errata():
    pins = _foreign_pins()
    assert len(pins) >= 2
    for pf in pins:
        src = json.loads(pf.read_text(encoding="utf-8"))["source"]
        assert src["url"].startswith("https://github.com/") and src["license"]
        assert src["errata"]
        d = ROOT / "pinned" / pf.stem
        assert any(n.name.upper().startswith(("LICENSE", "LICENCE", "COPYING"))
                   for n in d.iterdir()), pf.stem
        assert len(list((d / "kicad").glob("*.kicad_pro"))) == 1


def test_every_erratum_is_held_real_with_its_source():
    for pf in _foreign_pins():
        led = json.loads((ROOT / "pinned" / "known" / pf.name).read_text(encoding="utf-8"))
        assert led["board"] == pf.stem
        errata = [e for e in led["known"] if "source" in e]
        assert errata, pf.stem
        for e in errata:
            assert e["verdict"] == "real"
            assert e["source"].startswith("https://github.com/") and "/blob/" in e["source"]


def test_foreign_pin_copies_only_kicad_sources_and_the_licence(tmp_path):
    import subprocess
    import pin as pinmod
    assert pinmod.kicad_source("a/b.kicad_pcb") and pinmod.kicad_source("fp-lib-table")
    assert not pinmod.kicad_source("a/b.step") and not pinmod.kicad_source("README.md")
    repo = tmp_path / "repo"
    (repo / "hw" / "lib").mkdir(parents=True)
    for rel, body in {"LICENSE": "MIT", "README.md": "x", "hw/x.kicad_pro": "{}",
                      "hw/x.kicad_pcb": "(kicad_pcb)", "hw/x.step": "big",
                      "hw/lib/p.kicad_mod": "(footprint)"}.items():
        (repo / rel).write_text(body)
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(git[:3] + ["init", "-q"], check=True)
    subprocess.run(git + ["add", "."], check=True)
    subprocess.run(git + ["commit", "-qm", "x"], check=True)
    sha = subprocess.run(git[:3] + ["rev-parse", "HEAD"], check=True,
                         capture_output=True, text=True).stdout.strip()
    dest = tmp_path / "pinned" / "x"
    pinmod.foreign(repo, sha, "hw", "x", dest)
    got = sorted(str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file())
    assert got == ["LICENSE", "kicad/lib/p.kicad_mod", "kicad/x.kicad_pcb",
                   "kicad/x.kicad_pro", "state.json"]
    st = json.loads((dest / "state.json").read_text())
    assert st["workspace"] == "pinned/x"


def test_foreign_pin_keeps_the_cpl_it_shipped_as_fab_cpl(tmp_path):
    """--cpl: the pick-and-place file the project sent its assembler is pinned
    byte for byte as fab/CPL.csv (oasis v0.2's positions.csv, with its BOM
    mark), where the dfm gate checks its rotations."""
    import subprocess
    import pin as pinmod
    repo = tmp_path / "repo"
    (repo / "hw" / "fab").mkdir(parents=True)
    body = "﻿Designator,Mid X,Mid Y,Layer,Rotation\nJ1,1,-1,top,90\n"
    for rel, text in {"LICENSE": "MIT", "hw/x.kicad_pro": "{}",
                      "hw/x.kicad_pcb": "(kicad_pcb)",
                      "hw/fab/positions.csv": body}.items():
        (repo / rel).write_text(text, encoding="utf-8")
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(git[:3] + ["init", "-q"], check=True)
    subprocess.run(git + ["add", "."], check=True)
    subprocess.run(git + ["commit", "-qm", "x"], check=True)
    sha = subprocess.run(git[:3] + ["rev-parse", "HEAD"], check=True,
                         capture_output=True, text=True).stdout.strip()
    dest = tmp_path / "pinned" / "x"
    pinmod.foreign(repo, sha, "hw", "x", dest, cpl="hw/fab/positions.csv")
    assert (dest / "fab" / "CPL.csv").read_bytes() == body.encode("utf-8")
    # the CSV is no KiCad source: only the --cpl copy lands in the pin
    assert not (dest / "kicad" / "fab").exists()


def test_a_pinned_shipped_cpl_keeps_its_models():
    """oasis-v0-2 ships its CPL; the LCSC models its dfm run needs are pinned
    beside it, so a scratch copy never asks EasyEDA (rate-limited) again."""
    d = ROOT / "pinned" / "oasis-v0-2"
    src = json.loads((ROOT / "pinned" / "oasis-v0-2.json").read_text())["source"]
    assert src["cpl"].endswith("positions.csv") and (d / "fab" / "CPL.csv").is_file()
    assert len(list((d / "fab" / "easyeda").glob("C*.json"))) >= 20


def test_the_planted_faults_run_offline_too(monkeypatch):
    """score's own process (the planted faults' dfm) never fetches either."""
    monkeypatch.delenv("HWDE_EASYEDA_OFFLINE", raising=False)
    monkeypatch.delenv("HWDE_STOCK_OFFLINE", raising=False)
    monkeypatch.setattr(score, "pins", lambda: ["p"])
    monkeypatch.setattr(score, "problems", lambda p: [])
    seen = {}

    def planted():
        seen.update(os.environ)
        raise RuntimeError("stop here")
    monkeypatch.setattr(score, "planted", planted)
    monkeypatch.setattr(sys, "argv", ["score.py"])
    assert score.main() == 1
    assert seen["HWDE_EASYEDA_OFFLINE"] == "1" and seen["HWDE_STOCK_OFFLINE"] == "1"
