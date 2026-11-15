"""check_rf_match (#488): the populated matching network vs the load it
feeds. Known answers on the rf4 golden's 868 MHz pi (C14 5.6 pF / L2 6.8 nH
/ C15 8.2 pF between the RFM95W and the SMA): designed for 25+j12 ohm it
gives ~19 dB across 863-870 MHz; the planted mutant (C15 82 pF) ~1 dB."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import check_rf_match  # noqa: E402
import rfmatch  # noqa: E402

GOLD = ROOT / "tests" / "golden" / "rf4" / "rf4.kicad_pcb"
MUT = ROOT / "tests" / "golden" / "mutants" / "rf-match-decade"
ENTRY = {"load": "J1", "zin": "25+j12@868M", "band_hz": [863e6, 870e6]}


def _board(tmp_path, *edits):
    text = GOLD.read_text(encoding="utf-8")
    for old, new in edits:
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    p = tmp_path / "rf4.kicad_pcb"
    p.write_text(text, encoding="utf-8")
    return p


def _run(pcb, *entries, base=None):
    return check_rf_match.check(pcb, {"rf_match": list(entries)}, base)


def test_golden_pi_walks_and_passes():
    vios, facts = _run(GOLD, ENTRY)
    assert vios == []
    net = facts["networks"][0]
    assert [(e["ref"], e["where"], e["kind"]) for e in net["elements"]] == [
        ("C15", "shunt", "C"), ("L2", "series", "L"), ("C14", "shunt", "C")]
    assert net["nets"] == ["/RF_FEED", "/RF1"]
    assert net["stop"] == "source" and net["at"] == ["U1"]
    assert 17 < net["rl_min_db"] < 21


def test_planted_mutant_is_caught():
    cons = json.loads((MUT / "constraints.json").read_text())
    vios, facts = check_rf_match.check(MUT / "rf4.kicad_pcb", cons, MUT)
    assert [v["kind"] for v in vios] == ["rf_match_rl"]
    v = vios[0]
    assert "C15" in v["refs"] and "J1" in v["refs"]
    assert v["rl_min_db"] < 3
    assert v["source"] == "check_rf_match" and v["severity"] == "warning"
    assert "rf_synth match --z 25" in v["msg"]


def test_cli_contract(tmp_path):
    cp = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_rf_match.py"),
         "--pcb", str(MUT / "rf4.kicad_pcb"), "--constraints",
         str(MUT / "constraints.json")], capture_output=True, text=True)
    assert cp.returncode == 1
    assert json.loads(cp.stdout)["violations"][0]["kind"] == "rf_match_rl"
    cp = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_rf_match.py"),
         "--pcb", str(GOLD)], capture_output=True, text=True)
    assert cp.returncode == 0
    out = json.loads(cp.stdout)
    assert out["checked"][0]["networks"] == []


def test_wrong_antenna_fails_the_golden_values():
    vios, _ = _run(GOLD, dict(ENTRY, zin="150-j80@868M"))
    assert [v["kind"] for v in vios] == ["rf_match_rl"]
    vios, _ = _run(GOLD, dict(ENTRY, zin="150-j80@868M", rl_db=1))
    assert vios == []


def test_dnp_shunt_is_open_and_zero_ohm_links_join(tmp_path):
    pcb = _board(tmp_path, ('"Value" "5.6pF"', '"Value" "DNP"'))
    _, facts = _run(pcb, ENTRY)
    assert [e["ref"] for e in facts["networks"][0]["elements"]] == \
        ["C15", "L2"]
    pcb = _board(tmp_path, ('"Reference" "L2"', '"Reference" "R2"'),
                 ('"Value" "6.8nH"', '"Value" "0R"'))
    _, facts = _run(pcb, ENTRY)
    net = facts["networks"][0]
    assert [e["ref"] for e in net["elements"]] == ["C15", "C14"]
    assert net["nets"] == ["/RF_FEED", "/RF1"]


def test_unknown_value_and_missing_load(tmp_path):
    pcb = _board(tmp_path, ('"Value" "6.8nH"', '"Value" "L_RF"'))
    vios, _ = _run(pcb, ENTRY)
    assert [(v["kind"], v["refs"]) for v in vios] == [
        ("rf_match_value_unknown", ["L2"])]
    vios, _ = _run(GOLD, dict(ENTRY, load="AE9"))
    assert [v["kind"] for v in vios] == ["rf_match_no_network"]
    # a net with nothing passive on it: no network
    vios, _ = _run(GOLD, {"net": "/MISO", "zin": "50@868M"})
    assert [v["kind"] for v in vios] == ["rf_match_no_network"]


def test_s1p_load_relative_to_constraints(tmp_path):
    freqs = [850e6 + 1e6 * i for i in range(31)]
    lum = rfmatch.Load(z0=25 + 12j, f0=868e6)
    (tmp_path / "ant.s1p").write_text(
        rfmatch.write_s1p(freqs, [lum.z(f) for f in freqs]))
    vios, facts = _run(GOLD, {"load": "J1", "s1p": "ant.s1p",
                              "band_hz": [863e6, 870e6]}, base=tmp_path)
    assert vios == []
    assert facts["networks"][0]["rl_min_db"] == pytest.approx(
        _run(GOLD, ENTRY)[1]["networks"][0]["rl_min_db"], abs=0.05)


def test_bad_constraints_raise():
    import checklib
    for bad in ([{"load": "J1"}], [{"zin": "50@1G"}], "x",
                [{"load": "J1", "zin": "50@1G", "foo": 1}],
                [{"load": "J1", "zin": "-5@1G"}]):
        with pytest.raises(checklib.CheckError):
            check_rf_match.check(GOLD, {"rf_match": bad})


def test_comment_keys_pass_and_typos_still_stop():
    """#1097: a `_why` on an entry, or a `_note` beside the loads in the
    keyed form, passes as constraints_lint passes it; a typo still stops."""
    import checklib
    want = _run(GOLD, ENTRY)[0]
    assert _run(GOLD, dict(ENTRY, _why="868 MHz chip antenna"))[0] == want
    keyed = {"_note": "measured on the VNA", "J1": {
        k: v for k, v in ENTRY.items() if k != "load"}}
    assert check_rf_match.check(GOLD, {"rf_match": keyed})[0] == want
    with pytest.raises(checklib.CheckError, match="unknown keys"):
        _run(GOLD, dict(ENTRY, band_hzz=[1, 2]))


def test_constraints_lint_knows_rf_match(tmp_path):
    p = tmp_path / "constraints.json"
    p.write_text(json.dumps({"rf_match": [ENTRY], "rf_matchx": 1}))
    cp = subprocess.run([sys.executable, str(ROOT / "scripts" /
                                             "constraints_lint.py"),
                         "--file", str(p)], capture_output=True, text=True)
    vios = json.loads(cp.stdout)["violations"]
    msgs = [v["msg"] for v in vios]
    assert all("'rf_match'" not in m.split(" - ")[0] for m in msgs)
    assert any("did you mean 'rf_match'" in m for m in msgs)
    p.write_text(json.dumps({"rf_match": 5}))
    cp = subprocess.run([sys.executable, str(ROOT / "scripts" /
                                             "constraints_lint.py"),
                         "--file", str(p)], capture_output=True, text=True)
    assert any(v.get("kind") == "bad_type" for v in
               json.loads(cp.stdout)["violations"])
