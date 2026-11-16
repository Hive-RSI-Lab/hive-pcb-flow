"""The board's fact ledger: every fact it relies on is verified with a source,
failed, or unknown - and release refuses while any is not verified.

The person, 10-28: "know what we dont know". The hi-fi core board reached the
BOM review with J1 oversold and U8 swapped; its ledger would have listed both
as failed before any order step.
"""
from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))
import ledger as ledger_cli  # noqa: E402
import ledgerlib  # noqa: E402
import stock_check  # noqa: E402
from test_stock_check import fake_fetch  # noqa: E402


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setenv("HWDE_LEDGER_WRITE", "1")
    ws = tmp_path / "boards" / "core"
    (ws / "parts").mkdir(parents=True)
    (ws / "fab").mkdir()
    (ws / "state.json").write_text("{}", encoding="utf-8")
    (ws / "parts" / "parts.json").write_text(json.dumps({"parts": [
        {"refdes": ["C1", "C2", "C3"], "lcsc": "C1525", "value": "100nF",
         "refdes_class": {"C3": "dnp"}},
        {"refdes": ["U8"], "lcsc": "C84681", "value": "CH340C",
         "mpn": "CH340C"},
        {"refdes": ["J1"], "lcsc": "C5184243", "mpn": "USB4105-GF-A-120",
         "value": "USB4105-GF-A-120"},
        {"refdes": ["J9"], "value": "screw", "assembly_class":
         "customer_supplied"},
    ]}), encoding="utf-8")
    (ws / "fab" / "BOM.csv").write_text(
        "Comment,Designator,Footprint,LCSC Part #\n"
        "100nF,\"C1,C2\",C0402,C1525\n"
        "CH340C,U8,SOP-16,C84681\n"
        "USB4105-GF-A-120,J1,USB-C,C5184243\n", encoding="utf-8")
    return ws


def test_expected_lists_what_the_board_relies_on(ws):
    exp = ledgerlib.expected(ws)
    kinds = lambda s: sorted(e["kind"] for e in exp.values()  # noqa: E731
                             if e["subject"] == s)
    assert kinds("C84681") == ["lifecycle", "part_match", "stock"]
    assert kinds("U8") == ["footprint", "pinout", "rating"]
    assert kinds("C1") == ["footprint", "rating"]       # two pins: no pinout
    assert kinds("C3") == [] and kinds("J9") == []      # dnp, not bought


def test_nothing_recorded_is_all_unknown_and_blocks(ws):
    rep = ledgerlib.report(ws)
    assert rep["ready"] is False and not rep["failed"]
    assert rep["counts"] == {"unknown": len(ledgerlib.expected(ws))}
    assert any("stock" in b and "unknown" in b
               for b in ledgerlib.blockers(ws))


def test_stock_check_writes_stock_match_and_lifecycle(ws):
    res = stock_check.check_rows(stock_check.read_bom(ws / "fab" / "BOM.csv"),
                                 boards=5, fetch=fake_fetch(), jobs=1)
    assert ledgerlib.record_stock(ws, res) == ws / "ledger.json"
    data = json.loads((ws / "ledger.json").read_text(encoding="utf-8"))
    f = data["facts"]
    assert f["C1525/stock"]["status"] == "verified"
    assert f["C1525/part_match"]["status"] == "verified"
    assert f["C5184243/stock"]["status"] == "failed"
    assert "-372" in f["C5184243/stock"]["value"]
    assert f["C84681/lifecycle"]["status"] == "failed"
    assert "C7464026" in f["C84681/lifecycle"]["value"]
    assert f["C1525/stock"]["source"] and f["C1525/stock"]["checked_at"]
    # the file lists the gaps too, not only what was checked
    assert f["U8/pinout"]["status"] == "unknown"
    blk = ledgerlib.blockers(ws)
    assert any(b.startswith("ledger: 1 lifecycle fact(s) failed - C84681")
               for b in blk)
    assert any("stock fact(s) failed - C5184243" in b for b in blk)


def test_stock_goes_stale(ws):
    res = stock_check.check_rows(
        [{"Comment": "100nF", "Designator": "C1,C2", "LCSC": "C1525"}],
        fetch=fake_fetch(), jobs=1)
    ledgerlib.record_stock(ws, res)
    fact = ledgerlib.load(ws)["facts"]["C1525/stock"]
    later = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=73)
    assert ledgerlib.effective(fact, None)["status"] == "verified"
    eff = ledgerlib.effective(fact, None, at=later)
    assert eff["status"] == "unknown" and "stale" in eff["why"]


