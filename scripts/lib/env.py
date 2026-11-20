"""Toolchain discovery for hwde scripts.

Every hwde script resolves external tools through this module so that:

1. The whole pipeline agrees on ONE KiCad install. File formats are not
   forward-compatible (a KiCad-10-format board is unreadable by kicad-cli 9,
   exit 3), so mixing versions between steps silently breaks gates.
2. Tests and CI can redirect discovery with environment variables instead of
   mutating the machine.

Environment overrides (all optional; a set-but-invalid override is an error,
never silently ignored - a wrong pin must fail loudly):

  HWDE_KICAD_CLI        full path to kicad-cli executable (pins the install)
  HWDE_KICAD_ROOT       KiCad install root containing bin/
  HWDE_JAVA             full path to a java executable (for Freerouting)
  HWDE_FREEROUTING_JAR  full path to a freerouting jar
  HWDE_FR_HEAP_MB       Freerouting JVM heap cap in MB (default: half the job's
                        memory budget, 512-1536; 0 = Java's default; #897)
  HWDE_PDFLATEX         full path to a pdflatex executable (for report_gen)
  HWDE_NGSPICE_DLL      full path to a shared ngspice library (for sim_run)
  HWDE_BOARDS_ROOT      where board workspaces live (default ~/dev/boards, the
                        boards repo; hwde itself holds no boards)
  HWDE_EASYEDA_CACHE    LCSC model cache (default: the board's easyeda/ dir,
                        made on the first fetch, else ~/.cache/hwde/easyeda)
  HWDE_EASYEDA_PINNED   read-only LCSC model dirs (os.pathsep-separated), read
                        after the caches and never written (the test suite's
                        tests/fixtures/easyeda_pinned; #1254)
  HWDE_TOOLS_DIR        the tools/ dir holding jre/, freerouting/ and krt/
                        (default: this checkout's tools/ when it has one, else
                        ~/dev/ai-ee/tools - a vendored copy of the skill, as in
                        the boards repo, has no tools/ of its own). A shared
                        tools folder (the Hive's /hive/tools) may also hold
                        KiCad AppImage AppDirs (kicad-<ver>/) and KRT under
                        kicad-routing-tools/; both are discovered there.
  HWDE_KRT_DIR          KiCadRoutingTools plugins dir (pins find_krt)

Version selection (the Hive's toolver keeps several versions of a tool side by
side, ~/tools/versions/<tool>/<version>/, beside the pinned tools folder; a
HWDE_* path pin above still wins over these):

  HIVE_TOOL_KRT         unset or "default": the newest KRT in tools_dir() (as
                        before); "newest": the highest version there or in the
                        versions folder; "X.Y.Z": exactly that one (an error
                        when it isn't installed)
  HIVE_TOOL_FREEROUTING the same for the Freerouting jar
  HIVE_TOOL_VERSIONS    the versions folder (default: tools_dir()'s parent's
                        versions/, so ~/tools/versions for ~/tools/hive-tools)
  HIVE_TOOL_KRT_RUNGS   several KRT versions at once (find_krt_versions): unset,
                        "" or "off": the one find_krt() picks; "all": every
                        installed version, the default one first; "X.Y.Z,A.B.C":
                        exactly those, in that order. A router stage that reads
                        it (route_auto's KRT finish, route_critical) runs each
                        as a rung from the same board and keeps the best

The skill was ai-ee before it was hwde, so the pre-rename AIEE_* spelling of
every one of these is still read (HWDE_ wins when both are set) - an existing
shell profile or CI job keeps working untouched. Errors name whichever
variable is actually set.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Minimum KiCad per SPEC.md section 1.
MIN_KICAD = (9, 0, 5)
# The exact KiCad the box host runs and CI runs (the image tag in
# .github/workflows/checks.yml and docker/Dockerfile; tests/test_kicad_pin.py
# holds them equal). check_env warns, never fails, on any other version:
# 10.0.x share one file format, but DRC/render numbers move between patches.
KICAD_PIN = (10, 0, 6)
# The pipeline authors all boards in KiCad 10 format and
# formats are not forward-compatible: a 9.x kicad-cli exits 3 on them. An
# EXPLICIT pin (HWDE_KICAD_CLI / HWDE_KICAD_ROOT) below major 10 is therefore
# always a stale-pin mistake and is rejected loudly (ladder row 18). Plain
# discovery still accepts >= MIN_KICAD, so 9.0.5 stays reachable for manual
# experiments by NOT pinning.
PIN_MIN_MAJOR = 10
# Preference order among installed majors. 10.x preferred: it adds
# `pcb drc --refill-zones --save-board` (headless zone refill without SWIG)
# and `sch upgrade`. 9.0.5 is the verified fallback.
KICAD_PREFERENCE = ("10.", "9.")
# Freerouting 2.2.4 requires Java 25 (class file v69). Vendor docs saying
# "Java 21+" are stale for 2.2.x - verified on this machine (Java 24 fails
# with UnsupportedClassVersionError).
MIN_JAVA_FOR_FREEROUTING = 25

_TIMEOUT = 30
# `kicad-cli version` starts KiCad (an AppImage unpacks itself first): on a
# strained machine that alone has taken over 30 s, and a pin whose version
# probe times out is refused as "not a working kicad-cli" (issue #252).
_VERSION_TIMEOUT = 120

# Linux headless: KiCad's SWIG Specctra export/import (route_swig) and other
# wx-backed pcbnew paths refuse to run without an X display ("Unable to access
# the X Display"). The container starts an Xvfb on :99 (docker/project-init);
# hand it to every child process when the caller has no display of its own.
# Only a live server: a socket file left by an Xvfb that died answers nothing,
# and a DISPLAY that points at it makes Java and wx fail where no DISPLAY
# lets them run headless or under bin/xvfb-private (#1166).


def _x_live(sock: str) -> bool:
    import socket
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        s.connect(sock)
        return True
    except OSError:
        return False
    finally:
        s.close()


if (sys.platform.startswith("linux") and not os.environ.get("DISPLAY")
        and Path("/tmp/.X11-unix/X99").exists()
        and _x_live("/tmp/.X11-unix/X99")):
    os.environ["DISPLAY"] = ":99"


class EnvError(RuntimeError):
    """An explicit override (HWDE_*/AIEE_*) points at something unusable."""


def skill_env(suffix: str) -> tuple[str | None, str]:
    """Read the skill's HWDE_<suffix> variable, falling back to pre-rename AIEE_<suffix>.

    Returns (value, varname). varname is the variable that supplied the value,
    so an error message points at the spelling the user actually set; when
    neither is set it is the current HWDE_ name.
    """
    for prefix in ("HWDE_", "AIEE_"):
        val = os.environ.get(prefix + suffix)
        if val:
            return val, prefix + suffix
    return None, "HWDE_" + suffix


def repo_root() -> Path:
    """Repo root = nearest ancestor of this file containing SPEC.md or .git."""
    p = Path(__file__).resolve()
    for parent in p.parents:
        if (parent / "SPEC.md").exists() or (parent / ".git").exists():
            return parent
    # Fallback: fixed relative position scripts/lib/env.py
    return p.parents[2]


def boards_root() -> Path:
    """Where board workspaces live: HWDE_BOARDS_ROOT, else ~/dev/boards.

    Boards are their own repo (ihsan-sa/boards), not part of the skill, so
    every default workspace location, sweep and render resolves through here.
    Not validated: a fresh box may not have cloned the boards repo yet, and
    the caller that needs a workspace says so when it is missing.
    """
    val, _ = skill_env("BOARDS_ROOT")
    return Path(val or "~/dev/boards").expanduser()


def easyeda_cache(board_dir: Path | None = None) -> Path:
    """The EasyEDA model cache: HWDE_EASYEDA_CACHE, else `<board_dir>/easyeda`
    (created on the first fetch), else ~/.cache/hwde/easyeda (the shared
    cache).

    The board's own dir is the cache even before it exists, so the models a
    board's checks fetch are kept in the board and committed with it: a fresh
    checkout or a scratch copy then never asks EasyEDA again. easyeda.get
    reads the shared cache too when the board's has no copy of a part, so a
    board without the dir yet does not look model-less.
    """
    val, _ = skill_env("EASYEDA_CACHE")
    if val:
        return Path(val).expanduser()
    if board_dir is not None:
        return Path(board_dir) / "easyeda"
    return Path.home() / ".cache" / "hwde" / "easyeda"


def tools_dir() -> Path:
    """Where the vendored JRE, Freerouting and KRT live (tools/jre, ...).

    HWDE_TOOLS_DIR wins (a set-but-missing dir is an error). Otherwise the
    checkout's own tools/ when it exists, else the hwde checkout's at
    ~/dev/ai-ee/tools: a copy of the skill vendored into another repo (the
    boards repo) resolves repo_root() to that repo, which carries no tools/.
    """
    pin, var = skill_env("TOOLS_DIR")
    if pin:
        p = Path(pin).expanduser()
        if not p.is_dir():
            raise EnvError(f"{var} does not exist: {pin}")
        return p
    own = repo_root() / "tools"
    return own if own.is_dir() else Path("~/dev/ai-ee/tools").expanduser()


def is_boards_dir(path: Path) -> bool:
    """True when `path` is a directory that holds board workspaces: the
    configured boards_root(), or any directory named `boards` (the layout a
    test fixture or an older checkout uses)."""
    p = Path(path)
    if p.name == "boards":
        return True
    try:
        return p.resolve() == boards_root().resolve()
    except OSError:
        return False


def _run(cmd: list[str], timeout: int = _TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout,
        encoding="utf-8", errors="replace",
    )


def parse_version(text: str) -> tuple[int, ...]:
    """First dotted integer group in text -> tuple. '9.0.5-1' -> (9, 0, 5)."""
    m = re.search(r"(\d+(?:\.\d+)+)", text)
    if not m:
        return ()
    return tuple(int(x) for x in m.group(1).split("."))


# ---------------------------------------------------------------- KiCad

def kicad_installs() -> list[Path]:
    """Discover KiCad install roots (dirs containing bin/kicad-cli[.exe]).

    On Linux this includes KiCad AppImage AppDirs (extracted, as in a shared
    tools folder) at tools_dir()/kicad-*/: bin/kicad-cli there runs as is,
    with bin/python3.<n> (pcbnew), lib/libngspice.so and the 3D loaders under
    usr/lib/<triplet>/kicad/plugins/3d beside it.
    """
    roots: list[Path] = []
    if sys.platform == "win32":
        for pf in (os.environ.get("PROGRAMFILES", r"C:\Program Files"),):
            base = Path(pf) / "KiCad"
            if base.is_dir():
                roots += [d for d in base.iterdir()
                          if (d / "bin" / "kicad-cli.exe").exists()]
    elif sys.platform == "darwin":
        for app in Path("/Applications").glob("KiCad*/KiCad.app"):
            d = app / "Contents" / "MacOS"
            if (d / "kicad-cli").exists():
                roots.append(app / "Contents")
    else:
        w = shutil.which("kicad-cli")
        if w:
            roots.append(Path(w).parent.parent)
        roots += [d for d in sorted(tools_dir().glob("kicad-*"))
                  if (d / "AppRun").exists() and (d / "bin" / "kicad-cli").exists()]
    return roots


def _cli_name() -> str:
    return "kicad-cli.exe" if sys.platform == "win32" else "kicad-cli"


def _cli_of_root(root: Path) -> Path:
    # Windows/Linux: <root>/bin/kicad-cli; macOS .app bundle: Contents/MacOS.
    for sub in ("bin", "MacOS"):
        c = root / sub / _cli_name()
        if c.exists():
            return c
    raise EnvError(f"KiCad root pin has no bin/{_cli_name()}: {root}")


def _validate_pin(path: Path, var: str = "HWDE_KICAD_CLI",
                  ver: tuple[int, ...] | None = None) -> Path:
    """Reject an explicit KiCad pin whose major is below the pipeline format.

    ver is probed from the binary when not given (tests pass a stub tuple)."""
    if ver is None:
        ver = kicad_cli_version(path)
    if not ver:
        raise EnvError(
            f"{var} points at {path} but its version could not be determined "
            "(not a working kicad-cli?); unset the pin or point it at the "
            "10.0.3 install")
    if ver[0] < PIN_MIN_MAJOR:
        vs = ".".join(str(x) for x in ver)
        raise EnvError(
            f"{var} pins KiCad {vs}: pipeline file formats are 10.x "
            "(9.x cannot load them); unset the stale pin or point it at the "
            "10.0.3 install")
    return path


def find_kicad_cli() -> Path | None:
    """Resolve the pipeline's kicad-cli. Explicit pin > PATH > preferred install."""
    pin, var = skill_env("KICAD_CLI")
    if pin:
        p = Path(pin)
        if not p.exists():
            raise EnvError(f"{var} does not exist: {pin}")
        return _validate_pin(p, var)
    root_pin, root_var = skill_env("KICAD_ROOT")
    if root_pin:
        return _validate_pin(_cli_of_root(Path(root_pin)), root_var)
    w = shutil.which("kicad-cli")
    if w:
        return Path(w)
    installs = kicad_installs()
    if not installs:
        return None
    scored: list[tuple[int, tuple[int, ...], Path]] = []
    for root in installs:
        cli = _cli_of_root(root)
        ver = kicad_cli_version(cli)
        if ver < MIN_KICAD:
            continue
        vs = ".".join(str(x) for x in ver)
        pref = next((len(KICAD_PREFERENCE) - i
                     for i, p in enumerate(KICAD_PREFERENCE) if vs.startswith(p)), 0)
        scored.append((pref, ver, cli))
    if not scored:
        return None
    scored.sort(key=lambda t: (t[0], t[1]))
    return scored[-1][2]


