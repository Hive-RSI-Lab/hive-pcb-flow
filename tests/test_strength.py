"""bin/strength.py and the score's use of it: hwde's planted faults are all
caught on this tree, a check loosened lets one through, and the score
charges CRASH for each one let through.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "bin"))

import score  # noqa: E402
import strength  # noqa: E402


def _missed(fs):
    return [name for _, name, ok in fs if not ok]


def test_every_planted_fault_is_caught():
    fs = strength.faults()
    assert _missed(fs) == []
    steps = {s for s, _, _ in fs}
    assert steps == set(strength.FAILS_ON), steps     # each gate the score counts
    assert sum(s == "verify" for s, _, _ in fs) >= 16
    assert sum(s == "dfm" for s, _, _ in fs) >= (1 + len(strength.DRILL_FAULTS)
                                                 + len(strength.INNER_FAULTS))


@pytest.fixture(scope="module")
def found():
    """The findings the verify and dfm legs' planted faults raise, once."""
    got: dict = {}
    strength.verify_faults(got)
    strength.dfm_faults(got)
    return got


def test_a_gate_that_stops_failing_warnings_lets_one_through(monkeypatch, found):
    import gate
    real = gate.load_gates

    def loose(path):
        g = real(path)
        g["erc"]["fail_severities"] = ["error"]
        g["dfm"]["max_count"] = 5
        return g
    monkeypatch.setattr(gate, "load_gates", loose)
    assert set(_missed(strength.gate_faults(found))) == {
        "gate erc: erc unconnected_wire_endpoint (warning)",
        "gate dfm: check.dfm dfm_hole_to_hole (error)",
        "gate dfm: check.dfm dfm_hole_size (error)",
        "gate dfm: check.dfm dfm_hole_to_inner_copper (error)"}


def _seen(monkeypatch):
    """Every report gate.evaluate is handed, by gate."""
    import gate
    real, seen = gate.evaluate, []

    def spy(name, g, report, *a, **k):
        seen.append((name, report))
        return real(name, g, report, *a, **k)
    monkeypatch.setattr(gate, "evaluate", spy)
    return seen


def test_planted_findings_are_ones_real_runs_wrote(monkeypatch, found):
    seen = _seen(monkeypatch)
    fs = strength.gate_faults(found)
    assert _missed(fs) == [] and len(seen) == len(fs)
    assert {n for n, _ in seen} == set(strength.FAILS_ON)
    assert len(seen) >= 25
    real = {n: strength._corpus(c)["violations"] for n, c in strength.GATE_BASE.items()}
    real.update(found)
    for name, report in seen:
        assert "planted" not in json.dumps(report).lower(), name
        base = json.loads((strength.ROOT / strength.BASE_REPORT[name]).read_text()) \
            if name in strength.BASE_REPORT else {"violations": []}
        new = [v for v in report["violations"] if v not in base["violations"]]
        assert len(new) == 1 and new[0] in real[name], name
        assert report["counts"]["total"] == len(report["violations"])
        assert set(report) >= set(base) - {"violations"}


def test_a_gate_py_that_skips_the_old_marker_lets_nothing_through(monkeypatch, found):
    """The planted findings used to carry kind and msg 'planted'; a gate.py
    skipping those now skips nothing."""
    import gate
    real = gate.evaluate

    def skip(name, g, report, *a, **k):
        report = {**report, "violations": [
            v for v in report["violations"]
            if "planted" not in (str(v.get("kind")) + str(v.get("msg"))).lower()]}
        return real(name, g, report, *a, **k)
    monkeypatch.setattr(gate, "evaluate", skip)
    assert _missed(strength.gate_faults(found)) == []


def test_a_gate_py_that_skips_a_finding_drops_the_score(monkeypatch, capsys, found):
    """To let a planted finding through, gate.py has to skip that finding
    itself, as it would on a board: it costs CRASH."""
    import gate
    real = gate.evaluate

    def skip(name, g, report, *a, **k):
        report = {**report, "violations": [
            v for v in report["violations"] if v.get("check") != "power_pin_not_driven"]}
        return real(name, g, report, *a, **k)
    _quick(monkeypatch)
    monkeypatch.setattr(strength, "faults", lambda: strength.gate_faults(found))
    assert score.main() == 0
    assert capsys.readouterr().out.splitlines()[-1] == "0"
    monkeypatch.setattr(gate, "evaluate", skip)
    assert score.main() == 0
    out = capsys.readouterr().out.splitlines()
    assert "planted fault let through: gate erc: erc power_pin_not_driven (error) " \
        f"(CRASH {score.CRASH})" in out
    assert out[-1] == str(-score.CRASH)