def test_fact_about_a_replaced_part_reads_unknown(ws):
    ledgerlib.put(ws, [{"subject": "U8", "kind": "pinout",
                        "status": "verified", "lcsc": "C84681",
                        "source": "CH340 datasheet p.4"}], "hand")
    exp = ledgerlib.expected(ws)["U8/pinout"]
    assert ledgerlib.effective(ledgerlib.load(ws)["facts"]["U8/pinout"],
                               exp)["status"] == "verified"
    eff = ledgerlib.effective(ledgerlib.load(ws)["facts"]["U8/pinout"],
                              {**exp, "lcsc": "C7464026"})
    assert eff["status"] == "unknown" and "C7464026" in eff["why"]


def test_cli_set_needs_a_source_and_report_exits_on_gaps(ws, capsys):
    assert ledger_cli.main(["set", "--workspace", str(ws), "--subject", "U8",
                            "--kind", "pinout", "--status", "verified"]) == 2
    assert ledger_cli.main(["set", "--workspace", str(ws), "--subject", "U8",
                            "--kind", "pinout", "--status", "verified",
                            "--source", "CH340 datasheet v3B p.4"]) == 0
    capsys.readouterr()
    assert ledger_cli.main(["report", "--workspace", str(ws)]) == 1
    rep = json.loads(capsys.readouterr().out)
    assert rep["ready"] is False
    assert rep["by_kind"]["pinout"]["verified"] == 1


def _ds_ws(ws, entry=None, pdf=None):
    """U8's part (its fixture LCSC) with a datasheet index entry and PDF."""
    lcsc = ledgerlib.expected(ws)["U8/pinout"]["lcsc"]
    d = ws / "parts" / "datasheets"
    d.mkdir(parents=True, exist_ok=True)
    if entry is not None:
        (d / "index.json").write_text(json.dumps({lcsc: entry}))
    if pdf is not None:
        (d / f"{lcsc}.pdf").write_bytes(pdf)
    return lcsc


_SET = ["set", "--subject", "U8", "--kind", "pinout", "--status", "verified",
        "--source", "CH340 datasheet v3B p.4"]


def _set(ws, *extra):
    return ledger_cli.main([_SET[0], "--workspace", str(ws), *_SET[1:], *extra])


def test_cli_set_refuses_verified_on_a_suspect_datasheet(ws, capsys):
    lcsc = _ds_ws(ws, {"status": "suspect", "reasons": ["no text layer"]})
    assert _set(ws) == 2
    err = json.loads(capsys.readouterr().out)["error"]
    assert lcsc in err and "no text layer" in err and "--force" in err
    assert "U8/pinout" not in {k for k, f in ledgerlib.load(ws)["facts"].items()
                               if f.get("status") == "verified"}


def test_cli_set_refuses_verified_on_a_stub_datasheet(ws, capsys):
    from test_datasheet_fetch import NOTICE
    _ds_ws(ws, None, NOTICE)
    assert _set(ws) == 2
    assert "placeholder" in json.loads(capsys.readouterr().out)["error"]


def test_cli_set_force_needs_a_reason_and_records_it(ws, capsys):
    _ds_ws(ws, {"status": "suspect", "reasons": ["no text layer"]})
    assert _set(ws, "--force", "  ") == 2
    capsys.readouterr()
    assert _set(ws, "--force", "read the scan by eye, p.4") == 0
    f = ledgerlib.load(ws)["facts"]["U8/pinout"]
    assert f["status"] == "verified"
    assert f["forced"] == "read the scan by eye, p.4"
    assert "no text layer" in f["forced_over"]


def test_cli_set_allows_failed_unknown_and_other_kinds_on_a_suspect_datasheet(ws):
    lcsc = _ds_ws(ws, {"status": "suspect", "reasons": ["no text layer"]})
    base = ["set", "--workspace", str(ws), "--subject", "U8", "--kind", "pinout"]
    assert ledger_cli.main([*base, "--status", "failed", "--source", "p.4"]) == 0
    assert ledger_cli.main([*base, "--status", "unknown"]) == 0
    # a stock count does not come from the datasheet
    assert ledger_cli.main(["set", "--workspace", str(ws), "--subject", lcsc,
                            "--kind", "stock", "--status", "verified",
                            "--source", "JLC page 2026-11-05"]) == 0


