"""T9 tests: intake.py - external-board import into an hwde workspace.

Pure tests build a synthetic "foreign" KiCad project in tmp_path (project
discovery, sheet hierarchy, lib-table URI classification, the copy plan,
version-consistency refusals, digest rendering, and a full toolchain-free
intake with --no-upgrade --no-gates --no-renders --no-report).

Smoke tests (`-m smoke`, KiCad 10 required) do the two acceptance intakes:
a golden board and a real KiCad demo project (the foreign fixture - KiCad
10 ships its demos in KiCad-9 format, so the demo also exercises the
`sch/pcb upgrade` leg). Both assert the SOURCE is byte-identical afterwards.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import gerblib  # noqa: E402
import env  # noqa: E402
import intake  # noqa: E402
from checklib import CheckError  # noqa: E402

GOLDEN = REPO / "tests" / "golden" / "blinky2"
# KiCad's own demos: the foreign fixture. Absent -> the smoke test skips.
DEMOS = [Path(r"C:/Program Files/KiCad/10.0/share/kicad/demos"),
         Path("/usr/share/kicad/demos"),
         Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/demos")]

PCB_HEAD = """(kicad_pcb
\t(version 20260206)
\t(generator "pcbnew")
\t(generator_version "10.0")
\t(general (thickness 1.6))
\t(paper "A4")
\t(layers (0 "F.Cu" signal) (2 "B.Cu" signal))
"""
SCH_ROOT = """(kicad_sch (version 20260306) (generator "eeschema")
\t(generator_version "10.0")
\t(uuid "aaaaaaaa-1111-2222-3333-444444444444") (paper "A4")
\t(sheet (at 10 10) (size 20 20)
\t\t(uuid "bbbbbbbb-1111-2222-3333-444444444444")
\t\t(property "Sheetname" "sub") (property "Sheetfile" "sub.kicad_sch"))
)
"""
SCH_SUB = """(kicad_sch (version 20260306) (generator "eeschema")
\t(generator_version "10.0")
\t(uuid "cccccccc-1111-2222-3333-444444444444") (paper "A4"))
"""


def make_project(root: Path, stem: str = "tiny", *, routed: bool = True,
                 escaping_lib: bool = True, sidecars: bool = False,
                 sub_sheet: bool = True) -> Path:
    """A synthetic foreign KiCad-10 project; returns its directory."""
    pdir = root / "proj"
    pdir.mkdir(parents=True, exist_ok=True)
    pcb = PCB_HEAD
    if routed:
        pcb += ('\t(segment (start 10 10) (end 20 10) (width 0.25) '
                '(layer "F.Cu") (net 1))\n')
    (pdir / f"{stem}.kicad_pcb").write_text(pcb + ")\n", encoding="utf-8")
    (pdir / f"{stem}.kicad_sch").write_text(
        SCH_ROOT if sub_sheet else SCH_ROOT.replace(
            '(property "Sheetfile" "sub.kicad_sch")', ""), encoding="utf-8")
    if sub_sheet:
        (pdir / "sub.kicad_sch").write_text(SCH_SUB, encoding="utf-8")
    (pdir / f"{stem}.kicad_pro").write_text(
        json.dumps({"meta": {"filename": f"{stem}.kicad_pro", "version": 3},
                    "board": {}, "schematic": {}}), encoding="utf-8")
    if escaping_lib:
        shared = root / "shared" / "foo.pretty"
        shared.mkdir(parents=True, exist_ok=True)
        (shared / "x.kicad_mod").write_text(
            '(footprint "x" (version 20260206))', encoding="utf-8")
        (pdir / "local.pretty").mkdir(exist_ok=True)
        (pdir / "local.pretty" / "y.kicad_mod").write_text(
            '(footprint "y" (version 20260206))', encoding="utf-8")
        (pdir / "fp-lib-table").write_text(
            '(fp_lib_table\n  (version 7)\n'
            '  (lib (name "shared")(type "KiCad")'
            '(uri "${KIPRJMOD}/../shared/foo.pretty")(options "")(descr ""))\n'
            '  (lib (name "local")(type "KiCad")'
            '(uri "${KIPRJMOD}/local.pretty")(options "")(descr ""))\n'
            '  (lib (name "gone")(type "KiCad")'
            '(uri "${KIPRJMOD}/nope.pretty")(options "")(descr ""))\n)\n',
            encoding="utf-8")
    if sidecars:
        (pdir / "constraints.json").write_text('{"power": []}',
                                               encoding="utf-8")
        (pdir / "decoupling.json").write_text('{"associations": []}',
                                              encoding="utf-8")
    return pdir


def run_intake(argv: list[str]) -> dict:
    payload, _ = intake.run(argv)
    return payload


def hermetic(pdir: Path, ws: Path, *extra: str) -> dict:
    return run_intake(["--source", str(pdir), "--workspace", str(ws),
                       "--no-upgrade", "--no-gates", "--no-renders",
                       "--no-report", *extra])


# ---------------------------------------------------------------------------
# pure: naming, discovery, parsing
# ---------------------------------------------------------------------------
def test_sanitize_board_folds_illegal_characters():
    assert intake.sanitize_board("sonde xilinx") == "sonde-xilinx"
    assert intake.sanitize_board("ecc83-pp") == "ecc83-pp"
    assert intake.sanitize_board("--x--") == "x"
    with pytest.raises(CheckError):
        intake.sanitize_board("///")


def test_discover_project_from_dir_file_and_project_flag(tmp_path):
    pdir = make_project(tmp_path)
    spec = intake.discover_project(pdir)
    assert spec["stem"] == "tiny"
    assert spec["pcb"].name == "tiny.kicad_pcb"
    assert spec["sch"].name == "tiny.kicad_sch"
    assert spec["dru"] is None
    # a file names its own project
    assert intake.discover_project(pdir / "tiny.kicad_pcb")["stem"] == "tiny"
    # a second project makes the directory ambiguous
    for sfx in (".kicad_pro", ".kicad_pcb"):
        shutil.copy(pdir / f"tiny{sfx}", pdir / f"other{sfx}")
    with pytest.raises(CheckError, match="several projects"):
        intake.discover_project(pdir)
    assert intake.discover_project(pdir, "other")["stem"] == "other"
    with pytest.raises(CheckError, match="no .*with that stem|--project"):
        intake.discover_project(pdir, "nosuch")


def test_discover_project_refuses_empty_and_unusable_sources(tmp_path):
    with pytest.raises(CheckError, match="does not exist"):
        intake.discover_project(tmp_path / "nope")
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(CheckError, match="no KiCad project"):
        intake.discover_project(empty)
    lone = tmp_path / "lone"
    lone.mkdir()
    (lone / "a.kicad_pro").write_text("{}", encoding="utf-8")
    with pytest.raises(CheckError, match="neither a .kicad_pcb nor a"):
        intake.discover_project(lone)


def test_sheet_files_recurses_is_cycle_safe_and_reports_missing(tmp_path):
    pdir = make_project(tmp_path)
    found, missing = intake.sheet_files(pdir / "tiny.kicad_sch")
    assert [p.name for p in found] == ["tiny.kicad_sch", "sub.kicad_sch"]
    assert missing == []
    # a cycle (sub points back at the root) terminates
    (pdir / "sub.kicad_sch").write_text(
        SCH_SUB[:-1] + '\n\t(sheet (property "Sheetfile" "tiny.kicad_sch"))\n)',
        encoding="utf-8")
    found, _ = intake.sheet_files(pdir / "tiny.kicad_sch")
    assert len(found) == 2
    # a referenced-but-absent sheet is reported, not raised
    (pdir / "sub.kicad_sch").unlink()
    found, missing = intake.sheet_files(pdir / "tiny.kicad_sch")
    assert missing == ["sub.kicad_sch"] and len(found) == 1


def test_file_format_and_board_phase(tmp_path):
    pdir = make_project(tmp_path)
    fmt = intake.file_format(pdir / "tiny.kicad_pcb")
    assert fmt == {"version": 20260206, "generator_version": "10.0"}
    assert intake.board_phase(pdir / "tiny.kicad_pcb") == "P8"
    assert intake.board_phase(None) == "P4"
    unrouted = make_project(tmp_path / "u", routed=False)
    assert intake.board_phase(unrouted / "tiny.kicad_pcb") == "P6"
    # a hand-rolled file without the tokens is reportable, not fatal
    odd = tmp_path / "odd.kicad_pcb"
    odd.write_text("(kicad_pcb)", encoding="utf-8")
    assert intake.file_format(odd) == {"version": None,
                                       "generator_version": None}


def test_lib_table_entries_and_uri_classification(tmp_path):
    pdir = make_project(tmp_path)
    entries = intake.lib_table_entries(pdir / "fp-lib-table")
    assert [e["name"] for e in entries] == ["shared", "local", "gone"]
    inside = intake.classify_uri("${KIPRJMOD}/local.pretty", pdir)
    assert inside["kind"] == "project" and inside["rel"] == "local.pretty"
    out = intake.classify_uri("${KIPRJMOD}/../shared/foo.pretty", pdir)
    assert out["kind"] == "escaping"
    absolute = intake.classify_uri(str(tmp_path / "x.pretty"), pdir)
    assert absolute["kind"] == "absolute"
    # KiCad's own path variables are resolved against the INSTALL, not os.environ
    share = tmp_path / "share" / "kicad"
    (share / "footprints" / "LED_THT.pretty").mkdir(parents=True)
    kvar = intake.classify_uri("${KICAD10_FOOTPRINT_DIR}/LED_THT.pretty",
                               pdir, share)
    assert kvar["kind"] == "env" and kvar["var_major"] == 10
    assert kvar["target"].exists()
    assert intake.classify_uri("${KICAD10_FOOTPRINT_DIR}/LED_THT.pretty",
                               pdir, None)["target"] is None


def test_kicad_share_dir_walks_up_from_the_cli(tmp_path):
    share = tmp_path / "KiCad" / "share" / "kicad"
    share.mkdir(parents=True)
    cli = tmp_path / "KiCad" / "bin" / "kicad-cli.exe"
    cli.parent.mkdir(parents=True)
    cli.write_text("", encoding="utf-8")
    assert intake.kicad_share_dir(cli) == share
    assert intake.kicad_share_dir(None) is None


# ---------------------------------------------------------------------------
# pure: copy plan
# ---------------------------------------------------------------------------
def test_plan_copy_renames_stem_keeps_sheets_and_imports_libs(tmp_path):
    pdir = make_project(tmp_path, sidecars=True)
    copies, libs, findings = intake.plan_copy(
        intake.discover_project(pdir), "renamed")
    dests = [c["dest"] for c in copies]
    assert "kicad/renamed.kicad_pcb" in dests
    assert "kicad/renamed.kicad_sch" in dests
    assert "kicad/sub.kicad_sch" in dests          # sub-sheets keep their name
    assert "kicad/local.pretty" in dests
    assert "kicad/imported_libs/foo.pretty" in dests
    assert "kicad/constraints.json" in dests       # hwde sidecars travel
    assert "kicad/decoupling.json" in dests
    by_name = {lib["name"]: lib for lib in libs}
    assert by_name["local"]["disposition"] == "copied"
    assert by_name["shared"]["disposition"] == "copied_rewritten"
    assert by_name["shared"]["rewritten_to"] == \
        "${KIPRJMOD}/imported_libs/foo.pretty"
    assert by_name["gone"]["disposition"] == "missing"
    kinds = {f["kind"] for f in findings}
    assert kinds == {"lib_uri_rewritten", "lib_uri_unresolved"}
    assert all(f["severity"] == "warning" for f in findings)


def test_plan_copy_does_not_let_two_imported_libs_collide(tmp_path):
    pdir = make_project(tmp_path)
    for side in ("a", "b"):
        d = tmp_path / side / "foo.pretty"
        d.mkdir(parents=True)
        (d / f"{side}.kicad_mod").write_text("(footprint)", encoding="utf-8")
    (pdir / "fp-lib-table").write_text(
        '(fp_lib_table\n  (version 7)\n'
        '  (lib (name "A")(type "KiCad")'
        '(uri "${KIPRJMOD}/../a/foo.pretty")(options "")(descr ""))\n'
        '  (lib (name "B")(type "KiCad")'
        '(uri "${KIPRJMOD}/../b/foo.pretty")(options "")(descr ""))\n)\n',
        encoding="utf-8")
    copies, libs, _ = intake.plan_copy(intake.discover_project(pdir), "tiny")
    dests = [c["dest"] for c in copies if "imported_libs" in c["dest"]]
    assert len(dests) == 2 and len(set(dests)) == 2
    rewrites = {lib["name"]: lib["rewritten_to"] for lib in libs}
    assert len(set(rewrites.values())) == 2
    # each rewritten URI names the destination its OWN source was copied to
    by_dest = {c["dest"]: c["src"] for c in copies}
    for name, uri in rewrites.items():
        src = by_dest["kicad/" + uri.split("}/", 1)[1]]
        assert Path(src).parent.name == name.lower()


def test_plan_copy_flags_a_missing_sub_sheet_as_an_error(tmp_path):
    pdir = make_project(tmp_path)
    (pdir / "sub.kicad_sch").unlink()
    _, _, findings = intake.plan_copy(intake.discover_project(pdir), "tiny")
    bad = [f for f in findings if f["kind"] == "sheet_missing"]
    assert len(bad) == 1 and bad[0]["severity"] == "error"


def test_rewrite_lib_uris_and_patch_pro_filename(tmp_path):
    table = tmp_path / "fp-lib-table"
    table.write_text('(fp_lib_table (lib (name "s")(uri "${KIPRJMOD}/../a")))',
                     encoding="utf-8")
    intake.rewrite_lib_uris(table, [{"table": "fp-lib-table",
                                     "uri": "${KIPRJMOD}/../a",
                                     "rewritten_to": "${KIPRJMOD}/b"}])
    assert '(uri "${KIPRJMOD}/b")' in table.read_text(encoding="utf-8")
    pro = tmp_path / "new.kicad_pro"
    pro.write_text(json.dumps({"meta": {"filename": "old.kicad_pro"}}),
                   encoding="utf-8")
    assert intake.patch_pro_filename(pro) is True
    assert json.loads(pro.read_text(encoding="utf-8"))["meta"]["filename"] \
        == "new.kicad_pro"
    assert intake.patch_pro_filename(pro) is False        # idempotent


def test_source_fingerprint_expands_dirs_and_sees_a_change(tmp_path):
    d = tmp_path / "lib.pretty"
    d.mkdir()
    (d / "a.kicad_mod").write_text("a", encoding="utf-8")
    f = tmp_path / "b.kicad_pcb"
    f.write_text("b", encoding="utf-8")
    before = intake.source_fingerprint([d, f])
    assert len(before) == 2
    (d / "a.kicad_mod").write_text("changed", encoding="utf-8")
    after = intake.source_fingerprint([d, f])
    assert after != before


# ---------------------------------------------------------------------------
# pure: version consistency
# ---------------------------------------------------------------------------
def test_version_consistency_refuses_only_a_split_within_one_type():
    ok = [{"file": "a.kicad_sch", "after": 20260306},
          {"file": "b.kicad_sch", "after": 20260306},
          {"file": "a.kicad_pcb", "after": 20260206}]
    intake.check_version_consistency(ok)          # pcb != sch is NORMAL
    bad = ok + [{"file": "c.kicad_sch", "after": 20260101}]
    with pytest.raises(CheckError, match="mixed-version project"):
        intake.check_version_consistency(bad)


def test_no_upgrade_mixed_versions_is_refused_before_materializing(tmp_path):
    pdir = make_project(tmp_path)
    sub = pdir / "sub.kicad_sch"
    sub.write_text(sub.read_text(encoding="utf-8")
                   .replace("20260306", "20250114"), encoding="utf-8")
    ws = tmp_path / "ws"
    with pytest.raises(CheckError, match="mixed-version project"):
        hermetic(pdir, ws)
    assert not ws.exists()
    assert not (tmp_path / f"ws{intake.STAGE_SUFFIX}").exists()


# ---------------------------------------------------------------------------
# pure: end-to-end (toolchain-free flags)
# ---------------------------------------------------------------------------
def test_hermetic_intake_builds_a_valid_workspace(tmp_path):
    pdir = make_project(tmp_path, sidecars=True)
    ws = tmp_path / "ws"
    payload = hermetic(pdir, ws, "--board", "tiny")

    # the lib rewrite/missing findings are warnings: warnings never fail
    assert {v["severity"] for v in payload["violations"]} == {"warning"}
    assert payload["status"] == "pass"
    assert payload["phase"] == "P8"
    assert payload["source"]["verified_unmodified"] is True

    state = json.loads((ws / "state.json").read_text(encoding="utf-8"))
    assert state["version"] == 2 and state["board"] == "tiny"
    assert state["phase"] == "P8"
    assert state["gates"] == {}                   # --no-gates: nothing claimed
    assert state["decisions"][0]["what"].startswith("imported external")
    for sub in ("kicad", "reports", "log", "fab"):
        assert (ws / sub).is_dir()
    assert (ws / "kicad" / "tiny.kicad_pcb").is_file()
    assert (ws / "kicad" / "sub.kicad_sch").is_file()
    assert (ws / "kicad" / "imported_libs" / "foo.pretty").is_dir()
    assert (ws / "reports" / "intake-digest.md").is_file()
    assert (ws / "reports" / "intake.json").is_file()
    digest = (ws / "reports" / "intake-digest.md").read_text(encoding="utf-8")
    assert "# Intake digest - tiny" in digest
    assert digest.isascii()


def test_hermetic_intake_never_writes_to_the_source(tmp_path):
    pdir = make_project(tmp_path)
    before = intake.source_fingerprint(sorted(pdir.rglob("*")))
    hermetic(pdir, tmp_path / "ws")
    assert intake.source_fingerprint(sorted(pdir.rglob("*"))) == before


def test_intake_refuses_an_existing_workspace_unless_forced(tmp_path):
    pdir = make_project(tmp_path)
    ws = tmp_path / "ws"
    hermetic(pdir, ws)
    with pytest.raises(CheckError, match="already exists"):
        hermetic(pdir, ws)
    # --force replaces a real workspace...
    payload = hermetic(pdir, ws, "--force")
    assert payload["workspace"].endswith("ws")
    # ...but refuses a directory that is not one
    other = tmp_path / "not-a-workspace"
    other.mkdir()
    (other / "keep.txt").write_text("mine", encoding="utf-8")
    with pytest.raises(CheckError, match="not an hwde workspace"):
        hermetic(pdir, other, "--force")
    assert (other / "keep.txt").is_file()


def test_board_only_project_imports_with_a_no_schematic_warning(tmp_path):
    pdir = make_project(tmp_path, escaping_lib=False)
    (pdir / "tiny.kicad_sch").unlink()
    (pdir / "sub.kicad_sch").unlink()
    payload = hermetic(pdir, tmp_path / "ws")
    assert payload["status"] == "pass"          # warnings only
    assert payload["source"]["has_sch"] is False
    kinds = {v["kind"] for v in payload["violations"]}
    assert kinds == {"no_schematic"}
    assert any("no schematic" in a for a in payload["next_actions"])


def test_schematic_only_project_lands_at_p4(tmp_path):
    pdir = make_project(tmp_path, escaping_lib=False)
    (pdir / "tiny.kicad_pcb").unlink()
    payload = hermetic(pdir, tmp_path / "ws")
    assert payload["phase"] == "P4"
    assert {v["kind"] for v in payload["violations"]} == {"no_board"}


def test_digest_and_next_actions_report_skipped_checks_honestly():
    payload = {
        "board": "b", "imported": "2026-08-07T00:00:00", "phase": "P8",
        "workspace": "boards/b", "formats": [],
        "toolchain": {"version": "10.0.3"},
        "source": {"dir": "/src", "project": "b", "files": 3,
                   "verified_unmodified": True, "has_sch": True},
        "libs": [], "violations": [],
        "baseline": {
            "gates": {"erc": {"status": "pass", "failing_count": 0,
                              "counts": {"total": 0},
                              "report": "reports/gate-erc.json"},
                      "drc_routed": {"status": "skipped",
                                     "reason": "no board"},
                      "dfm": {"status": "error", "error": "boom"}},
            "verify_checks": {"check_silk": "pass",
                              "check_current": "skipped"},
            "netlist_audit": None},
    }
    payload["next_actions"] = intake.next_actions(payload)
    text = intake.build_digest(payload)
    assert "| erc | PASS | 0 | 0 |" in text
    assert "| dfm | ERROR |" in text
    assert "| drc_routed | SKIPPED | - | - | no board |" in text
    assert "1 of 2 checks were SKIPPED (check_current)" in text
    assert "not 'the board is verified'" in text
    assert any("could not run" in a for a in payload["next_actions"])
    assert any("constraints.json" in a for a in payload["next_actions"])
    assert text.isascii()


# ---------------------------------------------------------------------------
# pure: the gerblib copper fallback intake depends on (user-renamed layers)
# ---------------------------------------------------------------------------
def test_gerblib_keys_copper_by_protel_extension_when_layers_are_renamed(
        tmp_path):
    for name in ("b-top_cu.gtl", "b-bottom_cu.gbl", "b-inner1.g1",
                 "b-F_Silkscreen.gto", "b-Edge_Cuts.gm1", "b.drl"):
        (tmp_path / name).write_text("", encoding="utf-8")
    fab = gerblib.open_fab(tmp_path)
    assert set(fab.copper_files) == {"F.Cu", "B.Cu", "In1.Cu"}
    assert fab.copper_files["F.Cu"].name == "b-top_cu.gtl"
    assert fab.edge_file is not None and fab.drill_files


def test_gerblib_canonical_names_still_win(tmp_path):
    for name in ("b-F_Cu.gtl", "b-B_Cu.gbl", "b-In1_Cu.g1", "b-In2_Cu.g2"):
        (tmp_path / name).write_text("", encoding="utf-8")
    fab = gerblib.open_fab(tmp_path)
    assert set(fab.copper_files) == {"F.Cu", "B.Cu", "In1.Cu", "In2.Cu"}
    assert fab.copper_layer_names() == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]


# ---------------------------------------------------------------------------
# smoke: the two acceptance intakes (need KiCad 10)
# ---------------------------------------------------------------------------
def _pinned_demos() -> list[Path]:
    """The demos beside the pipeline's kicad-cli: <root>/share/kicad/demos, or
    <root>/usr/share/kicad/demos on an unpacked Linux install."""
    try:
        cli = env.find_kicad_cli()
    except env.EnvError:
        return []
    if not cli:
        return []
    root = Path(cli).parent.parent
    return [root / "share" / "kicad" / "demos",
            root / "usr" / "share" / "kicad" / "demos"]


def _demo_dir() -> Path | None:
    for d in [*_pinned_demos(), *DEMOS]:
        if d.is_dir():
            return d
    return None


# What verify_all finds on blinky2 through intake, by check: (kind, refs). All
# warnings, so the gates still pass. check_ratings: no datasheet extractions, so
# the scaffold's empty parts/ leaves every part unrated, never a silent pass (#32).
# check_connector_fit: KiCad's stock 1.0 mm header drill under IPC-2222 (#474, the
# same known warning as test_checks_s5.GOLDEN_KNOWN_WARNINGS).
GOLDEN_WARNINGS = {
    "check_ratings": ("rating_unrated", {"C1", "D1", "J1", "J2"}),
    "check_connector_fit": ("tht_hole_ipc2222", {"J1", "J2"}),
}
# check_straps and check_reset_drive know blinky2's STM32F103 since #885 (the
# STM32F1 row in reference/reset_states.yaml) and must pass it, as below.
# The checks blinky2 gives nothing to judge: it has none of what they read.
GOLDEN_NOTHING_TO_JUDGE = {
    "check_antenna", "check_cap_load", "check_creepage", "check_ddr_timing", "check_diffpair",
    "check_isolation", "check_kelvin", "check_length_groups", "check_mate_pins",
    "check_net_tie", "check_pinouts", "check_rf_match", "check_stackup", "check_surge", "check_thermal",
    "check_transformer", "check_usb_phy", "check_via_stubs",
}


@pytest.mark.smoke
def test_intake_golden_board_runs_every_gate(tmp_path):
    """Acceptance leg 1: a golden board imports to a clean, gated workspace."""
    before = intake.source_fingerprint(sorted(GOLDEN.rglob("*")))
    ws = tmp_path / "ws"
    payload = run_intake(["--source", str(GOLDEN), "--board", "blinky2-in",
                          "--workspace", str(ws), "--no-renders",
                          "--no-report"])
    assert payload["status"] == "pass", payload["violations"]
    assert payload["source"]["verified_unmodified"] is True
    assert intake.source_fingerprint(sorted(GOLDEN.rglob("*"))) == before

    gates = payload["baseline"]["gates"]
    assert set(gates) == set(intake.BASELINE_GATES)
    assert all(g["status"] == "pass" for g in gates.values()), gates
    # every check ran, and each says what it really did on this board
    checks = dict(payload["baseline"]["verify_checks"])
    assert {n for n, s in checks.items() if s == "violations"} == set(
        GOLDEN_WARNINGS)
    assert {n for n, s in checks.items() if s == "nothing_to_judge"} == (
        GOLDEN_NOTHING_TO_JUDGE)
    assert {s for n, s in checks.items() if n not in GOLDEN_WARNINGS
            and n not in GOLDEN_NOTHING_TO_JUDGE} == {"pass"}, checks
    for name, want in GOLDEN_WARNINGS.items():
        found = json.loads((ws / "reports" / "checks" / f"{name}.json")
                           .read_text(encoding="utf-8"))["violations"]
        assert {(v["kind"], v["severity"]) for v in found} == {(want[0],
                                                                "warning")}
        assert {r for v in found for r in v["refs"]} >= want[1], name
    assert payload["baseline"]["netlist_audit"]["status"] in ("pass",
                                                              "violations")
    # gate results are recorded AND hash-fresh in state v2
    import state as state_mod
    st = state_mod.State.load(ws / "state.json")
    resume = st.resume_summary()
    assert set(resume["gates_passed"]) >= {"erc", "drc_routed", "verify",
                                           "dfm"}
    assert resume["gates_stale"] == [] and resume["gates_freshness_unknown"] == []
    assert set(resume["gates_passed_fresh"]) == set(resume["gates_passed"])
    assert (ws / "reports" / "intake-digest.md").is_file()
    assert (ws / "reports" / "verify_all.json").is_file()
    assert (ws / "kicad" / "blinky2-in.net").is_file()


@pytest.mark.parametrize("share", ["share", "usr/share"])
def test_demos_are_found_beside_the_pinned_kicad_cli(tmp_path, monkeypatch, share):
    """Tools-folder installs keep their demos here, not under /usr/share (#795)."""
    root = tmp_path / "kicad-x"
    demos = root / share / "kicad" / "demos"
    demos.mkdir(parents=True)
    monkeypatch.setattr(env, "find_kicad_cli", lambda: root / "bin" / "kicad-cli")
    assert _demo_dir() == demos


@pytest.mark.kicad_recorded
@pytest.mark.smoke
def test_intake_kicad_demo_upgrades_the_format_and_reports_findings(tmp_path):
    """Acceptance leg 2: a foreign project (KiCad's own demo, shipped in
    KiCad-9 format) imports, upgrades to the pin, and gets reviewed."""
    demos = _demo_dir()
    if demos is None or not (demos / "ecc83").is_dir():
        pytest.skip("KiCad demo projects not installed")
    src = demos / "ecc83"
    before = intake.source_fingerprint(sorted(src.rglob("*")))
    ws = tmp_path / "ws"
    payload = run_intake(["--source", str(src), "--project", "ecc83-pp",
                          "--workspace", str(ws), "--no-renders",
                          "--no-report"])
    # the source is untouched even though the COPIES were format-upgraded
    assert intake.source_fingerprint(sorted(src.rglob("*"))) == before
    assert payload["source"]["verified_unmodified"] is True

    fmt = {f["file"]: f for f in payload["formats"]}
    assert fmt["ecc83-pp.kicad_pcb"]["before"] < fmt["ecc83-pp.kicad_pcb"]["after"]
    assert fmt["ecc83-pp.kicad_sch"]["updated"] is True
    assert len({f["after"] for f in payload["formats"]
                if f["file"].endswith(".kicad_sch")}) == 1

    gates = payload["baseline"]["gates"]
    # the demo's own fp-lib-table lists only "Footprints", and its symbols link
    # Capacitor_THT and Resistor_THT: KiCad 10.0.6 reports that, nothing else
    erc = json.loads((ws / "reports" / "gate-erc.json").read_text(encoding="utf-8"))
    assert {f["check"] for f in erc["failing"]} <= {"footprint_link_issues"}
    # the demo's copper layers are USER-RENAMED (top_cu/bottom_cu): dfm only
    # runs because gerblib keys copper by the Protel extension too
    assert gates["dfm"]["status"] in ("pass", "fail")
    assert "error" not in gates["dfm"]
    # a foreign board's own findings are the deliverable, not intake failures
    assert gates["drc_routed"]["status"] == "fail"
    assert payload["status"] == "pass"
    assert (ws / "kicad" / "footprints.pretty").is_dir()
    digest = (ws / "reports" / "intake-digest.md").read_text(encoding="utf-8")
    assert "SKIPPED" in digest and digest.isascii()


@pytest.mark.smoke
def test_intake_emits_the_design_document(tmp_path):
    """report_gen is the 'review this board' deliverable (plan T9)."""
    ws = tmp_path / "ws"
    payload = run_intake(["--source", str(GOLDEN), "--board", "blinky2-doc",
                          "--workspace", str(ws), "--no-gates",
                          "--no-renders"])
    doc = payload["deliverables"].get("design_doc")
    assert doc and (ws / doc).is_file()
    assert (ws / "reports" / "intake-digest.md").is_file()
