"""simlib.py - shared plumbing for the SPICE sim gate (sim_run.py).

Three concerns, all deterministic and engine-agnostic except run_circuit():

1. Fragment synthesis: kicadsexpr netlist -> SPICE element lines + a recorded
   net rename map, so the sim-analyst agent composes testbenches around the
   pipeline's own exported .net files. (KiCad's `sch export netlist --format
   spice` is unusable here: any symbol without a sim model emits `REF __REF`
   with ZERO nodes - topology destroyed. Verified on this host.) This parser
   duplicates netlist_audit.parse_netlist on purpose: lib modules must not
   import scripts, and the audit parser drops `pinfunction`, which is what
   maps diode A/K and BJT B/C/E pins here.

2. Bounds: load/validate `<bench>.bounds.json` sidecars and compare measured
   values against them, emitting the S2 normalized violation schema
   (checklib.violation) with kinds sim_bound_fail / sim_measure_missing.

3. Engine: run one circuit string through the shared-library ngspice that
   KiCad bundles, via InSpice. Machine-verified recipe traps (all reproduced
   on this host, ngspice v46 / InSpice 1.7.0.5):
     - NGSPICE_LIBRARY_PATH must be the BARE file name ("ngspice.dll") with
       the DLL's directory prepended to PATH: InSpice's find_library splits
       the name at the FIRST "." so a full path containing "KiCad/10.0"
       truncates to garbage.
     - Off Windows that name search is ctypes.util.find_library, which only
       sees the linker cache: run_circuit points InSpice's find_library at
       the pinned library itself (a user-space KiCad is never in the cache).
     - SPICE_LIB_DIR must be SET (any directory) or _load_library crashes on
       Path(None); the resulting "can't find spinit" warning is benign.
     - The circuit string MUST end with ".end" or ngspice silently reports
       "no circuits loaded" (prepare_circuit appends it).
     - `.options rshunt=1e9` is always injected: one floating node otherwise
       poisons a whole .ac solve via a singular matrix while DC stays fine.
     - .measure is valid ONLY for tran/dc/ac (NOT op). Results are NOT
       vectors: they are parsed from the captured ngspice stdout lines
       "name = value" under a "Measurements for ..." header. A measure whose
       condition is never met prints "...failed!" on stderr AND makes
       InSpice's run() raise - run_circuit() catches that and still returns
       the measures that did succeed.
     - InSpice registers a no-op ControlledExit callback and keeps refs to
       every callback thunk on the NgSpiceShared instance, which its class
       cache keeps alive; _NG holds an extra module ref for belt&braces.
   Hard timeouts and crash isolation are the CALLER's job: sim_run.py runs
   run_circuit() in a worker subprocess it can kill.
"""
from __future__ import annotations

import math
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sexpdata  # noqa: E402

import checklib  # noqa: E402

SOURCE = "sim.ngspice"
CHECK = "sim"
SEVERITIES = ("error", "warning")

# ------------------------------------------------------------ netlist parse


def _sym(x):
    return x.value() if isinstance(x, sexpdata.Symbol) else x


def _kids(node, tag):
    return [k for k in node
            if isinstance(k, list) and k and _sym(k[0]) == tag]


def _atom(node, tag, default=None):
    for k in _kids(node, tag):
        if len(k) > 1:
            return str(_sym(k[1]))
    return default


def _comp_fields(comp) -> dict[str, str]:
    """{name: value} from a comp's (fields (field (name N) "v")) and its
    (property (name N) (value v)) entries; empty values left out."""
    out: dict[str, str] = {}
    for blk in _kids(comp, "fields"):
        for f in _kids(blk, "field"):
            name = _atom(f, "name")
            val = next((str(x) for x in f[1:] if isinstance(x, str)), "")
            if name and val.strip():
                out.setdefault(name, val.strip())
    for pr in _kids(comp, "property"):
        name, val = _atom(pr, "name"), _atom(pr, "value", "")
        if name and val.strip():
            out.setdefault(name, val.strip())
    return out


