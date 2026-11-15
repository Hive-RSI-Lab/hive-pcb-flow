"""Real board workspaces for the few tests that need one.

Boards left hwde for their own repo (env.boards_root(): HWDE_BOARDS_ROOT,
default ~/dev/boards), so hwde's suite never depends on them. A test that
really needs a shipped board asks for it here: it runs against the boards
repo when that is cloned, and SKIPS with the reason when it is not - never a
silent pass, never a failure on a box without the boards repo. Anything a
small fixture under tests/ can stand in for uses the fixture instead.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from lib import boardreg, env  # noqa: E402

BOARDS = env.boards_root()

# A check over every board's content (not one named board) belongs to the
# boards repo's own suite. hwde runs it only when asked, so a note added on a
# boards branch never turns hwde's check red.
SHIPPED_CHECKS = os.environ.get("HWDE_LINT_SHIPPED") == "1"


def need_shipped_checks() -> None:
    """Skip the calling test unless HWDE_LINT_SHIPPED=1 asks for checks
    over every board in the boards repo."""
    if not SHIPPED_CHECKS:
        pytest.skip("opt-in: set HWDE_LINT_SHIPPED=1 to check every board in"
                    " the boards repo; the boards repo checks its own content")


def board_path(name: str) -> Path:
    """Where board `name` lives in the boards repo (may not exist); safe at
    import time. Pair it with need_board() inside the test. `name` is the
    board's bare name: a workspace renamed to <PN>_<name> is found through
    the register (boardreg.resolve)."""
    return boardreg.resolve(name, BOARDS) or BOARDS / name


def need_board(*names: str) -> None:
    """Skip the calling test unless every named board is in the boards repo."""
    gone = [n for n in names if not board_path(n).is_dir()]
    if gone:
        pytest.skip(f"needs the real board(s) {', '.join(gone)} from the "
                    f"boards repo ({BOARDS}; set HWDE_BOARDS_ROOT)")


def real_board(name: str) -> Path:
    """The board's workspace, skipping the test when it is not there."""
    need_board(name)
    return board_path(name)


def copy_board(name: str, dst: Path, *, skip: tuple[str, ...] = ()) -> Path:
    """A private copy of board `name` at dst/<name> for a test that writes
    into its workspace (skipping the test when the board is not there). The
    boards repo is never written. `skip` names top-level entries left out
    (scratch like work/ or state_snapshots/ the test does not read)."""
    src = real_board(name)
    out = Path(dst) / name
    top = set(skip)
    shutil.copytree(src, out, symlinks=True,
                    ignore=lambda d, names: [n for n in names
                                             if Path(d) == src and n in top])
    return out


def stock_footprints() -> Path | None:
    """KiCad's stock footprint library, or None: the newest
    KICAD<n>_FOOTPRINT_DIR that is a directory (a bare KICAD_FOOTPRINT_DIR
    last), the order board_swig.resolve_default_fp_root takes, else the one
    beside kicad-cli."""
    hits = []
    for key, val in os.environ.items():
        m = re.fullmatch(r"KICAD(\d*)_FOOTPRINT_DIR", key)
        if m and val:
            hits.append((int(m[1]) if m[1] else -1, val))
    for _, val in sorted(hits, key=lambda kv: -kv[0]):
        if Path(val).is_dir():
            return Path(val)
    try:
        cli = env.find_kicad_cli()
    except env.EnvError:
        return None
    fp = cli and Path(cli).parents[1] / "share" / "kicad" / "footprints"
    return fp if fp and fp.is_dir() else None
