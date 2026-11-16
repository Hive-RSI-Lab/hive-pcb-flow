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
import signal
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
                            cwd=cwd or repo, env=e, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


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


def run_now(repo: Path, tmp: Path, script: str, **env: str) -> subprocess.CompletedProcess:
    e = {**os.environ, "TMPDIR": str(tmp), **env}
    return subprocess.run([sys.executable, str(repo / "bin" / "frozen.py"), "--", "sh", "-c",
                           script], cwd=repo, env=e, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=60)


def test_a_folder_swapped_for_a_link_out_of_the_tree_leaves_its_target_alone(checkout, tmp_path):
    """A folder replaced by a link to a folder outside the checkout: status
    lists the old files under the link's name, and a copy that followed the
    link would delete or overwrite the files out there."""
    repo, _side, tmp = checkout
    (repo / "d").mkdir()
    (repo / "d" / "f").write_text("committed")
    git(repo, "add", "d")
    git(repo, "commit", "-q", "-m", "d")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f").write_text("outside")
    shutil.rmtree(repo / "d")
    (repo / "d").symlink_to(outside)
    git(repo, "add", "-A", "d")
    cp = run_now(repo, tmp, "readlink d")
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout.strip() == str(outside)
    assert (outside / "f").read_text() == "outside"
    assert sorted(p.name for p in outside.iterdir()) == ["f"]
    assert list(tmp.iterdir()) == []


def test_a_file_swapped_for_a_folder_is_copied(checkout):
    repo, _side, tmp = checkout
    (repo / "a.txt").unlink()
    (repo / "a.txt").mkdir()
    (repo / "a.txt" / "in").write_text("in a folder")
    cp = run_now(repo, tmp, "cat a.txt/in")
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout == "in a folder"


def test_a_folder_swapped_for_a_file_is_copied(checkout):
    repo, _side, tmp = checkout
    shutil.rmtree(repo / "sub")
    (repo / "sub").write_text("now a file")
    cp = run_now(repo, tmp, "cat sub")
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout == "now a file"


def test_an_untracked_repo_inside_and_hidden_edits_are_copied(checkout):
    """`git status` shows an untracked repo inside only as its folder, and does
    not show edits to files marked skip-worktree or assume-unchanged."""
    repo, _side, tmp = checkout
    inner = repo / "inner"
    inner.mkdir()
    (inner / "f").write_text("inner")
    git(inner, "init", "-q")
    git(repo, "update-index", "--skip-worktree", "sub/keep")
    (repo / "sub" / "keep").write_text("edited, skip-worktree")
    git(repo, "update-index", "--assume-unchanged", "bin/pcb-flow")
    (repo / "bin" / "pcb-flow").write_text("#!/bin/sh\necho edited, assume-unchanged\n")
    cp = run_now(repo, tmp, "cat inner/f; echo; cat sub/keep; echo; sh bin/pcb-flow")
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout.split("\n")[:3] == ["inner", "edited, skip-worktree", "edited, assume-unchanged"]


def test_git_variables_from_the_caller_do_not_aim_at_the_checkout(checkout):
    """A run started from a git hook inherits GIT_DIR and GIT_WORK_TREE; the copy
    and the run must still use the copy's repo, and the checkout stays as it is."""
    repo, side, tmp = checkout
    head = git(repo, "rev-parse", "HEAD")
    p = start(repo, side, tmp, ["--", "bin/show"], GIT_DIR=str(repo / ".git"),
              GIT_WORK_TREE=str(repo), GIT_INDEX_FILE=str(repo / ".git" / "index"))
    wait_ready(p, side)
    git(repo, "commit", "-q", "-m", "while waiting")
    later = git(repo, "rev-parse", "HEAD")
    rc, seen, err = finish(p, side)
    assert rc == 0, err
    assert seen["head"] == head and seen["branch"] == "work" and seen["remotes"] == ""
    assert seen["a"] == "queued" and "staged.txt" not in seen["tracked"]
    # The checkout itself is as the test left it: no checkout, no lost remote.
    assert git(repo, "rev-parse", "HEAD") == later
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "work"
    assert git(repo, "remote") == "origin"
    assert (repo / "a.txt").read_text() == "queued" and not (repo / "gone.txt").exists()


def test_sigint_to_the_runner_alone_reaches_the_command(checkout):
    """Not at a terminal (a queue, a script), a SIGINT goes to this process only;
    it passes it on rather than waiting for a command that never heard it."""
    repo, side, tmp = checkout
    p = start(repo, side, tmp, ["--", "bin/show"])
    wait_ready(p, side)
    p.send_signal(signal.SIGINT)
    try:
        p.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        (side / "go").write_text("1")  # let the job end rather than leave it waiting
        p.terminate()
        p.communicate(timeout=60)
        raise
    assert p.returncode == 128 + signal.SIGINT
    assert list(tmp.iterdir()) == []


# --- --root DIR (#1416): freeze another checkout, such as an old commit whose
# own pcb-flow predates `frozen`. The runner is this repo's bin/frozen.py.

