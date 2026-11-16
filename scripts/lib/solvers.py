"""solvers.py - the open solvers hwde drives, from an IIC-OSIC-TOOLS tree.

The Hive keeps a read-only, unpacked IIC-OSIC-TOOLS image (the chip-flow
module's toolchain) that already holds the open simulators a board needs:

  ngspice    circuit (SPICE), the standalone binary
  xyce       circuit (Sandia Xyce, a second SPICE engine to cross-check ngspice)
  openems    full-wave 3D FDTD (openEMS + CSXCAD), driven by XML decks
  palace     full-wave 3D FEM (AWS Palace: eigenmode, driven S-params,
             electrostatic / magnetostatic extraction), driven by JSON + gmsh
  gmsh       the mesher Palace reads
  fasthenry  quasi-static inductance / resistance extraction (FastHenry2)
  fastercap  quasi-static capacitance extraction (FasterCap)
  openvaf    Verilog-A -> OSDI compiler, for compact models in ngspice

ngspice runs with hwde's own spinit (ngspice_init_dir): the tree's names
/foss/tools/... for its XSPICE code models (digital.cm, analog.cm,
tlines.cm, ...), so without it no `A` device or `d_` model parses.

Every binary there is an Ubuntu 24.04 ELF built against glibc 2.39, and the
host's libc is newer, so each one runs through the image's own loader
(`ld-linux-x86-64.so.2 --library-path ...`), never the kernel's PT_INTERP
(the same approach as hive-chip-flow's bin/eda, see there for the long
account). Two traps of the tree that bin/eda's generic path does not cover:

  * usr/lib/x86_64-linux-gnu/liblapack.so.3 and libblas.so.3 are absolute
    symlinks into /etc/alternatives, so they resolve to the HOST's lapack,
    which wants the host's libm (GLIBC_2.43) and kills Xyce at load. The
    library path therefore names the image's openblas-pthread/ directory
    first, which holds real copies.
  * Xyce and Palace are MPI programs. Started bare, Open MPI's singleton
    init spawns `orted` by name, outside the loader, and dies. OPAL_PREFIX
    points Open MPI at the image, and ess_singleton_isolated=1 keeps the
    singleton from spawning anything; one rank is all hwde asks of either.

The tree is found at HWDE_EDA_TOOLCHAIN (or EDA_TOOLCHAIN, chip-flow's name),
else the newest /opt/eda-toolchain/iic-osic-tools-* or
~/.cc/toolchains/iic-osic-tools-*. No tree: every tool reports missing and
the callers say so; nothing here installs anything.

HFSS (Ansys) is the commercial full-wave reference. It needs a licence,
which is money and the person's call, so hwde does not drive it: Palace and
openEMS are the open stand-ins, each validated against closed-form answers
(sim_solve.py validate, docs/solvers.md).
"""
from __future__ import annotations

import glob
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

ENV_VARS = ("HWDE_EDA_TOOLCHAIN", "EDA_TOOLCHAIN")
SEARCH = ("/opt/eda-toolchain/iic-osic-tools-*",
          "~/.cc/toolchains/iic-osic-tools-*")

# tool -> (path in the tree, args that print its version, version regex)
TOOLS = {
    "ngspice": ("foss/tools/ngspice/bin/ngspice", ["-v"], r"ngspice-(\S+)"),
    "xyce": ("foss/tools/xyce/bin/Xyce", ["-v"], r"Xyce Release (\S+)"),
    "openems": ("foss/tools/openems/bin/openEMS", [], r"version (v\S+)"),
    "nf2ff": ("foss/tools/openems/bin/nf2ff", [], r"(nf2ff), near-field"),
    "palace": ("foss/tools/palace/bin/palace-x86_64.bin", ["--version"],
               r"(?i)palace[^\n]*?(v?\d+\.\d+[\w.\-+]*)"),
    "gmsh": ("usr/bin/gmsh", ["--version"], r"(\d+\.\d+\.\d+)"),
    "fasthenry": ("foss/tools/rftoolkit/bin/fasthenry", [],
                  r"FastHenry (\S+)"),
    "fastercap": ("foss/tools/rftoolkit/bin/FasterCap", ["-v"],
                  r"(?i)FasterCap[^\n]*?(\d+\.\d+\.\d+)"),
    # bin/openvaf is an absolute symlink to /foss/...; openvaf-r is the ELF.
    "openvaf": ("foss/tools/openvaf/bin/openvaf-r", ["--version"],
                r"OpenVAF\S*\s+(\S+)"),
}

