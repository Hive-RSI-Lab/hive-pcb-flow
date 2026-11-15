#!/usr/bin/env python
"""kc.py - thin, normalizing wrappers over kicad-cli (the hwde "firm bottom").

One place that knows how to drive kicad-cli and how to read its JSON. Every
gate and check consumes the NORMALIZED violation schema this module emits, so
the rest of the pipeline never re-parses kicad-cli's raw output.

Normalized violation schema (SPEC.md S2):

    {check, severity, pos, layer, net, refs, msg}

  check     kicad violation `type` (e.g. "track_width", "silk_over_copper")
  severity  "error" | "warning" | "exclusion"
  pos       [x, y] in mm, board/schematic space (the primary item's position)
  layer     e.g. "F.Cu", "In1.Cu", "F.Silkscreen" (parsed from item text) or null
  net       e.g. "+5V", "GND", "/USB_DP" (parsed from item text) or null
  refs      list of reference designators involved, e.g. ["D1"]
  msg       the human-readable rule description

Plus two traceability fields kept alongside the seven required keys:
  source    "erc" | "drc" | "unconnected" | "parity" (which report section)
  items     [{msg, pos}] for every kicad item in the violation (clearance
            violations name two items; checks/clusterers want both points)

kicad-cli does not expose layer/net/refdes as fields - they are embedded in the
item description strings ("Pad 1 [GND] of D1 on F.Cu"). We parse them
best-effort; `pos`, `check`, `severity`, `msg` are always exact.

Subcommands: erc, drc (-> normalized report, exit 1 if any violations);
gerbers, drill, pos, step, render, sch-pdf, netlist (-> export result, exit 2
on failure; render also fails when kicad-cli has no 3D model loaders, and lists
unresolved model paths in models_missing, rendering a relinked temp copy when
the workspace lib holds a moved model - see render_env/model_audit/model_relink). All:
JSON to stdout or --out FILE, exit 0/1/2, no interactivity (SPEC.md section 6). Tools resolve through lib/env.py (KiCad 10.0.3 pin).

Importable: gate.py and render.py call run_erc()/run_drc()/render_png() and the
export_* helpers directly; the pure parsers (parse_drc_data, normalize_violation)
take raw dicts so they can be unit-tested without the toolchain.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import checklib  # noqa: E402
from lib import env  # noqa: E402

# A kicad-cli run ends when it stops making progress, not at a fixed time
# (issue #414: core's DRC passed 300 s on a loaded machine while working).
# Progress is output on its pipes or CPU time gained by its process group
# (the AppImage's AppRun forks the real kicad-cli). A run with neither for
# STALL_S is wedged and is killed; HARD_S is the wall-clock backstop.
# HWDE_KC_STALL_S and HWDE_KC_TIMEOUT (the hard cap; 0 = none) override them.
STALL_S = 180.0
HARD_S = 3600.0
_TIMEOUT = None   # run_cli's default: HWDE_KC_TIMEOUT, else HARD_S
_POLL_S = 1.0

# The seven required keys, in schema order (used by tests and docs).
SCHEMA_KEYS = ("check", "severity", "pos", "layer", "net", "refs", "msg")

# ---------------------------------------------------------------- item parsing

# Layer token after " on ", e.g. "on F.Cu", "on In1.Cu", "on F.Silkscreen".
# Anchored on "on " so "length 7.5000 mm" cannot masquerade as a layer.
_LAYER_RE = re.compile(r"\bon ([A-Za-z][A-Za-z0-9]*\.[A-Za-z0-9_]+)")
# Net names are bracketed by kicad: "Track [+5V]", "Pad 1 [GND]".
_NET_RE = re.compile(r"\[([^\]]+)\]")
# Reference designators: 1-4 uppercase letters then digits (D1, U1, TP1, J2).
# Lowercase in layer tokens ("In1") cannot match, so layers are not mistaken
# for refdes.
# Optional single trailing letter covers split-part refdes (R2A/R2B, C1A -
# standard practice for paralleled/series pairs; S14: suffixed refs failed to
# extract, under-counting a violation's refs and mis-classifying cross-part
# silk as single-part).
_REF_RE = re.compile(r"\b([A-Z]{1,4}[0-9]+[A-Z]?)\b")


def _extract_layer(text: str) -> str | None:
    m = _LAYER_RE.search(text or "")
    return m.group(1) if m else None


def _extract_net(text: str) -> str | None:
    m = _NET_RE.search(text or "")
    return m.group(1) if m else None


def _extract_refs(text: str) -> list[str]:
    # Strip bracketed net / pin names first: "[PB0]", "[GND]" look like refdes
    # but are nets/pins, not reference designators.
    return _REF_RE.findall(_NET_RE.sub("", text or ""))


def normalize_violation(v: dict, source: str) -> dict:
    """One raw kicad violation object -> the normalized schema dict.

    Aggregates layer/net over the violation's items (first non-null wins) and
    unions refdes across them; `pos` is the first item's position.
    """
    layer: str | None = None
    net: str | None = None
    refs: list[str] = []
    norm_items: list[dict] = []
    for it in v.get("items") or []:
        desc = it.get("description", "") or ""
        pos = it.get("pos") or {}
        point = [pos["x"], pos["y"]] if "x" in pos and "y" in pos else None
        if layer is None:
            layer = _extract_layer(desc)
        if net is None:
            net = _extract_net(desc)
        refs.extend(_extract_refs(desc))
        norm_items.append({"msg": desc, "pos": point,
                           "uuid": it.get("uuid")})
    return {
        "check": v.get("type"),
        "severity": v.get("severity"),
        "pos": norm_items[0]["pos"] if norm_items else None,
        "layer": layer,
        "net": net,
        "refs": sorted(set(refs)),
        "msg": v.get("description"),
        "source": source,
        "items": norm_items,
    }


def parse_erc_data(data: dict) -> list[dict]:
    """kicad-cli `sch erc --format json` dict -> [normalized violation].

    ERC nests violations per sheet under sheets[].violations (DRC's are flat).
    """
    out = []
    for sheet in data.get("sheets", []):
        for v in sheet.get("violations", []):
            n = normalize_violation(v, "erc")
            n["sheet"] = sheet.get("uuid_path")
            out.append(n)
    return out


# DRC report section key -> normalized `source` label.
_DRC_SECTIONS = (
    ("violations", "drc"),
    ("unconnected_items", "unconnected"),
    ("schematic_parity", "parity"),
)


def parse_drc_data(data: dict) -> list[dict]:
    """kicad-cli `pcb drc --format json` dict -> [normalized violation].

    Merges the three parallel sections (violations, unconnected_items,
    schematic_parity) - they share one violation object shape.
    """
    out = []
    for key, source in _DRC_SECTIONS:
        for v in data.get(key, []):
            out.append(normalize_violation(v, source))
    return out


def summarize(violations: list[dict]) -> dict:
    """Counts by severity and by source, plus total."""
    counts = {"total": len(violations)}
    by_sev: dict[str, int] = {}
    by_src: dict[str, int] = {}
    for v in violations:
        by_sev[v["severity"]] = by_sev.get(v["severity"], 0) + 1
        by_src[v["source"]] = by_src.get(v["source"], 0) + 1
    counts["by_severity"] = by_sev
    counts["by_source"] = by_src
    return counts


# ---------------------------------------------------------------- cli plumbing

def resolve_cli() -> Path:
    cli = env.find_kicad_cli()
    if cli is None:
        raise RuntimeError(
            "kicad-cli not found (see check_env.py). The pipeline pins "
            "KiCad 10.0.3; set HWDE_KICAD_CLI to override.")
    return cli


def _env_s(name: str, default: float | None) -> float | None:
    """Seconds from the environment (0 or less = no limit), else default."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
    except ValueError:
        return default
    return v if v > 0 else None


def _group_cpu_ticks(pgid: int) -> int | None:
    """CPU ticks (user + system, own and reaped children) of every process in
    the group, from /proc; None where /proc can't say (not Linux)."""
    proc = Path("/proc")
    if not (proc / "self" / "stat").exists():
        return None
    total = 0
    try:
        entries = list(proc.iterdir())
    except OSError:
        return None
    for d in entries:
        if not d.name.isdigit():
            continue
        try:
            stat = (d / "stat").read_text()
        except OSError:
            continue
        # comm may hold spaces/parens: the fields start after the last ')'
        f = stat[stat.rfind(")") + 2:].split()
        try:
            if int(f[2]) != pgid:          # field 5: pgrp
                continue
            total += sum(int(x) for x in f[11:15])   # utime stime cutime cstime
        except (IndexError, ValueError):
            continue
    return total


def run_watched(cmd: list[str], *, stall_s: float | None, hard_s: float | None,
                env: dict | None = None, cwd: str | None = None
                ) -> subprocess.CompletedProcess:
    """subprocess.run(cmd, capture_output, text) that ends a run only when it
    stops making progress (no output and no CPU time for stall_s) or passes
    hard_s. On either, the whole process group is killed and
    subprocess.TimeoutExpired is raised (its `timeout` is the limit hit, and
    `stalled` says which)."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=env, cwd=cwd, start_new_session=True)
    out: list[bytes] = []
    err: list[bytes] = []
    last = [time.monotonic()]

    def pump(stream, sink):
        # os.read returns what is there: a buffered read would wait for a
        # full block and see a run that prints slowly as silent
        fd = stream.fileno()
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            sink.append(chunk)
            last[0] = time.monotonic()

    pumps = [threading.Thread(target=pump, args=(proc.stdout, out), daemon=True),
             threading.Thread(target=pump, args=(proc.stderr, err), daemon=True)]
    for t in pumps:
        t.start()
    start = time.monotonic()
    ticks = _group_cpu_ticks(proc.pid)
    hit = None
    while True:
        try:
            proc.wait(timeout=_POLL_S)
            break
        except subprocess.TimeoutExpired:
            pass
        now = time.monotonic()
        t = _group_cpu_ticks(proc.pid)
        if t is not None and ticks is not None and t > ticks:
            last[0] = now
        if t is not None:
            ticks = t
        if hard_s is not None and now - start > hard_s:
            hit = ("hard", hard_s)
        elif stall_s is not None and now - last[0] > stall_s:
            hit = ("stall", stall_s)
        if hit:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                proc.kill()
            proc.wait()
            break
    for t in pumps:
        t.join(5)
    proc.stdout.close()
    proc.stderr.close()

    def text(chunks: list[bytes]) -> str:
        return b"".join(chunks).decode("utf-8", "replace").replace("\r\n", "\n")

    if hit:
        exc = subprocess.TimeoutExpired(cmd, hit[1], text(out), text(err))
        exc.stalled = hit[0] == "stall"
        raise exc
    return subprocess.CompletedProcess(cmd, proc.returncode, text(out), text(err))


def run_cli(cli: Path, args: list[str], timeout: float | None = _TIMEOUT,
            env: dict | None = None) -> subprocess.CompletedProcess:
    """Run kicad-cli. It is killed when it makes no progress for STALL_S
    (HWDE_KC_STALL_S), or past its hard cap: HWDE_KC_TIMEOUT when set, else
    `timeout` when a caller gives one, else HARD_S."""
    hard = _env_s("HWDE_KC_TIMEOUT", timeout if timeout is not None else HARD_S)
    return run_watched([str(cli), *args], stall_s=_env_s("HWDE_KC_STALL_S", STALL_S),
                       hard_s=hard, env=env)


def _run_json_report(cli: Path, section_args: list[str],
                     input_file: Path) -> dict:
    """Run a kicad-cli report command that writes JSON to a temp file; return
    the parsed dict. The temp file is always cleaned up."""
    with tempfile.TemporaryDirectory(prefix="aiee_kc_") as td:
        rep = Path(td) / "report.json"
        cp = run_cli(cli, [*section_args, "--format", "json",
                           "--severity-all", "-o", str(rep), str(input_file)])
        if not rep.exists():
            raise RuntimeError(
                f"kicad-cli produced no report (rc={cp.returncode}): "
                f"{(cp.stderr or cp.stdout).strip()[:400]}")
        return json.loads(rep.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- reports

def run_erc(cli: Path, sch: Path) -> dict:
    data = _run_json_report(cli, ["sch", "erc"], sch)
    violations = parse_erc_data(data)
    return checklib.stamp({
        "script": "kc", "tool": "erc", "input": str(sch),
        "units": data.get("coordinate_units", "mm"),
        "kicad_version": data.get("kicad_version"),
        "counts": summarize(violations),
        "status": "pass" if not violations else "violations",
        "violations": violations,
    }, sch)


def dru_ignored_row(pcb: Path) -> dict | None:
    """A DRC error row for a board whose .kicad_dru KiCad drops whole: one
    value its rule parser rejects (0.5MM, 20mils, a bare 0.2 on a length)
    is a parse error, and DRC then checks none of the file's rules while
    kicad-cli reports nothing (#1273). None when the file is absent or
    every value in it reads."""
    dru = Path(pcb).with_suffix(".kicad_dru")
    try:
        text = dru.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    lib = str(Path(__file__).resolve().parent / "lib")
    if lib not in sys.path:
        sys.path.insert(0, lib)
    import routelib  # noqa: PLC0415 - lazy: only a board with a DRU pays
    errs = routelib.dru_unit_errors(text)
    if not errs:
        return None
    more = f" (and {len(errs) - 1} more)" if len(errs) > 1 else ""
    return {"check": "rules_file_ignored", "severity": "error", "pos": None,
            "layer": None, "net": None, "refs": [],
            "msg": (f"KiCad ignores every rule in {dru.name}, and DRC checks "
                    f"none of them: {errs[0]}{more}"),
            "source": "drc",
            "items": [{"msg": e, "pos": None, "uuid": None} for e in errs]}


def run_drc(cli: Path, pcb: Path, *, parity: bool = False,
            all_track_errors: bool = False, refill: bool = False,
            save_board: bool = False) -> dict:
    args = ["pcb", "drc"]
    if parity:
        args.append("--schematic-parity")
    if all_track_errors:
        args.append("--all-track-errors")
    if refill:
        args.append("--refill-zones")
        if save_board:
            args.append("--save-board")
    data = _run_json_report(cli, args, pcb)
    violations = parse_drc_data(data)
    ignored = dru_ignored_row(pcb)
    if ignored:
        violations.insert(0, ignored)
    return checklib.stamp({
        "script": "kc", "tool": "drc", "input": str(pcb),
        "units": data.get("coordinate_units", "mm"),
        "kicad_version": data.get("kicad_version"),
        "options": {"parity": parity, "all_track_errors": all_track_errors,
                    "refill": refill, "save_board": save_board},
        "counts": summarize(violations),
        "status": "pass" if not violations else "violations",
        "violations": violations,
    }, pcb)


# ---------------------------------------------------------------- exports

def _export_result(tool: str, input_file: Path, cp: subprocess.CompletedProcess,
                   outputs: list[Path]) -> dict:
    existing = [str(p) for p in outputs if p.exists()]
    ok = cp.returncode == 0 and bool(existing)
    return {
        "script": "kc", "tool": tool, "input": str(input_file),
        "status": "pass" if ok else "error",
        "outputs": existing,
        "returncode": cp.returncode,
        "stderr_tail": (cp.stderr or "").strip()[-400:] if not ok else "",
    }


def export_gerbers(cli: Path, pcb: Path, out_dir: Path,
                   layers: str | None = None, extra: list[str] | None = None) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    before = {p.name for p in out_dir.iterdir()}
    args = ["pcb", "export", "gerbers", "-o", str(out_dir)]
    if layers:
        args += ["--layers", layers]
    args += (extra or [])
    args.append(str(pcb))
    cp = run_cli(cli, args)
    produced = [out_dir / p.name for p in out_dir.iterdir()
                if p.name not in before]
    res = _export_result("gerbers", pcb, cp, produced or list(out_dir.iterdir()))
    res["output_dir"] = str(out_dir)
    return res


def export_drill(cli: Path, pcb: Path, out_dir: Path,
                 fmt: str = "excellon", extra: list[str] | None = None) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    before = {p.name for p in out_dir.iterdir()}
    args = ["pcb", "export", "drill", "-o", str(out_dir) + "/",
            "--format", fmt]
    args += (extra or [])
    args.append(str(pcb))
    cp = run_cli(cli, args)
    produced = [out_dir / p.name for p in out_dir.iterdir()
                if p.name not in before]
    res = _export_result("drill", pcb, cp, produced or list(out_dir.iterdir()))
    res["output_dir"] = str(out_dir)
    return res


def export_pos(cli: Path, pcb: Path, out_file: Path, side: str = "both",
               fmt: str = "csv", units: str = "mm") -> dict:
    # kicad-cli defaults pos units to INCHES (LEARNINGS [kicad-cli]); always
    # force units so downstream CPL geometry is mm.
    out_file.parent.mkdir(parents=True, exist_ok=True)
    args = ["pcb", "export", "pos", "-o", str(out_file), "--side", side,
            "--format", fmt]
    if fmt in ("ascii", "csv"):
        args += ["--units", units]
    args.append(str(pcb))
    cp = run_cli(cli, args)
    return _export_result("pos", pcb, cp, [out_file])


def export_ipcd356(cli: Path, pcb: Path, out_file: Path) -> dict:
    """IPC-D-356 netlist (the bare-board tester's netlist, lib/ipcd356.py)."""
    out_file.parent.mkdir(parents=True, exist_ok=True)
    cp = run_cli(cli, ["pcb", "export", "ipcd356", "-o", str(out_file),
                       str(pcb)])
    return _export_result("ipcd356", pcb, cp, [out_file])


def export_step(cli: Path, pcb: Path, out_file: Path, force: bool = True) -> dict:
    out_file.parent.mkdir(parents=True, exist_ok=True)
    args = ["pcb", "export", "step", "-o", str(out_file)]
    if force:
        args.append("--force")
    args.append(str(pcb))
    cp = run_cli(cli, args)
    return _export_result("step", pcb, cp, [out_file])


def export_sch_pdf(cli: Path, sch: Path, out_file: Path) -> dict:
    out_file.parent.mkdir(parents=True, exist_ok=True)
    args = ["sch", "export", "pdf", "-o", str(out_file), str(sch)]
    cp = run_cli(cli, args)
    return _export_result("sch-pdf", sch, cp, [out_file])


def export_netlist(cli: Path, sch: Path, out_file: Path,
                   fmt: str = "kicadsexpr") -> dict:
    out_file.parent.mkdir(parents=True, exist_ok=True)
    args = ["sch", "export", "netlist", "-o", str(out_file),
            "--format", fmt, str(sch)]
    cp = run_cli(cli, args)
    res = _export_result("netlist", sch, cp, [out_file])
    if res["status"] == "pass":
        # kicad-cli opens a KiCad 4/5 .sch, exits 0 and writes an empty
        # netlist: say so, or every check downstream passes on nothing (#1023)
        from lib import legacy_net  # noqa: PLC0415
        n = legacy_net.empty_export(sch, out_file)
        if n:
            res["status"] = "error"
            res["error"] = (f"kicad-cli wrote a netlist with 0 components from "
                            f"{Path(sch).name}, which has {n}"
                            + (" (a KiCad 4/5 legacy schematic: read it with "
                               "lib/legacy_net)" if legacy_net.is_legacy(sch)
                               else ""))
    return res


# ---------------------------------------------------------------- render

# `pcb render --side` has no "iso"; isometric is orthographic rotate -45,0,45.
ISO_ROTATE = "-45,0,45"

# kicad-cli parses .wrl/.step models through loader plugins (libs3d_plugin_*)
# that it looks for ONLY at its compiled install path. Without them it opens
# every model, parses none, renders a bare board and still exits 0.
# A KiCad AppImage AppDir (kicad-cli at <appdir>/bin) matches the first glob
# through its usr/lib/<triplet>/kicad/plugins -> shared/lib/... link.
_SYS_PLUGIN_GLOBS = ("usr/lib/*/kicad/plugins/3d", "usr/lib*/kicad/plugins/3d",
                     "usr/local/lib/*/kicad/plugins/3d",
                     "usr/local/lib*/kicad/plugins/3d")
_SYS_ROOT = Path("/")


def _plugin_dirs(root: Path) -> list[Path]:
    return [d for g in _SYS_PLUGIN_GLOBS for d in root.glob(g)
            if any(d.glob("libs3d_plugin_*"))]


def render_env(cli: Path) -> tuple[dict | None, str]:
    """Environment under which `pcb render` finds its 3D model loaders.

    Returns (env, error): env None = inherit ours. A KiCad unpacked into a
    user prefix (debs extracted without root) keeps its loaders under
    <prefix>/usr/lib/.../kicad/plugins/3d, which kicad-cli only searches when
    APPDIR=<prefix> (its AppImage path) - so set it. error is non-empty when
    no loaders exist anywhere: the render would show a bare board."""
    if sys.platform != "linux":
        return None, ""  # Windows/macOS load plugins beside the executable
    appdir = os.environ.get("APPDIR")
    if appdir and _plugin_dirs(Path(appdir)):
        return None, ""
    if _plugin_dirs(_SYS_ROOT):
        return None, ""
    for anc in [*Path(cli).absolute().parents, *Path(cli).resolve().parents]:
        if anc != anc.parent and _plugin_dirs(anc):
            return dict(os.environ, APPDIR=str(anc)), ""
    return None, (f"no KiCad 3D model loaders (libs3d_plugin_*) under any "
                  f"{'|'.join(_SYS_PLUGIN_GLOBS)} for {cli} - a render would "
                  f"show a bare board with no parts")


_MODEL_RE = re.compile(r'\(model\s+"((?:[^"\\]|\\.)*)"')
_VAR_RE = re.compile(r"\$\{([A-Za-z0-9_]+)\}")


def _model_resolver(pcb: Path, env_vars: dict | None):
    """model path as written -> the file KiCad would open. Expands ${VAR}
    from the environment (KIPRJMOD = the board's dir; an unset
    KICAD*_3DMODEL_DIR falls back to the stock /usr/share/kicad/3dmodels) and
    reads a relative path against the board's dir, as KiCad does."""
    envv = dict(os.environ if env_vars is None else env_vars)
    envv.setdefault("KIPRJMOD", str(pcb.resolve().parent))

    def expand(m: re.Match) -> str:
        name = m.group(1)
        if name in envv:
            return envv[name]
        if re.fullmatch(r"KICAD\d*_3DMODEL_DIR", name):
            return "/usr/share/kicad/3dmodels"
        return m.group(0)

    def resolve(p: str) -> Path:
        q = Path(_VAR_RE.sub(expand, p))
        return q if q.is_absolute() else pcb.resolve().parent / q
    return resolve


def model_audit(pcb: Path, env_vars: dict | None = None) -> dict:
    """List the board's 3D model paths that do not resolve to a file."""
    resolve = _model_resolver(pcb, env_vars)
    text = pcb.read_text(encoding="utf-8", errors="replace")
    paths = [m.group(1) for m in _MODEL_RE.finditer(text)]
    missing = [p for p in sorted(set(paths)) if not resolve(p).is_file()]
    return {"referenced": len(paths), "missing": missing}


def model_libs(base: Path) -> list[Path]:
    """The workspace's own model dirs: lib/*.3dshapes beside the board dir
    `base`, or one level up as in boards/<name>/lib."""
    base = Path(base).resolve()
    return [d for root in (base, base.parent)
            for d in sorted((root / "lib").glob("*.3dshapes"))]


def model_lib_file(path: str, libs: list[Path]) -> Path | None:
    """The file named like model `path`'s last component in the first of
    `libs` that has it, else None. The lookup by name that render's relink,
    check_model_paths and its --fix share."""
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    return next((d / name for d in libs if (d / name).is_file()), None)


_STOCK_MODEL_RE = re.compile(r"\$\{KICAD\d*_3DMODEL_DIR\}/")
_KIPRJMOD = "${KIPRJMOD}/"


EMBED_PREFIX = "kicad-embed://"


def model_path_kind(path: str, project: Path, ws: Path) -> str | None:
    """Why a model path is not portable, or None when it is. Portable means
    KiCad finds the same file on any checkout: the stock library through
    ${KICAD<n>_3DMODEL_DIR}, or a ${KIPRJMOD}/ path (project = the board's
    dir) that stays inside workspace `ws`.
      absolute            /home/.., C:/.., \\\\host\\..: dies with that checkout
      outside_workspace   ${KIPRJMOD}/ climbing out of the workspace
      unanchored          a bare relative path or any other ${VAR}
    kicad-embed://<name> (KiCad 9+ embedded files, what the Altium importer
    writes) is portable: the model is stored in the board or footprint."""
    if _STOCK_MODEL_RE.match(path):
        return None
    if path.startswith(EMBED_PREFIX):  # the file travels inside the board/footprint
        return None
    if path.startswith(_KIPRJMOD):
        target = os.path.normpath(Path(project).resolve() / path[len(_KIPRJMOD):])
        root = str(Path(ws).resolve())
        inside = target == root or target.startswith(root.rstrip(os.sep) + os.sep)
        return None if inside else "outside_workspace"
    if re.match(r"^([A-Za-z]:)?[\\/]", path):
        return "absolute"
    return "unanchored"


def portable_model_path(file: Path, project: Path) -> str:
    """${KIPRJMOD}/-relative path of `file` for a board in dir `project`
    (boards/<name>/lib beside boards/<name>/kicad -> ${KIPRJMOD}/../lib/..)."""
    rel = os.path.relpath(Path(file).resolve(), Path(project).resolve())
    return _KIPRJMOD + Path(rel).as_posix()


def model_paths_portable(text: str, project: Path, ws: Path,
                         libs: list[Path]) -> tuple[str, dict[str, str]]:
    """Rewrite every non-portable (model "...") path in board or footprint
    text whose file, by name, is in `libs` to its ${KIPRJMOD}/ form.
    Returns (text, {old: new}); a path with no file in `libs` is left as
    written, for check_model_paths to report."""
    new_of: dict[str, str] = {}
    for m in _MODEL_RE.finditer(text):
        p = m.group(1)
        if p in new_of or model_path_kind(p, project, ws) is None:
            continue
        hit = model_lib_file(p, libs)
        if hit:
            new_of[p] = portable_model_path(hit, project)
    if not new_of:
        return text, {}

    def sub(m: re.Match) -> str:
        p = m.group(1)
        if p not in new_of:
            return m.group(0)
        return m.group(0).replace(f'"{p}"', f'"{new_of[p]}"', 1)
    return _MODEL_RE.sub(sub, text), new_of


def model_relink(pcb: Path, env_vars: dict | None = None
                 ) -> tuple[str | None, dict[str, str]]:
    """Board text to render when a model path no longer resolves but the
    workspace's own library has the file, by name, under lib/*.3dshapes
    (beside the board, or one level up as in boards/<name>/lib). A board
    whose paths point into another checkout loses its parts once that
    checkout is gone; the copy points them back at the board's own models.

    Returns (text, relinked): text None = render the board as it is. In the
    copy every model path is absolute, so it renders the same from any dir.
    The board file itself is never written."""
    resolve = _model_resolver(pcb, env_vars)
    text = pcb.read_text(encoding="utf-8", errors="replace")
    libs = model_libs(pcb.resolve().parent)
    relinked: dict[str, str] = {}
    for m in _MODEL_RE.finditer(text):
        p = m.group(1)
        if p in relinked or resolve(p).is_file():
            continue
        hit = model_lib_file(p, libs)
        if hit:
            relinked[p] = str(hit)
    if not relinked:
        return None, {}

    def sub(m: re.Match) -> str:
        p = m.group(1)
        new = relinked.get(p) or os.path.normpath(resolve(p))
        if p not in relinked and not resolve(p).is_file():
            return m.group(0)  # still missing: leave it as written
        new = new.replace("\\", "\\\\").replace('"', '\\"')
        return m.group(0).replace(f'"{p}"', f'"{new}"', 1)
    return _MODEL_RE.sub(sub, text), relinked


def render_png(cli: Path, pcb: Path, out_file: Path, *, side: str = "top",
               width: int = 1600, height: int = 900, quality: str = "high",
               rotate: str | None = None, pivot: str | None = None,
               zoom: float | None = None) -> dict:
    """pivot: 'X,Y,Z' cm from the board centre (y up) the camera turns
    about and centres on; zoom: kicad-cli's camera zoom (1 = whole board)."""
    out_file.parent.mkdir(parents=True, exist_ok=True)
    renv, err = render_env(cli)
    if err:
        return {"script": "kc", "tool": "render", "input": str(pcb),
                "status": "error", "outputs": [], "returncode": None,
                "stderr_tail": err, "view": side if not rotate else "iso"}
    # -h collides with --help; use long form --height (LEARNINGS [kicad-cli]).
    args = ["pcb", "render", "-o", str(out_file), "--side", side,
            "--width", str(width), "--height", str(height),
            "--quality", quality]
    if rotate:
        args += ["--rotate", rotate]
    if pivot:
        args += ["--pivot", pivot]
    if zoom:
        args += ["--zoom", f"{zoom:g}"]
    # models moved since the board was saved: render a relinked copy (in a
    # temp dir beside the output), never the board file itself
    text, relinked = model_relink(pcb, renv)
    with tempfile.TemporaryDirectory(dir=out_file.parent,
                                     prefix="_tmp_render_") as td:
        src = pcb
        if text is not None:
            src = Path(td) / pcb.name
            src.write_text(text, encoding="utf-8")
        args.append(str(src))
        cp = run_cli(cli, args, env=renv)
    res = _export_result("render", pcb, cp, [out_file])
    res["view"] = side if not rotate else "iso"
    # a part whose model file is missing renders as bare pads - say which
    res["models_missing"] = [m for m in model_audit(pcb, renv)["missing"]
                             if m not in relinked]
    res["models_relinked"] = sorted(relinked)
    return res


# ---------------------------------------------------------------- cli

def _emit(report: dict, out: str | None) -> None:
    text = json.dumps(report, indent=2)
    if out:
        p = Path(out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    else:
        print(text)


def _report_exit(report: dict) -> int:
    # pass -> 0, violations present -> 1, error -> 2
    return 0 if report.get("status") == "pass" else 1


def _export_exit(report: dict) -> int:
    return 0 if report.get("status") == "pass" else 2


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_common(p, out_help="write JSON report here instead of stdout"):
        p.add_argument("input", help="input .kicad_sch or .kicad_pcb")
        p.add_argument("--out", help=out_help)

    p_erc = sub.add_parser("erc", help="ERC -> normalized violations")
    add_common(p_erc)

    p_drc = sub.add_parser("drc", help="DRC -> normalized violations")
    add_common(p_drc)
    p_drc.add_argument("--parity", action="store_true",
                       help="test schematic/PCB parity")
    p_drc.add_argument("--all-track-errors", action="store_true")
    p_drc.add_argument("--refill", action="store_true",
                       help="refill zones before DRC (in memory)")
    p_drc.add_argument("--save-board", action="store_true",
                       help="persist refilled zones (mutates the board; "
                            "requires --refill)")

    p_ger = sub.add_parser("gerbers", help="export gerbers to a directory")
    p_ger.add_argument("input")
    p_ger.add_argument("--out", required=True, help="output directory")
    p_ger.add_argument("--layers", help="comma list e.g. F.Cu,B.Cu,Edge.Cuts")

    p_dri = sub.add_parser("drill", help="export drill files to a directory")
    p_dri.add_argument("input")
    p_dri.add_argument("--out", required=True, help="output directory")
    p_dri.add_argument("--format", default="excellon",
                       choices=["excellon", "gerber"])

    p_pos = sub.add_parser("pos", help="export placement (pos) file")
    p_pos.add_argument("input")
    p_pos.add_argument("--out", required=True, help="output file")
    p_pos.add_argument("--side", default="both",
                       choices=["front", "back", "both"])
    p_pos.add_argument("--format", default="csv",
                       choices=["ascii", "csv", "gerber"])
    p_pos.add_argument("--units", default="mm", choices=["mm", "in"])

    p_step = sub.add_parser("step", help="export STEP 3D model")
    p_step.add_argument("input")
    p_step.add_argument("--out", required=True, help="output file")

    p_ren = sub.add_parser("render", help="render one 3D view to PNG")
    p_ren.add_argument("input")
    p_ren.add_argument("--out", required=True, help="output PNG")
    p_ren.add_argument("--side", default="top",
                       choices=["top", "bottom", "left", "right",
                                "front", "back"])
    p_ren.add_argument("--iso", action="store_true",
                       help="isometric (rotate -45,0,45)")
    p_ren.add_argument("--width", type=int, default=1600)
    p_ren.add_argument("--height", type=int, default=900)
    p_ren.add_argument("--quality", default="high",
                       choices=["basic", "high", "user", "job_settings"])

    p_pdf = sub.add_parser("sch-pdf", help="export schematic to PDF")
    p_pdf.add_argument("input")
    p_pdf.add_argument("--out", required=True, help="output PDF")

    p_net = sub.add_parser("netlist", help="export netlist")
    p_net.add_argument("input")
    p_net.add_argument("--out", required=True, help="output file")
    p_net.add_argument("--format", default="kicadsexpr")

    args = ap.parse_args(argv)

    try:
        cli = resolve_cli()
        inp = Path(args.input)
        if args.cmd == "erc":
            r = run_erc(cli, inp)
            _emit(r, args.out)
            return _report_exit(r)
        if args.cmd == "drc":
            if args.save_board and not args.refill:
                ap.error("--save-board requires --refill")
            r = run_drc(cli, inp, parity=args.parity,
                        all_track_errors=args.all_track_errors,
                        refill=args.refill, save_board=args.save_board)
            _emit(r, args.out)
            return _report_exit(r)
        if args.cmd == "gerbers":
            r = export_gerbers(cli, inp, Path(args.out), layers=args.layers)
        elif args.cmd == "drill":
            r = export_drill(cli, inp, Path(args.out), fmt=args.format)
        elif args.cmd == "pos":
            r = export_pos(cli, inp, Path(args.out), side=args.side,
                           fmt=args.format, units=args.units)
        elif args.cmd == "step":
            r = export_step(cli, inp, Path(args.out))
        elif args.cmd == "render":
            r = render_png(cli, inp, Path(args.out), side=args.side,
                           width=args.width, height=args.height,
                           quality=args.quality,
                           rotate=ISO_ROTATE if args.iso else None)
        elif args.cmd == "sch-pdf":
            r = export_sch_pdf(cli, inp, Path(args.out))
        elif args.cmd == "netlist":
            r = export_netlist(cli, inp, Path(args.out), fmt=args.format)
        else:  # pragma: no cover - argparse guarantees a known cmd
            ap.error(f"unknown command {args.cmd}")
        # export path: --out is a destination, so echo the result to stdout too
        print(json.dumps(r, indent=2))
        return _export_exit(r)
    except Exception:
        print(json.dumps({"script": "kc", "status": "error",
                          "error": traceback.format_exc()}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
