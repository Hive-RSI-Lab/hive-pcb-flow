"""bin/hwde in a published copy (hwde publish) runs on its own python.

A check sandbox has a fresh HOME and only /work and /hive/tools mounted, so
the copy at /hive/tools/hwde must start with no venv to build and no uv: this
fakes that layout (the copy's python is this test's own) and asks it for its
pins. The publish itself downloads Python and the lock's packages, so it is
not run here.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(hwde: Path, home: Path, *args: str, modules: Path | None = None) -> subprocess.CompletedProcess:
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "HIVE_TOOLS": str(home / "no-tools"),
           "HIVE_MODULES": str(modules or home / "no-modules")}
    return subprocess.run([str(hwde), *args], env=env, cwd=home, capture_output=True, text=True)


def test_published_copy_uses_its_own_python(tmp_path):
    copy = tmp_path / "tools" / "hwde"
    (copy / "bin").mkdir(parents=True)
    shutil.copy2(ROOT / "bin" / "hwde", copy / "bin" / "hwde")
    shutil.copy2(ROOT / "requirements.lock", copy / "requirements.lock")
    # env prints the tools as scripts/lib/env.py resolves them; a
    # published copy carries the scripts (publish tars scripts/)
    lib = Path("scripts") / "lib"
    shutil.copytree(ROOT / lib, copy / lib, ignore=shutil.ignore_patterns("__pycache__"))
    py = copy / "python" / "cpython-3.14.0-linux-x86_64-gnu" / "bin" / "python3.14"
    py.parent.mkdir(parents=True)
    os.symlink(sys.executable, py)
    home = tmp_path / "home"
    home.mkdir()
    r = run(copy / "bin" / "hwde", home, "env")
    assert r.returncode == 0, r.stderr
    assert f"export HWDE_PY='{py}'" in r.stdout
    assert not (home / ".cache" / "hwde" / "venv").exists()


def fake_copy(tmp_path: Path) -> tuple[Path, Path]:
    """A published copy (VERSION, its own python) and that python."""
    copy = tmp_path / "tools" / "hwde"
    (copy / "bin").mkdir(parents=True)
    shutil.copy2(ROOT / "bin" / "hwde", copy / "bin" / "hwde")
    shutil.copy2(ROOT / "requirements.lock", copy / "requirements.lock")
    (copy / "VERSION").write_text("old\n")
    py = copy / "python" / "cpython-3.14.0-linux-x86_64-gnu" / "bin" / "python3.14"
    py.parent.mkdir(parents=True)
    os.symlink(sys.executable, py)
    return copy, py


def fake_module(tmp_path: Path, lock: str) -> Path:
    """The installed module as the Hive keeps it: a hive-hwde folder at main."""
    mod = tmp_path / "modules" / "hive-hwde"
    (mod / "bin").mkdir(parents=True)
    shutil.copy2(ROOT / "bin" / "hwde", mod / "bin" / "hwde")
    (mod / "requirements.lock").write_text(lock)
    return mod


def test_published_copy_runs_the_module_at_main(tmp_path):
    copy, py = fake_copy(tmp_path)
    mod = fake_module(tmp_path, (ROOT / "requirements.lock").read_text())
    home = tmp_path / "home"
    home.mkdir()
    r = run(copy / "bin" / "hwde", home, "env", modules=mod.parent)
    assert r.returncode == 0, r.stderr
    assert f"export HWDE_MODULE='{mod}'" in r.stdout
    assert f"export HWDE_PY='{py}'" in r.stdout  # the copy's python, no venv
    assert not (home / ".cache" / "hwde" / "venv").exists()


def test_published_copy_finds_the_module_under_its_new_name(tmp_path):
    """After the rename the folder is hive-pcb-flow and the command bin/pcb-flow."""
    copy, py = fake_copy(tmp_path)
    mod = tmp_path / "modules" / "hive-pcb-flow"
    (mod / "bin").mkdir(parents=True)
    shutil.copy2(ROOT / "bin" / "pcb-flow", mod / "bin" / "pcb-flow")
    (mod / "requirements.lock").write_text((ROOT / "requirements.lock").read_text())
    home = tmp_path / "home"
    home.mkdir()
    r = run(copy / "bin" / "hwde", home, "env", modules=mod.parent)
    assert r.returncode == 0, r.stderr
    assert f"export HWDE_MODULE='{mod}'" in r.stdout


def test_published_copy_keeps_its_own_code_when_the_lock_moved(tmp_path):
    copy, py = fake_copy(tmp_path)
    mod = fake_module(tmp_path, (ROOT / "requirements.lock").read_text() + "newpkg==1.0\n")
    home = tmp_path / "home"
    home.mkdir()
    r = run(copy / "bin" / "hwde", home, "env", modules=mod.parent)
    assert r.returncode == 0, r.stderr
    assert f"export HWDE_MODULE='{copy}'" in r.stdout
    assert f"export HWDE_PY='{py}'" in r.stdout


def test_publish_needs_an_existing_dir(tmp_path):
    r = run(ROOT / "bin" / "hwde", tmp_path, "publish", str(tmp_path / "missing"))
    assert r.returncode == 2
    assert "usage: pcb-flow publish" in r.stderr


def ruff_copy(tmp_path: Path) -> tuple[Path, Path]:
    """A published copy with the code publish ships (scripts, bin, ruff.toml), no tests/."""
    copy, py = fake_copy(tmp_path)
    # A link to a venv's python loses the venv (pyvenv.cfg sits beside the
    # link's target's parent, not the link): exec it, so ruff is importable.
    py.unlink()
    py.write_text(f"#!/bin/sh\nexec {sys.executable} \"$@\"\n")
    py.chmod(0o755)
    (copy / "scripts").mkdir()
    (copy / "scripts" / "ok.py").write_text("x = 1\n")
    shutil.copy2(ROOT / "ruff.toml", copy / "ruff.toml")
    home = tmp_path / "home"
    home.mkdir()
    return copy, home


def test_published_copy_lints_without_a_tests_folder(tmp_path):
    copy, home = ruff_copy(tmp_path)
    r = run(copy / "bin" / "hwde", home, "lint")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "E902" not in r.stdout


def test_published_copy_lint_still_finds_a_fault(tmp_path):
    copy, home = ruff_copy(tmp_path)
    (copy / "scripts" / "bad.py").write_text("y = undefined_name\n")
    r = run(copy / "bin" / "hwde", home, "lint")
    assert r.returncode == 1
    assert "F821" in r.stdout


def test_published_copy_test_says_it_has_no_tests(tmp_path):
    copy, home = ruff_copy(tmp_path)
    r = run(copy / "bin" / "hwde", home, "test")
    assert r.returncode == 2
    assert "has no tests/" in r.stderr