def parse_netlist(path: Path | str, fields: bool = False) -> dict:
    """kicadsexpr netlist -> {"components": {ref: {value, footprint}},
    "nets": {name: [{ref, pin, pintype, pinfunction}]}}. With fields=True
    each component also carries `fields`: its non-empty user fields and
    properties ({"MPN": ...}). The default shape is the golden contract fwe
    copies (tests/golden/netlist), so it stays as it is.

    Hierarchical exports drop `unconnected-*` singleton nets entirely, so
    nothing downstream may rely on their presence (synth_fragment emits
    nc_* placeholder nodes for pins with no net)."""
    p = Path(path)
    try:
        with open(p, encoding="utf-8") as fh:
            data = sexpdata.load(fh)
    except OSError as exc:
        raise checklib.CheckError(f"cannot read netlist {p}: {exc}") from exc
    except Exception as exc:
        raise checklib.CheckError(f"netlist {p} does not parse: {exc}") from exc
    if not isinstance(data, list) or not data or str(_sym(data[0])) != "export":
        raise checklib.CheckError(f"netlist {p}: not a kicadsexpr export")
    comps: dict[str, dict] = {}
    for blk in _kids(data, "components"):
        for c in _kids(blk, "comp"):
            ref = _atom(c, "ref")
            if ref:
                comps[ref] = {"value": _atom(c, "value"),
                              "footprint": _atom(c, "footprint")}
                if fields:
                    comps[ref]["fields"] = _comp_fields(c)
    nets: dict[str, list] = {}
    for blk in _kids(data, "nets"):
        for n in _kids(blk, "net"):
            name = _atom(n, "name")
            if name is None:
                continue
            nets[name] = [{"ref": _atom(nd, "ref"), "pin": _atom(nd, "pin"),
                           "pintype": _atom(nd, "pintype", ""),
                           "pinfunction": _atom(nd, "pinfunction", "")}
                          for nd in _kids(n, "node")]
    return {"components": comps, "nets": nets}


# ------------------------------------------------------------- rename map

_UNCONNECTED = "unconnected-"


def spice_net_name(name: str) -> str:
    """One KiCad net name -> a SPICE-safe candidate (no collision handling).

    Deterministic rules (recorded in the fragment payload so testbenches and
    reviews can be traced back to schematic nets):
      "GND"          -> "0"      (the SPICE ground node)
      "+3V3"         -> "P3V3"   (leading "+" is the SPICE continuation char)
      "/LED"         -> "N_LED"  (local labels; further "/" -> "_")
      "unconnected-(D2-Pad2)" -> "NC_D2_PAD2"
      any residual non [A-Za-z0-9_] -> "_"; a leading digit gets an "N".
    """
    if name in ("0", "GND"):
        return "0"
    if name.startswith(_UNCONNECTED):
        base = "NC_" + name[len(_UNCONNECTED):].strip("()").replace("Pad", "PAD")
    elif name.startswith("+"):
        base = "P" + name[1:]
    elif name.startswith("/"):
        base = "N_" + name[1:]
    else:
        base = name
    out = re.sub(r"[^A-Za-z0-9_]", "_", base).strip("_") or "NET"
    if out[0].isdigit():
        out = "N" + out
    return out.upper()


def rename_map(names) -> dict[str, str]:
    """Deterministic collision-free map {kicad_net: spice_node} for a set of
    net names (processed sorted; a collision appends _2, _3, ...)."""
    out: dict[str, str] = {}
    taken: set[str] = set()
    for name in sorted(names):
        cand = spice_net_name(name)
        if cand != "0":
            n, base = 2, cand
            while cand in taken:
                cand = f"{base}_{n}"
                n += 1
            taken.add(cand)
        out[name] = cand
    return out


# ------------------------------------------------------- fragment synthesis

_NUM_RE = re.compile(r"^\d+(\.\d+)?([eE][-+]?\d+)?[a-zA-Z]*$")


