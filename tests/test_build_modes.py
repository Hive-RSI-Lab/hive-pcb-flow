"""U18 build-mode tests: a TARGET LEARNING OUTCOME derives scope AND binding.

bb-buck is the defect this step exists for. Its P2 derived 40 x 30 mm because
the outline is the radiator (R_ba 39 -> 31 C/W with area); the owner handed P5
35 x 25 at H1; P6 closed "OUTLINE IS FINAL AT 35 x 25" with 0.05 mm of slack on
all four edges. Correct board, wrong lesson - the geometry bound the stage that
was supposed to teach the geometry.

Criteria -> tests:
  - the mode table is test-pinned against build-modes.md (targets, scope tiers,
    binding levels, every derived triple)   -> test_target_table_is_pinned,
    test_scope_tiers_are_pinned, test_binding_levels_are_pinned
  - a doc whose table breaks its own vocabulary does not parse
                                            -> test_doc_lint_*
  - tokens resolve, unknown targets name the known ones
                                            -> test_detect_*, test_resolve_*
  - the same brief at canonical and at constrained produces DIFFERENT outlines,
    the canonical run records the relaxation and ends no larger than its own
    placement needs                         -> test_geometry_plan_*,
    test_canonical_and_constrained_diverge_on_the_same_board (smoke)
  - a product-scope target admits what block-only excludes, and the reviewers
    flag the ABSENCE at that tier           -> test_product_scope_*,
    test_reviewer_contracts_carry_the_tier_rule
  - the binding is MECHANICAL, not prose: board_init refuses a fixed outline
                                            -> test_board_init_*
  - P0 refuses an artifact that hides its mode
                                            -> test_check_requirements_*
  - router --validate green + the wiring is pinned
                                            -> test_full_run_wiring
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import board_init  # noqa: E402
import check_requirements  # noqa: E402
import modeslib  # noqa: E402
import state as state_cli  # noqa: E402
import task_router  # noqa: E402
from _boards import board_path, need_board  # noqa: E402

DOC = SKILL / "reference" / "build-modes.md"
AGENTS = SKILL / "agents"
BB = board_path("bb-buck")

# The table this step ships, written out so a doc edit that changes the
# contract has to change the test too (the plan's "test-pinned against
# build-modes.md").
TARGETS = {
    "stage-placement":   ("block-only", "canonical", "P6"),
    "stage-routing":     ("block-only", "canonical", "P7"),
    "stage-schematic":   ("block-only", "canonical", "P4"),
    "block-basics":      ("block-only", "canonical", None),
    "block-integration": ("block+interfaces", "bounded", None),
    "production-block":  ("product", "product", None),
    "fit-check":         ("block-only", "constrained", None),
}
CONFORMING = "\n".join(
    [f"## {n}. {name}\n\nbody {n}"
     for n, name in check_requirements.SECTION_NAMES.items() if n != 9]
    + ["## 9. Open questions", "", "1. A numbered question? (default: yes)"]
) + "\n"


# ---------------------------------------------------------------- helpers

def _ws(tmp_path: Path, brief: str | None = None,
        requirements: str | None = None) -> Path:
    ws = tmp_path / "ws"
    (ws / "brief").mkdir(parents=True)
    if brief is not None:
        (ws / "brief" / "brief.md").write_text(brief, encoding="utf-8")
    (ws / "requirements.md").write_text(requirements or CONFORMING,
                                        encoding="utf-8")
    return ws


def _lint(ws: Path) -> tuple[int, dict]:
    payload, _ = check_requirements.run(["--workspace", str(ws)])
    return (1 if any(v["severity"] == "error" for v in payload["violations"])
            else 0), payload


def _kinds(payload: dict) -> list[str]:
    return [v["kind"] for v in payload["violations"]]


def _mutant_doc(tmp_path: Path, old: str, new: str) -> Path:
    p = tmp_path / "build-modes.md"
    p.write_text(DOC.read_text(encoding="utf-8").replace(old, new, 1),
                 encoding="utf-8")
    return p


# =========================================================== the doc table

def test_target_table_is_pinned():
    """Every target's derived (scope, binding, stage) triple, from the doc."""
    doc = modeslib.parse_doc(DOC)
    assert set(doc["targets"]) == set(TARGETS)
    for name, (scope, binding, stage) in TARGETS.items():
        mode = modeslib.resolve(f"learning {name}:", doc)
        assert (mode["scope"], mode["binding"], mode["stage"]) == \
            (scope, binding, stage), name
        assert mode["teaches"], f"{name}: a target with no lesson is a label"


