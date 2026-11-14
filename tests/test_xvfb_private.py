"""A script run with no X display gets an Xvfb of its own, or doesn't run (#1037).

`xvfb-run -a` takes the first display with no lock file, so runs that start
together take the same one and all but one of their servers die ("Server is
already active"). It tries the next display, up to 10 times, and when every
try collides it runs the command anyway, on a display it doesn't own: "unable
to open display", or another run's server, which refuses its cookie with
"Authorization required". 30 starts at once on a busy machine failed 23 of
150 that way; bin/xvfb-private (Xvfb picks its display with -displayfd) failed
0 of 390.

The root cause is the command running when its own server never came up, and
a stand-in Xvfb that always loses the race shows it every time, not one start
in seven.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
import benchcorpus  # noqa: E402
from checklib import CheckError  # noqa: E402

LAUNCHER = ROOT / "bin" / "xvfb-private"
PROBE = "../tests/fixtures/xvfb/x_probe"     # bin/pcb-flow runs scripts/<name>.py
KEPT = 'HWDE_X_PROBE(STRING) = "kept"'


def _needs_x():
    for tool in ("Xvfb", "xauth", "xprop"):
        if not shutil.which(tool):
            pytest.skip(f"{tool} not installed")


@pytest.fixture
def no_display():
    _needs_x()
    return {k: v for k, v in os.environ.items() if k != "DISPLAY"}


@pytest.fixture
def losing_xvfb(tmp_path, no_display):
    """An Xvfb on PATH that always loses its display to another run."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "Xvfb"
    fake.write_text("#!/bin/sh\necho 'Fatal server error:' >&2\n"
                    "echo 'Server is already active for display 99' >&2\nexit 1\n")
    fake.chmod(0o755)
    return dict(no_display, PATH=f"{bindir}{os.pathsep}{no_display['PATH']}")


def test_launcher_runs_the_command_on_its_own_display(no_display):
    out = subprocess.run([str(LAUNCHER), "-noreset", "--", "sh", "-c",
                          'xprop -root -f HWDE_X_PROBE 8s -set HWDE_X_PROBE kept'
                          ' && xprop -root HWDE_X_PROBE && echo "$DISPLAY"; exit 7'],
                         env=no_display, capture_output=True, text=True, timeout=60)
    assert out.returncode == 7, out.stderr        # the command's own exit code
    assert KEPT in out.stdout, out.stderr
    assert out.stdout.strip().splitlines()[-1].startswith(":")


def test_launcher_leaves_no_server_or_files_behind(no_display, tmp_path):
    e = dict(no_display, TMPDIR=str(tmp_path))
    out = subprocess.run([str(LAUNCHER), "--", "sh", "-c", 'echo "$DISPLAY"'],
                         env=e, capture_output=True, text=True, timeout=60)
    display = out.stdout.strip()
    assert display.startswith(":"), out.stderr
    assert list(tmp_path.iterdir()) == []
    gone = subprocess.run(["xprop", "-root"], env=dict(no_display, DISPLAY=display),
                          capture_output=True, text=True, timeout=30)
    assert gone.returncode != 0


def test_a_server_that_does_not_start_stops_the_run(losing_xvfb):
    out = subprocess.run([str(LAUNCHER), "--", "sh", "-c", "echo ran"],
                         env=losing_xvfb, capture_output=True, text=True, timeout=60)
    assert out.returncode != 0
    assert "ran" not in out.stdout
    assert "Xvfb did not start" in out.stderr
    assert "already active" in out.stderr


def test_planted_xvfb_run_runs_the_command_with_no_server_of_its_own(losing_xvfb):
    # the launcher as it was: after 10 lost displays it runs the command anyway
    real = "/usr/bin/xvfb-run"
    if not Path(real).is_file():
        pytest.skip("xvfb-run not installed")
    out = subprocess.run([real, "-a", "-s", "-extension GLX -noreset",
                          "sh", "-c", 'echo "ran on $DISPLAY"'],
                         env=losing_xvfb, capture_output=True, text=True, timeout=120)
    assert "ran on :" in out.stdout


def test_launcher_usage(no_display):
    out = subprocess.run([str(LAUNCHER), "-noreset"], env=no_display,
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 2 and "usage" in out.stderr


def test_pcb_flow_starts_at_once_each_get_a_display(no_display):
    """The failure as it was seen: many pcb-flow scripts starting together."""
    n = 24

    def run(_):
        out = subprocess.run([str(ROOT / "bin" / "pcb-flow"), PROBE, "reset"],
                             env=no_display, capture_output=True, text=True,
                             timeout=240)
        return out.stdout + out.stderr

    with ThreadPoolExecutor(n) as pool:
        outs = list(pool.map(run, range(n)))
    bad = [o for o in outs if KEPT not in o]
    assert not bad, bad[0]


def test_pcb_flow_script_does_not_run_when_its_server_does_not_start(losing_xvfb):
    out = subprocess.run([str(ROOT / "bin" / "pcb-flow"), PROBE, "reset"],
                         env=losing_xvfb, capture_output=True, text=True, timeout=120)
    assert out.returncode != 0
    assert "Xvfb did not start" in out.stderr
    assert KEPT not in out.stdout


def test_benchcorpus_starts_at_once_each_get_their_own_display(no_display):
    with ThreadPoolExecutor(8) as pool:
        started = list(pool.map(lambda _: benchcorpus.start_xvfb(), range(8)))
    try:
        displays = [d for _, d in started]
        assert len(set(displays)) == 8, displays
        assert all(p.poll() is None for p, _ in started)
    finally:
        for p, _ in started:
            p.kill()
            p.wait()


def test_benchcorpus_server_that_does_not_start_is_an_error(losing_xvfb, monkeypatch):
    monkeypatch.setenv("PATH", losing_xvfb["PATH"])
    with pytest.raises(CheckError, match="did not come up"):
        benchcorpus.start_xvfb()
