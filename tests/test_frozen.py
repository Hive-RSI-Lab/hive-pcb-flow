"""bin/frozen.py, `pcb-flow frozen` (#1372): a run judges the checkout as it was
when it was asked for, not as it is when a slot in the heavy queue lets it start.

Each test builds a small git checkout with bin/frozen.py in it and runs the
runner there. The planted case changes the checkout while the job waits (an
edit, a commit, a restored file, a deleted one) and checks that the job still
sees the state it was queued with."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

# The job: say it has started, wait for the test to let it go (the heavy
# queue), then report the tree it sees.
SHOW = r'''#!/usr/bin/env python3
import json, os, subprocess, sys, time
from pathlib import Path
here = Path(__file__).resolve().parent.parent
side = Path(os.environ["FROZEN_SIDE"])
(side / "ready").write_text("1")
while not (side / "go").exists():
    time.sleep(0.05)
def read(p):
    f = here / p
    return f.read_text() if f.exists() else None
def g(*a):
    return subprocess.run(["git", "-C", str(here), *a], capture_output=True, text=True).stdout.strip()
print(json.dumps({
    "argv0": sys.argv[0], "args": sys.argv[1:], "cwd": os.getcwd(),
    "env": os.environ.get("FROZEN_X"), "pwd": os.environ.get("PWD"),
    "from": os.environ.get("HWDE_FROZEN_FROM"), "here": str(here),
    "a": read("a.txt"), "gone": read("gone.txt"), "new": read("new.txt"),
    "staged": read("staged.txt"), "ignored": read("x.log"),
    "renamed_old": read("old.txt"), "renamed_new": read("moved.txt"),
    "head": g("rev-parse", "HEAD"), "branch": g("rev-parse", "--abbrev-ref", "HEAD"),
    "remotes": g("remote"), "tracked": sorted(g("ls-files").split()),
}))
sys.exit(int(os.environ.get("FROZEN_RC", "0")))
'''


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
                           *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def checkout(tmp_path):
    """A checkout with every kind of uncommitted state: an edit, a deletion,
    an untracked file, an ignored one, a staged new file and a staged rename."""
    repo = tmp_path / "mod"
    (repo / "bin").mkdir(parents=True)
    (repo / "sub").mkdir()
    shutil.copy2(ROOT / "bin" / "frozen.py", repo / "bin" / "frozen.py")
    (repo / "bin" / "show").write_text(SHOW)
    (repo / "bin" / "show").chmod(0o755)
    # The copy's own pcb-flow, for `frozen SUBCOMMAND`.
    (repo / "bin" / "pcb-flow").write_text('#!/bin/sh\nexec "$(dirname "$0")/show" "$@"\n')
    (repo / "bin" / "pcb-flow").chmod(0o755)
    (repo / ".gitignore").write_text("*.log\n")
    (repo / "a.txt").write_text("committed")
    (repo / "gone.txt").write_text("committed")
    (repo / "old.txt").write_text("renamed")
    (repo / "sub" / "keep").write_text("")
    git(repo, "init", "-q", "-b", "work")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "remote", "add", "origin", "https://example.invalid/mod.git")
    (repo / "a.txt").write_text("queued")
    (repo / "gone.txt").unlink()
    (repo / "new.txt").write_text("untracked")
    (repo / "x.log").write_text("ignored")
    (repo / "staged.txt").write_text("staged")
    git(repo, "add", "staged.txt")
    git(repo, "mv", "old.txt", "moved.txt")
    side = tmp_path / "side"
    side.mkdir()
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    return repo, side, tmp


def start(repo: Path, side: Path, tmp: Path, args: list[str], cwd: Path | None = None,
          **env: str) -> subprocess.Popen:
    e = {**os.environ, "TMPDIR": str(tmp), "FROZEN_SIDE": str(side), **env}
    e.pop("HWDE_FROZEN_FROM", None)
    return subprocess.Popen([sys.executable, str(repo / "bin" / "frozen.py"), *args],
                            cwd=cwd or repo, env=e, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)


def wait_ready(p: subprocess.Popen, side: Path) -> None:
    t0 = time.monotonic()
    while not (side / "ready").exists():
        assert p.poll() is None, p.communicate()
        assert time.monotonic() - t0 < 60, "the job never started"
        time.sleep(0.05)


def finish(p: subprocess.Popen, side: Path) -> tuple[int, dict, str]:
    (side / "go").write_text("1")
    out, err = p.communicate(timeout=60)
    return p.returncode, (json.loads(out) if out.strip() else {}), err


def test_planted_drift_while_queued(checkout):
    """The checkout changes while the job waits; the job sees the queued tree."""
    repo, side, tmp = checkout
    head = git(repo, "rev-parse", "HEAD")
    p = start(repo, side, tmp, ["--", "bin/show"])
    wait_ready(p, side)
    # Everything an agent might do while its run waits for a heavy slot.
    (repo / "a.txt").write_text("changed while waiting")
    (repo / "gone.txt").write_text("back while waiting")
    (repo / "new.txt").unlink()
    (repo / "later.txt").write_text("later")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "while waiting")
    rc, seen, err = finish(p, side)
    assert rc == 0, err
    assert seen["a"] == "queued"
    assert seen["gone"] is None
    assert seen["new"] == "untracked"
    assert seen["staged"] == "staged"
    assert (seen["renamed_old"], seen["renamed_new"]) == (None, "renamed")
    assert seen["ignored"] is None  # ignored files stay behind
    assert seen["head"] == head and seen["branch"] == "work"
    assert seen["remotes"] == ""  # nothing in the copy can push to the checkout's repo
    assert "later.txt" not in seen["tracked"]
    # The run was in a copy, the copy is gone, and the checkout kept its edits.
    assert seen["here"] != str(repo) and not Path(seen["here"]).exists()
    assert list(tmp.iterdir()) == []
    assert (repo / "a.txt").read_text() == "changed while waiting"
    assert git(repo, "rev-parse", "HEAD") != head
    assert f"at {head[:12]}" in err and "2 deleted" in err


def test_paths_into_the_checkout_point_into_the_copy(checkout):
    repo, side, tmp = checkout
    p = start(repo, side, tmp,
              ["--", str(repo / "bin" / "show"), str(repo / "a.txt"), f"--in={repo}/sub",
               f"{repo}-other/a.txt", "rel/a.txt"],
              cwd=repo / "sub", FROZEN_X=f"{repo}/bin:/usr/bin:{repo}")
    wait_ready(p, side)
    rc, seen, err = finish(p, side)
    assert rc == 0, err
    copy = seen["here"]
    assert copy.endswith("/mod") and copy != str(repo)
    assert seen["argv0"] == f"{copy}/bin/show"  # the command itself
    assert seen["args"] == [f"{copy}/a.txt", f"--in={copy}/sub",
                            f"{repo}-other/a.txt", "rel/a.txt"]  # a mere prefix stays
    assert seen["env"] == f"{copy}/bin:/usr/bin:{copy}"
    assert seen["cwd"] == f"{copy}/sub" and seen["pwd"] == f"{copy}/sub"
    assert seen["from"] == str(repo)


def test_subcommand_runs_the_copys_pcb_flow_and_keeps_its_status(checkout):
    repo, side, tmp = checkout
    p = start(repo, side, tmp, ["test", "-k", "x"], FROZEN_RC="7")
    wait_ready(p, side)
    rc, seen, err = finish(p, side)
    assert rc == 7, err
    assert seen["argv0"] == f"{seen['here']}/bin/show"
    assert seen["args"] == ["test", "-k", "x"]
    assert seen["a"] == "queued"
    assert list(tmp.iterdir()) == []


def test_a_killed_run_removes_its_copy(checkout):
    repo, side, tmp = checkout
    p = start(repo, side, tmp, ["--", "bin/show"])
    wait_ready(p, side)
    p.terminate()
    p.communicate(timeout=60)
    assert p.returncode == 128 + 15
    assert list(tmp.iterdir()) == []


def test_a_tree_with_no_git_is_copied_whole(tmp_path):
    tree = tmp_path / "pub"
    (tree / "bin").mkdir(parents=True)
    shutil.copy2(ROOT / "bin" / "frozen.py", tree / "bin" / "frozen.py")
    (tree / "f.txt").write_text("here")
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    cp = subprocess.run([sys.executable, str(tree / "bin" / "frozen.py"), "--", "sh", "-c",
                         f'cat {tree}/f.txt; [ "$PWD" != "{tree}" ]'],
                        cwd=tree, env={**os.environ, "TMPDIR": str(tmp)},
                        capture_output=True, text=True)
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout == "here"
    assert list(tmp.iterdir()) == []


def test_usage():
    cp = subprocess.run([sys.executable, str(ROOT / "bin" / "frozen.py")],
                        capture_output=True, text=True)
    assert cp.returncode == 2 and "usage: pcb-flow frozen" in cp.stderr


def test_a_linked_path_to_the_checkout_is_repointed_too(checkout, tmp_path):
    """A shell that reached the checkout through a link keeps the link's name
    in $PWD and in the paths it passes; those point into the copy as well."""
    repo, side, tmp = checkout
    link = tmp_path / "link"
    link.symlink_to(repo)
    p = start(repo, side, tmp, ["--", "../bin/show", f"{link}/a.txt"], cwd=link / "sub",
              PWD=str(link / "sub"))
    wait_ready(p, side)
    rc, seen, err = finish(p, side)
    assert rc == 0, err
    assert seen["args"] == [f"{seen['here']}/a.txt"]
    assert seen["pwd"] == f"{seen['here']}/sub"
