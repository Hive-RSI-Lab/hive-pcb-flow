#!/usr/bin/env python
"""check_env.py - validate the hwde toolchain on this machine.

Script contract (SPEC.md section 6): argparse, JSON to stdout (or --out FILE),
exit 0 = environment OK (warnings allowed), 1 = one or more checks failed,
2 = internal error. Human-readable remediation goes to stderr so stdout stays
machine-parseable.

Run it with the repo venv interpreter - package checks inspect the running
interpreter:  bin/hwde check (the module's runner)

Fast mode (default) checks presence/versions only. --full adds live probes:
SWIG pcbnew roundtrip via KiCad's bundled python (needed for Specctra DSN/SES
and KiCad-9 zone refill) and an IPC headless-connect attempt (kipy). Both
modes run the impedance field solver (lib/impedance.py, pure numpy + scipy -
nothing to install beyond the venv) on Cohn's exact stripline and fail if it
is off by 1% or more. Probe
failures are recorded as warnings with the working fallback named, because
each has a sanctioned alternative path in SPEC.md.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import env  # noqa: E402

MIN_PYTHON = (3, 11)

# import name -> pip distribution name
REQUIRED_PACKAGES = {
    "kipy": "kicad-python",
    "kiutils": "kiutils",
    "kicad_sch_api": "kicad-sch-api",
    "skidl": "skidl",
    "shapely": "shapely",
    "numpy": "numpy",
    "gerbonara": "gerbonara",
    "sexpdata": "sexpdata",
    "yaml": "PyYAML",
    "easyeda2kicad": "easyeda2kicad",
    "pypdf": "pypdf",
    "jsonschema": "jsonschema",
    "scipy": "scipy",
    "pytest": "pytest",
    "InSpice": "InSpice",
}

KICAD_INSTALL_HELP = (
    "Install KiCad >= 9.0.5: direct installer from "
    "https://kicad-downloads.s3.cern.ch/windows/stable/ or "
    "'winget install -e --id KiCad.KiCad' (on this host winget may need its "
    "full path: %LOCALAPPDATA%/Microsoft/WindowsApps/winget.exe). "
    "Or point HWDE_KICAD_CLI at an existing kicad-cli."
)
JAVA_HELP = (
    "On the Linux host: . ~/.local/kicad10/hwde-env.sh (tools under "
    "~/.local/hwde-tools; see CLAUDE.md, Linux host section). "
    "Freerouting 2.2.4 needs Java >= 25 (the 'Java 21+' docs are stale; "
    "verified: Java 24 fails with UnsupportedClassVersionError). Unzip a "
    "portable Temurin 25 JRE into tools/jre/ "
    "(https://adoptium.net/temurin/releases/?version=25 - no system install "
    "needed), or set HWDE_JAVA, or start Docker Desktop and use the "
    "ghcr.io/freerouting/freerouting image."
)
JAR_HELP = (
    "On the Linux host: . ~/.local/kicad10/hwde-env.sh (tools under "
    "~/.local/hwde-tools; see CLAUDE.md, Linux host section). "
    "Download freerouting-2.2.4.jar from "
    "https://github.com/freerouting/freerouting/releases into "
    "tools/freerouting/, or set HWDE_FREEROUTING_JAR."
)
LUALATEX_HELP = (
    "Install TeX Live (lualatex) or set HWDE_LUALATEX, and install the "
    "pdf-material-builder skill (its house style) or set HWDE_HOUSE_STYLE. "
    "Without both report_gen degrades to --tex-only (no PDF)."
)
NGSPICE_HELP = (
    "The SPICE sim gate (sim_run.py) drives a shared ngspice library. "
    "KiCad 10 bundles one next to kicad-cli (C:/Program Files/KiCad/10.0/"
    "bin/ngspice.dll - install KiCad with its simulator component), or set "
    "HWDE_NGSPICE_DLL to any ngspice shared library. Without it the sim "
    "gate cannot run."
)

SYMBOLS_HELP = (
    "Schematics place power: and Device: symbols from KiCad's stock "
    "libraries (share/kicad/symbols beside kicad-cli, or KICAD_SYMBOL_DIR). "
    "Install KiCad with its symbol libraries."
)
KRT_HELP = (
    "route_critical and route_auto's KRT finishing pass need "
    "KiCadRoutingTools 0.19.0 under HWDE_TOOLS_DIR (krt/ or "
    "kicad-routing-tools/), or HWDE_KRT_DIR at its plugins dir."
)
SWIG_PROBE = """\
import sys
import pcbnew
out = sys.argv[1]
b = pcbnew.CreateEmptyBoard()
b.Save(out)
b2 = pcbnew.LoadBoard(out)
assert b2 is not None
print("build=" + pcbnew.GetBuildVersion())
"""


def check(name: str, ok: bool, detail: str, remediation: str = "",
          warn: bool = False) -> dict:
    status = "pass" if ok else ("warn" if warn else "fail")
    c = {"name": name, "status": status, "detail": detail}
    if not ok and remediation:
        c["remediation"] = remediation
    return c


def check_python() -> dict:
    v = sys.version_info
    return check(
        "python-version", (v.major, v.minor) >= MIN_PYTHON,
        f"{v.major}.{v.minor}.{v.micro} at {sys.executable}",
        f"Recreate the venv with Python >= {'.'.join(map(str, MIN_PYTHON))}: "
        "py -3.13 -m venv .venv && .venv/Scripts/python -m pip install -r requirements.txt",
    )


LOCK_FILE = "requirements.lock"
# Windows-only pins in requirements.lock carry no environment marker there;
# a Linux venv is built from the lock minus these.
WINDOWS_ONLY = {"pywin32", "pywin32-ctypes"}


def _norm(name: str) -> str:
    return name.lower().replace("_", "-").replace(".", "-")


def parse_lock(text: str) -> list[tuple[str, str, str]]:
    """(name, version, marker) for each `name==version[ ; marker]` line."""
    rows = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-") or "==" not in line:
            continue
        spec, _, marker = line.partition(";")
        name, _, ver = spec.partition("==")
        rows.append((name.strip().split("[")[0], ver.strip(), marker.strip()))
    return rows


def _lock_applies(name: str, marker: str) -> bool:
    if marker:
        try:
            from packaging.markers import Marker
            return bool(Marker(marker).evaluate())
        except Exception:  # noqa: BLE001 - unparseable marker: still check it
            return True
    return not (sys.platform != "win32" and _norm(name) in WINDOWS_ONLY)


def check_lock(lock_path: Path | None = None) -> dict | None:
    """Installed distributions vs requirements.lock (missing / wrong version)."""
    lock_path = lock_path or env.repo_root() / LOCK_FILE
    if not lock_path.is_file():
        return None
    problems = []
    missing = False  # a missing package fails; version drift only warns
    for name, want, marker in parse_lock(lock_path.read_text(encoding="utf-8")):
        if not _lock_applies(name, marker):
            continue
        try:
            have = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            problems.append(f"{name} missing (locked {want})")
            missing = True
            continue
        if have != want:
            problems.append(f"{name} {have} installed, locked {want}")
    return check(
        "lock-packages", not problems,
        "all requirements.lock packages installed at the locked version"
        if not problems else f"{len(problems)} off the lock: " + "; ".join(problems),
        f"{Path(sys.executable)} -m pip install -r {lock_path}"
        + ("   (drop the pywin32 lines on Linux)" if sys.platform != "win32" else ""),
        warn=not missing,
    )


def check_packages() -> list[dict]:
    import contextlib
    import io
    out = []
    # skidl prints env-var warnings on import AND drops <script>.log/.erc
    # files in the CWD; sandbox both so stdout stays pure JSON and the repo
    # stays clean.
    scratch = tempfile.mkdtemp(prefix="aiee_imports_")
    for mod, dist in REQUIRED_PACKAGES.items():
        try:
            sink = io.StringIO()
            with contextlib.chdir(scratch), \
                 contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                importlib.import_module(mod)
            try:
                ver = importlib.metadata.version(dist)
            except importlib.metadata.PackageNotFoundError:
                ver = "unknown"
            out.append(check(f"package:{dist}", True, f"import {mod} ok, {ver}"))
        except Exception as e:  # import errors vary wildly across packages
            out.append(check(
                f"package:{dist}", False,
                f"import {mod} failed: {type(e).__name__}: {e}",
                f"{Path(sys.executable)} -m pip install {dist}   "
                "(or: pip install -r requirements.txt)",
            ))
    return out


def check_kicad(resolved: dict, full: bool) -> list[dict]:
    out = []
    try:
        cli = env.find_kicad_cli()
    except env.EnvError as e:
        return [check("kicad-cli", False, str(e), KICAD_INSTALL_HELP)]
    if cli is None:
        return [check("kicad-cli", False, "no KiCad >= 9.0.5 found", KICAD_INSTALL_HELP)]

    ver = env.kicad_cli_version(cli)
    vs = ".".join(map(str, ver))
    resolved["kicad_cli"] = str(cli)
    resolved["kicad_version"] = vs
    out.append(check(
        "kicad-cli", ver >= env.MIN_KICAD, f"{cli} ({vs})", KICAD_INSTALL_HELP))
    if ver < env.MIN_KICAD:
        return out
    pin = ".".join(map(str, env.KICAD_PIN))
    out.append(check(
        "kicad-pin", ver == env.KICAD_PIN,
        f"kicad-cli {vs} matches the pin" if ver == env.KICAD_PIN else
        f"kicad-cli {vs} is not the pinned {pin}: same file format, but "
        "check numbers may differ from the host's and CI's",
        f"Install KiCad {pin} to match the host and CI.", warn=True))

    r = subprocess.run([str(cli), "pcb", "render", "--help"],
                       capture_output=True, text=True, timeout=60)
    out.append(check(
        "kicad-cli-render", r.returncode == 0,
        "pcb render subcommand available" if r.returncode == 0
        else "pcb render missing (KiCad too old?)",
        KICAD_INSTALL_HELP))

    d = subprocess.run([str(cli), "pcb", "drc", "--help"],
                       capture_output=True, text=True, timeout=60)
    has_refill = "--refill-zones" in (d.stdout + d.stderr)
    resolved["drc_refill_zones"] = has_refill
    out.append(check(
        "kicad-cli-refill-zones", has_refill,
        "drc --refill-zones supported" if has_refill else
        "drc lacks --refill-zones (KiCad 9): zone refill must go through the "
        "bundled-python SWIG fallback",
        "Prefer KiCad 10.x for headless zone refill.", warn=True))

    kpy = env.find_kicad_python(cli)
    resolved["kicad_python"] = str(kpy) if kpy else None
    out.append(check(
        "kicad-bundled-python", kpy is not None,
        f"{kpy}" if kpy else "bundled python.exe not found next to kicad-cli",
        "SWIG pcbnew (Specctra DSN/SES, KiCad-9 zone refill) needs KiCad's own "
        "python. Reinstall KiCad with the Python component."))

    out.append(check_symbols(resolved))
    out.append(check_3d_loaders(cli))

    if full and kpy is not None:
        out.append(probe_swig(kpy))
    return out


def check_symbols(resolved: dict) -> dict:
    """The stock power/Device libraries every schematic places from, in the
    flat form kicad-sch-api reads (KiCad 10's symdirs are flattened)."""
    try:
        libs = {lib: env.stock_symbol_lib(lib) for lib in env.SYMDIR_EAGER}
    except OSError as e:
        return check("kicad-symbols", False, f"flattening failed: {e}",
                     SYMBOLS_HELP)
    missing = [lib for lib, p in libs.items() if p is None]
    resolved["kicad_symbols"] = {lib: str(p) if p else None
                                 for lib, p in libs.items()}
    return check("kicad-symbols", not missing,
                 f"{', '.join(missing)} not found under "
                 f"{env.kicad_symbol_dir()}" if missing else
                 ", ".join(f"{lib}: {p}" for lib, p in libs.items()),
                 SYMBOLS_HELP)


def check_3d_loaders(cli: Path) -> dict:
    """kicad-cli's 3D model loaders: without them a render is a bare board."""
    import kc
    renv, err = kc.render_env(cli)
    return check("kicad-3d-loaders", not err,
                 err or ("found beside " + (renv or {}).get("APPDIR", str(cli))),
                 "Install KiCad's 3D plugins (libs3d_plugin_*) with kicad-cli.",
                 warn=True)


def check_krt(resolved: dict) -> dict:
    """KiCadRoutingTools: route_critical and route_auto's finishing pass."""
    try:
        krt = env.find_krt()
    except env.EnvError as e:
        return check("krt", False, str(e), KRT_HELP)
    resolved["krt"] = str(krt) if krt else None
    return check("krt", krt is not None,
                 str(krt) if krt else "KiCadRoutingTools not found",
                 KRT_HELP, warn=True)


def probe_swig(kpy: Path) -> dict:
    with tempfile.TemporaryDirectory() as td:
        board = str(Path(td) / "trivial.kicad_pcb")
        try:
            r = subprocess.run([str(kpy), "-c", SWIG_PROBE, board],
                               capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            return check("swig-pcbnew-roundtrip", False,
                         "timed out after 180 s (wx assert dialog wedge?)",
                         "See LEARNINGS.md [kicad][swig]; fallback: vendored "
                         "Python DSN writer per SPEC.md P7.", warn=True)
    ok = r.returncode == 0 and "build=" in r.stdout
    detail = r.stdout.strip() if ok else (r.stderr.strip() or r.stdout.strip())[-400:]
    return check("swig-pcbnew-roundtrip", ok,
                 f"CreateEmptyBoard/Save/LoadBoard ok ({detail})" if ok
                 else f"probe failed: {detail}",
                 "Fallback: vendored Python DSN writer per SPEC.md P7.",
                 warn=True)


def probe_ipc() -> dict:
    smoke = Path(__file__).resolve().parent / "smoke_ipc.py"
    try:
        r = subprocess.run([sys.executable, str(smoke)],
                           capture_output=True, text=True, timeout=300)
        data = json.loads(r.stdout) if r.stdout.strip() else {}
    except Exception as e:
        return check("ipc-headless", False, f"probe crashed: {e}", warn=True)
    verdict = data.get("verdict", "no-verdict")
    ok = verdict == "headless-ok"
    return check(
        "ipc-headless", ok,
        json.dumps(data)[:600],
        "IPC edits fall back to SWIG bundled python (verified working); "
        "see PROGRESS.md S0 smoke results.",
        warn=True)


def check_java(resolved: dict) -> list[dict]:
    out = []
    try:
        j = env.find_java()
    except env.EnvError as e:
        return [check("java-for-freerouting", False, str(e), JAVA_HELP)]
    docker = env.docker_state()
    resolved["docker"] = docker
    if j:
        resolved["java"] = str(j[0])
        resolved["java_major"] = j[1]
    if j and j[1] >= env.MIN_JAVA_FOR_FREEROUTING:
        out.append(check("java-for-freerouting", True, f"{j[0]} (major {j[1]})"))
    elif docker == "up":
        out.append(check(
            "java-for-freerouting", True,
            f"java {'major %d' % j[1] if j else 'absent'} is insufficient, but "
            "Docker daemon is up (freerouting container path)"))
    else:
        have = f"best java found: {j[0]} (major {j[1]})" if j else "no java found"
        out.append(check("java-for-freerouting", False,
                         f"{have}; docker: {docker}", JAVA_HELP))

    jar = None
    try:
        jar = env.find_freerouting_jar()
    except env.EnvError as e:
        out.append(check("freerouting-jar", False, str(e), JAR_HELP))
        return out
    resolved["freerouting_jar"] = str(jar) if jar else None
    out.append(check("freerouting-jar", jar is not None,
                     str(jar) if jar else
                     "no jar in tools/freerouting/ and HWDE_FREEROUTING_JAR not set",
                     JAR_HELP))
    return out


def check_lualatex(resolved: dict) -> dict:
    """report_gen's engine (lualatex) and the house style it sets every doc
    in are optional (report_gen degrades to --tex-only), so absence is a
    warning - but a set-but-invalid HWDE_LUALATEX pin still fails loudly."""
    try:  # the engine ladder lives with its only user, report_gen
        import report_gen
    except Exception as e:  # e.g. pyyaml missing: its own check reports that
        return check("lualatex", False, f"cannot load report_gen: {e}",
                     LUALATEX_HELP, warn=True)
    try:
        p = report_gen.find_lualatex()
    except env.EnvError as e:
        return check("lualatex", False, str(e), LUALATEX_HELP)
    style = report_gen.house_style_dir()
    resolved["lualatex"] = str(p) if p else None
    resolved["house_style"] = str(style) if style else None
    if p is None:
        detail = "no lualatex found (HWDE_LUALATEX or PATH)"
    elif style is None:
        detail = f"{p}; house style not found (HWDE_HOUSE_STYLE)"
    else:
        detail = f"{p}; house style {style}"
    return check("lualatex", p is not None and style is not None, detail,
                 LUALATEX_HELP, warn=True)


def check_ngspice(resolved: dict) -> dict:
    """ngspice is optional (only the sim gate needs it), so absence is a
    warning - but a set-but-invalid HWDE_NGSPICE_DLL pin still fails loudly."""
    try:
        dll = env.find_ngspice_dll()
    except env.EnvError as e:
        return check("ngspice-dll", False, str(e), NGSPICE_HELP)
    resolved["ngspice_dll"] = str(dll) if dll else None
    return check("ngspice-dll", dll is not None,
                 str(dll) if dll else
                 "no ngspice shared library found (KiCad bin or HWDE_NGSPICE_DLL)",
                 NGSPICE_HELP, warn=True)


SOLVERS_HELP = (
    "The simulators beyond KiCad's ngspice (Xyce, openEMS, Palace, gmsh, FastHenry2, "
    "FasterCap, OpenVAF) come from an unpacked IIC-OSIC-TOOLS image: unpack "
    "hpretl/iic-osic-tools (no docker daemon, no root; see the chip-flow module's "
    ".github/actions/toolchain) into ~/.cc/toolchains/iic-osic-tools-<ver> or "
    "/opt/eda-toolchain/iic-osic-tools-<ver>, or set HWDE_EDA_TOOLCHAIN. "
    "Optional: only sim_solve and the EM stages need it. docs/tools.md."
)


def check_solvers(resolved: dict) -> dict:
    """The IIC-OSIC-TOOLS tree with the open solvers (optional: a warning)."""
    try:
        from lib import solvers
        root = solvers.toolchain_root()
    except Exception as e:
        return check("solvers", False, str(e), SOLVERS_HELP, warn=True)
    resolved["eda_toolchain"] = str(root) if root else None
    return check("solvers", root is not None,
                 str(root) if root else "no IIC-OSIC-TOOLS tree found",
                 SOLVERS_HELP, warn=True)


def check_impedance_solver() -> dict:
    """lib/impedance.py's 2D field solver: numpy + scipy only, so it lives in
    the venv. A wrong answer here would mis-size every controlled-impedance
    trace, so it is a fail, not a warn."""
    try:
        from lib import impedance
        st = impedance.solver_status()
    except Exception as e:  # a broken scipy install fails in many ways
        return check("impedance-solver", False, f"{type(e).__name__}: {e}",
                     f"{Path(sys.executable)} -m pip install -r requirements.lock")
    detail = (f"stripline Z0 {st['z0']} vs exact {st['exact']} ohm "
              f"({st['err_pct']}% off)" if "z0" in st else st.get("detail", ""))
    return check("impedance-solver", bool(st["ok"]), detail,
                 f"{Path(sys.executable)} -m pip install -r requirements.lock")


def check_git() -> dict:
    import shutil
    g = shutil.which("git")
    return check("git", g is not None, g or "git not on PATH",
                 "Install git: https://git-scm.com/download/win")


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):  # cp1252 console guard
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", help="write JSON report to this file instead of stdout")
    ap.add_argument("--full", action="store_true",
                    help="also run live probes (SWIG roundtrip, IPC headless)")
    ap.add_argument("--quiet", action="store_true", help="suppress stderr summary")
    args = ap.parse_args(argv)

    resolved: dict = {"platform": sys.platform, "repo_root": str(env.repo_root())}
    checks: list[dict] = []
    try:
        checks.append(check_python())
        checks.extend(check_packages())
        lock = check_lock()
        if lock is not None:
            checks.append(lock)
        checks.extend(check_kicad(resolved, args.full))
        checks.extend(check_java(resolved))
        checks.append(check_krt(resolved))
        checks.append(check_lualatex(resolved))
        checks.append(check_ngspice(resolved))
        checks.append(check_solvers(resolved))
        checks.append(check_git())
        checks.append(check_impedance_solver())
        if args.full:
            checks.append(probe_ipc())
    except Exception:
        print(json.dumps({"script": "check_env", "status": "error",
                          "error": traceback.format_exc()}))
        return 2

    failed = [c for c in checks if c["status"] == "fail"]
    warned = [c for c in checks if c["status"] == "warn"]
    report = {
        "script": "check_env",
        "status": "fail" if failed else "pass",
        "resolved": resolved,
        "checks": checks,
    }
    text = json.dumps(report, indent=2)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    else:
        print(text)

    if not args.quiet:
        for c in failed:
            print(f"FAIL {c['name']}: {c['detail']}", file=sys.stderr)
            if c.get("remediation"):
                print(f"  fix: {c['remediation']}", file=sys.stderr)
        for c in warned:
            print(f"warn {c['name']}: {c['detail']}", file=sys.stderr)
        print(f"check_env: {len(failed)} failed, {len(warned)} warnings, "
              f"{len(checks)} checks", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print(json.dumps({"script": "check_env", "status": "error",
                          "error": traceback.format_exc()}))
        sys.exit(2)