def test_scope_tiers_are_pinned():
    doc = modeslib.parse_doc(DOC)
    assert set(doc["scopes"]) == {"block-only", "block+interfaces", "product"}
    block = doc["scopes"]["block-only"]
    assert {"protection", "filtering", "indicators", "test-points",
            "second-rail"} <= set(block["excludes"])
    assert block["requires"] == []
    prod = doc["scopes"]["product"]
    assert prod["excludes"] == []
    assert {"protection", "filtering", "connectors", "thermal",
            "enclosure-fit"} <= set(prod["requires"])
    # a datasheet-required thermal solution is support, not a feature
    for tier, row in doc["scopes"].items():
        assert "thermal" not in row["excludes"], tier


def test_binding_levels_are_pinned():
    doc = modeslib.parse_doc(DOC)
    b = doc["bindings"]
    assert set(b) == {"canonical", "bounded", "constrained", "product"}
    assert b["canonical"]["geometry"] == "output" and \
        b["canonical"]["cap_factor"] is None
    assert b["bounded"]["geometry"] == "output" and \
        b["bounded"]["cap_factor"] == pytest.approx(1.30)
    assert b["constrained"]["geometry"] == "input"
    assert b["product"]["geometry"] == "input"
    assert set(b["product"]["also_binds"]) == {"cost", "thermal"}


def test_doc_lint_rejects_a_table_that_breaks_its_own_vocabulary(tmp_path):
    """The doc IS the registry, so a broken row must fail loudly rather than
    resolve to something plausible."""
    cases = [
        ("| `fit-check` | block-only |", "| `fit-check` | block-alone |",
         "unknown scope"),
        ("| `stage-routing` | block-only | canonical |",
         "| `stage-routing` | block-only | canonicals |", "unknown binding"),
        ("| `bounded` | output | +30% |", "| `bounded` | sideways | +30% |",
         "geometry must be input|output"),
        ("| `bounded` | output | +30% |", "| `bounded` | output | 30 |",
         "cap must be"),
        ("| `product` | - | protection,", "| `product` | protection | protection,",
         "both excludes and requires"),
        ("## Targets", "## Targetz", "no '## Targets' section"),
        # a section that loses its table must raise, never inherit the next
        # section's (the parser stops at the following heading)
        ("| `learning <target>:` | `<target>` |\n"
         "| `ultra bare bones design:` | block-basics |\n"
         "| `ultra-bare-bones:` | block-basics |\n", "", "has no table"),
    ]
    for old, new, msg in cases:
        p = _mutant_doc(tmp_path, old, new)
        assert p.read_text(encoding="utf-8") != DOC.read_text(encoding="utf-8"), \
            f"mutant {old!r} did not apply - the doc moved under the test"
        with pytest.raises(modeslib.ModeError, match=msg.replace("|", r"\|")):
            modeslib.parse_doc(p)


# =============================================================== the tokens

def test_detect_reads_only_the_opening_declaration():
    d = modeslib.load()
    assert modeslib.detect("learning stage-placement: a buck", d) == \
        "learning stage-placement:"
    assert modeslib.detect("# Brief\n\nultra bare bones design: a buck", d) == \
        "ultra bare bones design:"
    assert modeslib.detect("ultra-bare-bones: a buck", d) == \
        "ultra-bare-bones:"
    assert modeslib.detect("Design a buck.\n\nlearning fit-check: no", d) is None
    assert modeslib.detect("", d) is None


def test_detect_reads_a_blockquoted_brief():
    """An owner's brief quoted verbatim as a markdown blockquote still
    declares its mode; a quoted provenance line above the token is passed
    over, and plain prose before any token still decides None."""
    d = modeslib.load()
    want = modeslib.detect("learning stage-placement: a buck", d)
    assert modeslib.detect("> learning stage-placement: a buck", d) == want
    assert modeslib.detect(
        "# Brief\n\n> Owner brief, verbatim, received 2026-08-16\n>\n"
        "> learning stage-placement: a buck", d) == want
    assert modeslib.detect("Design a buck.\n\n> learning fit-check: no",
                           d) is None
    assert modeslib.detect("> Design a buck, please.", d) is None