def test_cli_set_allows_verified_on_an_ok_or_unlisted_datasheet(ws):
    _ds_ws(ws, {"status": "ok", "reasons": []})
    assert _set(ws) == 0
    (ws / "parts" / "datasheets" / "index.json").unlink()
    assert _set(ws) == 0


def test_cli_set_refuses_a_source_naming_a_suspect_pdf(ws, capsys):
    from test_datasheet_fetch import NOTICE
    d = ws / "parts" / "datasheets"
    d.mkdir(parents=True)
    (d / "C999.pdf").write_bytes(NOTICE)
    assert ledger_cli.main(["set", "--workspace", str(ws), "--subject", "U8",
                            "--kind", "footprint", "--status", "verified",
                            "--source", "parts/datasheets/C999.pdf p.2"]) == 2
    assert "C999" in json.loads(capsys.readouterr().out)["error"]


def test_every_fact_verified_is_ready(ws):
    facts = [{**e, "status": "verified", "source": "test"}
             for e in ledgerlib.expected(ws).values()]
    ledgerlib.put(ws, facts, "test")
    assert ledgerlib.report(ws)["ready"] is True
    assert ledgerlib.blockers(ws) == []


def test_writes_nothing_outside_a_workspace_or_when_off(ws, monkeypatch):
    (ws / "state.json").unlink()
    assert ledgerlib.put(ws, [], "x") is None
    (ws / "state.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HWDE_LEDGER_WRITE", "0")
    assert ledgerlib.put(ws, [], "x") is None
    assert not (ws / "ledger.json").exists()


def test_release_refuses_on_ledger_gaps(tmp_path, monkeypatch):
    """attest build refuses while a fact is not verified; once all are, it
    issues, and verify() turns invalid again when stock goes stale."""
    import releaselib
    from test_attest import green_ws
    monkeypatch.setenv("HWDE_LEDGER_WRITE", "1")
    w = green_ws(tmp_path)
    att, problems = releaselib.build(w)
    assert att is not None, problems          # no parts: nothing relied on
    (w / "fab" / "attestation.json").unlink(missing_ok=True)
    (w / "parts").mkdir(exist_ok=True)
    (w / "parts" / "parts.json").write_text(json.dumps({"parts": [
        {"refdes": ["U8"], "lcsc": "C84681", "mpn": "CH340C"}]}))
    (w / "fab" / "BOM.csv").write_text(
        "Comment,Designator,Footprint,LCSC Part #\nCH340C,U8,SOP-16,C84681\n")
    att, problems = releaselib.build(w)
    assert att is None
    assert any(p.startswith("ledger: 1 stock fact(s) unknown - C84681")
               for p in problems)
    facts = [{**e, "status": "verified", "source": "test"}
             for e in ledgerlib.expected(w).values()]
    ledgerlib.put(w, facts, "test")
    att, problems = releaselib.build(w)
    assert att is not None, problems
    releaselib.write_attestation(w, att)
    assert releaselib.verify(w)["valid"] is True
    data = ledgerlib.load(w)
    data["facts"]["C84681/stock"]["checked_at"] = "2020-01-01T00:00:00+00:00"
    (w / "ledger.json").write_text(json.dumps(data))
    v = releaselib.verify(w)
    assert v["valid"] is False
    assert any("stock fact(s) unknown" in p for p in v["problems"])


# ------------------------------------------- listed: no datasheet read (#781)

def _listed(e):
    return {**e, "status": "listed", "source": "parts.json attributes of C1",
            "rated_from": "distributor"}


def test_listed_is_kept_and_does_not_block(ws):
    exp = ledgerlib.expected(ws)
    ledgerlib.put(ws, [_listed(e) if e["kind"] == "rating"
                       else {**e, "status": "verified", "source": "test"}
                       for e in exp.values()], "check_ratings")
    rep = ledgerlib.report(ws)
    assert rep["ready"] is True and ledgerlib.blockers(ws) == []
    rows = {r["subject"]: r for r in rep["listed"]}
    assert "U8" in rows and rows["U8"]["rated_from"] == "distributor"
    assert rep["by_kind"]["rating"].get("verified") is None


