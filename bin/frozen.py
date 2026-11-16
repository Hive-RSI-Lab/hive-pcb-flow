"""pcb-flow frozen: run a command on a frozen copy of the checkout (#1372).

    pcb-flow frozen SUBCOMMAND [ARGS...]   the copy's own `bin/pcb-flow SUBCOMMAND`
                                           (test, test-slow, check-all, score, ...)
    pcb-flow frozen -- CMD [ARGS...]       any command, run in the copy

`pcb-flow test`, `test-slow`, `check-all` and `score` read the checkout while
they run: pytest imports each test module when it reaches it, and the gates
start `scripts/*.py` from the checkout. A run that waits in the heavy queue
judges the tree as it is when the job starts, and an edit made while it runs
gives a result that belongs to neither tree. This copies the checkout as it is
now and runs the command there, so the result is about the tree as it was when
the run was asked for, whatever happens to the checkout meanwhile.

The copy:
- is a git clone of the checkout's HEAD (`--shared`: it borrows the objects, so
  it is quick and small), on the same branch name and commit, so anything that
  asks git for the commit or the tracked files gets the checkout's answer;
- carries the checkout's uncommitted edits, deletions and untracked files
  (but not ignored ones) on top, as unstaged changes;
- has no remote, so nothing run in it can push to the checkout's repo;
- goes in $TMPDIR and is removed when the command ends.

Every path into the checkout (an argument, an environment value, the command
itself, the working folder) points into the copy instead. The run exits with
the command's status. What the command writes inside the copy goes with it, so
this suits runs that read the tree, not ones meant to write into it.

Stdlib only, like bin/pin.py: it runs before the venv exists.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
USAGE = ("usage: pcb-flow frozen SUBCOMMAND [ARGS...]   (test, test-slow, check-all, score, ...)\n"
         "       pcb-flow frozen -- CMD [ARGS...]       any command, in the copy")


class FrozenError(Exception):
    pass


def git(root: Path, *args: str) -> bytes:
    # No optional locks: `git status` here must not rewrite the checkout's index.
    cp = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    if cp.returncode != 0:
        raise FrozenError(f"git {' '.join(args)}: {cp.stderr.decode(errors='replace').strip()}")
    return cp.stdout


def is_git_root(root: Path) -> bool:
    cp = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                        capture_output=True, text=True)
    return cp.returncode == 0 and Path(cp.stdout.strip()).resolve() == root.resolve()


def changed_paths(root: Path) -> list[str]:
    """Every path whose working-tree state differs from HEAD: edited, staged,
    added, deleted, renamed (both names) or untracked (not ignored)."""
    out = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all",
              "--ignore-submodules=none").decode("utf-8", "surrogateescape")
    parts = out.split("\0")
    paths: list[str] = []
    i = 0
    while i < len(parts):
        e = parts[i]
        i += 1
        if len(e) < 4:
            continue
        paths.append(e[3:])
        if e[0] in "RC":  # the next field is the old name
            paths.append(parts[i])
            i += 1
    return paths


def copy_entry(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_symlink() or dst.is_file():
        dst.unlink()
    elif dst.is_dir():
        shutil.rmtree(dst)
    if src.is_symlink():
        os.symlink(os.readlink(src), dst)
    else:
        shutil.copy2(src, dst)


def freeze(root: Path, dest: Path) -> dict:
    """Copy root as it is into dest (which must not exist). Returns a summary."""
    if not is_git_root(root):
        # Not a git checkout (a published copy): copy the tree as it stands.
        shutil.copytree(root, dest, symlinks=True,
                        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
        return {"commit": None, "changed": 0, "deleted": 0}
    sha = git(root, "rev-parse", "HEAD").decode().strip()
    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD").decode().strip()
    common = Path(git(root, "rev-parse", "--path-format=absolute",
                      "--git-common-dir").decode().strip())
    # The paths that differ from HEAD, listed before the clone so an edit made
    # while it runs can't slip in between the list and the copy of a file.
    paths = changed_paths(root)
    subprocess.run(["git", "clone", "-q", "--shared", "--no-checkout", str(common), str(dest)],
                   check=True, capture_output=True)
    git(dest, "remote", "remove", "origin")
    if branch and branch != "HEAD":
        git(dest, "checkout", "-q", "-B", branch, sha)
    else:
        git(dest, "checkout", "-q", "--detach", sha)
    changed = deleted = 0
    for rel in dict.fromkeys(paths):
        src, dst = root / rel, dest / rel
        if src.is_symlink() or src.is_file():
            copy_entry(src, dst)
            changed += 1
        elif os.path.lexists(dst) and not os.path.lexists(src):
            if dst.is_dir() and not dst.is_symlink():
                shutil.rmtree(dst)
            else:
                dst.unlink()
            deleted += 1
    return {"commit": sha, "branch": branch, "changed": changed, "deleted": deleted}


def repoint(value: str, roots: list[str], copy: str) -> str:
    """Point every path into the checkout at the copy: a root that starts the
    value or follows a separator (: = , or space) and ends it or is followed
    by / or a separator."""
    for r in roots:
        value = re.sub(r"(?<![^:=,\s])" + re.escape(r) + r"(?=$|[/:,\s])",
                       lambda _m: copy, value)
    return value


def roots_of(root: Path) -> list[str]:
    """The checkout's path as given and as resolved, longest first."""
    rs = {str(root), str(root.resolve()), os.path.abspath(str(root))}
    return sorted(rs, key=len, reverse=True)