def start_root(cwd: Path, side: Path, tmp: Path, args: list[str], **env: str) -> subprocess.Popen:
    e = {**os.environ, "TMPDIR": str(tmp), "FROZEN_SIDE": str(side), "PWD": str(cwd), **env}
    e.pop("HWDE_FROZEN_FROM", None)
    return subprocess.Popen([sys.executable, str(ROOT / "bin" / "frozen.py"), *args],
                            cwd=cwd, env=e, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


@pytest.fixture
def old_checkout(checkout):
    """The checkout as an old commit: no bin/frozen.py, committed that way."""
    repo, side, tmp = checkout
    git(repo, "rm", "-q", "bin/frozen.py")
    git(repo, "commit", "-q", "-m", "before frozen")
    return repo, side, tmp


def test_root_freezes_another_checkout_from_outside_it(old_checkout, tmp_path):
    """Planted: the other checkout drifts while the job waits; run from outside
    it, the job runs at the copy's top and sees the tree it was queued with."""
    repo, side, tmp = old_checkout
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    head = git(repo, "rev-parse", "HEAD")
    p = start_root(elsewhere, side, tmp, ["--root", str(repo), "--", "bin/show",
                                          f"{repo}/a.txt"])
    wait_ready(p, side)
    (repo / "a.txt").write_text("changed while waiting")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "while waiting")
    rc, seen, err = finish(p, side)
    assert rc == 0, err
    copy = seen["here"]
    assert copy.endswith("/mod") and copy != str(repo)
    assert seen["cwd"] == copy and seen["pwd"] == copy
    assert seen["args"] == [f"{copy}/a.txt"]
    assert seen["a"] == "queued" and seen["head"] == head
    assert seen["from"] == str(repo)
    assert f"{repo} at {head[:12]}" in err
    assert list(tmp.iterdir()) == []
    assert (repo / "a.txt").read_text() == "changed while waiting"


def test_root_equals_form_runs_dirs_own_pcb_flow_from_a_subfolder(old_checkout):
    repo, side, tmp = old_checkout
    p = start_root(repo / "sub", side, tmp, [f"--root={repo}", "test", "-k", "x"],
                   FROZEN_RC="5")
    wait_ready(p, side)
    rc, seen, err = finish(p, side)
    assert rc == 5, err
    copy = seen["here"]
    assert seen["argv0"] == f"{copy}/bin/show"  # DIR's pcb-flow, not this repo's
    assert seen["args"] == ["test", "-k", "x"]
    assert seen["cwd"] == f"{copy}/sub"  # inside DIR: mapped into the copy


def test_root_relative_path_is_taken_from_the_working_folder(old_checkout):
    repo, side, tmp = old_checkout
    p = start_root(repo.parent, side, tmp, ["--root", repo.name, "--", "bin/show"])
    wait_ready(p, side)
    rc, seen, err = finish(p, side)
    assert rc == 0, err
    assert seen["from"] == str(repo) and seen["a"] == "queued"


def test_root_without_pcb_flow_refuses_a_subcommand_but_runs_a_command(tmp_path):
    tree = tmp_path / "plain"
    tree.mkdir()
    (tree / "f.txt").write_text("here")
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    env = {**os.environ, "TMPDIR": str(tmp)}
    runner = [sys.executable, str(ROOT / "bin" / "frozen.py"), "--root", str(tree)]
    cp = subprocess.run([*runner, "test"], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert cp.returncode == 2 and "has no bin/pcb-flow" in cp.stderr
    cp = subprocess.run([*runner, "--", "sh", "-c", 'cat f.txt; [ "$PWD" != "' + str(tree) + '" ]'],
                        cwd=tmp_path, env=env, capture_output=True, text=True)
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout == "here"
    assert list(tmp.iterdir()) == []


@pytest.mark.parametrize("args", [["--root"], ["--root", "/nonexistent/x", "--", "true"],
                                  ["--root="], ["--root", "."], ["--root", ".", "--"]])
def test_root_usage_errors(args, tmp_path):
    cp = subprocess.run([sys.executable, str(ROOT / "bin" / "frozen.py"), *args],
                        cwd=tmp_path, env={**os.environ, "TMPDIR": str(tmp_path)},
                        capture_output=True, text=True)
    assert cp.returncode == 2, cp.stderr
    assert "usage: pcb-flow frozen" in cp.stderr or "is not a folder" in cp.stderr
    assert [p for p in tmp_path.iterdir()] == []


def test_root_subcommand_from_outside_keeps_the_callers_folder(old_checkout, tmp_path):
    """A SUBCOMMAND keeps the caller's folder, as without --root: a project's
    boards/ (pcb-flow reads $PWD/boards) must not become DIR's copy's."""
    repo, side, tmp = old_checkout
    project = tmp_path / "project"
    project.mkdir()
    p = start_root(project, side, tmp, ["--root", str(repo), "score", "boards/x"])
    wait_ready(p, side)
    rc, seen, err = finish(p, side)
    assert rc == 0, err
    assert seen["argv0"] == f"{seen['here']}/bin/show"
    assert seen["cwd"] == str(project) and seen["pwd"] == str(project)
    assert seen["args"] == ["score", "boards/x"]


def test_root_given_twice_is_a_usage_error(tmp_path):
    cp = subprocess.run([sys.executable, str(ROOT / "bin" / "frozen.py"), "--root", ".",
                         "--root=.", "--", "true"], cwd=tmp_path,
                        env={**os.environ, "TMPDIR": str(tmp_path)}, capture_output=True, text=True)
    assert cp.returncode == 2 and "usage: pcb-flow frozen" in cp.stderr