def _value_token(value: str | None) -> str | None:
    """First token of a KiCad value field if it reads as a SPICE number.
    KiCad's "1M" means 1 megohm but SPICE "m" is milli, so a trailing
    uppercase M (not "meg"/"MEG") is rewritten to "meg"."""
    if not value:
        return None
    tok = value.split()[0]
    if not _NUM_RE.match(tok):
        return None
    if tok.endswith("M") and not tok.upper().endswith("MEG"):
        tok = tok[:-1] + "meg"
    return tok


def _ref_pins(parsed: dict) -> dict[str, dict[str, dict]]:
    """{ref: {pin: {net, pinfunction, pintype}}} from the nets table."""
    out: dict[str, dict[str, dict]] = {}
    for net, nodes in parsed["nets"].items():
        for nd in nodes:
            out.setdefault(nd["ref"], {})[nd["pin"]] = {
                "net": net, "pinfunction": nd.get("pinfunction", ""),
                "pintype": nd.get("pintype", "")}
    return out


def _pin_by_role(pins: dict[str, dict], prefix: str) -> list[str]:
    return sorted(p for p, meta in pins.items()
                  if meta["pinfunction"].upper().startswith(prefix))


def synth_fragment(parsed: dict, refs=None, nets=None) -> dict:
    """SPICE element lines for a ref/net selection of a parsed netlist.

    Selection: explicit refs, plus every ref touching any of `nets`. Emits
    what maps onto SPICE primitives unambiguously (R/C/L 2-pin, D via A/K
    pinfunctions with the KiCad pin1=K/pin2=A fallback, 3-pin Q via B/C/E
    pinfunctions). Anything else (ICs, connectors, multi-unit transistors -
    a BC847BS dual does NOT pin-sort into units) lands in `unresolved` with
    its full pin/net table so the sim-analyst wires it explicitly. Model
    cards are emitted as named placeholders in `models`; the analyst fills
    in datasheet-derived generic parameters (never vendored vendor files).

    Pins with no net entry (hierarchical exports drop unconnected-* nets)
    get deterministic nc_<ref>_<pin> floating nodes."""
    comps = parsed["components"]
    by_ref = _ref_pins(parsed)
    selected = set(refs or [])
    for net in nets or []:
        if net not in parsed["nets"]:
            raise checklib.CheckError(f"net {net!r} not in netlist")
        selected.update(nd["ref"] for nd in parsed["nets"][net])
    missing = sorted(r for r in selected if r not in comps)
    if missing:
        raise checklib.CheckError(f"refs not in netlist: {', '.join(missing)}")
    if not selected:
        raise checklib.CheckError("empty selection: give --refs and/or --nets")

    used_nets: set[str] = set()
    for ref in selected:
        used_nets.update(m["net"] for m in by_ref.get(ref, {}).values())
    names = rename_map(used_nets)

    elements: list[str] = []
    models: list[dict] = []
    unresolved: list[dict] = []
    floating: list[str] = []

    def node(ref: str, pin: str) -> str:
        meta = by_ref.get(ref, {}).get(pin)
        if meta is None:
            nc = f"NC_{ref}_{pin}".upper()
            if nc not in floating:
                floating.append(nc)
            return nc
        return names[meta["net"]]

    def pin_table(ref: str) -> list[dict]:
        return [{"pin": p, "net": m["net"], "spice_node": names[m["net"]],
                 "pinfunction": m["pinfunction"]}
                for p, m in sorted(by_ref.get(ref, {}).items())]

    for ref in sorted(selected):
        pins = by_ref.get(ref, {})
        kind = re.match(r"[A-Za-z]+", ref)
        kind = kind.group(0).upper() if kind else ""
        value = comps[ref].get("value")

        if kind in ("R", "C", "L") and len(pins) == 2:
            tok = _value_token(value)
            if tok is None:
                unresolved.append({"ref": ref, "value": value,
                                   "reason": "value is not a SPICE number",
                                   "pins": pin_table(ref)})
                continue
            p1, p2 = sorted(pins)
            elements.append(f"{ref} {node(ref, p1)} {node(ref, p2)} {tok}")
        elif kind == "D":
            anodes = _pin_by_role(pins, "A")
            cathodes = _pin_by_role(pins, "K")
            if len(anodes) == 1 and len(cathodes) == 1:
                a, k = anodes[0], cathodes[0]
            elif len(pins) == 2 and set(pins) == {"1", "2"}:
                k, a = "1", "2"   # KiCad Device:LED/diode convention
            else:
                unresolved.append({"ref": ref, "value": value,
                                   "reason": "cannot identify anode/cathode",
                                   "pins": pin_table(ref)})
                continue
            model = f"D_{ref}".upper()
            elements.append(f"{ref} {node(ref, a)} {node(ref, k)} {model}")
            models.append({"name": model, "ref": ref, "value": value,
                           "kind": "diode",
                           "card": f".model {model} D()"})
        elif kind == "Q" and len(pins) == 3:
            b = _pin_by_role(pins, "B")
            c = _pin_by_role(pins, "C")
            e = _pin_by_role(pins, "E")
            if len(b) == len(c) == len(e) == 1:
                model = f"Q_{ref}".upper()
                elements.append(
                    f"{ref} {node(ref, c[0])} {node(ref, b[0])} "
                    f"{node(ref, e[0])} {model}")
                models.append({"name": model, "ref": ref, "value": value,
                               "kind": "bjt",
                               "card": f".model {model} NPN()"})
            else:
                unresolved.append({"ref": ref, "value": value,
                                   "reason": "cannot identify B/C/E",
                                   "pins": pin_table(ref)})
        else:
            unresolved.append({"ref": ref, "value": value,
                               "reason": f"no generic SPICE primitive for "
                                         f"{kind or '?'} with {len(pins)} pins",
                               "pins": pin_table(ref)})

    return {"elements": elements, "rename_map": names, "models": models,
            "unresolved": unresolved, "floating_nodes": floating,
            "refs": sorted(selected)}