def run(argv: list[str], root: Path = ROOT) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE, file=sys.stderr if not argv else sys.stdout)
        return 2 if not argv else 0
    raw = argv[0] == "--"
    if raw:
        argv = argv[1:]
        if not argv:
            print(USAGE, file=sys.stderr)
            return 2
    base = os.environ.get("TMPDIR") or tempfile.gettempdir()
    parent = Path(tempfile.mkdtemp(prefix="pcb-flow-frozen-", dir=base))
    copy = parent / root.name
    child: subprocess.Popen | None = None

    def forward(signum, _frame):
        if child is not None and child.poll() is None:
            child.send_signal(signum)
        else:
            shutil.rmtree(parent, ignore_errors=True)
            sys.exit(128 + signum)

    old = {s: signal.signal(s, forward) for s in (signal.SIGTERM, signal.SIGHUP)}
    try:
        t0 = time.monotonic()
        try:
            info = freeze(root, copy)
        except (FrozenError, OSError, subprocess.CalledProcessError) as e:
            print(f"pcb-flow frozen: could not copy {root}: {e}", file=sys.stderr)
            return 2
        roots = roots_of(root)
        cmd = [repoint(a, roots, str(copy)) for a in argv]
        if not raw:
            cmd = [str(copy / "bin" / "pcb-flow"), *cmd]
        env = {k: repoint(v, roots, str(copy)) for k, v in os.environ.items()}
        cwd = Path.cwd()
        new_cwd = Path(repoint(str(cwd), roots, str(copy)))
        if new_cwd != cwd and not new_cwd.is_dir():
            new_cwd = copy
        env["PWD"] = str(new_cwd)
        env["HWDE_FROZEN_FROM"] = str(root)
        what = f"{info['commit'][:12]}" if info["commit"] else "a tree with no git"
        print(f"pcb-flow frozen: {root} at {what}, {info['changed']} changed or untracked, "
              f"{info['deleted']} deleted, copied to {copy} in {time.monotonic() - t0:.1f}s",
              file=sys.stderr, flush=True)
        try:
            child = subprocess.Popen(cmd, cwd=new_cwd, env=env)
        except OSError as e:
            print(f"pcb-flow frozen: {cmd[0]}: {e}", file=sys.stderr)
            return 127
        while True:
            try:
                rc = child.wait()
                break
            except KeyboardInterrupt:
                # The terminal sent SIGINT to the child too: let it finish.
                continue
        return 128 - rc if rc < 0 else rc
    finally:
        for s, h in old.items():
            signal.signal(s, h)
        shutil.rmtree(parent, ignore_errors=True)


def main() -> int:
    return run(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