def _state_dir() -> Path:
    """bin/hwde's state dir: HWDE_HOME, else ~/.cache/hwde (always in HOME:
    a published copy of the skill is read-only)."""
    return Path(os.environ.get("HWDE_HOME") or Path.home() / ".cache" / "hwde")


def _binary_key(cli: Path) -> str | None:
    """What identifies a kicad-cli binary for the version cache.

    A small file (bin/hwde's wrapper, rewritten on every run, so its mtime
    says nothing) is keyed by its contents, which name the AppDir and so its
    version; a real binary by its size and mtime."""
    try:
        real = Path(cli).resolve()
        st = real.stat()
        if st.st_size <= 64 * 1024:
            import hashlib
            tag = hashlib.sha256(real.read_bytes()).hexdigest()
        else:
            tag = f"{st.st_size}:{st.st_mtime_ns}"
    except OSError:
        return None
    return f"{real}|{tag}"


_VERSION_LINES: dict[str, str | None] = {}


def kicad_cli_version_line(cli: Path) -> str | None:
    """First line of `kicad-cli version` (None when it fails), asked once
    per binary: kept in this process and in <state>/kicad-cli-version.json,
    so a pipeline of scripts doesn't start KiCad again on every call."""
    import json
    key = _binary_key(cli)
    if key is not None and key in _VERSION_LINES:
        return _VERSION_LINES[key]
    cache = _state_dir() / "kicad-cli-version.json"
    known: dict = {}
    if key is not None:
        try:
            known = json.loads(cache.read_text(encoding="utf-8"))
            if not isinstance(known, dict):
                known = {}
        except (OSError, ValueError):
            known = {}
        line = known.get(key)
        if isinstance(line, str):
            _VERSION_LINES[key] = line
            return line
    try:
        cp = _run([str(cli), "version"], timeout=_VERSION_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = (cp.stdout or "").strip().splitlines()
    line = lines[0] if lines and cp.returncode == 0 else None
    # Only a version that parses is kept: a failed or odd run is asked again.
    if key is None or line is None or not parse_version(line):
        return line
    _VERSION_LINES[key] = line
    known[key] = line
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_name(f"{cache.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(known, indent=1, sort_keys=True),
                       encoding="utf-8")
        os.replace(tmp, cache)
    except OSError:
        pass
    return line


def kicad_cli_version(cli: Path) -> tuple[int, ...]:
    return parse_version(kicad_cli_version_line(cli) or "")


_SYM_ENV_RE = re.compile(r"^KICAD(\d*)_SYMBOL_DIR$")


def kicad_symbol_dir() -> Path | None:
    """Stock symbol libraries of the pipeline's KiCad, or None.

    KiCad's own KICAD<N>_SYMBOL_DIR wins, highest major first (the same rule
    as board_swig's footprint root). Else the pinned kicad-cli's install:
    <root>/share/kicad/symbols (container, Windows) or, for the no-container
    Linux host's unpacked debs (bin/kicad-cli is a wrapper beside usr/),
    <root>/usr/share/kicad/symbols.
    """
    hits = []
    for k, v in os.environ.items():
        m = _SYM_ENV_RE.match(k)
        if m and v:
            hits.append((int(m.group(1)) if m.group(1) else -1, v))
    for _, v in sorted(hits, key=lambda kv: -kv[0]):
        # Not our own flat copies (export_kicad_symbol_dir points
        # KICAD_SYMBOL_DIR there): the stock dir is what they are copied from.
        if Path(v).is_dir() and _symbol_cache_root() not in Path(v).resolve().parents:
            return Path(v)
    try:
        cli = find_kicad_cli()
    except EnvError:
        return None
    if cli is None:
        return None
    root = cli.parents[1]
    for cand in (root / "share" / "kicad" / "symbols",
                 root / "usr" / "share" / "kicad" / "symbols"):
        if cand.is_dir():
            return cand
    return None


def export_kicad_symbol_dir() -> None:
    """Point kicad-sch-api at the pinned KiCad's symbol libraries.

    It reads only KICAD_SYMBOL_DIR and KICAD7-9_SYMBOL_DIR, then fixed system
    paths, so on the no-container host (libs under ~/.local/kicad10/usr) it
    finds none and every lib_id lookup fails. Call before its first
    get_symbol_cache(); an existing KICAD_SYMBOL_DIR is left alone.
    """
    if os.environ.get("KICAD_SYMBOL_DIR"):
        return
    d = kicad_symbol_dir()
    if d is None:
        return
    if any(d.glob("*.kicad_symdir")):
        # KiCad 10 form: point it at flat copies (its rglob would take each
        # symbol file in a symdir for a library); schlib registers any other
        # stock library on first use (register_stock_libs).
        for lib in SYMDIR_EAGER:
            stock_symbol_lib(lib, d)
        d = symbol_cache_dir(d)
        if not any(d.glob("*.kicad_sym")):
            return
    os.environ["KICAD_SYMBOL_DIR"] = str(d)


# KiCad 10 ships its stock symbols as one-file-per-symbol <lib>.kicad_symdir
# folders; kicad-sch-api 0.5.6 reads only flat <lib>.kicad_sym files (and its
# rglob would take every symbol file in a symdir for a library of its own).
# The libraries the skill places by lib_id on every board are flattened up
# front; the rest on demand.
SYMDIR_EAGER = ("power", "Device")


def _symbol_cache_root() -> Path:
    return (Path.home() / ".cache" / "hwde" / "symbols").resolve()


def symbol_cache_dir(stock: Path) -> Path:
    """Where flat copies of `stock`'s symdir libraries go: one folder per
    stock symbols dir (so two KiCads never share copies), under
    ~/.cache/hwde/symbols."""
    key = hashlib.sha256(str(Path(stock).resolve()).encode()).hexdigest()[:12]
    return _symbol_cache_root() / key


def flatten_symdir(symdir: Path, out: Path) -> Path:
    """Write the symbols of a <lib>.kicad_symdir folder as one flat
    <lib>.kicad_sym at `out`, unchanged, so a schematic embeds the same
    lib_symbols as one built against KiCad's own library. Written to a temp
    file and renamed: concurrent runs never see half a library."""
    bodies, version = [], "20251024"
    for f in sorted(symdir.glob("*.kicad_sym")):
        txt = f.read_text(encoding="utf-8")
        m = re.search(r"\(version\s+(\d+)\)", txt)
        if m:
            version = max(version, m.group(1))
        bodies.append(txt[txt.index("(symbol "):txt.rindex(")")].rstrip())
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.{os.getpid()}.tmp")
    tmp.write_text(f'(kicad_symbol_lib\n\t(version {version})\n'
                   '\t(generator "hwde")\n\t' + "\n\t".join(bodies) + "\n)\n",
                   encoding="utf-8")
    tmp.replace(out)
    return out


def stock_symbol_lib(lib: str, stock: Path | None = None) -> Path | None:
    """The flat .kicad_sym file for stock library `lib` of the pipeline's
    KiCad, or None when it has no such library. A KiCad-10 symdir library is
    flattened into symbol_cache_dir() on first use and again whenever the
    symdir is newer than the copy."""
    stock = stock or kicad_symbol_dir()
    if stock is None:
        return None
    flat = stock / f"{lib}.kicad_sym"
    if flat.is_file():
        return flat
    symdir = stock / f"{lib}.kicad_symdir"
    if not symdir.is_dir():
        return None
    out = symbol_cache_dir(stock) / f"{lib}.kicad_sym"
    if not out.is_file() or out.stat().st_mtime < symdir.stat().st_mtime:
        flatten_symdir(symdir, out)
    return out


def register_stock_libs(cache, lib_ids) -> list[Path]:
    """Add the stock libraries `lib_ids` ("Lib:Symbol") name to a
    kicad-sch-api symbol cache, flattening KiCad-10 symdirs as needed.

    A library the cache already knows (a project library, or one found under
    KICAD_SYMBOL_DIR) is left alone. Returns the files added."""
    known = getattr(cache, "_library_index", {})
    added = []
    for lib in sorted({str(i).split(":", 1)[0] for i in lib_ids if ":" in str(i)}):
        if lib in known:
            continue
        p = stock_symbol_lib(lib)
        if p is not None and cache.add_library_path(str(p)):
            added.append(p)
    return added


def find_kicad_python(cli: Path) -> Path | None:
    """KiCad's BUNDLED python (has the SWIG pcbnew module; the venv does not).

    Needed for Specctra DSN export / SES import and KiCad-9 zone refill.
    """
    name = "python.exe" if sys.platform == "win32" else "python3"
    for cand in (cli.parent / name, cli.parent.parent / "bin" / name):
        if cand.exists():
            return cand
    # A KiCad AppImage ships only bin/python3.<n>, which finds its own
    # dist-packages (pcbnew) when run from the AppDir.
    if sys.platform.startswith("linux") and (cli.parent.parent / "AppRun").exists():
        pys = sorted(cli.parent.glob("python3.*"),
                     key=lambda p: parse_version(p.name))
        if pys:
            return pys[-1]
    return None


# ---------------------------------------------------------------- Java / Freerouting

def java_major(java: Path) -> int:
    """Major version of a java executable, 0 if undeterminable."""
    try:
        cp = _run([str(java), "-version"])
    except (OSError, subprocess.TimeoutExpired):
        return 0
    ver = parse_version(cp.stderr or cp.stdout)
    if not ver:
        return 0
    return ver[1] if ver[0] == 1 and len(ver) > 1 else ver[0]  # "1.8" -> 8


def find_java() -> tuple[Path, int] | None:
    """Best java for Freerouting: HWDE_JAVA > tools_dir()/jre > PATH.

    Returns (path, major). Callers decide if major is sufficient.
    """
    pin, var = skill_env("JAVA")
    if pin:
        p = Path(pin)
        if not p.exists():
            raise EnvError(f"{var} does not exist: {pin}")
        return p, java_major(p)
    name = "java.exe" if sys.platform == "win32" else "java"
    candidates = sorted(tools_dir().glob(f"jre/*/bin/{name}"))
    w = shutil.which("java")
    if w:
        candidates.append(Path(w))
    best: tuple[Path, int] | None = None
    for c in candidates:
        major = java_major(c)
        if best is None or major > best[1]:
            best = (c, major)
    return best


def find_freerouting_jar() -> Path | None:
    pin, var = skill_env("FREEROUTING_JAR")
    if pin:
        p = Path(pin)
        if not p.exists():
            raise EnvError(f"{var} does not exist: {pin}")
        _pin_agrees("FREEROUTING", var, parse_version(p.name))
        return p
    jars = sorted(tools_dir().glob("freerouting/freerouting-*.jar"))
    side = [j for j in tool_versions_dir().glob("freerouting/*/freerouting-*.jar")
            if _vetted(j.parent)]
    return _pick_version("FREEROUTING", jars[-1:], jars + side,
                         lambda p: parse_version(p.name))


def find_krt() -> Path | None:
    """KiCadRoutingTools plugins dir (vendored under tools/krt, S11).

    Returns the newest KiCadRoutingTools-*/plugins dir under tools_dir()/krt
    or, in a shared tools folder, tools_dir()/kicad-routing-tools (the
    scripts sys.path-insert relative dirs, so callers must run them with
    cwd=plugins). Override with HWDE_KRT_DIR (the plugins dir; a release
    root or its parent, with plugins/ under it, is followed down to it).
    """
    pin, var = skill_env("KRT_DIR")
    if pin:
        p = Path(pin)
        if not p.is_dir():
            raise EnvError(f"{var} does not exist: {pin}")
        if not (p / "route_diff.py").exists():
            down = sorted(p.glob("plugins")) + sorted(
                p.glob("KiCadRoutingTools-*/plugins"))
            if down:
                p = down[-1]
        got = krt_scripts(p)
        _pin_agrees("KRT", var, krt_version(got))
        return got
    dirs = [d for sub in ("krt", "kicad-routing-tools")
            for d in tools_dir().glob(f"{sub}/KiCadRoutingTools-*/plugins")]
    dirs.sort(key=lambda d: parse_version(d.parent.name))
    side = [d for d in tool_versions_dir().glob("krt/*/KiCadRoutingTools-*/plugins")
            if _vetted(d.parents[1])]
    got = _pick_version("KRT", dirs[-1:], dirs + side,
                        lambda d: parse_version(d.parent.name))
    return krt_scripts(got) if got else None


def find_krt_versions(spec: str | None = None) -> list[Path]:
    """The KRT script dirs a stage should try in turn (several versions
    active at once, the Hive's toolver). spec (else HIVE_TOOL_KRT_RUNGS):
    None/""/"off" -> [find_krt()] (one version, as before; [] when none);
    "all" -> find_krt()'s pick first, then every other installed version,
    newest first; "X.Y.Z,A.B.C" -> exactly those, in that order (EnvError
    naming what is installed when one is missing). A HWDE_KRT_DIR pin
    means one version: with a spec other than off that is an error."""
    if spec is None:
        spec = os.environ.get("HIVE_TOOL_KRT_RUNGS") or ""
    spec = spec.strip()
    if spec in ("", "off"):
        one = find_krt()
        return [one] if one else []
    pin, var = skill_env("KRT_DIR")
    if pin:
        raise EnvError(f"{var} pins one KRT; it can't be combined with "
                       f"several versions ({spec}); unset one of them")
    dirs = [d for sub in ("krt", "kicad-routing-tools")
            for d in tools_dir().glob(f"{sub}/KiCadRoutingTools-*/plugins")]
    dirs += [d for d in tool_versions_dir().glob(
        "krt/*/KiCadRoutingTools-*/plugins") if _vetted(d.parents[1])]
    by_ver: dict[tuple, Path] = {}
    for d in sorted(dirs, key=lambda d: parse_version(d.parent.name)):
        by_ver.setdefault(parse_version(d.parent.name), krt_scripts(d))
    if spec == "all":
        first = find_krt()
        rest = [by_ver[v] for v in sorted(by_ver, reverse=True)
                if first is None or by_ver[v] != first]
        return ([first] if first else []) + rest
    out: list[Path] = []
    for part in spec.split(","):
        want = parse_version(part)
        if not want or want not in by_ver:
            have = ", ".join(".".join(map(str, v)) for v in sorted(by_ver)) \
                or "none"
            raise EnvError(f"HIVE_TOOL_KRT_RUNGS: KRT {part.strip()} is not "
                           f"installed (have: {have})")
        if by_ver[want] not in out:
            out.append(by_ver[want])
    return out


def krt_label(scripts: Path) -> str:
    """'0.24.1' for a find_krt() dir ('?' when its folder doesn't say)."""
    return ".".join(map(str, krt_version(scripts))) or "?"


def krt_scripts(plugins: Path) -> Path:
    """The dir holding KRT's scripts (route.py, route_diff.py, bga_fanout/).

    Up to 0.23 that is plugins/ itself; KRT 0.24 moved them into
    plugins/py_router/ (they find rust_router/ at ../ from there), so callers
    that run them with cwd=<this dir> work on both layouts.
    """
    sub = plugins / "py_router"
    return sub if (sub / "route.py").exists() else plugins


def krt_version(scripts: Path) -> tuple[int, ...]:
    """The KRT version of a find_krt() dir, from its KiCadRoutingTools-<v> parent."""
    for d in (scripts, *scripts.parents):
        if d.name.startswith("KiCadRoutingTools-"):
            return parse_version(d.name)
    return ()


def tool_versions_dir() -> Path:
    """Where toolver keeps other versions side by side: HIVE_TOOL_VERSIONS,
    else tools_dir()'s parent's versions/ (not validated: often absent)."""
    val = os.environ.get("HIVE_TOOL_VERSIONS")
    if val:
        return Path(val).expanduser()
    return tools_dir().parent / "versions"


def _vetted(version_dir: Path) -> bool:
    """A side-by-side version counts only once `toolver install` has checked
    its digest and written PROVENANCE.json (a half-copied or hand-dropped
    folder is not picked)."""
    return (version_dir / "PROVENANCE.json").is_file()


def _pin_agrees(tool: str, var: str, version: tuple) -> None:
    """A path pin (HWDE_*) set together with HIVE_TOOL_<tool> must name the
    same version, else EnvError: one run must not quietly take the pin when
    the caller asked for a version (or the newest) by HIVE_TOOL_<tool>."""
    spec = (os.environ.get("HIVE_TOOL_" + tool) or "default").strip()
    if spec == "default" or (spec != "newest" and version
                             and parse_version(spec) == version):
        return
    have = ".".join(map(str, version)) or "unknown"
    raise EnvError(f"{var} (version {have}) and HIVE_TOOL_{tool}={spec} "
                   "disagree; unset one of them")


def _pick_version(tool: str, default: list, every: list, key):
    """HIVE_TOOL_<tool>: unset/default -> `default`'s one entry; newest -> the
    highest of `every`; X.Y.Z -> the entry of exactly that version (else
    EnvError, naming what is installed)."""
    var = "HIVE_TOOL_" + tool
    spec = (os.environ.get(var) or "default").strip()
    if spec == "default":
        return default[0] if default else None
    every = sorted(every, key=key)
    if spec == "newest":
        return every[-1] if every else None
    want = parse_version(spec)
    hit = [p for p in every if want and key(p) == want]
    if not hit:
        have = ", ".join(".".join(map(str, key(p))) for p in every) or "none"
        raise EnvError(f"{var}={spec} is not installed (have: {have}); "
                       f"`toolver install {tool.lower()} {spec}` puts it side by side "
                       "(a side folder counts only with its PROVENANCE.json)")
    return hit[-1]


# ---------------------------------------------------------------- ngspice (optional)

def find_ngspice_dll() -> Path | None:
    """Shared ngspice library for the SPICE sim gate (sim_run.py).

    Ladder: HWDE_NGSPICE_DLL pin > the DLL KiCad bundles beside kicad-cli
    (KiCad >= 9 ships ngspice for its own simulator) > None (sim gate cannot
    run). The pipeline loads it in-process via InSpice's shared-library
    binding - there is no ngspice.exe on this ladder on purpose: only the
    shared library exposes the ngGet_Vec_Info API the runner needs.
    """
    pin, var = skill_env("NGSPICE_DLL")
    if pin:
        p = Path(pin)
        if not p.exists():
            raise EnvError(f"{var} does not exist: {pin}")
        return p
    cli = find_kicad_cli()  # EnvError from a bad KiCad pin stays loud
    if cli is None:
        return None
    if sys.platform == "win32":
        name = "ngspice.dll"
    elif sys.platform == "darwin":
        name = "libngspice.dylib"
    else:
        name = "libngspice.so"
    # Beside kicad-cli; an AppImage keeps it in lib/ (-> shared/lib).
    for cand in (cli.parent / name, cli.parent.parent / "lib" / name):
        if cand.exists():
            return cand
    return None


# ---------------------------------------------------------------- pdflatex (optional)

def find_pdflatex() -> Path | None:
    """pdflatex for report_gen's design document (optional toolchain member).

    Ladder: HWDE_PDFLATEX pin > PATH > the Windows per-user MiKTeX default
    install location. None = not installed (report_gen degrades to --tex-only).
    """
    pin, var = skill_env("PDFLATEX")
    if pin:
        p = Path(pin)
        if not p.exists():
            raise EnvError(f"{var} does not exist: {pin}")
        return p
    w = shutil.which("pdflatex")
    if w:
        return Path(w)
    if sys.platform == "win32":
        cand = (Path(os.environ.get("LOCALAPPDATA", ""))
                / "Programs" / "MiKTeX" / "miktex" / "bin" / "x64"
                / "pdflatex.exe")
        if cand.exists():
            return cand
    return None


# ---------------------------------------------------------------- Docker (optional)

def docker_state() -> str:
    """'up' (daemon reachable), 'down' (client only), or 'absent'."""
    if not shutil.which("docker"):
        return "absent"
    try:
        cp = _run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=10)
        return "up" if cp.returncode == 0 and cp.stdout.strip() else "down"
    except (OSError, subprocess.TimeoutExpired):
        return "down"