def test_listed_still_goes_unknown_when_the_part_changes(ws):
    e = ledgerlib.expected(ws)["U8/rating"]
    ledgerlib.put(ws, [{**_listed(e), "lcsc": "C999"}], "check_ratings")
    assert ledgerlib.report(ws)["by_kind"]["rating"].get("listed") is None
    assert any("U8" in b for b in ledgerlib.blockers(ws))


@pytest.mark.parametrize("by,source,reads", [
    ("check_ratings", "parts.json attributes of C84681", "listed"),
    ("check_ratings", "parts/C84681.pdf", "verified"),
    ("hand", "parts.json attributes of C84681", "verified")])
def test_a_ledger_written_before_the_tier_reads_honestly(ws, by, source, reads):
    """A ledger on disk is not rewritten until check_ratings runs again, and
    until then what it says about an attribute number must not stand."""
    e = ledgerlib.expected(ws)["U8/rating"]
    ledgerlib.put(ws, [{**e, "status": "verified", "source": source}], by)
    row = next(r for r in ledgerlib.report(ws)["facts"]
               if r["key"] == "U8/rating")
    assert row["status"] == reads, row
    if reads == "listed":
        assert row["rated_from"] == "distributor" and "#781" in row["why"]


def test_attest_issues_over_listed_and_says_which(tmp_path, monkeypatch):
    """Release: listed doesn't refuse, and the result names the facts."""
    import attest
    import releaselib
    from test_attest import green_ws
    monkeypatch.setenv("HWDE_LEDGER_WRITE", "1")
    w = green_ws(tmp_path)
    (w / "parts").mkdir(exist_ok=True)
    (w / "parts" / "parts.json").write_text(json.dumps({"parts": [
        {"refdes": ["U8"], "lcsc": "C84681", "mpn": "CH340C"}]}))
    (w / "fab" / "BOM.csv").write_text(
        "Comment,Designator,Footprint,LCSC Part #\nCH340C,U8,SOP-16,C84681\n")
    ledgerlib.put(w, [_listed(e) if e["kind"] == "rating"
                      else {**e, "status": "verified", "source": "test"}
                      for e in ledgerlib.expected(w).values()], "check_ratings")
    att, problems = releaselib.build(w)
    assert att is not None, problems
    res, _, code = attest.run(["build", "--workspace", str(w)])
    assert code == 0 and res["status"] == "pass", res
    assert res["listed_ledger_facts"] == ["U8/rating"]
    v, _, _ = attest.run(["verify", "--workspace", str(w)])
    assert v.get("listed_ledger_facts") == ["U8/rating"]


def test_cli_report_lists_listed_and_set_takes_it(ws, capsys):
    assert ledger_cli.main([
        "set", "--workspace", str(ws), "--subject", "U8", "--kind", "rating",
        "--status", "listed", "--source", "LCSC listing"]) == 0
    capsys.readouterr()
    ledger_cli.main(["report", "--workspace", str(ws)])
    rep = json.loads(capsys.readouterr().out)
    assert [r["subject"] for r in rep["listed"]] == ["U8"]


# ------------------------------------------------- check_ratings' facts

def test_pinout_fact():
    import check_ratings
    ext = {"mpn": "X", "pinout": [{"pin": "1", "name": "VCC"},
                                  {"pin": "2", "name": "GND"}]}
    b = {"ref": "U1", "nums": ["1", "2"], "fns": {"1": "VCC", "2": "GND"}}
    assert check_ratings.pinout_fact(b, ext)[0] == "verified"
    b["fns"]["1"] = "TXD"
    st, why = check_ratings.pinout_fact(b, ext)
    assert st == "unknown" and "TXD" in why
    b["nums"].append("3")
    assert check_ratings.pinout_fact(b, ext)[0] == "failed"
    assert check_ratings.pinout_fact(b, None)[0] == "unknown"


