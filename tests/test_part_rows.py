"""scripts/part_rows.py: draft cited datasheet rows and verify cites (#1100).

The planted fault is a row whose cite names the wrong page: --verify must
fail it and say which page the quote is really on."""
from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import netlist_audit  # noqa: E402
import part_rows  # noqa: E402

PAGES = [
    ["Table 5. Pin definitions",
     "NRST I/O Reset input. Do not leave the NRST pin floating."],
    ["Table 37. I/O static characteristics",
     "VIL Low-level input voltage - 0.3 x VDD V",
     "VIH High-level input voltage 0.7 x VDD - V",
     "RPU Weak pull-up equivalent resistor 25 40 55 kOhm",
     "Ilkg Input leakage current - - 1 uA"],
]


def _pdf(path: Path, pages=PAGES) -> Path:
    """A minimal PDF with one Helvetica text line per entry of each page."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", None,
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for lines in pages:
        body = "BT /F1 10 Tf 14 TL 40 760 Td " + " ".join(
            "(%s) Tj T*" % s.replace("(", r"\(").replace(")", r"\)")
            for s in lines) + " ET"
        objs.append(f"<< /Length {len(body)} >>\nstream\n{body}\nendstream")
        objs.append("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                    f"/Contents {len(objs)} 0 R "
                    "/Resources << /Font << /F1 3 0 R >> >> >>")
        kids.append(f"{len(objs)} 0 R")
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offs).encode()
    out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()
    path.write_bytes(out)
    return path


@pytest.fixture()
def pdf(tmp_path):
    return _pdf(tmp_path / "ds.pdf")


def _table(pdf: Path, page=2, table="Table 37", rpu=(25e3, 55e3)) -> dict:
    sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
    return {"parts": [{
        "mpn": "^TESTMCU", "verified": True,
        "pins": [{"pin": "^NRST$", "role": "reset", "rpu": list(rpu),
                  "cite": [{"doc": pdf.name, "sha256": sha, "page": page,
                            "table": table, "fields": ["rpu"],
                            "quote": "RPU Weak pull-up equivalent resistor "
                                     "25 40 55"}]}]}]}


def _verify(tmp_path, pdf, table):
    y = tmp_path / "t.yaml"
    y.write_text(yaml.safe_dump(table))
    return part_rows.verify([y], [pdf])


def test_pdf_text_is_read_per_page(pdf):
    pages = part_rows.pdf_pages(pdf)
    assert len(pages) == 2 and "Table 37" in pages[1]


def test_a_right_cite_verifies(tmp_path, pdf):
    res = _verify(tmp_path, pdf, _table(pdf))
    assert res["counts"] == {"ok": 1, "fail": 0, "unchecked": 0}
    assert res["cites"][0]["row"] == "parts[^TESTMCU].pins[^NRST$]"
    assert res["cites"][0]["verified"] is True   # inherited from the part


def test_planted_wrong_page_cite_is_caught(tmp_path, pdf):
    res = _verify(tmp_path, pdf, _table(pdf, page=1))
    assert res["counts"]["fail"] == 1
    probs = res["cites"][0]["problems"]
    assert "quote not on page 1; it is on page 2" in probs
    assert part_rows.main(["--verify", str(tmp_path / "t.yaml"),
                           "--pdf", str(pdf)]) == 1


def test_a_wrong_table_name_is_caught(tmp_path, pdf):
    res = _verify(tmp_path, pdf, _table(pdf, table="Table 99"))
    assert res["cites"][0]["problems"] == [
        "Table 99 is not on page 2 or the two before it"]


def test_a_number_not_in_the_quote_is_caught(tmp_path, pdf):
    res = _verify(tmp_path, pdf, _table(pdf, rpu=(25e3, 50e3)))
    assert res["cites"][0]["problems"] == [
        "rpu=50000 is not a number in the quote"]


def test_a_cite_with_no_pdf_given_is_unchecked(tmp_path, pdf):
    t = _table(pdf)
    t["parts"][0]["pins"][0]["cite"][0].update(sha256="0" * 64, doc="x.pdf")
    assert _verify(tmp_path, pdf, t)["counts"]["unchecked"] == 1


def test_numbers_survive_pdf_spacing():
    assert part_rows.numbers("25 40 55") == [25.0, 40.0, 55.0]
    assert part_rows.numbers("0 . 3 x VDD") == [0.3]


def test_draft_rows_are_cited_and_unverified(pdf, capsys):
    d = part_rows.draft("TESTMCU", pdf)
    rows = d["pin_states"] + d["pin_leakage"] + d["io_levels"]
    assert d["pin_states"] and d["pin_leakage"]
    assert all(r["verified"] is False and r["cite"] for r in rows)
    assert d["pin_states"][0]["pin"] == "NRST"
    assert d["pin_states"][0]["cite"][0]["page"] == 1
    leak = d["pin_leakage"][0]
    assert leak["vil_frac"] == 0.3 and leak["vih_frac"] == 0.7
    assert part_rows.main(["TESTMCU", "--pdf", str(pdf)]) == 0
    assert json.loads(capsys.readouterr().out)["pin_states"]


def test_draft_rows_pass_their_own_verify(tmp_path, pdf):
    d = part_rows.draft("TESTMCU", pdf)
    y = tmp_path / "d.yaml"
    y.write_text(yaml.safe_dump({"rows": d["pin_states"] + d["pin_leakage"]}))
    res = part_rows.verify([y], [pdf])
    assert res["counts"]["fail"] == 0 and res["counts"]["ok"] >= 2
    assert not any(c["verified"] for c in res["cites"])


# ------------------------------------------------- the audit's not_judged

def _alert():
    """test_pin_leakage's glasgow nALERT with a 100k pull-up: an error."""
    def m(ref, pin, func="", ptype="passive"):
        return {"ref": ref, "pin": pin, "pintype": ptype, "pinfunction": func}
    nets = {"+3V3": [m("U2", "1", "VCC", "power_in"), m("R9", "1")],
            "GND": [m("U2", "2", "GND", "power_in")],
            "/~{ALERT}": [m("R9", "2"),
                          m("U2", "33", "PA0/~{INT0}", "bidirectional"),
                          m("U8", "B4", "PT11B", "bidirectional")]}
    comps = {"U2": "CY7C68013A-56LTX", "U8": "LFE5U-25F", "R9": "100k"}
    return {"components": {r: {"value": v, "libsource": ("", "")}
                           for r, v in comps.items()}, "nets": nets}