def test_the_legacy_token_is_canonical_now():
    """The owner ruling (2026-08-16): `ultra bare bones design:` always meant
    'the smallest outline that keeps the layout HONEST', which is a canonical
    binding in prose. bb-buck's binding size was the accident."""
    need_board("bb-buck")
    legacy = modeslib.resolve_text(
        (BB / "brief" / "brief.md").read_text(encoding="utf-8"))
    assert legacy["target"] == "block-basics"
    assert legacy["binding"] == "canonical" and legacy["geometry_is_output"]
    assert legacy["alias"] is True
    assert modeslib.resolve("learning block-basics:")["scope"] == \
        legacy["scope"]


def test_resolve_unknown_target_names_the_known_ones():
    with pytest.raises(modeslib.ModeError) as exc:
        modeslib.resolve("learning stage-placment:")
    assert "unknown learning target" in str(exc.value)
    for t in TARGETS:
        assert t in str(exc.value)
    with pytest.raises(modeslib.ModeError, match="not a build-mode token"):
        modeslib.resolve("please design a buck")


def test_a_mode_never_relaxes_the_things_that_make_a_board_true():
    canonical = modeslib.resolve("learning stage-placement:")
    assert set(canonical["relaxes"]) == {"size", "aspect", "outline", "cost",
                                         "packaging"}
    for forbidden in ("gates", "coverage", "research"):
        assert forbidden in canonical["never_relaxed"]
        assert forbidden not in canonical["relaxes"]
    assert modeslib.resolve("learning fit-check:")["relaxes"] == []


# ============================================================ the geometry

def test_geometry_plan_canonical_makes_the_stated_size_lose():
    mode = modeslib.resolve("learning stage-placement:")
    at_p5 = modeslib.geometry_plan(mode, (35, 25))
    assert at_p5["binds"] is False
    assert at_p5["board_init_outline"] == "auto"
    assert at_p5["flow"] == "fit-after-place"
    assert at_p5["relaxed"] and "35x25" in at_p5["decision"]["what"]

    after_fit = modeslib.geometry_plan(mode, (35, 25), (40, 30))
    assert after_fit["final"] == [40, 30]          # bb-buck's own P2 answer
    assert after_fit["relaxed"] and after_fit["kept_stated"] is False
    assert "40x30" in after_fit["decision"]["what"]
    assert after_fit["decision"]["phase"] == "P6"
    # a stated size the layout does not need is dropped too (no cap at all)
    assert modeslib.geometry_plan(mode, (80, 60), (40, 30))["final"] == [40, 30]


def test_geometry_plan_bounded_keeps_a_stated_size_inside_the_cap():
    mode = modeslib.resolve("learning block-integration:")
    assert mode["cap_factor"] == pytest.approx(1.30)
    inside = modeslib.geometry_plan(mode, (44, 33), (40, 30))     # +10 %
    assert inside["final"] == [44, 33] and inside["kept_stated"] is True
    assert inside["relaxed"] is False and inside["decision"] is None
    beyond = modeslib.geometry_plan(mode, (60, 45), (40, 30))     # +50 %
    assert beyond["final"] == [40, 30] and beyond["relaxed"] is True
    smaller = modeslib.geometry_plan(mode, (35, 25), (40, 30))
    assert smaller["final"] == [40, 30] and smaller["relaxed"] is True


def test_geometry_plan_constrained_and_product_bind():
    for target in ("fit-check", "production-block"):
        mode = modeslib.resolve(f"learning {target}:")
        plan = modeslib.geometry_plan(mode, (35, 25))
        assert plan["binds"] is True, target
        assert plan["board_init_outline"] == "35x25", target
        assert plan["flow"] == "fixed" and plan["relaxed"] is False
        assert plan["decision"] is None
    # product binds cost and thermal on top of the size
    assert set(modeslib.resolve("learning production-block:")["also_binds"]) \
        == {"cost", "thermal"}


def test_geometry_plan_with_no_mode_is_todays_behaviour():
    assert modeslib.geometry_plan(None, (35, 25))["board_init_outline"] == \
        "35x25"
    assert modeslib.geometry_plan(None, None)["board_init_outline"] == "auto"


def test_parse_size():
    assert modeslib.parse_size("roughly 35 x 25 mm") == (35.0, 25.0)
    assert modeslib.parse_size("40x30") == (40.0, 30.0)
    assert modeslib.parse_size("no dimensions here") is None


