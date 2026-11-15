"""Net-tie bridges (#1008): KRT's writer moved every net-less copper graphic
to silkscreen, a net-tie footprint's bridge included, and DRC can't see the
pads come apart (each pad keeps its own net; footprint graphics aren't
connectivity). krt_launch now holds net-tie footprints out of that move, and
check_net_tie fails a net-tie group whose pads the footprint's own copper
leaves in more than one piece."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import check_net_tie  # noqa: E402
import env  # noqa: E402
import krt_launch  # noqa: E402
from _boards import stock_footprints  # noqa: E402

LAUNCH = SCRIPTS / "lib" / "krt_launch.py"
GOLD = ROOT / "tests" / "golden" / "blinky2" / "blinky2.kicad_pcb"
MUT = ROOT / "tests" / "golden" / "mutants" / "net-tie-open" / "blinky2.kicad_pcb"
# the mutant's bridge, on silk (its uuid makes the anchor unique)
MUT_BRIDGE = '(layer "F.SilkS")\n\t\t\t(uuid "4e71e000-0000-4000-8000-000000000004")'

HEAD = ('(kicad_pcb (version 20241229) (generator "pcbnew")\n'
        '  (general (thickness 1.6))\n'
        '  (layers (0 "F.Cu" signal) (4 "In1.Cu" signal) (6 "In2.Cu" signal)'
        ' (2 "B.Cu" signal) (1 "F.Mask" user) (3 "B.Mask" user)'
        ' (5 "F.SilkS" user) (7 "B.SilkS" user) (13 "F.Paste" user)'
        ' (15 "B.Paste" user) (25 "Edge.Cuts" user) (29 "B.CrtYd" user)'
        ' (31 "F.CrtYd" user) (33 "B.Fab" user) (35 "F.Fab" user))\n'
        '  (net 0 "")\n')
# a SolderJumper-3: pads 1 x 1.5 mm at x -1.3, 0, 1.3
PADS = (("1", -1.3, "GND"), ("2", 0, "/BOOT0"), ("3", 1.3, "+3V3"))
# x span of the bridge from pad 1 to pad 2, and from pad 2 to pad 3
SPAN = {"12": (-0.9, -0.4), "23": (0.4, 0.9)}


def poly(tag, x0, x1, layer, uid, y=0.3):
    return (f'({tag} (pts (xy {x0} -{y}) (xy {x1} -{y}) (xy {x1} {y}) '
            f'(xy {x0} {y})) (stroke (width 0) (type solid)) (fill yes) '
            f'(layer "{layer}") (uuid "{uid}"))')


def jumper(ref="JP1", groups=("1, 2",), bridges=None, side="F", at="50 40",
           pads=PADS, width=1, extra=()):
    """A jumper footprint, one item per line. groups: its
    net_tie_pad_groups (None: not a net tie); bridges: {"12" | "23": layer},
    each an fp_poly with uuid "<ref>-<name>" (default: 1-2 on copper)."""
    bridges = {"12": f"{side}.Cu"} if bridges is None else bridges
    lines = [f'  (footprint "Jumper:Test_{ref}" (layer "{side}.Cu") (at {at}) '
             f'(uuid "{ref}-fp")',
             f'    (property "Reference" "{ref}" (at 0 -2 0) '
             f'(layer "{side}.SilkS") (uuid "{ref}-ref"))']
    if groups is not None:
        lines.append("    (net_tie_pad_groups "
                     + " ".join(f'"{g}"' for g in groups) + ")")
    lines += [f"    {poly('fp_poly', *SPAN[k], layer, f'{ref}-{k}')}"
              for k, layer in bridges.items()]
    lines += [f"    {e}" for e in extra]
    lines += [f'    (pad "{n}" smd rect (at {x} 0) (size {width} 1.5) '
              f'(layers "{side}.Cu" "{side}.Mask") (net "{net}") '
              f'(uuid "{ref}-p{n}"))' for n, x, net in pads]
    return "\n".join(lines) + "\n  )\n"


# a copper logo that is a footprint, and one drawn on the board: both net-less
LOGO = ('  (footprint "Logo:Test_Copper" (layer "F.Cu") (at 70 40) '
        '(uuid "LOGO1-fp")\n'
        '    (property "Reference" "LOGO1" (at 0 -2 0) (layer "F.SilkS") '
        '(uuid "LOGO1-ref"))\n'
        f'    {poly("fp_poly", -1, 1, "F.Cu", "LOGO1-art", y=1)}\n'
        '  )\n')
ART = f'  {poly("gr_poly", 80, 82, "F.Cu", "art", y=1)}\n'


def board(*items):
    return HEAD + "".join(items) + ")\n"


def layer_of(text, uid):
    """The layer of the one-line item whose uuid is `uid`."""
    line = next(ln for ln in text.splitlines() if f'(uuid "{uid}")' in ln)
    return re.search(r'\(layer "([^"]+)"\)', line).group(1)


# KRT's mover, cut down: every one-line fp_poly / gr_poly on copper to silk
FAKE_WRITER = r'''
import re

seen = []


def move_copper_graphics_to_silkscreen(content, *a, **kw):
    seen.append((content, a, kw))
    return re.sub(r'(\((?:fp|gr)_poly .*?\(layer ")([FB])\.Cu"', r'\1\2.SilkS"',
                  content)
'''


def fake_writer():
    mod = types.ModuleType("kicad_writer")
    exec(FAKE_WRITER, mod.__dict__)
    return mod


def _judge(tmp_path, name, *items):
    p = tmp_path / f"{name}.kicad_pcb"
    p.write_text(board(*items), encoding="utf-8")
    return check_net_tie.check(p)


# ------------------------------------------------- krt_launch: the held move

def test_net_tie_footprints_finds_whole_blocks():
    # a string with an unbalanced paren and escaped quotes inside a net tie
    odd = ('(property "Description" "solder (bridged) \\"1(2\\" jumper" '
           '(at 0 0 0) (layer "F.Fab") (uuid "JP1-d"))')
    jp1 = jumper("JP1", extra=[odd])
    jp2 = jumper("JP2", groups=("1, 2", "2, 3"), side="B", at="60 40",
                 bridges={"12": "B.Cu", "23": "B.Cu"})
    text = board(jp1, LOGO, jumper("JP3", groups=None), jp2, ART)
    spans = krt_launch.net_tie_footprints(text)
    assert [text[s:e] for s, e in spans] == [jp1.strip(), jp2.strip()]
    assert krt_launch.net_tie_footprints(board(LOGO, ART)) == []


def test_held_mover_keeps_net_tie_footprints_byte_for_byte():
    mod = fake_writer()
    orig = mod.move_copper_graphics_to_silkscreen
    kept = krt_launch.keep_net_tie_copper(orig)
    assert kept.__wrapped__ is orig
    jp1 = jumper("JP1")
    jp2 = jumper("JP2", groups=("1, 2", "2, 3"), side="B", at="60 40",
                 bridges={"12": "B.Cu", "23": "B.Cu"})
    src = board(jp1, LOGO, jp2, ART)
    out = kept(src, 3, mode="x")
    assert jp1 in out and jp2 in out
    # the logos still move, the footprint's and the board's
    assert layer_of(out, "LOGO1-art") == "F.SilkS"
    assert layer_of(out, "art") == "F.SilkS"
    (seen, a, kw), = mod.seen
    assert "net_tie_pad_groups" not in seen and "LOGO1-art" in seen
    assert a == (3,) and kw == {"mode": "x"}
    # what the mover alone does to the same board (#1008)
    bare = orig(src)
    assert layer_of(bare, "JP1-12") == "F.SilkS"
    assert layer_of(bare, "JP2-23") == "B.SilkS"


def test_held_mover_leaves_a_board_without_net_ties_to_krt():
    mod = fake_writer()
    src = board(jumper("JP1", groups=None), LOGO)
    out = krt_launch.keep_net_tie_copper(mod.move_copper_graphics_to_silkscreen)(src)
    assert mod.seen[0][0] is src
    # a jumper that doesn't declare net_tie_pad_groups isn't held
    assert layer_of(out, "JP1-12") == "F.SilkS"


def test_held_mover_fails_loud_on_a_lost_or_doubled_footprint():
    src = board(jumper("JP1"), LOGO)
    mark = krt_launch._HELD.format(0)
    for mover in (lambda c: c.replace(mark, ""), lambda c: c + mark):
        with pytest.raises(RuntimeError, match="placeholder"):
            krt_launch.keep_net_tie_copper(mover)(src)


def test_patch_rebinds_the_mover_wherever_krt_holds_it(monkeypatch):
    kw = fake_writer()
    orig = kw.move_copper_graphics_to_silkscreen
    holder = types.ModuleType("output_writer")      # imported it by name
    holder.move_copper_graphics_to_silkscreen = orig
    other = types.ModuleType("unrelated")
    other.move_copper_graphics_to_silkscreen = own = (lambda c: c)
    for mod in (kw, holder, other):
        monkeypatch.setitem(sys.modules, mod.__name__, mod)
    krt_launch._patch_net_tie_copper()
    patched = kw.move_copper_graphics_to_silkscreen
    assert patched is not orig and patched.__wrapped__ is orig
    assert holder.move_copper_graphics_to_silkscreen is patched
    assert other.move_copper_graphics_to_silkscreen is own


def test_patch_without_krt_writer_is_quiet(monkeypatch):
    monkeypatch.setitem(sys.modules, "kicad_writer", None)
    krt_launch._patch_net_tie_copper()


def test_patch_reraises_a_broken_krt_import(tmp_path, monkeypatch):
    (tmp_path / "kicad_writer.py").write_text(
        "import hwde_no_such_module_1008\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "kicad_writer", raising=False)
    with pytest.raises(ModuleNotFoundError) as exc:
        krt_launch._patch_net_tie_copper()
    assert exc.value.name == "hwde_no_such_module_1008"


def test_patch_stops_on_a_krt_without_the_mover(monkeypatch):
    monkeypatch.setitem(sys.modules, "kicad_writer",
                        types.ModuleType("kicad_writer"))
    with pytest.raises(SystemExit, match="move_copper_graphics_to_silkscreen"):
        krt_launch._patch_net_tie_copper()


def test_launcher_keeps_the_bridge_through_a_krt_script(tmp_path):
    """A stand-in KRT: the script writes through a module that imported the
    mover by name, as KRT's output_writer does."""
    (tmp_path / "kicad_writer.py").write_text(FAKE_WRITER, encoding="utf-8")
    (tmp_path / "output_writer.py").write_text(
        "from kicad_writer import move_copper_graphics_to_silkscreen\n\n\n"
        "def write(src, dst):\n"
        "    with open(src, encoding='utf-8') as f:\n"
        "        text = move_copper_graphics_to_silkscreen(f.read())\n"
        "    with open(dst, 'w', encoding='utf-8') as f:\n"
        "        f.write(text)\n", encoding="utf-8")
    (tmp_path / "route_x.py").write_text(
        "import sys\nimport output_writer\n"
        "output_writer.write(sys.argv[1], sys.argv[2])\n", encoding="utf-8")
    (tmp_path / "in.kicad_pcb").write_text(board(jumper("JP1"), LOGO, ART),
                                           encoding="utf-8")
    subprocess.run([sys.executable, str(LAUNCH), "route_x.py", "in.kicad_pcb",
                    "out.kicad_pcb"], cwd=tmp_path, capture_output=True,
                   text=True, check=True, timeout=120)
    out = (tmp_path / "out.kicad_pcb").read_text(encoding="utf-8")
    assert layer_of(out, "JP1-12") == "F.Cu"
    assert layer_of(out, "LOGO1-art") == "F.SilkS"
    assert layer_of(out, "art") == "F.SilkS"
    assert check_net_tie.check(tmp_path / "out.kicad_pcb")[0] == []