def _leak_audit(specs):
    viol, facts = netlist_audit.audit(_alert(), {}, None, pin_requirements=[],
                                      pin_states=[], pin_leakage=specs)
    return [v for v in viol if v["kind"] == "pull_vs_leakage"], facts


def test_a_finding_on_an_unverified_row_is_not_judged():
    specs = netlist_audit.load_pin_leakage()
    hits, facts = _leak_audit(specs)
    assert [h["severity"] for h in hits] == ["error"]
    assert facts.get("unverified_findings", 0) == 0
    draft = copy.deepcopy(specs)
    for s in draft:
        if s["mpn"].startswith("ECP5"):
            s["verified"] = False
    hits, facts = _leak_audit(draft)
    assert hits == []
    assert facts["unverified_findings"] == 1
    assert facts["not_judged"][0].startswith("pull_vs_leakage (error) on "
                                             "/~{ALERT}")


def test_every_repo_cite_verifies_when_the_datasheets_are_here():
    """Runs only where the cited PDFs are (HWDE_DATASHEETS, a directory)."""
    import os
    d = os.environ.get("HWDE_DATASHEETS")
    if not d or not Path(d).is_dir():
        pytest.skip("HWDE_DATASHEETS not set")
    tables = sorted((REPO / "reference").glob("*.yaml"))
    res = part_rows.verify(tables, sorted(Path(d).glob("*.pdf")))
    assert res["counts"]["fail"] == 0, [
        c for c in res["cites"] if c["status"] == "fail"]