# ================================================================== scope

def test_product_scope_admits_what_block_only_excludes():
    """Acceptance: a product-scope target admits the protection/filtering
    blocks that block-only excludes - and their ABSENCE becomes a finding."""
    block = modeslib.resolve("learning block-basics:")
    product = modeslib.resolve("learning production-block:")
    for feature in ("protection", "filtering"):
        assert modeslib.feature_verdict(block, feature) == "excluded"
        assert modeslib.feature_verdict(product, feature) == "required"
    # an interface tier keeps protection out but demands the conditioning
    integration = modeslib.resolve("learning block-integration:")
    assert modeslib.feature_verdict(integration, "protection") == "excluded"
    assert modeslib.feature_verdict(integration, "filtering") == "required"
    # anything outside both lists is judged on merit, mode or not
    assert modeslib.feature_verdict(block, "decoupling") == "normal"
    assert modeslib.feature_verdict(None, "protection") == "normal"


def test_summary_is_ascii_and_names_the_dials():
    s = modeslib.summary(modeslib.resolve("learning stage-placement:"))
    assert s.isascii()
    for bit in ("stage-placement", "block-only", "canonical", "P6"):
        assert bit in s
    assert modeslib.summary(None) == "no build mode"


# ================================================================== state

def test_state_mode_records_the_dials_and_the_relaxation(tmp_path):
    ws = tmp_path / "ws"
    state_cli.State.init(ws, "t")
    payload, _ = state_cli.run(["mode", "--workspace", str(ws), "--token",
                                "learning stage-placement:", "--stated",
                                "35x25"])
    rec = payload["mode"]
    assert (rec["target"], rec["scope"], rec["binding"], rec["stage"]) == \
        ("stage-placement", "block-only", "canonical", "P6")
    assert rec["geometry_is_output"] is True
    assert rec["stated_size"] == [35.0, 25.0]
    assert rec["board_init_outline"] == "auto"

    data = json.loads((ws / "state.json").read_text(encoding="utf-8"))
    assert data["mode"]["target"] == "stage-placement"
    assert [h["event"] for h in data["history"]].count("mode") == 1
    whats = [d["what"] for d in data["decisions"]]
    assert any("Build mode:" in w for w in whats)
    assert any("geometry relaxed" in w and "35x25" in w for w in whats), whats
    # and resume surfaces it - the orchestrator reads resume, not show
    resume, _ = state_cli.run(["resume", "--workspace", str(ws)])
    assert resume["mode"]["binding"] == "canonical"


def test_state_mode_refuses_a_bad_token_and_a_bad_size(tmp_path):
    ws = tmp_path / "ws"
    state_cli.State.init(ws, "t")
    with pytest.raises(Exception, match="unknown learning target"):
        state_cli.run(["mode", "--workspace", str(ws), "--token",
                       "learning nonsense:"])
    with pytest.raises(Exception, match="not a W x H size"):
        state_cli.run(["mode", "--workspace", str(ws), "--token",
                       "learning fit-check:", "--stated", "small"])
    assert "mode" not in json.loads(
        (ws / "state.json").read_text(encoding="utf-8"))


def test_a_constrained_run_records_no_relaxation(tmp_path):
    ws = tmp_path / "ws"
    state_cli.State.init(ws, "t")
    state_cli.run(["mode", "--workspace", str(ws), "--token",
                   "learning fit-check:", "--stated", "35x25"])
    data = json.loads((ws / "state.json").read_text(encoding="utf-8"))
    assert data["mode"]["board_init_outline"] == "35x25"
    assert not any("relaxed" in d["what"] for d in data["decisions"])


# ==================================================== board_init, the tooth

def test_board_init_refuses_a_fixed_outline_where_the_size_is_earned(tmp_path):
    ws = tmp_path / "ws"
    state_cli.State.init(ws, "t")
    state_cli.run(["mode", "--workspace", str(ws), "--token",
                   "learning stage-placement:"])
    kicad = ws / "kicad"

    assert board_init.mode_outline_guard(kicad, "auto")["binding"] == \
        "canonical"
    # a fixed size is built, and the report says it was not earned
    note = board_init.mode_outline_guard(kicad, "35x25")[
        "fixed_outline_override"]
    for bit in ("OUTPUT of the placement", "did NOT come from the placement",
                "--outline auto", "board_edit.py --outline fit",
                "state.py decision"):
        assert bit in note
    # explicit consent words it as a choice, and SAYS SO in the report
    rec = board_init.mode_outline_guard(kicad, "35x25", allow_fixed=True)
    assert "did NOT come from the placement" in rec["fixed_outline_override"]


