"""Suite-wide guards: no test may file a document in the real register, and a
test marked `kicad_recorded` runs only on the kicad-cli its numbers were
recorded on (RECORDED_KICAD). On any other version it is skipped with the
reason, because the numbers move for known reasons (LEARNINGS 2026-08-27,
"KiCad 10.0.5 vs 10.0.3 deltas") until someone re-baselines them on purpose. A
skip like that is named at the end of the run, and moving the pin without
re-recording fails tests/test_kicad_pin.py, so the tests can't go quiet (#795).
"""
import functools
import os
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

RECORDED_KICAD = (10, 0, 6)
_STRANDED = pytest.StashKey[tuple]()


@functools.cache
def _kicad_version() -> tuple[tuple[int, ...], str]:
    """(version, what it is) of the pipeline's kicad-cli, () when unknown.

    Asked once, at collection: a pin whose version probe fails (a slow
    kicad-cli on a strained machine times out) skips the recorded tests with
    the reason, never aborts the whole run (issue #252)."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))
    import env
    try:
        cli = env.find_kicad_cli()
    except env.EnvError as e:
        return (), f"no usable kicad-cli ({e})"
    if not cli:
        return (), "no kicad-cli"
    ver = env.kicad_cli_version(cli)
    if not ver:
        return (), f"kicad-cli {cli} of unknown version"
    return ver, ".".join(map(str, ver))


# Upstream tests that fail in hive-hwde for a cause a brief already owns:
# each is an expected failure until that brief lands. An XPASS in the summary
# means its cause is gone: take its line out. (nodeid -> why) The _CITES lines
# that only lacked the source PDFs XPASS where `hwde sources` has fetched them
# (issue #97); they stay listed for a HOME that hasn't.
_CITES = ("the records cite files the module doesn't carry: source PDFs, "
          "upstream's LEARNINGS.md and board workspaces (261028-141932, "
          "261028-154518)")
KNOWN_FAILING = {
    "tests/test_coverage.py::test_committed_library_lint_green_and_checklists_present": _CITES,
    "tests/test_coverage.py::test_committed_records_are_strict_green_after_the_u14_backfill": _CITES,
    "tests/test_coverage.py::test_strict_cli_flag": _CITES,
    "tests/test_coverage.py::test_lint_generalizes_must_point_at_a_more_general_record": _CITES,
    "tests/test_coverage.py::test_instance_level_envelope_optional_and_proven_needs_evidence": _CITES,
    "tests/test_coverage.py::test_prove_upgrades_applied_records_only_and_is_idempotent": _CITES,
    "tests/test_coverage.py::test_prove_reaches_below_approved": _CITES,
    "tests/test_knowledge.py::test_committed_records_lint_green": _CITES,
    "tests/test_knowledge.py::test_validate_cli_contract": _CITES,
    "tests/test_knowledge.py::test_lint_accepts_a_not_redistributed_sidecar": _CITES,
    "tests/test_knowledge.py::test_app_note_grounding_payload": _CITES,
    "tests/test_knowledge.py::test_app_note_records_committed_and_cite_the_pdf": _CITES,
}


# Tests too heavy for the per-PR test (bin/hwde test deselects `slow`; bin/hwde
# test-slow runs them): each took 4 s or more on the Hive's loaded machine,
# together about 500 of its 790 s (261029-175316). A parametrized test is
# named without its ids, so every case of it is slow. (nodeid -> why)
_TOOLS = "runs the live toolchain (kicad-cli, KiCad's python, java)"
_BOARD = "runs a whole fixture board's checks or bench stage"
_INTAKE = "materializes a whole KiCad project through intake"
SLOW = {
    "tests/test_zone_patch.py::test_live_drop_and_rebuild_the_patches_on_a_copy_of_core": _TOOLS,
    "tests/test_zone_patch.py::test_live_via_array_on_a_copy_of_core": _TOOLS,
    "tests/test_necks.py::test_live_freerouting_neckdown_passes_the_neck_rule": _TOOLS,
    "tests/test_fr_fixed_overlap.py::test_live_route_along_a_fixed_stub_finishes_and_keeps_the_stub": _TOOLS,
    "tests/test_xvfb_noreset.py::test_live_kicad_starts_at_once_all_come_up": "starts KiCad's python 8 times at once, each on its own Xvfb",
    "tests/test_isolation_rules.py::test_live_drc_and_check_isolation_agree_on_the_same_copper": _TOOLS,
    "tests/test_isolation_rules.py::test_live_a_domains_own_nets_keep_their_ordinary_spacing": _TOOLS,
    "tests/test_isolation_rules.py::test_live_slot_board_rule_falls_to_the_clearance": _TOOLS,
    "tests/test_isolation_rules.py::test_live_rated_part_excepted_pad_to_pad_only": _TOOLS,
    "tests/test_isolation_rules.py::test_live_pour_stays_out_of_the_gap": _TOOLS,
    "tests/test_isolation_rules.py::test_live_route_auto_keeps_out_of_the_gap": _TOOLS,
    "tests/test_rf_synth.py::test_live_ngspice_ac_gives_the_same_rl": "runs ngspice AC decks in a worker",
    "tests/test_rf_synth.py::test_live_ngspice_mc_matches_the_closed_form": "runs a 40-draw Monte Carlo through ngspice workers",
    "tests/test_sim_si.py::test_planted_victim_beside_a_fast_clock": "field-solves pinned lite-main's nets twice and runs ngspice",
    "tests/test_check_testpoints.py::test_access_side_and_position_match_kicad_ipcd356": "exports pinned lite-main's IPC-D-356 with kicad-cli",
    "tests/test_check_wicking.py::test_mask_web_agrees_with_the_kicad_f_mask_gerber": _TOOLS,
    "tests/test_via_stubs.py::test_live_backdrill_round_trip": _TOOLS,
    "tests/test_netlist_compare.py::test_pinned_board_ships_a_netlist_its_gerbers_match": "exports the pinned lite-main/core fab packages with kicad-cli",
    "tests/test_hdi_route.py::test_live_every_ball_escaped": "escapes a ball field and runs kicad-cli DRC",
    "tests/test_hdi_route.py::test_live_escape_rotated_and_on_the_back": "escapes a ball field and runs kicad-cli DRC",
    "tests/test_hdi_route.py::test_live_fab_package_ships_a_drill_file_per_span_and_passes_dfm": "escape, DRC, fab_export and dfm_check on a ball field",
    "tests/test_hdi_route.py::test_live_plane_balls_join_through_the_stack": "escapes a ball field, refills and runs kicad-cli DRC",
    "tests/test_route_escape.py::test_live_escape_synthetic_bga": "runs KRT bga_fanout and kicad-cli DRC on a synthetic BGA",
    "tests/test_route_escape.py::test_live_power_first_lands_before_the_escape": "runs KRT bga_fanout and kicad-cli DRC on a synthetic BGA",
    "tests/test_route_escape.py::test_live_fault_via_on_a_neighbours_pad_is_ripped": "runs KRT bga_fanout and kicad-cli DRC on a synthetic BGA",
    "tests/test_route_escape.py::test_live_fault_track_between_two_pads_is_ripped": "runs KRT bga_fanout and kicad-cli DRC on a synthetic BGA",
    "tests/test_route_escape.py::test_live_fine_via_escape_6_layer_quiet": "runs KRT bga_fanout and kicad-cli DRC on a synthetic 6-layer BGA",
    "tests/test_route_escape.py::test_live_fault_fine_via_outside_the_courtyard_is_ripped": "runs KRT bga_fanout and kicad-cli DRC on a synthetic 6-layer BGA",
    "tests/test_zroute.py::test_live_route_critical_pair_l1_l3_drc_clean": "runs KRT route_diff/route and kicad-cli DRC on a synthetic 6-layer board",
    "tests/test_zroute.py::test_live_freerouting_draws_dsn_layer_widths": "runs Freerouting and kicad-cli DRC on a synthetic 6-layer board",
    "tests/test_check_net_tie.py::test_stock_net_tie_footprints_pass_and_keep_their_copper": "runs KRT's writer on every net-tie footprint KiCad ships, front and back",
    "tests/test_solvers.py::test_live_solver_validates": "runs ngspice, Xyce, FastHenry, FasterCap from the IIC-OSIC-TOOLS tree; machine-load sensitive",
    "tests/test_solvers.py::test_live_palace_validates": "runs Palace (FEM eigenmode) on the IIC-OSIC-TOOLS tree",
    "tests/test_planecav.py::test_live_palace_l_shape_is_the_benchmark": "runs Palace eigenmode on an L-shaped plane cavity",
    "tests/test_planecav.py::test_live_palace_agrees_with_segmentation_on_an_l": "runs Palace eigenmode on an L-shaped plane cavity",
    "tests/test_solvers.py::test_live_openems_validates": "runs an openEMS full-wave solve (minutes)",
    "tests/test_solvers.py::test_live_planar_thru_line_is_transparent": "runs an openEMS planar S-parameter solve (~1.5 min)",
    "tests/test_emvia.py::test_live_coax_via_matches_the_cage_closed_form": "runs an openEMS via deck",
    "tests/test_emvia.py::test_live_stub_notch_and_its_blind_twin": "runs two openEMS via decks",
    "tests/test_emvia.py::test_live_palace_driven_agrees_with_openems": "runs gmsh, Palace and openEMS on one via",
    "tests/test_emvia.py::test_live_launch_reads_a_matched_coax_and_a_passive_launch": "runs two openEMS launch decks",
    "tests/test_emloss.py::test_live_r_of_a_strip_over_ground_vs_fasthenry": "runs FastHenry on a 600-filament strip over plane",
    "tests/test_emloss.py::test_live_loss_in_the_deck_vs_on_the_line": "runs openEMS with Lorentz poles at two meshes",
    "tests/test_emloss.py::test_live_board_touchstone_carries_loss_and_passes_sparam_check": "runs an openEMS board S-parameter solve",
    "tests/test_emboard.py::test_live_board_thru_matches_the_line_closed_form": "runs an openEMS board S-parameter solve",
    "tests/test_emboard.py::test_live_board_run_is_the_same_under_any_load": "runs an openEMS board S-parameter solve twice",
    "tests/test_emboard.py::test_live_board_open_stub_notch": "runs an openEMS board S-parameter solve",
    "tests/test_emboard.py::test_live_board_mutant_wrong_width_misses_z0": "runs an openEMS board S-parameter solve",
    "tests/test_sparam.py::test_live_fit_spice_round_trip": "vector-fits with scikit-rf and runs ngspice AC from the IIC-OSIC-TOOLS tree",
    "tests/test_hdi_maze.py::test_live_tfbga54_maze_finishes": "runs the maze on a whole escaped TFBGA-54 (~2 min)",
    "tests/test_check_env.py::test_passes_on_dev_machine": _TOOLS,
    "tests/test_check_env.py::test_out_file_and_quiet": _TOOLS,
    "tests/test_check_env.py::test_unpinned_discovery_still_resolves_10x": _TOOLS,
    "tests/test_check_env.py::test_missing_kicad_fails_with_remediation": _TOOLS,
    "tests/test_check_env.py::test_missing_java_fails_with_remediation": _TOOLS,
    "tests/test_board_rename.py::test_kicad_cli_opens_the_renamed_project": _TOOLS,
    "tests/test_board_setup.py::test_board_init_fixed_outline_too_tight_gets_no_default_holes": _TOOLS,
    "tests/test_build_modes.py::test_board_init_cli_does_not_refuse_a_fixed_outline": _TOOLS,
    "tests/test_bench.py::test_offline_fixture_matches_baseline": _BOARD,
    "tests/test_route_tune.py::test_tune_blinky2_live": _TOOLS,
    "tests/test_pad_zone_connection.py::test_live_pad_zone_full_on_a_copy_of_core": _TOOLS,
    "tests/test_silk_fix.py::test_live_silk_fix_clears_the_findings_on_a_copy_of_core": _TOOLS,
    "tests/test_silk_fix.py::test_live_over_pads_and_edge_mm_on_a_copy_of_core": _TOOLS,
    "tests/test_teardrops.py::test_live_teardrops_led_count_drc_idempotent": _TOOLS,
    "tests/test_teardrops.py::test_live_teardrops_bb_adc_count": _TOOLS,
    "tests/test_dfm_zone_fill.py::test_live_refill_confirms_dfm1": _TOOLS,
    "tests/test_bench.py::test_known_answers_fire_where_expected": _BOARD,
    "tests/test_mcp_server.py::test_dfm_check_is_read_only": _BOARD,
    "tests/test_mcp_server.py::test_review_runs_every_gate_read_only": _BOARD,
    "tests/test_checks_s5.py::test_full_manifest_coverage": _BOARD,
    "tests/test_research.py::test_bench_p1_freeze_score_baseline_and_compare": _BOARD,
    "tests/test_research.py::test_validate_refuses_contract_breaches": _BOARD,
    "tests/test_learn.py::test_freeze_baseline_then_seeded_regression_exits_1": _BOARD,
    "tests/test_sim_board.py::test_engine_resistor_overpower": _BOARD,
    "tests/test_layout_sim.py::test_mutant_current_density_max_at_neck": _BOARD,
    "tests/test_intake.py::test_intake_refuses_an_existing_workspace_unless_forced": _INTAKE,
    "tests/test_intake.py::test_schematic_only_project_lands_at_p4": _INTAKE,
    "tests/test_intake.py::test_hermetic_intake_never_writes_to_the_source": _INTAKE,
    "tests/test_intake.py::test_no_upgrade_mixed_versions_is_refused_before_materializing": _INTAKE,
    "tests/test_intake.py::test_hermetic_intake_builds_a_valid_workspace": _INTAKE,
    "tests/test_intake.py::test_board_only_project_imports_with_a_no_schematic_warning": _INTAKE,
    "tests/test_eda_io.py::test_import_eagle_end_to_end": _TOOLS,
    "tests/test_eda_io.py::test_export_round_trips_parts_and_nets": _TOOLS,
    "tests/test_place_replicate.py::test_live_stamp_known_answer_drc_and_parity_clean": _TOOLS,
    "tests/test_place_replicate.py::test_live_stamp_rot180_drc_clean": _TOOLS,
    "tests/test_place_replicate.py::test_live_mutant_extra_part_falls_back_alone": _TOOLS,
    "tests/test_tolerance.py::test_live_ngspice_divider_wca_and_mc": "runs ~60 ngspice decks in sim_run batch workers",
    "tests/test_tolerance.py::test_live_ngspice_rc_fc_corners": "runs ngspice AC sweeps in sim_run batch workers",
    "tests/test_tolerance.py::test_live_xyce_sampling_agrees_with_python_mc": "runs Xyce .sampling from the IIC-OSIC-TOOLS tree",
    "tests/test_part_repair.py::test_live_renumber_r6_applies_and_replays": _TOOLS,
    "tests/test_part_repair.py::test_live_swap_fp_r6_to_0805_leaves_the_board": _TOOLS,
    "tests/test_part_repair.py::test_live_move_r6_replays_from_the_place_file": _TOOLS,
    "tests/test_part_repair.py::test_live_rotate_and_flip_r6_rejoin_and_replay": _TOOLS,
    "tests/test_part_repair.py::test_live_a_join_that_must_push_other_nets_aside": _TOOLS,
    "tests/test_part_repair.py::test_live_a_worse_board_writes_nothing_and_names_why": _TOOLS,
}


# Tests that open KiCad's stock symbol libraries (kicad-sch-api reads them
# from the install HIVE_TOOLS names): skipped, not failed, where there is none.
HOST_SYMBOLS = {
    "tests/test_lib_hygiene.py::test_write_preserves_lib_symbols",
    "tests/test_schem_layout.py::test_a_changed_netlist_leaves_the_sheet_alone",
    "tests/test_schem_layout.py::test_already_wired_sheets_are_skipped",
    "tests/test_schem_layout.py::test_a_short_is_caught_before_the_gate",
    "tests/test_schem_layout.py::test_only_reference_and_value_show",
    "tests/test_schem_layout.py::test_redraw_is_the_same_every_time",
    "tests/test_schem_layout.py::test_redraw_keeps_connectivity_and_draws_wires",
    "tests/test_schem_layout.py::test_redrawn_board_has_no_close_wires_or_text_on_text",
    "tests/test_schem_layout.py::test_two_nets_never_share_a_wire_end",
    "tests/test_schem_layout.py::test_worse_erc_restores_every_sheet",
    "tests/test_workarounds.py::test_workarounds_table_matches_its_probes",
}


# Full-wave solves too long for test-slow (bin/hwde test-em runs them): about a
# minute to 20 minutes each on the loaded tower, 80 min together at load 45, where
# the rest of test-slow took 17. They are `slow` too, so bin/hwde test leaves them out.
# (nodeid without ids -> why)
EM = {
    "tests/test_emantenna.py::test_live_half_wave_dipole_directivity_and_z": "runs openEMS and nf2ff on a dipole",
    "tests/test_emantenna.py::test_live_patch_tm010_against_the_cavity_formula_and_mesh": "runs openEMS at two meshes until the patch rings down",
    "tests/test_emantenna.py::test_live_palace_patch_is_a_second_solver": "runs a Palace eigenmode solve",
    "tests/test_emvia.py::test_live_stub_notch_and_its_blind_twin": "runs two openEMS via decks",
    "tests/test_emvia.py::test_live_palace_driven_agrees_with_openems": "runs gmsh, Palace and openEMS on one via",
    "tests/test_emvia.py::test_live_launch_reads_a_matched_coax_and_a_passive_launch": "runs two openEMS launch decks",
    "tests/test_empalace.py::test_live_open_stub_notch": "runs a Palace driven solve",
    "tests/test_empalace.py::test_live_thru_known_answer": "runs a Palace driven solve",
    "tests/test_emloss.py::test_live_loss_in_the_deck_vs_on_the_line": "runs openEMS with Lorentz poles at two meshes",
}

# What one test may take before it fails by name, seconds: a hang then costs its
# limit and the rest of the run still reports, where it used to look like a suite
# that never ends. About twice the slowest test of each tier on a quiet box; on a
# busy one the limit stretches (loadlib, up to 4x) while the test is still getting
# CPU, so the tower's load is not a red (#970) and a stuck test still is.
TEST_LIMIT = {"em": 2700, "slow": 900}


@contextmanager
def time_limit(seconds: float, what: str = "this test", **lim_kw):
    """Fail `what` after `seconds`, stretched for CPU pressure while the test's
    process tree still uses CPU (loadlib.Limit). The exception lands in the main
    thread, and subprocess.run kills its child on the way out, so no solver is
    left running."""
    lib = str(Path(__file__).resolve().parents[1] / "scripts" / "lib")
    if lib not in sys.path:
        sys.path.insert(0, lib)
    import loadlib

    def expired(lim):
        pytest.fail(f"{what} ran past its limit of {seconds:g} s "
                    f"({lim.why}, after {lim.elapsed():.0f} s)", pytrace=True)
    with loadlib.alarm(seconds, expired, **lim_kw):
        yield


@pytest.fixture(autouse=True)
def _test_limit(request):
    limit = next((v for k, v in TEST_LIMIT.items() if request.node.get_closest_marker(k)), None)
    if limit is None:
        yield
        return
    with time_limit(limit, request.node.nodeid):
        yield


@pytest.hookimpl(tryfirst=True)  # before -m deselects by mark
def pytest_collection_modifyitems(config, items):
    for item in items:
        why = KNOWN_FAILING.get(item.nodeid)
        if why:
            item.add_marker(pytest.mark.xfail(reason=why, strict=False))
        if item.nodeid.split("[")[0] in SLOW:
            item.add_marker(pytest.mark.slow)
        if item.nodeid.split("[")[0] in EM:
            item.add_marker(pytest.mark.em)
            item.add_marker(pytest.mark.slow)
    if HOST_SYMBOLS & {i.nodeid for i in items} and not _host_symbols():
        skip_sym = pytest.mark.skip(reason="needs KiCad's stock symbol libraries, this host has none")
        for item in items:
            if item.nodeid in HOST_SYMBOLS:
                item.add_marker(skip_sym)
    marked = [i for i in items if i.get_closest_marker("kicad_recorded")]
    if not marked:
        return
    ver, have = _kicad_version()
    if ver[:3] == RECORDED_KICAD:
        return
    skip = pytest.mark.skip(reason=(
        f"numbers recorded on KiCad {'.'.join(map(str, RECORDED_KICAD))}, "
        f"this is {have} (LEARNINGS 2026-08-27)"))
    for item in marked:
        item.add_marker(skip)
    config.stash[_STRANDED] = (len(marked), have)


def pytest_terminal_summary(terminalreporter, config):
    stranded = config.stash.get(_STRANDED, None)
    if stranded:
        n, have = stranded
        terminalreporter.write_line(
            f"kicad_recorded: {n} tests did NOT run: numbers recorded on KiCad "
            f"{'.'.join(map(str, RECORDED_KICAD))}, this is {have}", yellow=True)


MODELS = Path(__file__).resolve().parent / "fixtures" / "3dmodels"
MODEL_VARS = [f"KICAD{n}_3DMODEL_DIR" for n in ("", *range(5, 12))]


@pytest.fixture(autouse=True)
def _hermetic_3d_models(request, monkeypatch):
    """${KICAD<n>_3DMODEL_DIR} points at tests/fixtures/3dmodels (box models),
    never at the host's KiCad library: a board's models resolve the same on
    any host, so check_body_clash judges the same bodies everywhere and a test
    can't pass or fail by what the host happens to have installed (#757).

    A test that needs the real library says so with `@pytest.mark.host_models`;
    it is skipped where the host has none. Box models made from the stock
    library: tests/fixtures/3dmodels/make_boxes.py."""
    if request.node.get_closest_marker("host_models"):
        if not _host_models():
            pytest.skip("needs KiCad's stock 3D models, this host has none")
        return
    for k in MODEL_VARS:
        monkeypatch.setenv(k, str(MODELS))


@functools.cache
def _host_symbols() -> bool:
    root = Path(__file__).resolve().parents[1]
    sys.path[:0] = [str(root / "scripts"), str(root / "scripts" / "lib")]
    import env
    return env.kicad_symbol_dir() is not None


@functools.cache
def _host_models() -> bool:
    root = Path(__file__).resolve().parents[1]
    sys.path[:0] = [str(root / "scripts"), str(root / "scripts" / "lib")]
    import check_model_fit
    return bool(check_model_fit.stock_dirs())


@pytest.fixture(autouse=True)
def _never_file_documents(monkeypatch, tmp_path_factory):
    for k in [k for k in os.environ if k.startswith("DOC_")]:
        monkeypatch.delenv(k)
    # report_gen looks the board's filed documents up in cc-docs' register:
    # never the owner's, so a test reads an empty one unless it writes its own
    # (the env var reaches scripts a test runs as subprocesses too)
    empty = tmp_path_factory.mktemp("no-docs-register")
    monkeypatch.setenv("CC_DOCS_ROOT", str(empty))
    if "report_gen" in sys.modules:
        monkeypatch.setattr(sys.modules["report_gen"], "DOCS_HOME", empty)
    dirs = [d for d in os.environ.get("PATH", "").split(os.pathsep)
            if d and not os.access(os.path.join(d, "cc-docs"), os.X_OK)]
    monkeypatch.setenv("PATH", os.pathsep.join(dirs))
    assert shutil.which("cc-docs") is None


@pytest.fixture(autouse=True)
def _easyeda_offline(monkeypatch, tmp_path_factory):
    """No test fetches an LCSC footprint model or reads the user's cache: a
    test that needs models copies tests/fixtures/easyeda into its own dir."""
    monkeypatch.setenv("HWDE_EASYEDA_OFFLINE", "1")
    # nor asks JLC or LCSC for stock (test_stock_check fakes the sources)
    monkeypatch.setenv("HWDE_STOCK_OFFLINE", "1")
    # nor reads or fills the user's keyless stock cache (#1241)
    monkeypatch.setenv("HWDE_STOCK_CACHE",
                       str(tmp_path_factory.mktemp("stock_cache_empty")))
    # and writes no board's ledger.json unless it says so (test_ledger)
    monkeypatch.setenv("HWDE_LEDGER_WRITE", "0")
    # and fsyncs nothing (safelib): every file here is scratch, and on a
    # loaded disk each fsync costs seconds (test_attest spent 8 of 12 s in four)
    monkeypatch.setenv("HWDE_NO_FSYNC", "1")
    monkeypatch.setenv("HWDE_EASYEDA_CACHE",
                       str(tmp_path_factory.mktemp("easyeda_empty")))
    # The rate-limit latch is process state: one test's mocked 403 must not
    # skip every later test's fetches.
    mod = sys.modules.get("easyeda")
    if mod is not None and hasattr(mod, "reset_rate_limit"):
        mod.reset_rate_limit()
