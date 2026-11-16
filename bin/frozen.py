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
  on top, as unstaged changes, and files marked skip-worktree or
  assume-unchanged as they are on disk; an untracked repo inside the checkout
  is copied whole;
- leaves out ignored files (a datasheet PDF, a 3D model fetched by a script,
  a venv), empty folders, and the files of submodules;
- has no remote, so nothing run in it can push to the checkout's repo;
- goes in $TMPDIR and is removed when the command ends.

Every path into the checkout (an argument, an environment value, the command
itself, the working folder) points into the copy instead: an absolute path,
or one after `=`, `:` or `,`. Two kinds are not: a path glued to a short
option (`-I/path`), and a relative path that leads out of the checkout
(`../other/x`), which the run looks for beside the copy instead. Git's own
variables that aim git at a repo (GIT_DIR, GIT_WORK_TREE, GIT_INDEX_FILE, ...)
are dropped, so git in the run sees the copy. Ctrl-C at a terminal reaches the
command directly; a SIGINT, SIGTERM or SIGHUP sent to this process alone is
passed on to it. The run exits with the command's status. What the command
writes inside the copy goes with it, so this suits runs that read the tree,
not ones meant to write into it.

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


# Git's own variables that point a git command at a repository, an index or a
# config (`git rev-parse --local-env-vars`). A caller that set them (a hook, a
# `git rebase --exec`) would aim our git calls, and the run's, at the checkout's
# repo instead of the copy's; git drops them the same way for submodules.
GIT_LOCAL_ENV = ("GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS",
                 "GIT_CONFIG_COUNT", "GIT_OBJECT_DIRECTORY", "GIT_DIR", "GIT_WORK_TREE",
                 "GIT_IMPLICIT_WORK_TREE", "GIT_GRAFT_FILE", "GIT_INDEX_FILE",
                 "GIT_NO_REPLACE_OBJECTS", "GIT_REPLACE_REF_BASE", "GIT_PREFIX",
                 "GIT_SHALLOW_FILE", "GIT_COMMON_DIR")


def clean_env(env: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in env.items()
            if k not in GIT_LOCAL_ENV and not k.startswith("GIT_CONFIG_")}


class FrozenError(Exception):
    pass


def git(root: Path, *args: str) -> bytes:
    # No optional locks: `git status` here must not rewrite the checkout's index.
    cp = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                        env={**clean_env(dict(os.environ)), "GIT_OPTIONAL_LOCKS": "0"})
    if cp.returncode != 0:
        raise FrozenError(f"git {' '.join(args)}: {cp.stderr.decode(errors='replace').strip()}")
    return cp.stdout


def is_git_root(root: Path) -> bool:
    try:
        top = git(root, "rev-parse", "--show-toplevel").decode().strip()
    except FrozenError:
        return False
    return Path(top).resolve() == root.resolve()


def changed_paths(root: Path) -> list[str]:
    """Every path whose working-tree state may differ from HEAD: edited, staged,
    added, deleted, renamed (both names) or untracked (not ignored), and the
    files git was told not to look at (skip-worktree, assume-unchanged), which
    `git status` leaves out however they were edited."""
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
        paths.append(e[3:].rstrip("/"))  # `?? n/`: an untracked repo inside
        if e[0] in "RC":  # the next field is the old name
            paths.append(parts[i])
            i += 1
    listed = git(root, "ls-files", "-z", "-v").decode("utf-8", "surrogateescape")
    for e in listed.split("\0"):
        if len(e) > 2 and (e[0].islower() or e[0] == "S"):
            paths.append(e[2:])
    return list(dict.fromkeys(paths))


def parents_are_dirs(base: Path, rel: str) -> bool:
    """Whether every folder between base and rel is a real folder, not a link
    (which would lead out of the tree) or a file."""
    p = base
    for part in Path(rel).parts[:-1]:
        p = p / part
        if p.is_symlink() or not p.is_dir():
            return False
    return True


def kind(base: Path, rel: str) -> str | None:
    """What the tree has at rel: "link", "file", "dir" or None. A path under a
    link is not in the tree, whatever the link leads to."""
    if not parents_are_dirs(base, rel):
        return None
    p = base / rel
    if p.is_symlink():
        return "link"
    if p.is_file():
        return "file"
    if p.is_dir():
        return "dir"
    return None


def remove(p: Path) -> None:
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(p)
    else:
        p.unlink()


