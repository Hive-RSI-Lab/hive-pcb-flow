"""A command a script tells the user to run is one `pcb-flow` can run (#895).

check_fp_attr, check_courtyard and bom_cpl told the user to run
`fpfix.py --lib ...` / `fpfix.py --pcb ...`, but fpfix lives in
scripts/lib (other scripts import it as `fpfix`), so `pcb-flow fpfix` looked
for scripts/fpfix.py and found nothing. bin/pcb-flow now hands the tools in
its `lib_tools` list to scripts/lib/<name>.py, and the messages say
`pcb-flow fpfix`.

Static half: every `pcb-flow NAME` or `NAME.py --flag` written in a
scripts/*.py string (messages, fix hints, docstrings) names a script, a
lib tool the wrapper routes, or one of the wrapper's own commands. Dynamic
half: the wrapper, run over a stand-in python that prints what it was handed,
runs the file the name resolves to, and fpfix runs as a script from there.
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
LIB = SCRIPTS / "lib"
WRAPPER = ROOT / "bin" / "pcb-flow"
PRISTINE = ROOT / "tests" / "fixtures" / "lib" / "pristine"

NAMED = re.compile(r"(?<![\w/.-])(?:pcb-flow ([a-z][a-z_]*)\b"
                   r"|([a-z][a-z_]*)\.py --[a-z])")
# Names in script text that are not ours to run, each with the reason.
NOT_COMMANDS = {
    "route": "KiCadRoutingTools' own route.py, whose flag route_tune cites",
    "on": "prose in errata_corpus: 'pcb-flow on rev A'",
}


def _lib_tools(wrapper_text: str) -> set[str]:
    m = re.search(r'^\s*lib_tools="([^"]*)"', wrapper_text, re.M)
    return set(m.group(1).split()) if m else set()


def _wrapper_commands(wrapper_text: str) -> set[str]:
    """The wrapper's own commands: its case labels and `$1 = name` tests."""
    out = set(re.findall(r'"\$\{1:-\}" = ([a-z][\w-]*) \]', wrapper_text))
    for label in re.findall(r"^([a-z][\w.| -]*)\)\s*$", wrapper_text, re.M):
        out |= {w.strip() for w in label.split("|")}
    return out


def resolves(name: str, root: Path, wrapper_text: str) -> bool:
    scripts = root / "scripts"
    if (scripts / f"{name}.py").is_file():
        return True
    if name in _lib_tools(wrapper_text):
        return "def main(" in (scripts / "lib" / f"{name}.py").read_text(encoding="utf-8")
    return name in _wrapper_commands(wrapper_text)


def named_commands(source: str) -> set[str]:
    """Every command name a module's string literals tell the user to run."""
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for a, b in NAMED.findall(node.value):
                names.add(a or b)
    return names - set(NOT_COMMANDS)


def unresolved(root: Path) -> dict[str, list[str]]:
    wrapper_text = (root / "bin" / "pcb-flow").read_text(encoding="utf-8")
    bad: dict[str, list[str]] = {}
    for p in sorted((root / "scripts").glob("*.py")):
        for name in named_commands(p.read_text(encoding="utf-8")):
            if not resolves(name, root, wrapper_text):
                bad.setdefault(name, []).append(p.name)
    return bad


def test_every_command_a_script_names_is_runnable():
    assert unresolved(ROOT) == {}


def test_a_named_command_with_no_script_is_caught(tmp_path):
    """The planted fault: #895's message on a tree with no route for it."""
    (tmp_path / "scripts" / "lib").mkdir(parents=True)
    (tmp_path / "bin").mkdir()
    (tmp_path / "scripts" / "lib" / "fpfix.py").write_text("def main(): pass\n")
    (tmp_path / "scripts" / "check_x.py").write_text(
        'fix = f"fpfix.py --lib {d} sets it, then pcb-flow check_x --pcb {p}"\n')
    wrapper = WRAPPER.read_text(encoding="utf-8")
    (tmp_path / "bin" / "pcb-flow").write_text(
        re.sub(r'^\s*lib_tools="[^"]*"', '\tlib_tools=""', wrapper, flags=re.M))
    assert unresolved(tmp_path) == {"fpfix": ["check_x.py"]}
    # the quiet twin: the same tree with the wrapper's route resolves
    shutil.copy(WRAPPER, tmp_path / "bin" / "pcb-flow")
    assert unresolved(tmp_path) == {}


def test_the_fpfix_messages_name_the_command():
    for f in ("check_fp_attr.py", "check_courtyard.py", "bom_cpl.py"):
        text = (SCRIPTS / f).read_text(encoding="utf-8")
        assert "pcb-flow fpfix" in text and "fpfix.py --" not in text, f


def _wrapper_hands(tmp_path: Path, *argv: str) -> list[str]:
    """What bin/pcb-flow passes to its python, through a python that prints it."""
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True, exist_ok=True)
    fake = venv / "bin" / "python"
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    fake.chmod(0o755)
    shutil.copy(ROOT / "requirements.lock", venv / ".hwde-lock")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("HWDE_", "HIVE_"))}
    env.update(HWDE_VENV=str(venv), HOME=str(tmp_path), DISPLAY=":0",
               HIVE_TOOLS=str(tmp_path / "no-tools"),
               HIVE_MODULES=str(tmp_path / "no-modules"))
    r = subprocess.run(["sh", str(WRAPPER), *argv], capture_output=True,
                       text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.splitlines()


def test_pcb_flow_fpfix_runs_libs_fpfix(tmp_path):
    out = _wrapper_hands(tmp_path, "fpfix", "--lib", "x.pretty", "--dry-run")
    assert out == [str(LIB / "fpfix.py"), "--lib", "x.pretty", "--dry-run"]
    # `fpfix.py`, as the old messages wrote it, lands there too
    assert _wrapper_hands(tmp_path, "fpfix.py", "--pcb", "b.kicad_pcb")[0] == str(LIB / "fpfix.py")


def test_an_ordinary_script_still_runs_from_scripts(tmp_path):
    out = _wrapper_hands(tmp_path, "check_fp_attr", "--pcb", "b.kicad_pcb")
    assert out == [str(SCRIPTS / "check_fp_attr.py"), "--pcb", "b.kicad_pcb"]


def test_fpfix_runs_as_a_script_on_a_library(tmp_path):
    """The file the wrapper hands over runs on its own and repairs a pull."""
    lib = tmp_path / "aiee.pretty"
    shutil.copytree(PRISTINE, lib)
    out = tmp_path / "r.json"
    cmd = [sys.executable, "-I", str(LIB / "fpfix.py"), "--lib", str(lib), "--out", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    rep = json.loads(out.read_text())
    assert rep["status"] == "pass" and rep["changed"] == 4
    # a second run finds nothing left to repair
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    rep = json.loads(out.read_text())
    assert rep["changed"] == 0 and rep["actions"] == 0