def test_rating_facts_from_findings():
    import check_ratings
    board = [{"ref": "U1", "lcsc": "C1", "pads": {"1": "+5V"}},
             {"ref": "U2", "lcsc": "C2", "pads": {"1": "+5V"}},
             {"ref": "R1", "lcsc": "C3", "pads": {"1": "+5V", "2": "GND"}},
             {"ref": "U3", "lcsc": "C4", "pads": {"1": "X"}}]
    vios = [{"severity": "error", "refs": ["U1"], "kind":
             "rating_over_voltage", "msg": "over"},
            {"severity": "warning", "refs": ["R1"], "kind": "rating_unrated",
             "msg": "unrated"}]
    facts = {f["subject"]: f for f in check_ratings.ledger_facts(
        board, {}, vios, {"U2": 1}, {}) if f["kind"] == "rating"}
    assert facts["U1"]["status"] == "failed"
    # no extraction: the number can only be the distributor's (#781)
    assert facts["U2"]["status"] == "listed" and facts["U2"]["source"]
    assert facts["U2"]["rated_from"] == "distributor"
    assert facts["R1"]["status"] == "unknown"
    assert facts["U3"]["status"] == "unknown"


def test_cpl_verify_records_footprints(ws, tmp_path):
    import cpl_verify
    (ws / "kicad").mkdir()
    pcb = ws / "kicad" / "core.kicad_pcb"
    pcb.write_text("(kicad_pcb)", encoding="utf-8")
    rows = [{"ref": "U8", "verdict": "ok", "basis": "pad number"},
            {"ref": "J1", "verdict": "no_fit", "why": "pads miss"},
            {"ref": "C1", "verdict": "no_model", "why": "no model"},
            # a stand-in for a missing model verifies no footprint
            {"ref": "C2", "verdict": "ok", "basis": cpl_verify.TWO_PAD_BASIS,
             "why": "no LCSC model"}]
    assert cpl_verify.record_footprints(
        pcb, rows, {"U8": "C84681", "J1": "C5184243", "C1": "C1525",
                    "C2": "C1525"})
    f = ledgerlib.load(ws)["facts"]
    assert f["U8/footprint"]["status"] == "verified"
    assert f["J1/footprint"]["status"] == "failed"
    assert f["C1/footprint"]["status"] == "unknown"
    assert f["C2/footprint"]["status"] == "unknown"


# ------------------------------------------------- orphans: facts of gone parts

def _orphan_ws(ws):
    """Verified facts for U8 (still on the board) and Q201 + C15195 (left)."""
    facts = [{**e, "status": "verified", "source": "test"}
             for e in ledgerlib.expected(ws).values()
             if e["subject"] in ("U8", "C84681")]
    facts += [{"subject": "Q201", "kind": "rating", "status": "verified",
               "source": "test", "lcsc": "C15195", "refs": ["Q201"]},
              {"subject": "C15195", "kind": "stock", "status": "verified",
               "source": "test", "lcsc": "C15195", "refs": ["Q201"]}]
    ledgerlib.put(ws, facts, "test")


def test_orphans_listed_and_not_evidence(ws):
    _orphan_ws(ws)
    assert sorted(ledgerlib.orphans(ws)) == ["C15195/stock", "Q201/rating"]
    rep = ledgerlib.report(ws)
    assert sorted(o["key"] for o in rep["orphaned"]) == [
        "C15195/stock", "Q201/rating"]
    assert all(r["subject"] not in ("Q201", "C15195") for r in rep["facts"])
    # a hand-set fact for a gone part does not satisfy anything on the board
    every = [{**e, "status": "verified", "source": "t"}
             for e in ledgerlib.expected(ws).values()]
    ledgerlib.put(ws, every, "test")
    assert ledgerlib.report(ws)["ready"] is True       # orphans never block
    assert ledgerlib.blockers(ws) == []