def make_parents(base: Path, rel: str) -> None:
    """Make the folders down to rel in the copy real folders, replacing a file
    or a link that stands where one goes, so nothing written there can land
    outside the copy."""
    p = base
    for part in Path(rel).parts[:-1]:
        p = p / part
        if p.is_symlink() or (os.path.lexists(p) and not p.is_dir()):
            p.unlink()
        if not os.path.lexists(p):
            p.mkdir()


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
    cp = subprocess.run(["git", "clone", "-q", "--shared", "--no-checkout", str(common), str(dest)],
                        capture_output=True, env=clean_env(dict(os.environ)))
    if cp.returncode != 0:
        raise FrozenError(f"git clone: {cp.stderr.decode(errors='replace').strip()}")
    git(dest, "remote", "remove", "origin")
    if branch and branch != "HEAD":
        git(dest, "checkout", "-q", "-B", branch, sha)
    else:
        git(dest, "checkout", "-q", "--detach", sha)
    have = {rel: kind(root, rel) for rel in paths}
    changed = deleted = 0
    # Deletions first, so a folder that became a file (or the other way round)
    # is out of the way before the new one is written.
    for rel, k in have.items():
        d = dest / rel
        if k is None and parents_are_dirs(dest, rel) and os.path.lexists(d):
            remove(d)
            deleted += 1
    for rel, k in have.items():
        if k is None:
            continue
        s, d = root / rel, dest / rel
        make_parents(dest, rel)
        if k == "dir":
            if d.is_dir() and not d.is_symlink():
                continue  # a submodule's folder: its files are not copied
            if os.path.lexists(d):
                remove(d)
            # An untracked repo inside the checkout: git lists only its folder.
            shutil.copytree(s, d, symlinks=True)
        else:
            if os.path.lexists(d):
                remove(d)
            if k == "link":
                os.symlink(os.readlink(s), d)
            else:
                shutil.copy2(s, d)
        changed += 1
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
    # The shell's own name for it when it reached the checkout through a
    # link: $PWD keeps the link, getcwd() and this file resolve it.
    pwd = os.environ.get("PWD", "")
    if os.path.isabs(pwd):
        try:
            rel = Path(pwd).resolve().relative_to(root.resolve())
        except (ValueError, OSError):
            rel = None
        if rel is not None:
            alias = Path(pwd)
            for _ in rel.parts:
                alias = alias.parent
            if alias.resolve() == root.resolve():
                rs.add(str(alias))
    return sorted(rs, key=len, reverse=True)


def terminal_reaches_child() -> bool:
    """Whether a Ctrl-C at the terminal reaches the child by itself: it goes to
    the terminal's foreground process group, which is ours (and the child's)."""
    try:
        return os.isatty(0) and os.tcgetpgrp(0) == os.getpgrp()
    except OSError:
        return False


def remove_copy(parent: Path) -> None:
    """Remove the copy, making read-only folders writable on the way (a test
    that left one would stop a plain rmtree); say so if any of it stays."""
    def retry(func, path, _exc):
        try:
            if not os.path.islink(path):
                os.chmod(os.path.dirname(path), 0o700)
                if os.path.isdir(path):
                    os.chmod(path, 0o700)
            func(path)
        except OSError:
            pass
    if sys.version_info >= (3, 12):
        shutil.rmtree(parent, onexc=retry)
    else:
        shutil.rmtree(parent, onerror=retry)
    if os.path.lexists(parent):
        print(f"pcb-flow frozen: could not remove all of {parent}", file=sys.stderr)


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
    starting = False
    pending: list[int] = []

    def forward(signum, _frame):
        if child is None:
            if starting:  # the child is being started: hand it over once it is
                pending.append(signum)
                return
            sys.exit(128 + signum)  # still copying: stop, and `finally` cleans up
        if child.poll() is None:
            if signum == signal.SIGINT and terminal_reaches_child():
                return  # the terminal sent it to the child as well: let it finish
            child.send_signal(signum)

    old = {s: signal.signal(s, forward) for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}
    try:
        t0 = time.monotonic()
        try:
            info = freeze(root, copy)
        except (FrozenError, OSError) as e:
            print(f"pcb-flow frozen: could not copy {root}: {e}", file=sys.stderr)
            return 2
        roots = roots_of(root)
        cmd = [repoint(a, roots, str(copy)) for a in argv]
        if not raw:
            cmd = [str(copy / "bin" / "pcb-flow"), *cmd]
        env = {k: repoint(v, roots, str(copy)) for k, v in clean_env(dict(os.environ)).items()}
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
        starting = True
        try:
            child = subprocess.Popen(cmd, cwd=new_cwd, env=env)
        except OSError as e:
            print(f"pcb-flow frozen: {cmd[0]}: {e}", file=sys.stderr)
            return 128 + pending[0] if pending else 127
        finally:
            starting = False
        for s in pending:
            child.send_signal(s)
        rc = child.wait()
        return 128 - rc if rc < 0 else rc
    finally:
        for s, h in old.items():
            signal.signal(s, h)
        remove_copy(parent)


def main() -> int:
    return run(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