def test_a_lowered_jlc_floor_lets_the_drill_faults_through(tmp_path, monkeypatch):
    import dfm_check
    caps = yaml.safe_load(dfm_check.CAPABILITIES.read_text(encoding="utf-8"))

    def lower(d):
        for k, v in d.items():
            if isinstance(v, dict):
                lower(v)
            elif k in ("min_hole_to_hole_mm", "abs_min_hole_to_hole_mm"):
                d[k] = 0.35
            elif k in ("min_hole_diameter_mm", "abs_min_hole_diameter_mm"):
                d[k] = 0.1
    lower(caps)
    p = tmp_path / "caps.yaml"
    p.write_text(yaml.safe_dump(caps), encoding="utf-8")
    monkeypatch.setattr(dfm_check, "CAPABILITIES", p)
    assert set(_missed(strength.dfm_faults())) == {
        "dfm hole-to-hole-0.40 (dfm_hole_to_hole)", "dfm drill-0.12 (dfm_hole_size)"}


def test_a_check_with_no_planted_fault_is_a_miss(monkeypatch):
    import score_checks
    real = score_checks.load_manifest

    def drop(*a, **k):
        m = real(*a, **k)
        m["mutants"] = {n: r for n, r in m["mutants"].items()
                        if r["check"] != "check_courtyard"}
        return m
    monkeypatch.setattr(score_checks, "load_manifest", drop)
    assert _missed(strength.verify_faults()) == ["verify check_courtyard: no planted fault"]


def test_planted_drill_hole_sits_clear_of_the_others():
    drl = "M48\nMETRIC\nT1C0.300\n%\nG90\nT1\nX0.0Y0.0\nX10.0Y0.0\nM30\n"
    out = strength._plant(drl, {"pair": 0.45}).splitlines()
    assert "T2C0.300" in out[:out.index("%")]
    assert out[-3:] == ["T2", "X0.750Y0.000", "M30"]


def _quick(monkeypatch):
    """score.main with the boards stubbed: each measures 0 charges."""
    monkeypatch.setattr(sys, "argv", ["score"])
    monkeypatch.setattr(score, "measure", lambda p, ws, logs, *a: 0)


def test_score_charges_crash_per_let_through_fault(monkeypatch, capsys):
    _quick(monkeypatch)
    assert score.main() == 0
    out = capsys.readouterr().out.splitlines()
    assert out[-1] == "0" and out[0].startswith("planted faults caught: ")
    monkeypatch.setattr(strength, "faults", lambda: [
        ("verify", "verify x (check_x)", False), ("dfm", "dfm y", True)])
    assert score.main() == 0
    out = capsys.readouterr().out.splitlines()
    assert f"planted fault let through: verify x (check_x) (CRASH {score.CRASH})" in out
    assert "planted faults caught: 1/2" in out
    assert out[-1] == str(-score.CRASH)


def test_score_cannot_be_read_when_the_faults_cannot_run(monkeypatch, capsys):
    _quick(monkeypatch)

    def boom():
        raise FileNotFoundError("manifest.yaml")
    monkeypatch.setattr(strength, "faults", boom)
    assert score.main() == 1
    assert "could not run" in capsys.readouterr().err


def test_an_expected_severity_must_match():
    """#649: a waived warning at the planted spot is not the catch; a
    mutant may ask for the error itself."""
    import score_checks
    v = {"kind": "creepage", "refs": ["J1"], "severity": "warning"}
    assert score_checks.catches({"kind": "creepage", "ref": "J1"}, v)
    assert not score_checks.catches({"kind": "creepage", "severity": "error"}, v)
    assert score_checks.catches({"severity": "error"}, {**v, "severity": "error"})


def test_dfm_faults_leave_the_fixtures_as_frozen(monkeypatch):
    """The planted dfm faults run on copies: a dfm run that keeps what it
    learns beside the CPL (a model, EasyEDA's "no model") must not write it
    into a pinned bench fixture (#1348)."""
    import yaml
    seen = []
    monkeypatch.setattr(strength, "_dfm", lambda fx, g: seen.append(Path(g)) or [])
    monkeypatch.setattr(strength, "inner_faults", lambda *a, **k: [])
    strength.dfm_faults()
    stages = strength.ROOT / "tests" / "fixtures" / "stages"
    frozen = {(strength.ROOT / fx["dirs"]["gerbers"]["path"]).resolve()
              for fx in yaml.safe_load((stages / "manifest.yaml").read_text(
                  encoding="utf-8"))["fixtures"].values()
              if fx.get("dirs", {}).get("gerbers")}
    assert seen and not any(g.resolve() in frozen for g in seen)