# KRT's real mover, run through the launcher: `bare` is the mover the
# launcher wrapped, `kept` what KRT's code now calls
PROBE = r'''
import sys
from pathlib import Path

import kicad_writer
import output_writer
import plane_io
from kicad_writer import move_copper_graphics_to_silkscreen as kept

bare = kept.__wrapped__
for mod in (output_writer, plane_io):
    assert mod.move_copper_graphics_to_silkscreen is kept, mod.__name__
src, out_bare, out_kept = map(Path, sys.argv[1:4])
for p in sorted(src.glob("*.kicad_pcb")):
    text = p.read_text(encoding="utf-8")
    (out_bare / p.name).write_text(bare(text), encoding="utf-8")
    (out_kept / p.name).write_text(kept(text), encoding="utf-8")
'''


def _through_krt(tmp_path, boards):
    """Write `boards` ({name: text}), run KRT's real mover on each through
    the launcher; the (bare, kept) output dirs."""
    krt = env.find_krt()
    if krt is None:
        pytest.skip("needs KiCadRoutingTools")
    src, bare, kept = (tmp_path / d for d in ("src", "bare", "kept"))
    for d in (src, bare, kept):
        d.mkdir()
    for name, text in boards.items():
        (src / f"{name}.kicad_pcb").write_text(text, encoding="utf-8")
    probe = tmp_path / "probe.py"
    probe.write_text(PROBE, encoding="utf-8")
    cp = subprocess.run(
        [sys.executable, str(LAUNCH), str(probe), str(src), str(bare),
         str(kept)], cwd=krt, capture_output=True, text=True, timeout=300,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert cp.returncode == 0, cp.stderr[-3000:]
    return bare, kept


def test_real_krt_mover_keeps_net_tie_bridges(tmp_path):
    jp2 = jumper("JP2", side="B", at="60 40")
    bare, kept = _through_krt(
        tmp_path, {"b": board(jumper("JP1"), LOGO, jp2, ART)})
    was = (bare / "b.kicad_pcb").read_text(encoding="utf-8")
    now = (kept / "b.kicad_pcb").read_text(encoding="utf-8")
    assert layer_of(was, "JP1-12") == "F.SilkS"     # #1008
    assert layer_of(was, "JP2-12") == "B.SilkS"
    assert layer_of(now, "JP1-12") == "F.Cu"
    assert layer_of(now, "JP2-12") == "B.Cu"
    for text in (was, now):
        assert layer_of(text, "LOGO1-art") == "F.SilkS"
        assert layer_of(text, "art") == "F.SilkS"
    viols = check_net_tie.check(bare / "b.kicad_pcb")[0]
    assert [(v["refs"], v["layer"], v["moved_to"]) for v in viols] == [
        (["JP1"], "F.Cu", "F.SilkS"), (["JP2"], "B.Cu", "B.SilkS")]
    assert check_net_tie.check(kept / "b.kicad_pcb")[0] == []


def _embed(lib, mod_text, back):
    """A board holding one library footprint as JP1, on the back if `back`
    (layers swapped; the geometry needn't mirror for this)."""
    name = re.match(r'\(footprint\s+"([^"]+)"', mod_text).group(1)
    text = mod_text.replace(f'(footprint "{name}"',
                            f'(footprint "{lib}:{name}" (at 50 40)', 1)
    text = text.replace('"REF**"', '"JP1"', 1)
    if back:
        for s in ("Cu", "SilkS", "Mask", "Paste", "Fab", "CrtYd", "Adhes"):
            text = (text.replace(f'"F.{s}"', f'"@.{s}"')
                    .replace(f'"B.{s}"', f'"F.{s}"')
                    .replace(f'"@.{s}"', f'"B.{s}"'))
    return HEAD + text + ")\n"


def test_stock_net_tie_footprints_pass_and_keep_their_copper(tmp_path):
    """Every net-tie footprint KiCad ships passes as shipped and comes out of
    the launched KRT mover unchanged; the ones the bare mover opens are
    caught on their own side, the bridge named on its silk."""
    fp = stock_footprints()
    if fp is None:
        pytest.skip("needs KiCad's footprint library")
    mods = sorted(p for p in fp.glob("*.pretty/*.kicad_mod")
                  if "net_tie_pad_groups" in p.read_text(encoding="utf-8"))
    if not mods:
        pytest.skip("no net-tie footprints in KiCad's library")
    boards = {f"{m.parent.stem}--{m.stem}{'--B' if back else ''}":
              _embed(m.parent.stem, m.read_text(encoding="utf-8"), back)
              for m in mods for back in (False, True)}
    bare, kept = _through_krt(tmp_path, boards)
    opened = 0
    for name, text in boards.items():
        side = "B" if name.endswith("--B") else "F"
        assert (kept / f"{name}.kicad_pcb").read_text(encoding="utf-8") == text, name
        assert check_net_tie.check(tmp_path / "src" / f"{name}.kicad_pcb")[0] == [], name
        for v in check_net_tie.check(bare / f"{name}.kicad_pcb")[0]:
            opened += 1
            assert (v["layer"], v["moved_to"]) == (f"{side}.Cu", f"{side}.SilkS"), name
    assert opened, "KRT's bare mover opened no stock net tie"


# ------------------------------------------------------------- check_net_tie

def test_mutant_bridge_on_silk_is_caught():
    viols, seen, not_judged = check_net_tie.check(MUT)
    assert seen == {"net_ties": 1, "groups": 1} and not_judged == []
    (v,) = viols
    assert v["kind"] == "net_tie_open" and v["severity"] == "error"
    assert v["source"] == "check_net_tie" and v["refs"] == ["JP1"]
    assert v["footprint"] == "Jumper:SolderJumper-3_P1.3mm_Bridged12_Pad1.0x1.5mm"
    assert v["layer"] == "F.Cu" and v["pads"] == ["1", "2"]
    assert v["pieces"] == [["1"], ["2"]] and v["moved_to"] == "F.SilkS"
    assert v["nets"] == ["/BOOT0", "GND"]
    assert list(v["pos"]) == pytest.approx([143.35, 134.0])
    assert "JP1" in v["msg"] and "F.SilkS" in v["msg"]


def test_bridge_back_on_copper_passes(tmp_path):
    text = MUT.read_text(encoding="utf-8")
    assert text.count(MUT_BRIDGE) == 1
    p = tmp_path / "fixed.kicad_pcb"
    p.write_text(text.replace(MUT_BRIDGE, MUT_BRIDGE.replace("F.SilkS", "F.Cu")),
                 encoding="utf-8")
    viols, seen, _ = check_net_tie.check(p)
    # pad 3 stays apart: it's in no group
    assert viols == [] and seen["groups"] == 1


def test_golden_has_nothing_to_judge():
    payload, out = check_net_tie.run(["--pcb", str(GOLD)])
    assert payload["status"] == "pass" and out is None
    assert payload["nothing_to_judge"].startswith("no net tie")


def test_three_pad_group_needs_both_bridges(tmp_path):
    both = jumper(groups=("1, 2, 3",), bridges={"12": "F.Cu", "23": "F.Cu"})
    assert _judge(tmp_path, "both", both)[0] == []
    one = jumper(groups=("1, 2, 3",), bridges={"12": "F.Cu"})
    (v,) = _judge(tmp_path, "one", one)[0]
    assert v["pads"] == ["1", "2", "3"] and v["pieces"] == [["1", "2"], ["3"]]
    assert v["moved_to"] is None and v["nets"] == ["+3V3", "/BOOT0", "GND"]


def test_each_group_is_judged_on_its_own(tmp_path):
    jp = jumper(groups=("1, 2", "2, 3"), bridges={"12": "F.Cu", "23": "F.SilkS"})
    viols, seen, _ = _judge(tmp_path, "two", jp)
    assert seen["groups"] == 2
    assert [(v["pads"], v["pieces"], v["moved_to"]) for v in viols] == [
        (["2", "3"], [["2"], ["3"]], "F.SilkS")]


def test_back_side_bridge_on_silk(tmp_path):
    (v,) = _judge(tmp_path, "back",
                  jumper(side="B", bridges={"12": "B.SilkS"}))[0]
    assert v["layer"] == "B.Cu" and v["moved_to"] == "B.SilkS"


def test_arc_in_the_bridge_outline_counts(tmp_path):
    # only the arc reaches pad 2 (x 0.7..1.3): its chord stops at x 0.5
    arc = ('(fp_poly (pts (xy -1.2 -0.1) (arc (start 0.5 -0.1) (mid 0.8 0) '
           '(end 0.5 0.1)) (xy -1.2 0.1)) (stroke (width 0) (type solid)) '
           '(fill yes) (layer "F.Cu") (uuid "JP1-arc"))')
    jp = jumper(bridges={}, pads=PADS[:1] + (("2", 1.0, "/BOOT0"),),
                width=0.6, extra=[arc])
    assert _judge(tmp_path, "arc", jp)[0] == []
    chord = arc.replace("(arc (start 0.5 -0.1) (mid 0.8 0) (end 0.5 0.1))",
                        "(xy 0.5 -0.1) (xy 0.5 0.1)")
    (v,) = _judge(tmp_path, "chord", jumper(
        bridges={}, pads=PADS[:1] + (("2", 1.0, "/BOOT0"),), width=0.6,
        extra=[chord]))[0]
    assert v["pieces"] == [["1"], ["2"]] and v["moved_to"] is None


def test_group_short_of_two_pads_is_not_judged(tmp_path):
    p = tmp_path / "missing.kicad_pcb"
    p.write_text(board(jumper(groups=("1, 4",))), encoding="utf-8")
    payload, _ = check_net_tie.run(["--pcb", str(p)])
    assert payload["status"] == "pass" and payload["violations"] == []
    assert payload["not_judged"] == ["JP1 group 1, 4: 1 of its pads on the board"]
    assert "nothing_to_judge" in payload


def test_cli_contract(tmp_path):
    def cli(*args):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "check_net_tie.py"), *args],
            capture_output=True, text=True, timeout=120)
    out = tmp_path / "r.json"
    assert cli("--pcb", str(MUT), "--out", str(out)).returncode == 1
    rep = json.loads(out.read_text(encoding="utf-8"))
    assert rep["status"] == "violations"
    assert [v["kind"] for v in rep["violations"]] == ["net_tie_open"]
    cp = cli("--pcb", str(GOLD))
    assert cp.returncode == 0 and json.loads(cp.stdout)["status"] == "pass"
    cp = cli("--pcb", str(tmp_path / "nope.kicad_pcb"))
    assert cp.returncode == 2 and json.loads(cp.stdout)["status"] == "error"