# What each solver is for, for probe's table and docs/solvers.md.
KIND = {
    "ngspice": "circuit",
    "xyce": "circuit",
    "openems": "full-wave EM (FDTD)",
    "nf2ff": "openEMS near-field to far-field transform",
    "palace": "full-wave EM (FEM) + electrostatic/magnetostatic",
    "gmsh": "mesher",
    "fasthenry": "quasi-static L/R extraction",
    "fastercap": "quasi-static C extraction",
    "openvaf": "Verilog-A compiler (OSDI models for ngspice)",
}

# ngspice's XSPICE code models and the tree's prebuilt OSDI models.
NGSPICE_LIB = "foss/tools/ngspice/lib/ngspice"
CACHE = Path(os.environ.get("XDG_CACHE_HOME") or "~/.cache").expanduser() / "hwde"


class SolverError(RuntimeError):
    pass


class SolverTimeout(SolverError):
    """The solver ran past its (load-stretched) time limit: slow, not wrong."""


def toolchain_root() -> Path | None:
    for var in ENV_VARS:
        val = os.environ.get(var)
        if val:
            p = Path(val).expanduser()
            if not p.is_dir():
                raise SolverError(f"{var}={val} is not a directory")
            return p
    found: list[str] = []
    for pat in SEARCH:
        found += glob.glob(os.path.expanduser(pat))
    found = [f for f in found if Path(f, "lib64/ld-linux-x86-64.so.2").exists()]
    return Path(sorted(found)[-1]) if found else None


def _libpath(t: Path) -> str:
    u = t / "usr/lib/x86_64-linux-gnu"
    dirs = [u / "openblas-pthread", u, t / "usr/lib", u / "openmpi/lib",
            t / "foss/tools/openems/lib", t / "foss/tools/xyce/lib",
            t / "foss/tools/palace/lib"]
    return ":".join(str(d) for d in dirs)


def tool_path(tool: str, root: Path | None = None) -> Path | None:
    t = root or toolchain_root()
    if t is None or tool not in TOOLS:
        return None
    p = t / TOOLS[tool][0]
    return p if p.exists() else None


def command(tool: str, args: list[str], root: Path | None = None) -> tuple[list[str], dict]:
    """argv and environment that run `tool args` through the image's loader."""
    t = root or toolchain_root()
    if t is None:
        raise SolverError("no IIC-OSIC-TOOLS tree (set HWDE_EDA_TOOLCHAIN)")
    exe = tool_path(tool, t)
    if exe is None:
        raise SolverError(f"{tool} not in {t}")
    env = dict(os.environ)
    env.update({
        "OPAL_PREFIX": str(t / "usr"),
        "OMPI_MCA_ess_singleton_isolated": "1",
        "OMPI_MCA_btl": "self,vader",
        "OMPI_MCA_pml": "ob1",
        "OMPI_MCA_plm": "isolated",
        "OMP_NUM_THREADS": env.get("OMP_NUM_THREADS", "4"),
        # openblas-pthread's pool busy-waits; on a loaded machine Xyce's
        # 4 s RC deck ran past 300 s with it (load 244, 10-30). hwde's decks
        # are small, so one BLAS thread costs nothing.
        "OPENBLAS_NUM_THREADS": env.get("OPENBLAS_NUM_THREADS", "1"),
        # ngspice looks for its code models and init file under its prefix.
        "SPICE_LIB_DIR": str(t / "foss/tools/ngspice/share/ngspice"),
        "SPICE_SCRIPTS": str(t / "foss/tools/ngspice/share/ngspice/scripts"),
        "HWLOC_COMPONENTS": "-linux",
    })
    if tool == "ngspice":
        # the tree's own spinit loads its code models from /foss/tools/...,
        # which isn't here: hwde writes one naming the real files.
        env["SPICE_SCRIPTS"] = str(ngspice_init_dir(t))
    env.pop("PYTHONHOME", None)
    ld = t / "lib64/ld-linux-x86-64.so.2"
    return [str(ld), "--library-path", _libpath(t), str(exe), *args], env


def python_command(script, args, root: Path | None = None) -> tuple[list[str], dict]:
    """argv and environment that run a script under the tree's own python
    (3.12, with gmsh's Python API, which hwde's venv doesn't carry). The script
    finds libgmsh through HWDE_GMSH_LIBDIR: gmsh.py's own search can't, through
    the loader."""
    t = root or toolchain_root()
    if t is None:
        raise SolverError("no IIC-OSIC-TOOLS tree (set HWDE_EDA_TOOLCHAIN)")
    env = dict(os.environ)
    env.update({"PYTHONHOME": str(t / "usr"), "PYTHONPATH": str(t / "usr/lib/python3/dist-packages"),
                "HWDE_GMSH_LIBDIR": str(t / "usr/lib/x86_64-linux-gnu"), "PYTHONNOUSERSITE": "1"})
    ld = t / "lib64/ld-linux-x86-64.so.2"
    return [str(ld), "--library-path", _libpath(t), str(t / "usr/bin/python3"), str(script), *map(str, args)], env