# ----------------------------------------------------------------- bounds


def load_sidecar(path: Path | str) -> tuple[list[dict], dict | None]:
    """`<bench>.bounds.json` -> (bounds, tolerance config or None). The
    sidecar is a JSON list of bounds, or an object {"bounds": [...],
    "tolerance": {...}} (lib/simtol.py documents the tolerance block)."""
    data = checklib.load_json(path, "bounds sidecar")
    cfg = None
    if isinstance(data, dict):
        if "tolerance" in data:
            import simtol
            cfg = simtol.check_cfg(data["tolerance"], f"bounds sidecar {path}")
        data = data.get("bounds")
    return _check_bounds(data, path), cfg


def load_bounds(path: Path | str) -> list[dict]:
    """`<bench>.bounds.json` sidecar: a JSON LIST of
    {measure, min?, max?, severity: error|warning, msg?} (or an object
    holding it under "bounds", see load_sidecar)."""
    return load_sidecar(path)[0]


def _check_bounds(data, path) -> list[dict]:
    if not isinstance(data, list):
        raise checklib.CheckError(
            f"bounds sidecar {path}: must be a JSON list (or an object "
            "with a 'bounds' list)")
    if not data:
        raise checklib.CheckError(
            f"bounds sidecar {path}: empty list - an ungated testbench "
            "proves nothing")
    for i, b in enumerate(data):
        where = f"bounds sidecar {path} entry {i}"
        if not isinstance(b, dict) or not isinstance(b.get("measure"), str):
            raise checklib.CheckError(f"{where}: needs a string 'measure'")
        if b.get("min") is None and b.get("max") is None:
            raise checklib.CheckError(f"{where}: needs 'min' and/or 'max'")
        for lim in ("min", "max"):
            if b.get(lim) is not None and (
                    isinstance(b[lim], bool)
                    or not isinstance(b[lim], (int, float))
                    or not math.isfinite(b[lim])):
                raise checklib.CheckError(
                    f"{where}: '{lim}' must be a finite number")
        if b.get("severity", "error") not in SEVERITIES:
            raise checklib.CheckError(
                f"{where}: severity must be one of {SEVERITIES}")
    return data


