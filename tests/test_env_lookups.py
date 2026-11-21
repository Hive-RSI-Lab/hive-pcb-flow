"""The skill's own lookups for a shared tools folder (the Hive's /hive/tools):
KiCad AppImage AppDirs, KiCadRoutingTools under kicad-routing-tools/, and
KiCad 10's .kicad_symdir stock symbol libraries.

    $HWDE_PY -m pytest tests        (HWDE_PY from `bin/hwde env`)

The fake-tree tests run anywhere; the ones marked `hive` run against the real
tools in /hive/tools and skip where they aren't.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
sys.path.insert(0, str(SKILL / "scripts" / "lib"))
import env  # noqa: E402
import kc  # noqa: E402

HIVE_KICAD = Path("/hive/tools/kicad-10.0.6")
hive = pytest.mark.skipif(not (HIVE_KICAD / "AppRun").exists(),
                          reason="no Hive KiCad 10.0.6 AppDir here")

SYM = """(kicad_symbol_lib
\t(version 20251024)
\t(generator "kicad_symbol_editor")
\t(symbol "{name}"
\t\t(property "Reference" "{ref}")
\t)
)
"""


def touch(p: Path, text: str = "") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


@pytest.fixture
def clean(monkeypatch, tmp_path):
    """No pins, no PATH kicad-cli, HOME in tmp (the symbol cache goes there)."""
    for k in list(os.environ):
        if k.startswith(("HWDE_", "AIEE_", "HIVE_TOOL_")) or env._SYM_ENV_RE.match(k):
            monkeypatch.delenv(k)
    monkeypatch.setattr(env.shutil, "which", lambda name: None)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HWDE_TOOLS_DIR", str(tmp_path / "tools"))
    (tmp_path / "tools").mkdir()
    return tmp_path / "tools"


def appdir(tools: Path, ver: str = "10.0.6") -> Path:
    a = tools / f"kicad-{ver}"
    touch(a / "AppRun")
    touch(a / "bin" / "kicad-cli")
    touch(a / "bin" / "python3.11")
    touch(a / "shared" / "lib" / "libngspice.so")
    os.symlink("shared/lib", a / "lib")
    return a


# ------------------------------------------------------------ KiCad AppDir

@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
def test_kicad_installs_finds_appdirs_in_tools(clean):
    a = appdir(clean)
    touch(clean / "kicad-9.0.0" / "bin" / "kicad-cli")  # no AppRun: not an AppDir
    assert env.kicad_installs() == [a]
    assert env._cli_of_root(a) == a / "bin" / "kicad-cli"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
def test_kicad_python_of_appdir_is_bin_python3_n(clean):
    a = appdir(clean)
    touch(a / "bin" / "python3.9")
    assert env.find_kicad_python(a / "bin" / "kicad-cli") == a / "bin" / "python3.11"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
def test_ngspice_of_appdir_is_in_lib(clean, monkeypatch):
    a = appdir(clean)
    monkeypatch.setattr(env, "find_kicad_cli", lambda: a / "bin" / "kicad-cli")
    assert env.find_ngspice_dll() == a / "lib" / "libngspice.so"


def test_render_env_finds_appdir_3d_loaders(clean, monkeypatch):
    a = appdir(clean)
    touch(a / "shared" / "lib" / "kicad" / "plugins" / "3d" / "libs3d_plugin_vrml.so")
    (a / "usr" / "lib" / "x86_64-linux-gnu" / "kicad").mkdir(parents=True)
    os.symlink("../../../../shared/lib/kicad/plugins",
               a / "usr" / "lib" / "x86_64-linux-gnu" / "kicad" / "plugins")
    monkeypatch.setattr(kc.sys, "platform", "linux")
    monkeypatch.setattr(kc, "_SYS_ROOT", clean / "noroot")
    monkeypatch.delenv("APPDIR", raising=False)
    renv, err = kc.render_env(a / "bin" / "kicad-cli")
    assert err == ""
    assert renv["APPDIR"] == str(a)


# ------------------------------------------------------------ KRT

def test_find_krt_in_shared_tools_layout(clean):
    for v in ("0.9.0", "0.19.0"):
        (clean / "kicad-routing-tools" / f"KiCadRoutingTools-{v}" / "plugins").mkdir(parents=True)
    # newest by version, not by string: 0.19.0 > 0.9.0
    assert env.find_krt() == (clean / "kicad-routing-tools"
                              / "KiCadRoutingTools-0.19.0" / "plugins")


def test_find_krt_still_reads_vendored_krt_dir(clean):
    p = clean / "krt" / "KiCadRoutingTools-0.19.0" / "plugins"
    p.mkdir(parents=True)
    assert env.find_krt() == p


def side_krt(tools: Path, v: str, py_router: bool = True) -> Path:
    """A toolver side-by-side KRT: versions/krt/V/KiCadRoutingTools-V/plugins."""
    plugins = tools.parent / "versions" / "krt" / v / f"KiCadRoutingTools-{v}" / "plugins"
    touch(tools.parent / "versions" / "krt" / v / "PROVENANCE.json", "{}")
    if py_router:  # KRT 0.24 keeps its scripts in plugins/py_router
        touch(plugins / "py_router" / "route.py")
        return plugins / "py_router"
    plugins.mkdir(parents=True)
    return plugins


def test_hive_tool_krt_default_keeps_the_pinned_install(clean, monkeypatch):
    pinned = clean / "kicad-routing-tools" / "KiCadRoutingTools-0.19.0" / "plugins"
    pinned.mkdir(parents=True)
    side_krt(clean, "0.24.1")
    assert env.find_krt() == pinned
    monkeypatch.setenv("HIVE_TOOL_KRT", "default")
    assert env.find_krt() == pinned


def test_hive_tool_krt_picks_a_version_or_the_newest(clean, monkeypatch):
    pinned = clean / "kicad-routing-tools" / "KiCadRoutingTools-0.19.0" / "plugins"
    pinned.mkdir(parents=True)
    new = side_krt(clean, "0.24.1")
    side_krt(clean, "0.9.3", py_router=False)
    monkeypatch.setenv("HIVE_TOOL_KRT", "0.24.1")
    assert env.find_krt() == new          # the scripts dir of the 0.24 layout
    assert env.krt_version(env.find_krt()) == (0, 24, 1)
    monkeypatch.setenv("HIVE_TOOL_KRT", "0.19.0")
    assert env.find_krt() == pinned
    monkeypatch.setenv("HIVE_TOOL_KRT", "newest")
    assert env.find_krt() == new          # 0.24.1 > 0.19.0 > 0.9.3, by number


def test_hive_tool_krt_missing_version_is_loud(clean, monkeypatch):
    (clean / "kicad-routing-tools" / "KiCadRoutingTools-0.19.0" / "plugins").mkdir(parents=True)
    monkeypatch.setenv("HIVE_TOOL_KRT", "0.30.0")
    with pytest.raises(env.EnvError, match="0.30.0 is not installed.*0.19.0"):
        env.find_krt()


def test_hwde_krt_dir_pin_must_agree_with_hive_tool_krt(clean, monkeypatch):
    new = side_krt(clean, "0.24.1")
    side_krt(clean, "0.23.9", py_router=False)
    monkeypatch.setenv("HWDE_KRT_DIR", str(new.parent.parent))  # a release root
    assert env.find_krt() == new          # the pin alone
    monkeypatch.setenv("HIVE_TOOL_KRT", "0.24.1")
    assert env.find_krt() == new          # the pin and the same version
    for spec in ("0.23.9", "newest"):
        monkeypatch.setenv("HIVE_TOOL_KRT", spec)
        with pytest.raises(env.EnvError, match="HWDE_KRT_DIR.*disagree"):
            env.find_krt()


def test_side_version_without_provenance_is_not_picked(clean, monkeypatch):
    (clean / "kicad-routing-tools" / "KiCadRoutingTools-0.19.0" / "plugins").mkdir(parents=True)
    half = clean.parent / "versions" / "krt" / "0.24.1" / "KiCadRoutingTools-0.24.1" / "plugins"
    half.mkdir(parents=True)              # no PROVENANCE.json: not vetted
    monkeypatch.setenv("HIVE_TOOL_KRT", "0.24.1")
    with pytest.raises(env.EnvError, match="PROVENANCE"):
        env.find_krt()
    monkeypatch.setenv("HIVE_TOOL_KRT", "newest")
    assert env.krt_version(env.find_krt()) == (0, 19, 0)


def test_hive_tool_versions_moves_the_versions_dir(clean, monkeypatch, tmp_path):
    v = tmp_path / "elsewhere" / "krt" / "0.24.1" / "KiCadRoutingTools-0.24.1" / "plugins"
    v.mkdir(parents=True)
    touch(tmp_path / "elsewhere" / "krt" / "0.24.1" / "PROVENANCE.json", "{}")
    monkeypatch.setenv("HIVE_TOOL_VERSIONS", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("HIVE_TOOL_KRT", "newest")
    assert env.find_krt() == v


def test_hive_tool_freerouting_picks_a_side_jar(clean, monkeypatch):
    pinned = touch(clean / "freerouting" / "freerouting-2.2.4.jar")
    side = touch(clean.parent / "versions" / "freerouting" / "2.5.0" / "freerouting-2.5.0.jar")
    touch(side.parent / "PROVENANCE.json", "{}")
    assert env.find_freerouting_jar() == pinned
    monkeypatch.setenv("HIVE_TOOL_FREEROUTING", "2.5.0")
    assert env.find_freerouting_jar() == side
    monkeypatch.setenv("HIVE_TOOL_FREEROUTING", "newest")
    assert env.find_freerouting_jar() == side
    monkeypatch.setenv("HIVE_TOOL_FREEROUTING", "2.4.1")
    with pytest.raises(env.EnvError, match="HIVE_TOOL_FREEROUTING=2.4.1"):
        env.find_freerouting_jar()
    monkeypatch.setenv("HWDE_FREEROUTING_JAR", str(pinned))
    with pytest.raises(env.EnvError, match="HWDE_FREEROUTING_JAR.*disagree"):
        env.find_freerouting_jar()        # pin 2.2.4 vs HIVE_TOOL 2.4.1
    monkeypatch.setenv("HIVE_TOOL_FREEROUTING", "2.2.4")
    assert env.find_freerouting_jar() == pinned
    monkeypatch.delenv("HIVE_TOOL_FREEROUTING")
    assert env.find_freerouting_jar() == pinned


# ------------------------------------------------------------ symdir libraries

def stock(tmp: Path) -> Path:
    s = tmp / "symbols"
    touch(s / "Device.kicad_symdir" / "R.kicad_sym", SYM.format(name="R", ref="R"))
    touch(s / "Device.kicad_symdir" / "C.kicad_sym", SYM.format(name="C", ref="C"))
    touch(s / "power.kicad_symdir" / "GND.kicad_sym", SYM.format(name="GND", ref="#PWR"))
    touch(s / "Flat.kicad_sym", SYM.format(name="F", ref="F"))
    return s


def test_stock_symbol_lib_flattens_symdir(clean, tmp_path):
    s = stock(tmp_path)
    out = env.stock_symbol_lib("Device", s)
    assert out.parent == env.symbol_cache_dir(s)
    text = out.read_text()
    assert text.startswith("(kicad_symbol_lib\n\t(version 20251024)")
    assert '(symbol "C"' in text and '(symbol "R"' in text
    assert text.count("(") == text.count(")")
    assert env.stock_symbol_lib("Flat", s) == s / "Flat.kicad_sym"
    assert env.stock_symbol_lib("Nope", s) is None


def test_stock_symbol_lib_refreshes_when_symdir_changes(clean, tmp_path):
    s = stock(tmp_path)
    out = env.stock_symbol_lib("Device", s)
    os.utime(out, (1, 1))
    touch(s / "Device.kicad_symdir" / "L.kicad_sym", SYM.format(name="L", ref="L"))
    assert '(symbol "L"' in env.stock_symbol_lib("Device", s).read_text()


def test_export_points_at_flat_copies_and_lookup_sees_through(clean, tmp_path, monkeypatch):
    s = stock(tmp_path)
    monkeypatch.setenv("KICAD10_SYMBOL_DIR", str(s))
    monkeypatch.delenv("KICAD_SYMBOL_DIR", raising=False)
    env.export_kicad_symbol_dir()
    cache = Path(os.environ["KICAD_SYMBOL_DIR"])
    assert cache == env.symbol_cache_dir(s)
    assert sorted(p.name for p in cache.glob("*.kicad_sym")) == [
        "Device.kicad_sym", "power.kicad_sym"]
    # KICAD_SYMBOL_DIR now names the copies; the stock dir is still the source
    assert env.kicad_symbol_dir() == s


def test_register_stock_libs_adds_only_unknown_libs(clean, tmp_path, monkeypatch):
    s = stock(tmp_path)
    monkeypatch.setenv("KICAD10_SYMBOL_DIR", str(s))

    class Cache:
        def __init__(self):
            self._library_index = {"power": Path("/elsewhere/power.kicad_sym")}

        def add_library_path(self, p):
            self._library_index[Path(p).stem] = Path(p)
            return True

    c = Cache()
    added = env.register_stock_libs(c, ["Device:R", "Device:C", "power:GND",
                                        "aiee:X", "nocolon"])
    assert added == [env.symbol_cache_dir(s) / "Device.kicad_sym"]
    assert c._library_index["power"] == Path("/elsewhere/power.kicad_sym")


# ------------------------------------------------------------ the Hive's real tools

def run_skill(script: str, *args: str, tools: str = "/hive/tools") -> subprocess.CompletedProcess:
    e = {k: v for k, v in os.environ.items()
         if not k.startswith(("HWDE_", "KICAD")) and k != "APPDIR"}
    e["HWDE_TOOLS_DIR"] = tools
    return subprocess.run([sys.executable, str(SKILL / "scripts" / script), *args],
                          capture_output=True, text=True, env=e, timeout=300)


@hive
def test_hive_tools_resolve_without_pins():
    code = ("import env, kc; c = env.find_kicad_cli(); print(c); "
            "print(env.find_kicad_python(c)); print(env.find_krt()); "
            "print(env.kicad_symbol_dir()); print(kc.render_env(c)[1] == '')")
    e = {k: v for k, v in os.environ.items()
         if not k.startswith(("HWDE_", "KICAD")) and k != "APPDIR"}
    e["HWDE_TOOLS_DIR"] = "/hive/tools"
    e["PYTHONPATH"] = f"{SKILL / 'scripts'}:{SKILL / 'scripts' / 'lib'}"
    e["PATH"] = "/usr/bin:/bin"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, env=e, timeout=120).stdout.split()
    assert out == [str(HIVE_KICAD / "bin" / "kicad-cli"),
                   str(HIVE_KICAD / "bin" / "python3.11"),
                   "/hive/tools/kicad-routing-tools/KiCadRoutingTools-0.19.0/plugins",
                   str(HIVE_KICAD / "share" / "kicad" / "symbols"),
                   "True"]


@hive
def test_hive_schlib_reads_a_symdir_library_on_demand():
    # Regulator_Linear isn't flattened up front, and AMS1117-3.3 extends
    # another symbol in it.
    r = run_skill("schlib.py", "--pins", "Regulator_Linear:AMS1117-3.3")
    assert r.returncode == 0, r.stdout + r.stderr
    assert '"pin_count": 3' in r.stdout


@hive
def test_hive_sheet_places_from_a_symdir_library_on_demand():
    # schlib's project-library lookup runs first and raises for a library it
    # can't find; a stock symdir library must be flattened before that.
    code = ("import schlib; sh = schlib.Sheet('t', paper='A4'); "
            "c = sh.add_component('Connector_Generic:Conn_01x03', 'J1', 'x', "
            "at=(50.8, 50.8)); print(len(c.pins))")
    e = {k: v for k, v in os.environ.items()
         if not k.startswith(("HWDE_", "KICAD")) and k != "APPDIR"}
    e["HWDE_TOOLS_DIR"] = "/hive/tools"
    e["PYTHONPATH"] = f"{SKILL / 'scripts'}:{SKILL / 'scripts' / 'lib'}"
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, env=e, timeout=300)
    assert r.returncode == 0, r.stderr[-500:]
    assert r.stdout.split()[-1] == "3"


# ------------------------------------------------------------ KRT rungs

def _three_krts(clean):
    pinned = clean / "kicad-routing-tools" / "KiCadRoutingTools-0.19.0" / "plugins"
    pinned.mkdir(parents=True)
    return pinned, side_krt(clean, "0.24.1"), side_krt(clean, "0.23.9", py_router=False)


def test_krt_rungs_off_is_one_version(clean, monkeypatch):
    pinned, _, _ = _three_krts(clean)
    assert env.find_krt_versions() == [pinned]
    for spec in ("", "off"):
        monkeypatch.setenv("HIVE_TOOL_KRT_RUNGS", spec)
        assert env.find_krt_versions() == [pinned]


def test_krt_rungs_all_is_the_default_then_newest_first(clean, monkeypatch):
    pinned, v24, v23 = _three_krts(clean)
    monkeypatch.setenv("HIVE_TOOL_KRT_RUNGS", "all")
    assert env.find_krt_versions() == [pinned, v24, v23]
    monkeypatch.setenv("HIVE_TOOL_KRT", "0.24.1")   # the pick leads
    assert env.find_krt_versions() == [v24, v23, pinned]
    assert [env.krt_label(d) for d in env.find_krt_versions()] == \
        ["0.24.1", "0.23.9", "0.19.0"]


def test_krt_rungs_list_keeps_its_order_and_is_loud_on_a_missing_one(clean):
    pinned, v24, _ = _three_krts(clean)
    assert env.find_krt_versions("0.24.1, 0.19.0") == [v24, pinned]
    assert env.find_krt_versions("0.19.0,0.19.0") == [pinned]
    with pytest.raises(env.EnvError, match=r"0\.30\.0 is not installed.*0\.19\.0, 0\.23\.9, 0\.24\.1"):
        env.find_krt_versions("0.19.0,0.30.0")


def test_krt_rungs_skip_an_unvetted_side_version(clean):
    pinned = clean / "kicad-routing-tools" / "KiCadRoutingTools-0.19.0" / "plugins"
    pinned.mkdir(parents=True)
    (clean.parent / "versions" / "krt" / "0.24.1" / "KiCadRoutingTools-0.24.1"
     / "plugins").mkdir(parents=True)                     # no PROVENANCE.json
    assert env.find_krt_versions("all") == [pinned]


def test_krt_rungs_refuse_a_krt_dir_pin(clean, monkeypatch):
    _, v24, _ = _three_krts(clean)
    monkeypatch.setenv("HWDE_KRT_DIR", str(v24.parent.parent))
    assert env.find_krt_versions("off") == [v24]
    with pytest.raises(env.EnvError, match="HWDE_KRT_DIR pins one KRT"):
        env.find_krt_versions("all")


def test_krt_rungs_none_installed(clean):
    assert env.find_krt_versions() == []
    assert env.find_krt_versions("all") == []