def mpi_command(tool: str, args: list[str], np: int, root: Path | None = None) -> tuple[list[str], dict]:
    """argv and environment for `np` ranks of `tool`, each started through the
    image's loader, by the image's mpirun (itself through the loader)."""
    t = root or toolchain_root()
    argv, env = command(tool, args, t)
    for k in ("OMPI_MCA_ess_singleton_isolated", "OMPI_MCA_plm"):
        env.pop(k, None)
    env["OMPI_MCA_rmaps_base_oversubscribe"] = "1"
    env["OMPI_MCA_hwloc_base_binding_policy"] = "none"
    env["OMP_NUM_THREADS"] = "1"
    ld = t / "lib64/ld-linux-x86-64.so.2"
    return [str(ld), "--library-path", _libpath(t), str(t / "usr/bin/orterun"), "-np", str(np),
            "--oversubscribe", *argv], env


def codemodels(root: Path | None = None) -> list[Path]:
    """The XSPICE code model libraries (*.cm) in the tree's ngspice."""
    t = root or toolchain_root()
    return sorted((t / NGSPICE_LIB).glob("*.cm")) if t else []


def spinit_text(cms: list[Path]) -> str:
    """An ngspice init file that loads these code models (and nothing that
    isn't there: the tree's own spinit names /foss/tools/... paths and
    three OSDI files unconditionally). A deck loads OSDI itself with
    `.control / pre_osdi FILE.osdi / .endc`."""
    lines = ["* hwde: ngspice init (lib/solvers.py)", "alias exit quit",
             "set num_threads=1"]
    lines += [f"codemodel {p}" for p in cms]
    return "\n".join(lines) + "\n"


def ngspice_init_dir(root: Path) -> Path:
    """A directory holding hwde's spinit for this tree (SPICE_SCRIPTS)."""
    text = spinit_text(codemodels(root))
    d = CACHE / "ngspice-init" / root.name
    try:
        d.mkdir(parents=True, exist_ok=True)
        f = d / "spinit"
        if not f.exists() or f.read_text() != text:
            tmp = d / f"spinit.{os.getpid()}"
            tmp.write_text(text)
            os.replace(tmp, f)
    except OSError:
        import tempfile
        d = Path(tempfile.mkdtemp(prefix="hwde-spinit-"))
        (d / "spinit").write_text(text)
    return d


def compile_va(va: Path | str, out: Path | str | None = None,
               root: Path | None = None) -> Path:
    """Compile a Verilog-A file to an OSDI library with the tree's OpenVAF.
    Default output: ~/.cache/hwde/osdi/<stem>-<hash of the source>.osdi,
    reused while the source is unchanged."""
    import hashlib
    va = Path(va).resolve()
    if out is None:
        h = hashlib.sha256(va.read_bytes()).hexdigest()[:12]
        out = CACHE / "osdi" / f"{va.stem}-{h}.osdi"
        if out.exists():
            return out
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    r = run("openvaf", [str(va), "-o", str(out)], cwd=va.parent, timeout=300,
            stdin="", root=root)
    if not r.ok or not out.exists():
        raise SolverError(f"openvaf {va.name}: {(r.stdout + r.stderr)[-400:]}")
    return out


@dataclass
class Run:
    tool: str
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run(tool: str, args: list[str], cwd: Path | str | None = None,
        timeout: float = 1800, stdin: str | None = None,
        root: Path | None = None) -> Run:
    try:
        from . import loadlib
    except ImportError:
        import loadlib
    argv, env = command(tool, args, root)
    try:   # `timeout` stretches on a busy machine, never for a stuck solver (#970)
        p = loadlib.run(argv, cwd=cwd, env=env, input=stdin, text=True,
                        capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise SolverTimeout(f"{tool} ran past {e.timeout:.0f} s "
                          f"(limit {timeout:.0f} s)") from e
    return Run(tool, argv, p.returncode, p.stdout, p.stderr)


def version(tool: str, root: Path | None = None) -> str | None:
    """The tool's version, or None when it isn't there or won't start."""
    if tool_path(tool, root) is None:
        return None
    _, vargs, pat = TOOLS[tool]
    try:
        r = run(tool, vargs, timeout=60, stdin="", root=root)
    except SolverError:
        return None
    m = re.search(pat, r.stdout + r.stderr)
    if m:
        return m.group(1)
    return "present" if r.ok else None


def probe(root: Path | None = None) -> dict:
    t = root or toolchain_root()
    return {"toolchain": str(t) if t else None,
            "tools": {k: {"kind": KIND[k], "version": version(k, t) if t else None}
                      for k in TOOLS},
            "codemodels": [p.name for p in codemodels(t)] if t else []}