def _fmt_bound(b: dict) -> str:
    lo = b.get("min")
    hi = b.get("max")
    if lo is not None and hi is not None:
        return f"[{lo:g}, {hi:g}]"
    if lo is not None:
        return f">= {lo:g}"
    return f"<= {hi:g}"


def compare_bounds(bounds: list[dict], measures: dict[str, float],
                   testbench: str, failed_measures=None) -> list[dict]:
    """Measured values vs sidecar bounds -> normalized violations.

    Measure names are matched case-insensitively (ngspice lowercases them).
    A referenced measure absent from the results is sim_measure_missing at
    the bound's severity - an unproven bound must not pass silently."""
    lut = {k.lower(): v for k, v in measures.items()}
    failed = {f.lower() for f in (failed_measures or [])}
    vios: list[dict] = []
    for b in bounds:
        name = b["measure"]
        sev = b.get("severity", "error")
        note = f" - {b['msg']}" if b.get("msg") else ""
        extras = {"kind": None, "measure": name, "testbench": testbench,
                  "bound": {"min": b.get("min"), "max": b.get("max")}}
        if name.lower() not in lut:
            why = (".measure reported 'failed!' (condition never met)"
                   if name.lower() in failed
                   else "not produced by the analysis")
            extras["kind"] = "sim_measure_missing"
            vios.append(checklib.violation(
                CHECK, sev, None, None, None, [],
                f"{testbench}: measure {name} missing - {why}{note}",
                SOURCE, **extras))
            continue
        value = float(lut[name.lower()])
        extras["value"] = value
        bad = (not math.isfinite(value)
               or (b.get("min") is not None and value < b["min"])
               or (b.get("max") is not None and value > b["max"]))
        if bad:
            extras["kind"] = "sim_bound_fail"
            vios.append(checklib.violation(
                CHECK, sev, None, None, None, [],
                f"{testbench}: {name} = {value:g} outside {_fmt_bound(b)}"
                f"{note}", SOURCE, **extras))
    return vios


def engine_error(testbench: str, reason: str, **extras) -> dict:
    """One sim_engine_error violation (severity error - a bench that cannot
    run must fail the gate, never pass silently)."""
    return checklib.violation(
        CHECK, "error", None, None, None, [],
        f"{testbench}: engine run failed - {reason}", SOURCE,
        kind="sim_engine_error", testbench=testbench, **extras)


# ----------------------------------------------------------------- engine

_NG = None  # extra module-level ref to the NgSpiceShared instance (thunks)


_DLL: Path | None = None


def setup_engine_env(dll: Path) -> None:
    """Point InSpice at the given ngspice shared library. MUST run before
    the first InSpice import in the process (its module import walks the
    library search path). See the module docstring for why the name must be
    bare and SPICE_LIB_DIR must exist."""
    global _DLL
    dll = Path(dll)
    _DLL = dll
    os.environ["NGSPICE_LIBRARY_PATH"] = dll.name
    os.environ["PATH"] = str(dll.parent) + os.pathsep + os.environ.get("PATH", "")
    os.environ.setdefault("SPICE_LIB_DIR", str(dll.parent))


def codemodel_files(dll: Path | None) -> list[Path]:
    """The XSPICE code models (*.cm) KiCad ships beside its libngspice:
    lib/ngspice/ next to the library (an AppImage: shared/lib/ngspice).
    KiCad's own simulator loads them through its spinit; the bare library
    finds no spinit, so without these an `A` device does not parse."""
    if dll is None:
        return []
    dll = Path(dll).resolve()
    for d in (dll.parent / "ngspice", dll.parent.parent / "lib" / "ngspice"):
        cms = sorted(d.glob("*.cm"))
        if cms:
            return cms
    return []