def test_prune_dry_run_then_apply_keeps_present_facts(ws, capsys):
    _orphan_ws(ws)
    before = ledgerlib.load(ws)["facts"]
    assert ledger_cli.main(["prune", "--workspace", str(ws)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["applied"] is False and len(out["orphaned"]) == 2
    assert ledgerlib.load(ws)["facts"] == before          # dry run writes nothing
    assert ledger_cli.main(["prune", "--workspace", str(ws), "--apply"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["applied"] is True and len(out["removed"]) == 2
    after = ledgerlib.load(ws)["facts"]
    assert ledgerlib.orphans(ws) == {}
    assert after["U8/pinout"] == before["U8/pinout"]       # present: untouched
    assert "Q201/rating" not in after and "C15195/stock" not in after


# -------------------------------- #713: parts that need no LCSC code (placeholders)

def _placeholder_ws(ws, bom_rows=True):
    """The board gains H1 (hole), TP201, FID1, a DNP U9 and a placed R7, none
    with a code."""
    pj = ws / "parts" / "parts.json"
    d = json.loads(pj.read_text(encoding="utf-8"))
    d["parts"] += [
        {"refdes": ["H1"], "value": "MountingHole"},
        {"refdes": ["TP201"], "value": "GND"},
        {"refdes": ["FID1"], "value": "fid"},
        {"refdes": ["U9"], "value": "spare", "assembly_class": "dnp"},
        {"refdes": ["R7"], "value": "10k"}]
    pj.write_text(json.dumps(d), encoding="utf-8")
    if bom_rows:
        bom = ws / "fab" / "BOM.csv"
        bom.write_text(bom.read_text(encoding="utf-8") +
                       'MountingHole,H1,MH,\nGND,TP201,TP,\nfid,FID1,F,\n'
                       'spare,U9,SOP,\n10k,R7,R0402,\n', encoding="utf-8")


@pytest.mark.parametrize("bom_rows", [True, False])
def test_placeholders_need_no_lcsc_facts(ws, bom_rows):
    _placeholder_ws(ws, bom_rows)
    if not bom_rows:
        (ws / "fab" / "BOM.csv").unlink()
    subj = {e["subject"] for e in ledgerlib.expected(ws).values()}
    for gone in ("(no LCSC: H1)", "(no LCSC: TP201)", "(no LCSC: FID1)",
                 "(no LCSC: U9)"):
        assert gone not in subj
    # a placed resistor with no code: one clear item, and it blocks
    r7 = [e["kind"] for e in ledgerlib.expected(ws).values()
          if e["subject"] == "(no LCSC: R7)"]
    assert r7 == ["stock"]
    assert any("(no LCSC: R7)" in b for b in ledgerlib.blockers(ws))
    assert not any("(no LCSC: H1)" in b or "(no LCSC: TP201)" in b
                   for b in ledgerlib.blockers(ws))


def test_stock_check_apply_prunes_orphans_and_writes_no_placeholder(ws):
    _placeholder_ws(ws)
    # facts left by an older run: placeholder triples, a gone vendor
    old = [{"subject": f"(no LCSC: {r})", "kind": k, "status": "unknown",
            "lcsc": f"(no LCSC: {r})", "refs": [r]}
           for r in ("H1", "TP201") for k in ledgerlib.PART_KINDS]
    old.append({"subject": "oshpark", "kind": "vendor", "status": "verified",
                "source": "t"})
    ledgerlib.put(ws, old, "test")
    assert len(ledgerlib.orphans(ws)) == 7
    res = stock_check.check_rows(
        stock_check.read_bom(ws / "fab" / "BOM.csv"),
        fetch=fake_fetch())
    ledgerlib.record_stock(ws, res)
    keys = set(ledgerlib.load(ws)["facts"])
    assert not [k for k in keys if k.startswith("(no LCSC: ") and "R7" not in k]
    assert "oshpark/vendor" not in keys
    assert ledgerlib.orphans(ws) == {}
    assert "(no LCSC: R7)/stock" in keys
    assert "(no LCSC: R7)/lifecycle" not in keys


def test_stock_check_rows_skip_placeholders(ws):
    _placeholder_ws(ws)
    rows = stock_check.read_bom(ws / "fab" / "BOM.csv", ws=ws)
    refs = sorted(r for row in rows for r in row["refs"])
    assert "H1" not in refs and "TP201" not in refs and "U9" not in refs
    assert "R7" in refs


# ---------------- #789: holes, test points, fiducials get no rating/footprint facts

@pytest.mark.parametrize("bom_rows", [True, False])
def test_placeholders_get_no_ref_facts(ws, bom_rows):
    _placeholder_ws(ws, bom_rows)
    if not bom_rows:
        (ws / "fab" / "BOM.csv").unlink()
    exp = ledgerlib.expected(ws)
    for ref in ("H1", "TP201", "FID1", "U9"):
        assert [k for k, e in exp.items() if e["subject"] == ref] == []
    # a placed resistor keeps all its facts
    assert sorted(e["kind"] for e in exp.values()
                  if e["subject"] == "R7") == ["footprint", "rating"]
    assert not any(b.startswith(("H1/", "TP201/", "FID1/"))
                   for b in ledgerlib.blockers(ws))


def test_a_test_point_with_a_code_keeps_only_its_footprint(ws):
    """A bought test-point part has an LCSC model to land on, but no rating."""
    pj = ws / "parts" / "parts.json"
    d = json.loads(pj.read_text(encoding="utf-8"))
    d["parts"] += [{"refdes": ["TP5"], "lcsc": "C238128", "value": "TP"}]
    pj.write_text(json.dumps(d), encoding="utf-8")
    exp = ledgerlib.expected(ws)
    assert [e["kind"] for e in exp.values() if e["subject"] == "TP5"] == \
        ["footprint"]


def test_stale_placeholder_ref_facts_are_orphans(ws):
    _placeholder_ws(ws)
    ledgerlib.put(ws, [{"subject": "H1", "kind": "rating",
                        "status": "unknown"}], "test")
    assert "H1/rating" in ledgerlib.orphans(ws)


# ------------------------------------------- what a rating actually rests on (#799)

def _rating_ws(ws, ext=None):
    lcsc = _ds_ws(ws, {"status": "suspect", "reasons": ["no text layer"]})
    if ext is not None:
        (ws / "parts" / f"{lcsc}.json").write_text(json.dumps(ext))
    else:
        (ws / "parts" / f"{lcsc}.json").unlink(missing_ok=True)
    return lcsc


def _set_rating(ws, source="LCSC listing, read by hand"):
    return ledger_cli.main(["set", "--workspace", str(ws), "--subject", "U8",
                            "--kind", "rating", "--status", "verified",
                            "--source", source])


def test_cli_set_rating_is_not_refused_over_a_pdf_nothing_was_read_from(ws):
    """No extraction: nothing was read from the suspect PDF, so it can't taint
    a rating the source doesn't rest on."""
    _rating_ws(ws)
    assert _set_rating(ws) == 0


def test_cli_set_rating_is_refused_when_its_extraction_was_read_from_a_suspect(ws, capsys):
    _rating_ws(ws, {"mpn": "CH340", "abs_max": []})
    assert _set_rating(ws) == 2
    assert "no text layer" in json.loads(capsys.readouterr().out)["error"]


def test_cli_set_rating_is_refused_when_its_source_names_the_suspect_pdf(ws, capsys):
    lcsc = _rating_ws(ws)
    assert _set_rating(ws, f"parts/datasheets/{lcsc}.pdf p.3") == 2
    assert "no text layer" in json.loads(capsys.readouterr().out)["error"]


def test_cli_set_pinout_still_refused_without_an_extraction(ws, capsys):
    """A pinout has no listing to rest on: the part's own PDF is what it means."""
    _rating_ws(ws)
    assert _set(ws) == 2


# ------ #791: rating/pinout/footprint facts of refs the board no longer relies on

def _stale_ws(ws):
    """An older ledger: J7 and NT1 (now board_feature) and U9 (now DNP) kept
    the facts written while they were bought parts; U8's rating was checked
    by hand and outranks anything a table says (as on hdi-bga U104/U105)."""
    pj = ws / "parts" / "parts.json"
    d = json.loads(pj.read_text(encoding="utf-8"))
    d["parts"] += [
        {"refdes": ["J7"], "value": "pads", "assembly_class": "board_feature"},
        {"refdes": ["NT1"], "value": "net tie",
         "assembly_class": "board_feature"},
        {"refdes": ["U9"], "lcsc": "C84681", "value": "CH340C",
         "assembly_class": "dnp"}]
    pj.write_text(json.dumps(d), encoding="utf-8")
    facts = {}
    for ref in ("J7", "NT1", "U9"):
        for kind in ledgerlib.REF_KINDS:
            facts[ledgerlib.key(ref, kind)] = {
                "subject": ref, "kind": kind, "status": "unknown", "value": "",
                "source": "", "refs": [ref], "lcsc": "", "checked_at": None,
                "by": None}
    facts["U9/footprint"].update(status="verified", by="cpl_verify",
                                 source="C84681 model")
    facts["U8/rating"] = {"subject": "U8", "kind": "rating",
                          "status": "verified", "by": "hand",
                          "value": "VCC 5 V within 6 V abs max",
                          "source": "CH340 datasheet v3B p.6",
                          "lcsc": "C84681", "checked_at": "2026-11-01T00:00:00+00:00"}
    facts["J7/pinout"].update(status="verified", by="hand",
                              source="drawing of the pads, by hand")
    ledgerlib.path(ws).write_text(json.dumps(
        {"schema": 1, "facts": facts}), encoding="utf-8")
    return facts


STALE = sorted(["J7/rating", "J7/footprint", "NT1/rating", "NT1/pinout",
                "NT1/footprint", "U9/rating", "U9/pinout", "U9/footprint"])


def test_stale_facts_of_unbought_refs_are_orphans_and_never_count(ws):
    _stale_ws(ws)
    orph = ledgerlib.orphans(ws)
    assert sorted(orph) == STALE
    rep = ledgerlib.report(ws)
    assert sorted(o["key"] for o in rep["orphaned"]) == STALE
    assert not [r for r in rep["facts"] if r["subject"] in ("J7", "NT1", "U9")]
    assert not [b for b in ledgerlib.blockers(ws)
                if any(r in b for r in ("J7", "NT1", "U9,", "U9 "))]


def test_any_write_drops_stale_ref_facts_and_says_so_once(ws, capsys):
    _stale_ws(ws)
    ledgerlib.put(ws, [{"subject": "U8", "kind": "pinout",
                        "status": "unknown"}], "check_ratings")
    err = capsys.readouterr().err
    assert err.count("stale") == 1
    assert "8" in err and "J7/rating" in err
    keys = set(ledgerlib.load(ws)["facts"])
    assert not keys & set(STALE)
    ledgerlib.put(ws, [], "check_ratings")                # nothing left to say
    assert "stale" not in capsys.readouterr().err
    assert ledgerlib.orphans(ws) == {}


def test_hand_verified_facts_survive_a_write_untouched(ws):
    before = _stale_ws(ws)
    ledgerlib.put(ws, [], "check_ratings")
    ledgerlib.prune(ws, apply=True)
    after = ledgerlib.load(ws)["facts"]
    # a real bought part's hand check: kept byte for byte, still verified
    assert after["U8/rating"] == before["U8/rating"]
    rows = {r["key"]: r for r in ledgerlib.report(ws)["facts"]}
    assert rows["U8/rating"]["status"] == "verified"
    # a hand-set fact about a board feature is not relied on, so it is not
    # evidence, but nobody's hand record is thrown away
    assert after["J7/pinout"] == before["J7/pinout"]
    assert "J7/pinout" not in ledgerlib.orphans(ws)


def test_check_ratings_keeps_a_hand_verified_rating_over_its_own(ws):
    """check_ratings' write path holds a hand-verified rating (#429)."""
    import check_ratings
    _stale_ws(ws)
    (ws / "kicad").mkdir()
    pcb = ws / "kicad" / "core.kicad_pcb"
    pcb.write_text("(kicad_pcb (version 20240108))", encoding="utf-8")
    check_ratings.run(["--pcb", str(pcb)])
    data = ledgerlib.load(ws)
    assert data.get("updated")                  # its write happened ...
    assert not set(data["facts"]) & set(STALE)  # ... and dropped the stale
    f = data["facts"]["U8/rating"]
    assert f["by"] == "hand" and f["status"] == "verified"
    assert f["source"] == "CH340 datasheet v3B p.6"


def test_a_connector_with_no_datasheet_is_unjudged_with_the_reason():
    """No extraction and no rated listing: no pinout or rating is made up for
    it; both facts stay unknown (unjudged) and say what is missing."""
    import check_ratings
    b = {"ref": "J3", "lcsc": "C2040", "footprint": "Conn:PinHeader_1x04",
         "nums": ["1", "2", "3", "4"], "fns": {},
         "pads": {"1": "+5V", "2": "GND", "3": "SDA", "4": "SCL"}}
    vios, unrated = [], []

    def emit(kind, sev, ref, net, msg, **kw):
        vios.append({"kind": kind, "severity": sev, "refs": [ref],
                     "msg": msg})
    judged = check_ratings._check_unextracted(
        b, {"lcsc": "C2040", "attributes": {}}, {"+5V": 5.0, "GND": 0.0},
        emit, unrated)
    assert judged is None          # no tier: nothing was compared
    facts = {f["kind"]: f for f in check_ratings.ledger_facts(
        [b], {}, vios, {}, {"C2040": {"lcsc": "C2040"}})}
    for kind in ("rating", "pinout"):
        f = facts[kind]
        assert f["status"] == "unknown", f
        assert "datasheet" in f["value"], f
        assert not f.get("source") and "rated_from" not in f, f