def test_board_init_guard_is_silent_without_a_mode(tmp_path):
    ws = tmp_path / "ws"
    state_cli.State.init(ws, "t")
    assert board_init.mode_outline_guard(ws / "kicad", "35x25") is None
    # a constrained mode says nothing either - the size is the input
    state_cli.run(["mode", "--workspace", str(ws), "--token",
                   "learning fit-check:"])
    rec = board_init.mode_outline_guard(ws / "kicad", "35x25")
    assert rec["binding"] == "constrained"
    assert "fixed_outline_override" not in rec
    # no workspace at all (the golden corpus, a scratch export) = no opinion
    assert board_init.mode_outline_guard(REPO / "tests" / "golden",
                                         "35x25") is None


def test_board_init_cli_does_not_refuse_a_fixed_outline(tmp_path):
    """Once exit 2 before the netlist was read; now the outline is the
    agent's call, so the run goes on to the netlist (missing here, so that is
    the error) and never stops on the outline itself."""
    ws = tmp_path / "ws"
    state_cli.State.init(ws, "t")
    state_cli.run(["mode", "--workspace", str(ws), "--token",
                   "learning block-basics:"])
    cp = subprocess.run(
        [sys.executable, str(SCRIPTS / "board_init.py"),
         "--netlist", str(tmp_path / "does-not-exist.net"), "--name", "b",
         "--out", str(ws / "kicad"), "--layers", "2", "--outline", "35x25"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    payload = json.loads(cp.stdout)
    assert "refused" not in payload.get("error", "")
    assert "does-not-exist" in payload.get("error", "")


# ======================================================= P0, the lint tooth

def test_check_requirements_passes_a_conforming_mode_artifact(tmp_path):
    req = CONFORMING.replace(
        "body 1", "BUILD MODE: learning stage-placement (scope block-only, "
                  "binding canonical, stage under study P6).").replace(
        "body 5", "Outline: RELAXABLE (canonical) - roughly 35 x 25 mm.")
    ws = _ws(tmp_path, "learning stage-placement: a buck\n", req)
    code, payload = _lint(ws)
    assert code == 0, payload["violations"]
    assert payload["brief_token"] == "learning stage-placement:"
    assert payload["mode"]["target"] == "stage-placement"
    assert payload["mode"]["stage"] == "P6"


def test_check_requirements_fails_an_unnamed_mode(tmp_path):
    ws = _ws(tmp_path, "learning stage-placement: a buck\n")
    code, payload = _lint(ws)
    assert code == 1 and _kinds(payload) == ["req_mode_unnamed"]
    v = payload["violations"][0]
    assert v["target"] == "stage-placement" and v["binding"] == "canonical"
    assert "stage-placement" in v["msg"] and "canonical" in v["msg"]


def test_check_requirements_reads_a_blockquoted_brief(tmp_path):
    """The owner's brief quoted verbatim as a blockquote used to switch the
    whole mode leg off: no token, so req_mode_unnamed could never fire and
    the lint passed. The quoted token now counts."""
    ws = _ws(tmp_path, "> Owner brief, verbatim\n>\n"
                       "> learning stage-placement: a buck\n")
    code, payload = _lint(ws)
    assert payload["brief_token"] == "learning stage-placement:"
    assert code == 1 and _kinds(payload) == ["req_mode_unnamed"]


def test_check_requirements_fails_an_unmarked_relaxable_size(tmp_path):
    """The bb-buck failure mode: a number in section 5 that nothing marks as
    relaxable becomes a hard cap at P5 two checkpoints later."""
    req = CONFORMING.replace(
        "body 1", "Mode: learning stage-placement, binding canonical.").replace(
        "body 5", "Outline: 35 x 25 mm.")
    ws = _ws(tmp_path, "learning stage-placement: a buck\n", req)
    code, payload = _lint(ws)
    assert code == 1 and _kinds(payload) == ["req_mode_unmarked_size"]
    v = payload["violations"][0]
    assert v["stated"] == [35.0, 25.0] and v["section"] == 5
    assert "RELAXABLE" in v["msg"]
    # either marker clears it
    for marker in ("RELAXABLE (canonical) 35 x 25 mm",
                   "no HARD cap; roughly 35 x 25 mm"):
        (ws / "requirements.md").write_text(
            req.replace("Outline: 35 x 25 mm.", f"Outline: {marker}"),
            encoding="utf-8")
        code, payload = _lint(ws)
        assert code == 0, payload["violations"]
    # and a constrained binding never asks for the marker
    (ws / "brief" / "brief.md").write_text("learning fit-check: a buck\n",
                                           encoding="utf-8")
    (ws / "requirements.md").write_text(
        req.replace("learning stage-placement, binding canonical",
                    "learning fit-check, binding constrained"),
        encoding="utf-8")
    code, payload = _lint(ws)
    assert code == 0, payload["violations"]


def test_check_requirements_flags_an_unknown_token_and_stray_prose(tmp_path):
    ws = _ws(tmp_path, "learning stage-placment: a buck\n")
    code, payload = _lint(ws)
    assert code == 1 and _kinds(payload) == ["req_mode_unknown"]
    assert "unknown learning target" in payload["violations"][0]["msg"]
    assert payload["mode"] is None

    # prose alone is not a declaration
    ws2 = _ws(tmp_path / "b", "just design a buck, please\n",
              CONFORMING.replace("body 1", "BUILD MODE: ultra-bare-bones."))
    code, payload = _lint(ws2)
    assert code == 0 and _kinds(payload) == ["req_mode_stray"]
    assert payload["violations"][0]["severity"] == "warning"


def test_the_real_boards_and_bb_buck_under_the_new_leg():
    """The five mode-less boards stay green; bb-buck - the board that started
    this - is now non-conforming, and honestly so: its brief's token is
    canonical and its section 1 never said the geometry was an output."""
    need_board("stm32-blinky", "usb-buck", "pd-trigger", "lumina-par",
               "lumina-strobe", "bb-buck")
    for board in ("stm32-blinky", "usb-buck", "pd-trigger", "lumina-par",
                  "lumina-strobe"):
        code, payload = _lint(board_path(board))
        assert code == 0, (board, payload["violations"])
        assert payload["mode"] is None and payload["brief_token"] is None
    code, payload = _lint(BB)
    assert code == 1 and _kinds(payload) == ["req_mode_unnamed"]
    assert payload["mode"]["binding"] == "canonical"
    assert payload["brief_file"] == "brief/brief.md"
    # section 5 already says "no HARD cap" - P0 got that part right
    assert "req_mode_unmarked_size" not in _kinds(payload)


# ================================================================== wiring

def test_full_run_wiring():
    """The recipe and the registry both carry the mode, and the router still
    validates (every flag the new steps name exists)."""
    assert task_router.validate_registry() == []
    steps = yaml.safe_load(
        (SKILL / "reference" / "tasks.yaml").read_text(encoding="utf-8")
    )["verbs"]["full-run"]["steps"]
    dos = [s["do"] for s in steps if "do" in s]
    assert any("state.py mode --workspace {ws} --token" in d for d in dos)
    assert any("board_edit.py --pcb {pcb} --outline fit" in d for d in dos)
    # the fit step lands AFTER the place gate and BEFORE drc_routed
    order = [s.get("gate") or s.get("do", "") for s in steps]
    fit = next(i for i, s in enumerate(order) if "--outline fit" in str(s))
    assert order.index("place") < fit < order.index("drc_routed")

    recipe = (SKILL / "reference" / "recipes" / "full-run.md").read_text(
        encoding="utf-8")
    assert "state.py\nmode --token" in recipe or "state.py mode" in recipe
    assert "board_edit --outline fit" in recipe
    assert "noted unearned" in recipe               # the P5 rule
    assert len(recipe.splitlines()) <= 120


def test_agent_contracts_carry_the_binding_rule():
    analyst = (AGENTS / "requirements-analyst.md").read_text(encoding="utf-8")
    assert "RELAXABLE" in analyst and "check_requirements.py" in analyst
    assert "learning <target>:" in analyst

    architect = (AGENTS / "architect.md").read_text(encoding="utf-8")
    assert "GEOMETRY IS AN OUTPUT" in architect
    assert "board_edit --outline fit" in architect
    assert "state.py decision" in architect

    placement = (AGENTS / "placement.md").read_text(encoding="utf-8")
    assert "canonical" in placement and "board_edit.py --outline fit" in placement
    assert "never to fill or to fit the outline" in placement
    assert "PROVISIONAL room" in placement


def test_reviewer_contracts_carry_the_tier_rule():
    """A class the tier excludes is not a finding; a class it REQUIRES is one
    when absent - both reviewers, both halves."""
    for name in ("schematic-reviewer.md", "verify-reviewer.md"):
        text = (AGENTS / name).read_text(encoding="utf-8")
        assert "build-modes.md" in text, name
        assert "EXCLUDES" in text or "excludes" in text, name
        assert "REQUIRES" in text or "requires" in text, name
        assert "product" in text, name
    verify = (AGENTS / "verify-reviewer.md").read_text(encoding="utf-8")
    assert "EARNED" in verify        # compare to what the design earned


def test_skill_md_describes_the_two_dials():
    text = (SKILL / "docs" / "playbook.md").read_text(encoding="utf-8")
    assert "learning <target>:" in text
    assert "state.py mode --token" in text
    for bit in ("SCOPE TIER", "BINDING LEVEL", "build-modes.md"):
        assert bit in text


# =================================================================== smoke

@pytest.mark.smoke
def test_canonical_and_constrained_diverge_on_the_same_board(tmp_path):
    """Acceptance: the same board under the two bindings ends up with
    DIFFERENT outlines - the constrained one keeps the size it was given, the
    canonical one shrinks to what its placement actually needs and records the
    relaxation. Run on a copy of bb-buck, whose placement was squeezed into
    35 x 25; `fit` therefore returns 35.9 x 25.9 (0.05 mm slack + 2 x 0.5 mm
    margin), which is the whole point: the size was never earned.
    """
    need_board("bb-buck")
    def stage(name: str) -> Path:
        ws = tmp_path / name
        ws.mkdir()
        shutil.copy2(BB / "state.json", ws / "state.json")
        shutil.copytree(BB / "kicad", ws / "kicad")
        (ws / "reports").mkdir()
        return ws

    def outline(pcb: Path) -> tuple[float, float]:
        import geom  # noqa: E402  (lib dir on sys.path)
        x1, y1, x2, y2 = geom.BoardGeom.from_file(pcb).outline.bounds
        return round(x2 - x1, 3), round(y2 - y1, 3)

    # --- constrained: the stated size binds, nothing moves
    con = stage("constrained")
    state_cli.run(["mode", "--workspace", str(con), "--token",
                   "learning fit-check:", "--stated", "35x25"])
    before = outline(con / "kicad" / f"{BB.name}.kicad_pcb")
    assert before == (35.0, 25.0)
    con_state = json.loads((con / "state.json").read_text(encoding="utf-8"))
    assert con_state["mode"]["board_init_outline"] == "35x25"
    assert not any("relaxed" in d["what"] for d in con_state["decisions"])

    # --- canonical: the placement earns the size
    can = stage("canonical")
    state_cli.run(["mode", "--workspace", str(can), "--token",
                   "learning stage-placement:", "--stated", "35x25"])
    pcb = can / "kicad" / f"{BB.name}.kicad_pcb"
    cp = subprocess.run(
        [sys.executable, str(SCRIPTS / "board_edit.py"), "--pcb", str(pcb),
         "--outline", "fit", "--margin", "0.5", "--workspace", str(can)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert cp.returncode == 0, cp.stdout
    payload = json.loads(cp.stdout)
    assert payload["applied"] is True and payload["blocking"] == []

    after = outline(pcb)
    assert after != before, "the two bindings produced the same outline"
    x1, y1, x2, y2 = payload["outline"]["after"]["content_bbox"]
    assert after[0] <= (x2 - x1) + 2 * 0.5 + 0.01      # no larger than needed
    assert after[1] <= (y2 - y1) + 2 * 0.5 + 0.01

    can_state = json.loads((can / "state.json").read_text(encoding="utf-8"))
    assert any("geometry relaxed" in d["what"] and "35x25" in d["what"]
               for d in can_state["decisions"])
    assert can_state["edits"][-1]["class"] == "outline_change"
    # and the plan agrees with what actually happened on the board
    plan = modeslib.geometry_plan(
        modeslib.resolve("learning stage-placement:"), (35, 25), after)
    assert plan["final"] == list(after) and plan["relaxed"] is True