def _new_engine():
    """A fresh in-process ngspice with KiCad's code models loaded."""
    global _NG
    from InSpice.Spice.NgSpice.Shared import NgSpiceShared
    if os.name != "nt" and _DLL is not None:
        # off Windows InSpice's find_library is ctypes.util.find_library,
        # which only sees the system linker cache: a user-space KiCad's
        # libngspice.so.0 is never found (LEARNINGS 2026-09-27 [sim])
        NgSpiceShared.find_library = classmethod(lambda cls, n: str(_DLL))
    ng = NgSpiceShared.new_instance()
    _NG = ng
    if not getattr(ng, "_hwde_cm", False):
        for cm in codemodel_files(_DLL):
            ng.exec_command(f"codemodel {cm}")
        ng._hwde_cm = True
    return ng


_PRE_OSDI = re.compile(r"^(\s*pre_osdi\s+)(\S+)(.*)$", re.IGNORECASE)


def resolve_osdi(text: str, base: Path) -> str:
    """Make a deck's `pre_osdi FILE` lines (inside .control) absolute,
    relative to the bench's directory. A FILE ending in .va is compiled
    first with the IIC tree's OpenVAF (lib/solvers.compile_va, cached), so a
    bench can carry its Verilog-A model as source."""
    out = []
    for ln in text.splitlines():
        m = _PRE_OSDI.match(ln)
        if m:
            f = Path(m.group(2).strip('"'))
            if not f.is_absolute():
                f = Path(base) / f
            if f.suffix.lower() == ".va":
                import solvers
                f = solvers.compile_va(f)
            ln = f"{m.group(1)}{f}{m.group(3)}"
        out.append(ln)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def prepare_circuit(text: str) -> str:
    """Normalize a testbench: ensure the trailing .end (a missing one makes
    ngspice silently load NO circuit) and inject `.options rshunt=1e9` after
    the title line unless the bench sets rshunt itself."""
    lines = [ln.rstrip() for ln in text.splitlines()]
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        raise checklib.CheckError("empty circuit")
    if lines[-1].strip().lower() != ".end":
        lines.append(".end")
    if not any("rshunt" in ln.lower() for ln in lines):
        lines.insert(1, ".options rshunt=1e9")
    return "\n".join(lines) + "\n"


_MEASURE_HEADER = re.compile(r"^\s*Measurements for ", re.IGNORECASE)
_MEASURE_LINE = re.compile(
    r"^\s*([A-Za-z_][\w.]*)\s*=\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)")
_FAILED_LINE = re.compile(r"^\s*\.meas(?:ure)?\s+\S+\s+(\S+)\s.*failed!\s*$",
                          re.IGNORECASE)


def parse_measures(stdout_text: str) -> dict[str, float]:
    """ngspice stdout -> {measure: value}. Only lines inside a
    "Measurements for ... Analysis" region count; the first "name = value"
    on a line wins (TRIG/TARG echoes follow on the same line)."""
    out: dict[str, float] = {}
    in_region = False
    for line in stdout_text.splitlines():
        if _MEASURE_HEADER.match(line):
            in_region = True
            continue
        if not in_region:
            continue
        m = _MEASURE_LINE.match(line)
        if m:
            out[m.group(1).lower()] = float(m.group(2))
        elif line.strip():
            in_region = False
    return out


def parse_failed_measures(stderr_text: str) -> list[str]:
    """Names of .measure statements ngspice reported as 'failed!'."""
    out = []
    for line in stderr_text.splitlines():
        m = _FAILED_LINE.match(line)
        if m:
            out.append(m.group(1).lower())
    return out


def run_circuit(circuit: str) -> dict:
    """Run one prepared circuit through the in-process ngspice engine.

    Returns {"status": "ok", ngspice_version, measures, failed_measures,
    vectors, log_tail} or {"status": "error", "error": ...}. NEVER raises for
    engine-side problems; callers (the sim_run worker) run this whole
    function in a killable subprocess for hard timeouts / crash isolation.
    setup_engine_env() must have been called first."""
    try:
        ng = _new_engine()
    except Exception as exc:  # DLL missing/unloadable
        return {"status": "error",
                "error": f"engine load failed: {type(exc).__name__}: {exc}"}

    def tail(s: str, n: int = 800) -> str:
        return s[-n:] if s else ""

    try:
        ng.load_circuit(circuit)
    except Exception as exc:
        return {"status": "error",
                "error": f"circuit rejected: {type(exc).__name__}: "
                         f"{tail(ng.stdout) or exc}"}
    run_exc = None
    try:
        ng.run()
    except Exception as exc:
        # A failed .measure lands here too; successful measures are still
        # in stdout, so only give up when nothing measurable came back.
        run_exc = exc
    stdout, stderr = ng.stdout, ng.stderr
    measures = parse_measures(stdout)
    failed = parse_failed_measures(stderr)
    if run_exc is not None and not measures and not failed:
        return {"status": "error",
                "error": f"analysis failed: {type(run_exc).__name__}: "
                         f"{tail(stderr) or tail(stdout) or run_exc}"}
    vectors: list[str] = []
    try:
        vectors = sorted(ng.plot(None, ng.last_plot).keys())
    except Exception:
        pass  # vectors are debug sugar, never load-bearing
    return {"status": "ok", "ngspice_version": ng.ngspice_version,
            "measures": measures, "failed_measures": failed,
            "vectors": vectors, "log_tail": tail(stdout)}


def run_circuits(circuits: list[str]) -> dict:
    """Run prepared circuits one after another in ONE in-process engine
    (tolerance analyses: hundreds of variants of a bench) ->
    {"status": "ok", ngspice_version, "runs": [{"measures", "failed"} |
    {"error"}]} or {"status": "error", "error"}. Never raises for engine
    problems; setup_engine_env() first; the caller holds the kill switch."""
    global _NG
    try:
        from InSpice.Spice.NgSpice.Shared import NgSpiceShared
        if os.name != "nt" and _DLL is not None:
            NgSpiceShared.find_library = classmethod(lambda cls, n: str(_DLL))
        ng = NgSpiceShared.new_instance()
        _NG = ng
    except Exception as exc:  # DLL missing/unloadable
        return {"status": "error",
                "error": f"engine load failed: {type(exc).__name__}: {exc}"}
    runs = []
    for circuit in circuits:
        ng.clear_output()
        try:
            ng.load_circuit(circuit)
        except Exception as exc:
            runs.append({"error": f"circuit rejected: {type(exc).__name__}"})
            continue
        try:
            ng.run()
        except Exception:
            pass  # a failed .measure; the rest are still in stdout
        runs.append({"measures": parse_measures(ng.stdout),
                     "failed": parse_failed_measures(ng.stderr)})
        try:
            ng.destroy()
            ng.remove_circuit()
        except Exception:
            pass
    return {"status": "ok", "ngspice_version": ng.ngspice_version,
            "runs": runs}


def run_vectors(circuits: list[str]) -> dict:
    """Run prepared circuits one after another in the in-process engine and
    return every vector of each one's last plot as plain lists:
    {"status": "ok", ngspice_version, "plots": [{vector: [values]}]} or
    {"status": "error", "error": ...}. Like run_circuit it never raises for
    engine-side problems and wants setup_engine_env() first; sim_board.py
    runs it in a killable worker. An op plot's vectors hold one value each;
    a voltage source's branch current is "<name>#branch" (negative while
    the source delivers current)."""
    try:
        ng = _new_engine()
    except Exception as exc:  # DLL missing/unloadable
        return {"status": "error",
                "error": f"engine load failed: {type(exc).__name__}: {exc}"}
    plots = []
    for i, circuit in enumerate(circuits):
        try:
            ng.load_circuit(circuit)
            ng.run()
            vecs = ng.plot(None, ng.last_plot)
            plots.append({k.lower(): [float(x) for x in
                                      v.to_waveform().tolist()]
                          for k, v in vecs.items()})
        except Exception as exc:
            tail = (ng.stderr or ng.stdout or "")[-800:]
            return {"status": "error",
                    "error": f"circuit {i}: {type(exc).__name__}: "
                             f"{tail or exc}"}
    return {"status": "ok", "ngspice_version": ng.ngspice_version,
            "plots": plots}
