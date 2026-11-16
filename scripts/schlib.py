"""schlib.py - kicad-sch-api helpers for generated schematics (SPEC 6.2, P4).

The generator-script pattern
----------------------------
A design's schematic SOURCE is Python: one generator per sheet at
`<project>/kicad/gen/<sheet>.py`, the `.kicad_sch` files are BUILD OUTPUT.
Each sheet generator builds a `schlib.Sheet`; the root generator stitches
child sheets with `schlib.Project` and saves everything:

    import schlib
    power = schlib.Sheet("power", pwr_base=20)   # unique #PWR/#FLG range
    power.add_component("Regulator_Linear:AMS1117-3.3", "U2", "AMS1117-3.3",
                        at=(81.28, 60.96), footprint="...")
    power.wire_pins("U2", {"1": "GND", "2": "+3V3"})    # stub + local label
    power.hier_pin("VIN", ref="U2", pad="3", shape="input")
    root = schlib.Sheet("board")
    proj = schlib.Project(root)
    proj.add_sheet(power, at=(127.0, 63.5), size=(25.4, 12.7), nets=["VIN"])
    proj.save(out_dir)

Conventions (S1-proven against kicad-cli 10.0.3 ERC, see LEARNINGS [python]):
- ALL coordinates on the 1.27 mm (50 mil) grid; off-grid input raises.
- Every used pin gets a short outward stub wire plus a LOCAL label; nets
  form by label name. kicad-sch-api 0.5.6 add_global_label writes NOTHING
  (silent no-op) - root-sheet local nets come out "/NAME" in netlists.
- Unused pins are marked "NC" (no-connect); unconnected inputs are errors.
- Pins connect at wire ENDPOINTS only (mid-span contact does not connect).
- Power rails: power SYMBOLS make a net global across the whole hierarchy
  (no sheet pin needed); undriven rails carry a PWR_FLAG. Signals cross
  sheets via hierarchical labels (child) + sheet pins (root).
- Component references must be unique across ALL sheets (each sheet is its
  own file; nothing re-annotates). Give each sheet its own numbering range,
  including pwr_base for the invisible #PWR/#FLG refs. The one exception is
  an INSTANCED sheet (Project.add_sheet(child, instances=[...]), #563): one
  child file placed N times, its refs renumbered per block by ref_base
  (R1 -> R101/R201/..., #PWR9001 -> #PWR19001/...); keep the child's own
  numbers under ref_step (default 100) or it raises.
- Wiring/pinout facts come from datasheet-extract JSON, never from memory
  (SPEC section 5 grounding rule); `expect` pin-name checks are insurance.
- save() finishes with an automatic schem_refdes field-placement pass
  (electrically inert; place_fields=False opts out; see Sheet.place_report).
- A project library (lib_pull's `aiee:` parts) is found through the
  project's sym-lib-table: kicad-sch-api reads only the stock libraries, so
  Sheet registers the table's libraries with it on the first lib_id it
  doesn't know. The table is looked for in Sheet(project=...), then the
  generator's own kicad/ (kicad/gen/<sheet>.py's parent), then the CWD and
  CWD/kicad; use_project_libs(dir) does the same by hand.
- save() also makes the sheet readable: stub labels read outward, away
  from their pin numbers (apply_label_justify), two-pin parts hide their
  pin names (hide_pin_names), and paper="auto" (the default) takes the
  smallest A-size the drawing fits, the drawing centred (fit_sheet;
  fit=False opts out). schem_refdes.audit_sheet flags a label on a part.
- Project.save and a flat Sheet.save (readable=True, the default) finish by
  redrawing the result for people: schem_layout draws real wires, blocks and
  banks, gated on an unchanged kicad-cli netlist and with every symbol UUID
  kept. A sheet it can't redraw stays as generated; see layout_report.
- Label anchors are guarded: one landing on a FOREIGN wire run would merge
  two nets and raises ValueError at generation time.

Decoupling metadata: `place_ic_with_decoupling` records cap<->pin
associations in the exact shape S4's check_decoupling.py consumes;
`emit_decoupling()` / `Project.save(decoupling=...)` writes the JSON.

CLI (grounding aid for P4 agents):
    python schlib.py --pins "Device:C" [--out pins.json]
prints the symbol's pin table (number/name/type) as JSON. Exit 0/2.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import re
import sys
import uuid
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import env  # noqa: E402
import fplib  # noqa: E402

env.export_kicad_symbol_dir()   # before kicad-sch-api's first library scan

# kicad-sch-api prints library-scan noise on import; keep stdout JSON-clean
# (LEARNINGS [python]).
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    import kicad_sch_api as ksa

GRID = 1.27          # KiCad schematic grid (50 mil)
STUB = 2.54          # pin stub wire length, two grid steps

_SCRIPT = "schlib"


_LIB_TABLE = re.compile(r'\(lib\s+\(name\s+"?([^")\s]+)"?\)(?:.*?)\(uri\s+"([^"]+)"\)', re.S)


def lib_table(project: Path | str) -> dict[str, Path]:
    """nickname -> .kicad_sym path from <project>/sym-lib-table, with
    ${KIPRJMOD} read as the project dir. Missing table -> {}."""
    project = Path(project)
    table = project / "sym-lib-table"
    if not table.is_file():
        return {}
    out = {}
    for nick, uri in _LIB_TABLE.findall(table.read_text(encoding="utf-8",
                                                        errors="replace")):
        uri = uri.replace("${KIPRJMOD}", str(project.resolve()))
        out[nick] = Path(uri)
    return out


def use_project_libs(project: Path | str) -> list[str]:
    """Register every library in <project>/sym-lib-table with kicad-sch-api's
    symbol cache, so lib_ids like "aiee:CH224Q" resolve (ksa never reads a
    project's table). Returns the nicknames registered."""
    cache = ksa.get_symbol_cache()
    done = []
    for nick, path in lib_table(project).items():
        if not path.is_file():
            continue
        with _quiet():
            cache.add_library_path(str(path))
        if nick != path.stem:
            # ksa names a library by its file stem; the table's nickname wins
            cache._library_index[nick] = path
        done.append(nick)
    return done


def _known_lib(nick: str) -> bool:
    cache = ksa.get_symbol_cache()
    try:
        return nick in cache._library_index
    except AttributeError:
        return True     # a ksa without the index: let add() say what it finds


def _project_dirs(project) -> list[Path]:
    """Where a sheet's sym-lib-table may be, most specific first."""
    out = []
    if project is not None:
        out.append(Path(project))
    gen = Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else None
    if gen is not None and gen.parent.name == "gen":
        out.append(gen.parent.parent)
    out += [Path.cwd(), Path.cwd() / "kicad"]
    return out


def _stock_lib(lib_id: str) -> None:
    """Register lib_id's stock library with kicad-sch-api before placing it:
    a KiCad-10 .kicad_symdir library is flattened on first use (env)."""
    with _quiet():
        env.register_stock_libs(ksa.get_symbol_cache(), [lib_id])


@contextlib.contextmanager
def _quiet():
    """kicad-sch-api also prints at CALL time (emoji 'LOAD' lines on
    library misses); keep stdout JSON-clean around its entry points."""
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        yield


def snap(v: float) -> float:
    return round(round(v / GRID) * GRID, 4)


def assert_on_grid(xy, what: str) -> None:
    for v in xy:
        if abs(v - snap(v)) > 1e-6:
            raise ValueError(f"{what} at {tuple(xy)} is off the 1.27 mm grid")


def stub_dir(pin_rotation: float, comp_rotation: float) -> tuple[int, int]:
    """Outward stub direction for a pin, in SHEET coords (+y down).

    KiCad pin rotation points from the connection point TOWARD the body
    (0=right, 90=up, ...). Outward is the opposite, with y inverted for
    sheet coordinates. T6 rotmirror fixture: this composition matches real
    KiCad at every rotation - the V19 "inward stubs" defect was ksa's pin
    POSITIONS at 90/270 (see pin_pos), never this sign."""
    r = math.radians((pin_rotation + comp_rotation) % 360)
    return (round(-math.cos(r)), round(math.sin(r)))


def _point_on_segment(pos, a, b, tol: float = 1e-6) -> bool:
    """Is pos on segment a-b (endpoints included)? Wires are axis-aligned
    grid stubs, but the cross/dot form costs nothing extra."""
    ax, ay = a
    bx, by = b
    px, py = pos
    cross = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
    if abs(cross) > tol:
        return False
    dot = (px - ax) * (bx - ax) + (py - ay) * (by - ay)
    return -tol <= dot <= (bx - ax) ** 2 + (by - ay) ** 2 + tol


def _label_rotation(d: tuple[int, int]) -> float:
    """Label/hier-label angle so the text extends outward from the wire."""
    return {(1, 0): 0.0, (-1, 0): 180.0, (0, -1): 90.0, (0, 1): 270.0}.get(d, 0.0)


class Sheet:
    """One schematic file under construction (root or hierarchical child)."""

    def __init__(self, name: str, title: str | None = None, paper: str = "auto",
                 rev: str = "1", date: str = "", company: str = "hwde",
                 pwr_base: int = 1, project: Path | str | None = None):
        """project: the KiCad project dir whose sym-lib-table names this
        sheet's project libraries (default: found, see the module doc)."""
        self.name = name
        self.project = project
        with _quiet():
            self.sch = ksa.create_schematic(name)
        # "auto": save() picks the smallest paper the drawing fits (fit_sheet)
        self.paper = paper
        self.sch.set_paper_size("A4" if paper == "auto" else paper)
        self.fit_report: dict | None = None   # save-time paper fit
        self.sch.set_title_block(title=title or name, date=date, rev=rev,
                                 company=company)
        self.decoupling: list[dict] = []      # S4 association contract
        self.hier_pins: dict[str, str] = {}   # net -> shape (stitch contract)
        self.place_report: dict | None = None  # save-time field placement
        self._fixups: list[dict] = []         # saved-file pin-number repairs
        self._dnp: list[str] = []             # saved-file native-dnp repairs
        self._api_num: dict[tuple[str, str], str] = {}  # (lib_id, right)->wrong
        self._lib_ids: dict[str, str] = {}    # ref -> lib_id
        self._rotations: dict[str, float] = {}
        self._anchors: dict[str, tuple[float, float]] = {}
        # multi-unit parts (#378): ref -> {unit: (component, anchor)}, and
        # ref -> {pad: unit} for each pad drawn on a unit past the first
        self._units: dict[str, dict[int, tuple]] = {}
        self._pad_unit: dict[str, dict[str, int]] = {}
        self._wires: list[tuple] = []         # segments, for the label guard
        self._pin_nets: dict[tuple[str, str], str] = {}  # (ref, pad) -> net
        self._pwr_i = pwr_base - 1
        self._flg_i = pwr_base - 1
        self.uuid_map: dict[str, str] = {}    # save-time ksa uuid -> kept
        self.layout_report: dict | None = None  # save-time redraw (schem_layout)

    # ---------------------------------------------------------- components
    def add_component(self, lib_id: str, ref: str, value: str, at,
                      rotation: float = 0, footprint: str | None = None,
                      fields: dict | None = None, expect: dict | None = None,
                      fixups: list[dict] | None = None,
                      unit_at: dict | None = None):
        """Place a symbol. `expect` maps pad number -> substring the library
        pin NAME must contain (wiring insurance against wrong symbols).
        `fixups` repair kicad-sch-api's silent renumbering of alphanumeric
        pin numbers on save: [{"pin_name": "Shield", "wrong": "6",
        "right": "SH"}] (LEARNINGS [python]).
        A symbol drawn as several units (a dual op-amp, a quad gate) gets
        every unit, all as `ref` (#378): unit 1 at `at`, each further unit
        to the right of the one before, clear of its stubs and labels, or
        at `unit_at[unit]`. pin_pos/wire_pin find each pad on its own unit.
        Returns unit 1's component."""
        assert_on_grid(at[:2], f"{ref} anchor")
        self._project_lib(lib_id)
        with _quiet():
            c = self.sch.components.add(lib_id, reference=ref, value=value,
                                        position=tuple(at[:2]),
                                        rotation=rotation)
        fields = self._dress(c, lib_id, footprint, fields)
        self._lib_ids[ref] = lib_id
        self._rotations[ref] = rotation
        self._anchors[ref] = (at[0], at[1])
        units = lib_units(lib_id)
        if len([u for u in units if u > 0]) > 1:
            self._place_units(c, lib_id, ref, value, at, rotation, units,
                              footprint, fields, unit_at or {})
        for fx in fixups or []:
            fx = dict(fx, lib_id=lib_id)
            self._fixups.append(fx)
            self._api_num[(lib_id, fx["right"])] = fx["wrong"]
        if expect:
            names = {p.number: p.name for p in c.pins}
            for pad, want in expect.items():
                got = names.get(pad, "<missing>")
                if want not in got:
                    raise ValueError(
                        f"{ref} pin {pad}: expected name ~'{want}', got '{got}'")
        return c

    def _dress(self, c, lib_id: str, footprint: str | None,
               fields: dict | None) -> dict:
        """The library symbol's footprint, LCSC field and flags onto a placed
        instance (each unit of a part gets the same). Returns the fields."""
        if footprint is None:
            # kicad-sch-api leaves the instance's Footprint empty even when
            # the library symbol names one (lib_pull's symbols all do); KiCad
            # copies it on placement, so do the same (#412)
            footprint = lib_footprint(lib_id)
        if footprint:
            c.footprint = footprint
        fields = dict(fields or {})
        if not any(_LCSC_FIELD.match(k.strip()) for k in fields):
            # nor does it copy the symbol's LCSC number, so bom_cpl fell back
            # to the footprint's: a shared footprint (lib_pull's R0603,
            # C0603) embeds ONE part's, and every other value took it (1k
            # parts came out as the 10k's C25804). The symbol's wins.
            lcsc = lib_lcsc(lib_id)
            if lcsc:
                fields[lcsc[0]] = lcsc[1]
        for key, val in fields.items():
            c.set_property(key, val)
        # nor the symbol's (in_bom no)/(on_board no): a solder jumper or
        # test point came out in_bom yes against its footprint's
        # exclude_from_bom, which KiCad's DRC flags as
        # footprint_symbol_mismatch
        for flag, on in lib_flags(lib_id).items():
            if not on:
                setattr(c, flag, False)
        return fields

    def _place_units(self, c, lib_id: str, ref: str, value: str, at,
                     rotation: float, units: dict, footprint: str | None,
                     fields: dict, unit_at: dict) -> None:
        """Units 2..N of a multi-unit part (add_component). Pads shared by
        every unit (the symbol's unit 0) are wired on unit 1."""
        common = units.get(0, [])

        def half(u):
            pts = units.get(u, []) + common
            return max([max(abs(x), abs(y)) for _, x, y in pts] or [STUB])

        self._units[ref] = {1: (c, (at[0], at[1]))}
        self._pad_unit[ref] = {}
        x, prev = at[0], 1
        for u in sorted(k for k in units if k > 1):
            if u in unit_at:
                pos = tuple(unit_at[u][:2])
            else:
                gap = half(prev) + half(u) + 2 * (STUB + 12.7)
                x = round(x + max(25.4, math.ceil(gap / 2.54 - 1e-9) * 2.54), 4)
                pos = (x, at[1])
            assert_on_grid(pos, f"{ref} unit {u} anchor")
            with _quiet():
                cu = self.sch.components.add(lib_id, reference=ref,
                                             value=value, position=pos,
                                             rotation=rotation, unit=u)
            self._dress(cu, lib_id, footprint, fields)
            self._units[ref][u] = (cu, (pos[0], pos[1]))
            for num, _, _ in units[u]:
                self._pad_unit[ref][num] = u
            prev = u

    def _project_lib(self, lib_id: str) -> None:
        """Register the project's libraries the first time a lib_id names a
        library kicad-sch-api doesn't know, else the stock one of that name;
        say where it looked if none has it, rather than ksa's bare 'not
        found'."""
        nick = lib_id.split(":", 1)[0] if ":" in lib_id else ""
        if not nick or _known_lib(nick):
            return
        looked = []
        for d in _project_dirs(self.project):
            looked.append(str(d / "sym-lib-table"))
            if nick in lib_table(d):
                use_project_libs(d)
                if _known_lib(nick):
                    return
        # a stock library KiCad 10 ships as a .kicad_symdir (flattened here)
        _stock_lib(lib_id)
        if _known_lib(nick):
            return
        raise ValueError(
            f"{lib_id}: no library '{nick}' - kicad-sch-api knows only the "
            f"stock libraries, and no sym-lib-table naming '{nick}' was found "
            f"(looked in {', '.join(looked)}). Run lib_pull with --project, "
            f"or pass Sheet(project=<kicad dir>).")

    def mark_dnp(self, ref: str) -> None:
        """Set KiCad's NATIVE do-not-populate flag on `ref`'s placed
        instance. kicad-sch-api 0.5.6 exposes no writable `dnp` field on a
        schematic symbol - its writer hard-codes `(dnp no)` on every
        instance (LEARNINGS [kicad-sch-api][schematic][bom] 2026-08-07) - so
        this only records intent here; `save()` repairs the saved file text
        via `apply_native_dnp`, the same saved-file-repair pattern
        `apply_pin_number_fixups` already uses for alphanumeric pin
        renumbering. Does not touch in_bom/on_board (still caller's choice
        via add_component fields) or any custom field (e.g. `Variant`)."""
        self._dnp.append(ref)

    def _api_pad(self, ref: str, pad: str) -> str:
        return self._api_num.get((self._lib_ids.get(ref, ""), pad), pad)

    def pin_pos(self, ref: str, pad: str) -> tuple[float, float]:
        u = self._pad_unit.get(ref, {}).get(pad, 1)
        if u > 1:
            # ksa's ref lookup finds unit 1, and would put this pad where
            # unit 1's copy of it would be: ask the pad's own unit (#378)
            from kicad_sch_api.core import pin_utils
            comp, anchor = self._units[ref][u]
            p = pin_utils.get_component_pin_position(
                comp, self._api_pad(ref, pad))
        else:
            p = self.sch.get_component_pin_position(ref, self._api_pad(ref, pad))
            anchor = self._anchors.get(ref)
        if p is None:
            raise ValueError(f"{ref} pin {pad}: no such pin in symbol")
        x, y = p.x, p.y
        if self._rotations.get(ref, 0) % 360 in (90, 270):
            # V19 (T6, kicad-cli ERC-measured): kicad-sch-api 0.5.6 rotates
            # pin positions the WRONG WAY at 90/270 - its answer is the
            # true position mirrored through the anchor (R(-t) vs R(t);
            # correction R(2t) = -I at right angles). Real KiCad puts a
            # rot-90 Device:R pin1 (lib (0,+3.81)) at anchor x-3.81; ksa
            # says x+3.81. Reflect back through the anchor.
            ax, ay = anchor
            x, y = 2 * ax - x, 2 * ay - y
        return (round(x, 4), round(y, 4))

    def _pin_out_dir(self, ref: str, pad: str) -> tuple[int, int]:
        c = self.sch.components.get(ref)
        rot = {p.number: p.rotation for p in c.pins}[self._api_pad(ref, pad)]
        return stub_dir(rot, self._rotations.get(ref, 0))

    # ---------------------------------------------------------- wiring
    def _add_wire(self, start, end) -> tuple:
        """Add a wire and record its segment for the label guard."""
        self.sch.add_wire(start=start, end=end)
        seg = ((round(start[0], 4), round(start[1], 4)),
               (round(end[0], 4), round(end[1], 4)))
        self._wires.append(seg)
        return seg

    def _assert_label_clear(self, pos, net: str, own: tuple | None = None):
        """Labels attach anywhere ALONG a wire (LEARNINGS [erc]): an anchor
        landing on a FOREIGN wire run - interior or endpoint - silently
        merges two nets. Reject at generation time (ladder row 39).
        Segments geometrically identical to `own` are the label's own
        wire and are exempt."""
        p = (round(pos[0], 4), round(pos[1], 4))
        for seg in self._wires:
            if own is not None and seg == own:
                continue
            if _point_on_segment(p, seg[0], seg[1]):
                raise ValueError(
                    f"label '{net}' at {p} lands on an existing wire run "
                    f"{seg[0]}-{seg[1]} - it would merge nets")

    def wire_pin(self, ref: str, pad: str, net: str) -> None:
        """The auto-wire idiom: outward stub from the pin's actual position
        plus a local label naming the net. net == "NC" -> no-connect."""
        p = self.pin_pos(ref, pad)
        assert_on_grid(p, f"{ref} pin {pad}")
        if net == "NC":
            self.sch.no_connects.add(position=p)
            return
        d = self._pin_out_dir(ref, pad)
        end = (round(p[0] + d[0] * STUB, 4), round(p[1] + d[1] * STUB, 4))
        seg = self._add_wire(p, end)
        self._assert_label_clear(end, net, own=seg)
        # the text runs outward, away from the pin and its printed number
        self.sch.add_label(net, position=end, rotation=_label_rotation(d))
        self._pin_nets[(ref, pad)] = net

    def wire_pins(self, ref: str, pins: dict) -> None:
        """Wire every pad of `pins` ({pad: net}); deterministic order."""
        for pad, net in sorted(pins.items(), key=lambda kv: kv[0]):
            self.wire_pin(ref, pad, net)

    # ---------------------------------------------------------- power
    def power_flag(self, net: str, at, sym: str | None = None,
                   flag: bool = True) -> None:
        """Power-rail cluster: optional power symbol at the LEFT end,
        optional PWR_FLAG at the RIGHT end, label on the wire. Pins connect
        only at wire ENDPOINTS, so both symbols sit on endpoints and the
        label goes mid-wire when both ends are taken."""
        if not sym and not flag:
            raise ValueError(f"rail {net}: need a power symbol or PWR_FLAG")
        x, y = at
        assert_on_grid((x, y), f"rail {net}")
        end = (x + (2 if sym and flag else 1) * STUB, y)
        seg = self._add_wire((x, y), end)
        # every wire ENDPOINT must carry a pin or a label, or ERC flags it
        if sym and flag:
            label_at = (x + STUB, y)      # symbol start, flag end: label mid
        elif sym:
            label_at = end                # symbol start, label terminates end
        else:
            label_at = (x, y)             # label terminates start, flag end
        self._assert_label_clear(label_at, net, own=seg)
        # a label on the wire's start reads leftward, off the wire and flag
        self.sch.add_label(net, position=label_at,
                           rotation=180.0 if label_at == (x, y) else 0.0)
        if sym:
            self._pwr_i += 1
            # VALUE = the rail net: a power symbol's exported net name is
            # its VALUE, and the symbol WINS over a coincident label
            # (LEARNINGS [kicad]; ladder row 94) - equal by construction.
            self._place_pin1(sym, f"#PWR{self._pwr_i:02d}", net, (x, y))
        if flag:
            self._flg_i += 1
            self._place_pin1("power:PWR_FLAG", f"#FLG{self._flg_i:02d}",
                             "PWR_FLAG", end)

    def power_symbol_at_pin(self, ref: str, pad: str, sym: str) -> None:
        """Hang a power symbol off an existing pin stub end (no extra wire:
        avoids an unconnected-endpoint warning on driven rails that must not
        carry a PWR_FLAG). The symbol's VALUE - which names the exported
        global net - is taken from the net the pin was wired to, so the
        divergence class of ladder row 94 cannot exist. Wire the pin first."""
        net = self._pin_nets.get((ref, pad))
        if net is None:
            raise ValueError(
                f"{ref} pin {pad}: not wired yet - power_symbol_at_pin "
                f"derives the symbol VALUE (= global net name) from the "
                f"pin's wired net; call wire_pin/wire_pins first")
        p = self.pin_pos(ref, pad)
        d = self._pin_out_dir(ref, pad)
        tgt = (round(p[0] + d[0] * STUB, 4), round(p[1] + d[1] * STUB, 4))
        self._pwr_i += 1
        self._place_pin1(sym, f"#PWR{self._pwr_i:02d}", net, tgt)

    def _place_pin1(self, lib_id: str, ref: str, value: str, at) -> None:
        """Add a one-pin symbol with its PIN (not anchor) at `at`. The
        VALUE is enforced through the dedicated attribute too: on power
        symbols it names the exported global net, and set_property("Value")
        is a silent no-op on ksa 0.5.6 (LEARNINGS [kicad-sch-api])."""
        _stock_lib(lib_id)
        with _quiet():
            c = self.sch.components.add(lib_id, reference=ref, value=value,
                                        position=at)
            if getattr(c, "value", None) != value:
                c.value = value
        got = self.sch.get_component_pin_position(c.reference, "1")
        c.translate(round(at[0] - got.x, 4), round(at[1] - got.y, 4))

    # ---------------------------------------------------------- hierarchy
    def hier_pin(self, net: str, shape: str = "passive",
                 ref: str | None = None, pad: str | None = None,
                 at=None) -> None:
        """Expose `net` to the parent sheet. With ref/pad: stub from that
        pin with the hierarchical label on the stub end. With `at`: a small
        cluster in free area - wire with a LOCAL label of the net name at
        one end and the hierarchical label at the other, so the hier label
        joins the net by wire geometry (label name-merge not relied on)."""
        if ref is not None and pad is not None:
            p = self.pin_pos(ref, pad)
            assert_on_grid(p, f"{ref} pin {pad}")
            d = self._pin_out_dir(ref, pad)
            end = (round(p[0] + d[0] * STUB, 4), round(p[1] + d[1] * STUB, 4))
            seg = self._add_wire(p, end)
            self._assert_label_clear(end, net, own=seg)
            self.sch.add_hierarchical_label(net, position=end, shape=shape,
                                            rotation=_label_rotation(d))
            self._pin_nets[(ref, pad)] = net
        elif at is not None:
            x, y = at[:2]
            assert_on_grid((x, y), f"hier pin {net}")
            end = (round(x + STUB, 4), y)
            seg = self._add_wire((x, y), end)
            self._assert_label_clear((x, y), net, own=seg)
            self._assert_label_clear(end, net, own=seg)
            self.sch.add_label(net, position=(x, y), rotation=180.0)
            self.sch.add_hierarchical_label(net, position=end, shape=shape)
        else:
            raise ValueError(f"hier_pin {net}: give ref/pad or at")
        self.hier_pins[net] = shape

    # ---------------------------------------------------------- decoupling
    def place_ic_with_decoupling(self, ref: str, lib_id: str, value: str, at,
                                 pins: dict, footprint: str | None = None,
                                 rotation: float = 0,
                                 decoupling: list[dict] | None = None,
                                 caps_at=None, caps_dx: float = 12.7,
                                 expect: dict | None = None,
                                 fixups: list[dict] | None = None):
        """Place an IC, wire ALL its pins (stub+label / NC), and place one
        decoupling cap per `decoupling` entry in a row starting at `caps_at`
        (a free area the caller guarantees; caps connect by label, so their
        position is electrically free but must not collide with symbols).

        decoupling entries:
            {"cap": "C1", "pin": "48", "rail": "+3V3", "value": "100nF",
             "gnd": "GND",                       # optional, default GND
             "rail_net"/"gnd_net": "...",        # optional: exact NETLIST
                                                 #   names when they differ
                                                 #   from the wiring labels
             "lib_id": "Device:C",               # optional
             "footprint": "...",                 # optional
             "class"/"max_dist_mm"/"max_loop_nh"/"role": ...}  # passthrough
        Declare `"role": "reg_input"` on EVERY cap serving a switching
        regulator's input pin (buck/boost VIN) - check_decoupling then
        requires at least one HF ceramic (<= 1 uF) within 7.5 mm of that
        pin, the defect class that shipped lumina-carrier without U21's
        input ceramic (retro R1). The bulk input cap gets the role too;
        it does not tighten its own limits.
        Each entry is recorded as a cap<->pin association in EXACTLY the
        shape S4's check_decoupling.py consumes (see emit_decoupling).
        rail/gnd are the LOCAL wiring label names; the recorded metadata
        must carry the FINAL netlist names (S4: "exact board name"), which
        differ for root-local labels ("/NAME") and hier-crossed nets - give
        rail_net/gnd_net there. netlist_audit.py --decoupling verifies the
        recorded names against the real netlist."""
        ic = self.add_component(lib_id, ref, value, at, rotation=rotation,
                                footprint=footprint, expect=expect,
                                fixups=fixups)
        self.wire_pins(ref, pins)
        decoupling = decoupling or []
        if decoupling and caps_at is None:
            raise ValueError(f"{ref}: decoupling entries need caps_at")
        for i, ent in enumerate(decoupling):
            pin = str(ent["pin"])
            if pin not in pins:
                raise ValueError(f"{ref}: decoupling pin {pin} not in pins")
            rail = ent["rail"]
            if pins[pin] != rail:
                raise ValueError(
                    f"{ref} pin {pin}: wired to '{pins[pin]}' but decoupling "
                    f"entry says rail '{rail}'")
            gnd = ent.get("gnd", "GND")
            cx = round(caps_at[0] + i * caps_dx, 4)
            cy = round(caps_at[1], 4)
            assert_on_grid((cx, cy), f"decoupling cap {ent['cap']}")
            self.add_component(ent.get("lib_id", "Device:C"), ent["cap"],
                               ent["value"], (cx, cy),
                               footprint=ent.get("footprint"))
            self.wire_pin(ent["cap"], "1", rail)
            self.wire_pin(ent["cap"], "2", gnd)
            assoc = {"cap": ent["cap"], "ic": ref, "pin": pin,
                     "rail": ent.get("rail_net", rail),
                     "value": ent["value"]}
            gnd_net = ent.get("gnd_net", gnd)
            if gnd_net != "GND":
                assoc["gnd"] = gnd_net
            for opt in ("class", "max_dist_mm", "max_loop_nh", "role"):
                if opt in ent:
                    assoc[opt] = ent[opt]
            self.decoupling.append(assoc)
        return ic

    def emit_decoupling(self, path: Path | str,
                        extra: list[dict] | None = None) -> Path:
        """Write the cap<->pin association metadata (S4 contract)."""
        payload = {"associations": self.decoupling + (extra or [])}
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
        return p

    # ---------------------------------------------------------- save
    def save(self, out_dir: Path | str, project: bool = True,
             place_fields: bool = True, keep_uuids: bool = True,
             fit: bool = True, readable: bool = True) -> Path:
        """Write the sheet. place_fields (default) runs schem_refdes on the
        saved file so Reference/Value land on the correct side and clear of
        wires/labels/bodies (ksa mirrors every field to the wrong side -
        T6, ladder row 166 L3). Electrically inert; see self.place_report.
        keep_uuids (default): regenerating keeps the UUIDs of the file it
        overwrites, and anything new gets one derived from what it is, so
        the board's footprint paths keep matching (see stable_uuids).
        fit (default) sizes the paper to the drawing and centres it
        (fit_sheet), after labels are justified away from their pins and
        two-pin symbols' pin names are hidden (hide_pin_names)."""
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{self.name}.kicad_sch"
        previous = (path.read_text(encoding="utf-8")
                    if keep_uuids and path.exists() else None)
        with _quiet():
            self.sch.save(str(path))
        missing = missing_units(path)
        if missing:
            # a sheet built past add_component (sch.components.add) that
            # placed one unit of a dual op-amp: the rest of its pins float
            raise ValueError(
                f"{path.name}: "
                + "; ".join(f"{r} lacks unit(s) {', '.join(map(str, u))}"
                            for r, u in missing.items())
                + " - place every unit (Sheet.add_component does), or ERC "
                "reports missing_unit and those pins are in no net (#378)")
        import schem_refdes as sr  # sibling script; deferred (shapely)
        sr.strip_private_properties(path)   # KiCad-10 stock-lib notes ksa mangles (exit 3 at ERC)
        apply_pin_number_fixups(self._fixups, path)
        apply_label_justify(path)
        if place_fields:
            self._place_fields(path)
        # after field placement: its kicad-sch-api re-save drops pin_names
        hide_pin_names(path)
        if fit:
            self.fit_report = fit_sheet(path, self.paper)
        # native dnp LAST: write_placements() may round-trip the file
        # through kicad-sch-api (schem_refdes.write_placements ->
        # ksa.Schematic.load().save() when any field actually moved), and
        # ksa's writer hard-codes `(dnp no)` on every symbol instance
        # regardless of what it parsed - an earlier dnp patch would be
        # silently wiped by that later whole-file re-save (measured: with
        # the call before place_fields, J3/J4 came back `(dnp no)`).
        apply_native_dnp(self._dnp, path)
        apply_hier_shapes(self.hier_pins, path)
        if keep_uuids:
            self.uuid_map = stable_uuids(path, previous, self.name)
        if project:
            write_project(self.name, out_dir)
            if readable:
                self.layout_report = _readable(path)
        return path

    def _place_fields(self, path: Path) -> None:
        """NON-FATAL field placement: a failure must never break
        generation - the file is restored to the valid un-placed state and
        the error recorded in self.place_report."""
        import schem_refdes as sr  # sibling script; deferred (shapely)
        snapshot = path.read_text(encoding="utf-8")
        try:
            res = sr.place_sheet(sr.Sheet(path))
            sr.write_placements(path, res)   # project libs + lib_symbols guard
            self.place_report = {
                "moved": sum(1 for p in res["placements"] if p["moved"]),
                "residue": res["residue"]}
        except Exception as exc:  # noqa: BLE001 - never break generation
            path.write_text(snapshot, encoding="utf-8")
            self.place_report = {"error": f"{type(exc).__name__}: {exc}"}


class Project:
    """Root sheet + hierarchical children; stitches sheet pins and saves
    the whole set (root's name names the project)."""

    def __init__(self, root: Sheet, ref_step: int = 100):
        self.root = root
        self.children: list[Sheet] = []
        self.ref_step = ref_step
        # child name -> [(sheet block name, ref_base)] for a repeated sheet
        self.instances: dict[str, list[tuple[str, int]]] = {}
        self._block_names: set[str] = set()

    def add_sheet(self, child: Sheet, at=None, size=None, nets=(),
                  label_stub: float = 3 * STUB, *,
                  instances: list[dict] | None = None,
                  name: str | None = None,
                  ref_base: int | None = None) -> None:
        """Instantiate `child` on the root at `at` (top-left) with `size`
        (w, h). `nets`: the child hier pins to expose, as names (root label
        = same name) or (child_net, root_label) pairs. Sheet pins stack on
        the LEFT edge top-down; each is wired outward to a root local label.
        Rails via power symbols are global and need no entry here.

        One channel, N copies (#563): `instances=[{"at": (x, y), "nets":
        [...], "name": "ch2", "ref_base": 200}, ...]` puts N sheet blocks on
        the root that all point at the ONE file `<child.name>.kicad_sch`.
        Each block's parts are annotated from its `ref_base` (default 100*k
        for the k-th, 1-based): R1 -> R101 / R201 / R301, #PWR9001 ->
        #PWR19001 ...; a child ref numbered >= ref_step (default 100) raises.
        `size` (and `nets`, `label_stub`) are the defaults for every entry;
        `name` defaults to `<child.name><k>`. The same thing one block at a
        time: add_sheet(child, at, size, nets, name=..., ref_base=...)."""
        if instances is not None:
            if at is not None or name is not None or ref_base is not None:
                raise ValueError(f"sheet {child.name}: give at/name/ref_base "
                                 "per entry in instances=, not beside it")
            if not instances:
                raise ValueError(f"sheet {child.name}: instances= is empty")
            for k, inst in enumerate(instances, 1):
                unknown = set(inst) - {"at", "size", "nets", "name",
                                       "ref_base", "label_stub"}
                if unknown:
                    raise ValueError(f"sheet {child.name} instance {k}: "
                                     f"unknown keys {sorted(unknown)}")
                self.add_sheet(child, inst["at"], inst.get("size", size),
                               inst.get("nets", nets),
                               inst.get("label_stub", label_stub),
                               name=inst.get("name", f"{child.name}{k}"),
                               ref_base=inst.get("ref_base", 100 * k))
            return
        if at is None or size is None:
            raise ValueError(f"sheet {child.name}: add_sheet needs at and size")
        block = name or child.name
        insts = self.instances.get(child.name)
        if ref_base is None:
            if insts or child in self.children:
                raise ValueError(
                    f"sheet {child.name}: placed twice without ref_base; a "
                    "repeated sheet needs per-instance numbering")
        else:
            if child in self.children and not insts:
                raise ValueError(
                    f"sheet {child.name}: already placed without ref_base")
            if not isinstance(ref_base, int) or ref_base <= 0 \
                    or ref_base % self.ref_step:
                raise ValueError(
                    f"sheet {child.name}: ref_base {ref_base!r} must be a "
                    f"positive multiple of ref_step {self.ref_step}")
            taken = [(n, b) for ins in self.instances.values()
                     for n, b in ins]
            if any(b == ref_base for _n, b in taken):
                raise ValueError(f"sheet {block}: ref_base {ref_base} "
                                 "already used")
        if block in self._block_names:
            raise ValueError(f"sheet block name {block!r} used twice")
        assert_on_grid(at[:2], f"sheet {block}")
        entries = [(n, n) if isinstance(n, str) else tuple(n) for n in nets]
        missing = [n for n, _ in entries if n not in child.hier_pins]
        if missing:
            raise ValueError(
                f"sheet {child.name}: no hier_pin for {missing} "
                f"(has {sorted(child.hier_pins)})")
        suid = self.root.sch.add_sheet(block, f"{child.name}.kicad_sch",
                                       position=tuple(at[:2]),
                                       size=tuple(size[:2]))
        h = size[1]
        for i, (net, _root_label) in enumerate(entries):
            along = round(h - 2 * GRID * (i + 1), 4)
            if along <= 0:
                raise ValueError(f"sheet {child.name}: too many pins for "
                                 f"height {h}")
            self.root.sch.add_sheet_pin(suid, net, child.hier_pins[net],
                                        "left", along)
        sh = self.root.sch.sheets.get_sheet_by_name(block)
        for (net, root_label), sp in zip(entries, sh["pins"]):
            px, py = sp["position"]["x"], sp["position"]["y"]
            assert_on_grid((px, py), f"sheet pin {net}")
            end = (round(px - label_stub, 4), py)
            seg = self.root._add_wire((px, py), end)
            self.root._assert_label_clear(end, root_label, own=seg)
            self.root.sch.add_label(root_label, position=end, rotation=180.0)
        self._block_names.add(block)
        if ref_base is not None:
            self.instances.setdefault(child.name, []).append((block, ref_base))
        if child not in self.children:
            self.children.append(child)

    def instance_ref(self, ref: str, ref_base: int) -> str:
        """`ref` of a repeated sheet's part as annotated in the instance
        numbered from `ref_base`: R1 -> R101 (ref_base 100). Power and flag
        refs (#PWR9001) move up a whole range: #PWR19001."""
        m = _REF_NUM.match(ref)
        if not m:
            raise ValueError(f"reference {ref!r}: no trailing number to "
                             "annotate per instance")
        pre, num = m.group(1), int(m.group(2))
        if pre.startswith("#"):
            return f"{pre}{ref_base * 100 + num}"
        if num >= self.ref_step:
            raise ValueError(
                f"reference {ref}: number {num} >= ref_step {self.ref_step} "
                "would run into the next instance's range")
        return f"{pre}{ref_base + num}"

    @property
    def decoupling(self) -> list[dict]:
        out = list(self.root.decoupling)
        for c in self.children:
            insts = self.instances.get(c.name)
            if not insts:
                out.extend(c.decoupling)
                continue
            for _block, base in insts:
                for a in c.decoupling:
                    out.append({**a, "cap": self.instance_ref(a["cap"], base),
                                "ic": self.instance_ref(a["ic"], base)})
        return out

    def save(self, out_dir: Path | str,
             decoupling: Path | str | None = None,
             place_fields: bool = True, readable: bool = True) -> Path:
        out_dir = Path(out_dir)
        for c in self.children:
            c.save(out_dir, project=False, place_fields=place_fields)
        path = self.root.save(out_dir, project=True,
                              place_fields=place_fields, readable=False)
        # A child's instance paths name the root's and its sheet's UUIDs,
        # which the root's save just settled: carry them into every file.
        moved = {}
        for sh in (self.root, *self.children):
            moved.update(sh.uuid_map)
        for c in self.children:
            _rewrite_uuids(out_dir / f"{c.name}.kicad_sch", moved)
        if decoupling is not None:
            payload = {"associations": self.decoupling}
            p = Path(decoupling)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(payload, indent=1) + "\n",
                         encoding="utf-8")
        if readable:
            self.root.layout_report = _readable(path)
        # Last, after the redraw (which renumbers power symbols): each
        # repeated child's symbols get one (path) per sheet block.
        if self.instances:
            _write_instances(path, out_dir, self.instances,
                             self.instance_ref)
        return path


_REF_NUM = re.compile(r"^(#?[A-Za-z_]+)(\d+)$")


def _form_end(text: str, i: int) -> int:
    """Index just past the s-expression opening at text[i] == "("."""
    depth = 0
    for m in _SEXP_TOK.finditer(text, i):
        tok = m.group(0)
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth -= 1
            if depth == 0:
                return m.end()
    raise ValueError("unbalanced s-expression")


def _write_instances(root_path: Path, out_dir: Path,
                     instances: dict[str, list[tuple[str, int]]],
                     annotate) -> None:
    """Rewrite every repeated child file's symbol (instances) as one
    (path "/<root uuid>/<sheet block uuid>") per block, each with that
    block's reference, and give the root's blocks the project name and
    their own page numbers. KiCad annotates a reused sheet file this way;
    without it every block would show the child's own refs and the
    netlist would carry each part N times under one ref."""
    root_text = root_path.read_text(encoding="utf-8")
    top = next(f for f in _sexp(root_text) if isinstance(f, list)
               and f and f[0] == "kicad_sch")
    root_uuid = next(_unq(f[1]) for f in top
                     if isinstance(f, list) and f and f[0] == "uuid")
    project = root_path.stem
    blocks: dict[str, str] = {}
    for f in top:
        if not (isinstance(f, list) and f and f[0] == "sheet"):
            continue
        uid = next(_unq(g[1]) for g in f
                   if isinstance(g, list) and g and g[0] == "uuid")
        for g in f:
            if isinstance(g, list) and len(g) >= 3 and g[0] == "property" \
                    and _unq(g[1]) == "Sheetname":
                blocks[_unq(g[2])] = uid
    seen: dict[tuple[str, str], str] = {}   # (ref, unit) -> block
    for child, insts in instances.items():
        missing = [b for b, _ in insts if b not in blocks]
        if missing:
            raise ValueError(f"{root_path.name}: no sheet block {missing}")
        path = out_dir / f"{child}.kicad_sch"
        text = path.read_text(encoding="utf-8")
        out, pos = [], 0
        for m in re.finditer(r"\(instances\b", text):
            if m.start() < pos:
                continue
            end = _form_end(text, m.start())
            body = text[m.start():end]
            r = re.search(r'\(reference "([^"]*)"\)', body)
            u = re.search(r"\(unit (\d+)\)", body)
            if r is None:
                raise ValueError(f"{path.name}: symbol instances without "
                                 "a reference")
            ind = text[text.rfind("\n", 0, m.start()) + 1:m.start()]
            lines = ["(instances", f'{ind}\t(project "{project}"']
            unit = u.group(1) if u else "1"
            for block, base in insts:
                ref = annotate(r.group(1), base)
                # a multi-unit part is one ref on several symbols (#378)
                if (ref, unit) in seen:
                    raise ValueError(f"{ref} annotated twice "
                                     f"({seen[(ref, unit)]}, {block})")
                seen[(ref, unit)] = block
                lines += [f'{ind}\t\t(path "/{root_uuid}/{blocks[block]}"',
                          f'{ind}\t\t\t(reference "{ref}")',
                          f'{ind}\t\t\t(unit {unit})',
                          f"{ind}\t\t)"]
            lines += [f"{ind}\t)", f"{ind})"]
            out.append(text[pos:m.start()])
            out.append("\n".join(lines))
            pos = end
        out.append(text[pos:])
        path.write_text("".join(out), encoding="utf-8")
    # the root's own refs must not land in a repeated sheet's range
    blocks_of = {ref: block for (ref, _), block in seen.items()}
    for m in re.finditer(r'\(reference "([^"]*)"\)', root_text):
        if m.group(1) in blocks_of:
            raise ValueError(f"root reference {m.group(1)} collides with "
                             f"sheet block {blocks_of[m.group(1)]}")
    # each repeated block: this project, a page of its own
    want = {b for ins in instances.values() for b, _ in ins}
    used = {1}
    for m in re.finditer(r"\(sheet\b(?!_)", root_text):
        body = root_text[m.start():_form_end(root_text, m.start())]
        nm = re.search(r'\(property "Sheetname" "([^"]*)"', body)
        if nm and nm.group(1) not in want:
            used |= {int(p) for p in re.findall(r'\(page "(\d+)"\)', body)}
    nxt = max(used) + 1
    out, pos = [], 0
    for m in re.finditer(r"\(sheet\b(?!_)", root_text):
        if m.start() < pos:
            continue
        end = _form_end(root_text, m.start())
        body = root_text[m.start():end]
        nm = re.search(r'\(property "Sheetname" "([^"]*)"', body)
        if nm and nm.group(1) in want:
            body = re.sub(r'\(project "[^"]*"', f'(project "{project}"', body)
            body = re.sub(r'\(page "\d+"\)', f'(page "{nxt}")', body)
            nxt += 1
        out.append(root_text[pos:m.start()])
        out.append(body)
        pos = end
    out.append(root_text[pos:])
    root_path.write_text("".join(out), encoding="utf-8")


def _readable(path: Path) -> dict:
    """Redraw a saved schematic for people. Never breaks generation: what can't
    be redrawn stays as generated, and the report says why."""
    import schem_layout  # sibling script; deferred (shapely, kicad-cli)
    try:
        report, _code = schem_layout.run(path)
        return report
    except Exception as exc:  # noqa: BLE001 - the drawing is not the design
        return {"error": f"{type(exc).__name__}: {exc}"}


_UUID_NS = uuid.UUID("5b0c2a8e-6f3d-4f53-9a8e-6877646573c1")  # hwde schematics
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_SEXP_TOK = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+')


def _sexp(text: str) -> list:
    """The file's forms as nested lists; quoted strings keep their quotes
    (so `"uuid"` the string and `uuid` the head stay apart)."""
    stack: list[list] = [[]]
    for tok in _SEXP_TOK.findall(text):
        if tok == "(":
            stack.append([])
        elif tok == ")":
            done = stack.pop()
            stack[-1].append(done)
        else:
            stack[-1].append(tok)
    return stack[0]


def _unq(tok) -> str:
    return tok[1:-1] if isinstance(tok, str) and tok[:1] == '"' else str(tok)


def _child(form: list, head: str) -> list | None:
    return next((c for c in form[1:] if isinstance(c, list) and c
                 and c[0] == head), None)


def _prop(form: list, name: str) -> str | None:
    for c in form[1:]:
        if isinstance(c, list) and len(c) > 2 and c[0] == "property" \
                and _unq(c[1]) == name:
            return _unq(c[2])
    return None


def _uuid_of(form: list) -> str | None:
    u = _child(form, "uuid")
    return _unq(u[1]) if u and len(u) > 1 else None


def _at(form: list) -> tuple:
    a = _child(form, "at")
    return tuple(round(float(v), 4) for v in a[1:3]) if a else ()


def uuid_keys(text: str) -> list[tuple[tuple, str]]:
    """(key, uuid) for every UUID-bearing object of a .kicad_sch, the key
    being what the object IS, not where ksa happened to number it: the
    file; a symbol by Reference (+unit) and its pins by number; a wire/bus
    by its end points; a label, junction, no-connect, text by name and
    place; a sheet by Sheetname and its pins by name. Duplicate keys get an
    occurrence index, in file order."""
    top = next((f for f in _sexp(text) if isinstance(f, list) and f
                and f[0] == "kicad_sch"), None)
    if top is None:
        raise ValueError("not a kicad_sch file")
    out: list[tuple[tuple, str]] = []
    for f in top[1:]:
        if not (isinstance(f, list) and f):
            continue
        head = f[0]
        if head == "uuid":
            out.append((("file",), _unq(f[1])))
            continue
        u = _uuid_of(f)
        if head == "symbol":
            unit = _child(f, "unit")
            base = (_prop(f, "Reference") or "?",
                    _unq(unit[1]) if unit else "1")
            key = ("symbol",) + base
            pins = [(("pin",) + base + (_unq(p[1]),), _uuid_of(p))
                    for p in f[1:] if isinstance(p, list) and p
                    and p[0] == "pin"]
        elif head == "sheet":
            name = _prop(f, "Sheetname") or _prop(f, "Sheet name") or "?"
            key = ("sheet", name)
            pins = [(("sheet_pin", name, _unq(p[1])), _uuid_of(p))
                    for p in f[1:] if isinstance(p, list) and p
                    and p[0] == "pin"]
        else:
            pts = _child(f, "pts")
            where = (tuple(sorted(tuple(round(float(v), 4) for v in xy[1:3])
                                  for xy in pts[1:] if isinstance(xy, list)))
                     if pts else _at(f))
            name = _unq(f[1]) if len(f) > 1 and not isinstance(f[1], list) \
                else ""
            key = (head, name, where)
            pins = []
        if u:
            out.append((key, u))
        out.extend((k, pu) for k, pu in pins if pu)
    seen: dict[tuple, int] = {}
    numbered = []
    for k, u in out:
        n = seen.get(k, 0)
        seen[k] = n + 1
        numbered.append((k + (n,) if n else k, u))
    return numbered


def _rewrite_uuids(path: Path, mapping: dict[str, str]) -> None:
    if not mapping:
        return
    text = path.read_text(encoding="utf-8")
    new = _UUID_RE.sub(lambda m: mapping.get(m.group(0), m.group(0)), text)
    if new != text:
        path.write_text(new, encoding="utf-8")


def stable_uuids(path: Path, previous: str | None, name: str) -> dict[str, str]:
    """Give the just-saved schematic at `path` stable UUIDs, in place.
    kicad-sch-api draws fresh random UUIDs on every run, which would cut the
    board's footprints (path "/<sheet>/<symbol>") off the schematic each
    time a generator is re-run. Each object keeps the UUID it had in
    `previous` (the file this save overwrote) under the same key
    (uuid_keys); anything new gets uuid5(key), so a fresh generation is
    the same every time too. Returns {ksa uuid: kept uuid} (instance paths
    in child sheets need it, see Project.save)."""
    old = dict(uuid_keys(previous)) if previous else {}
    mapping: dict[str, str] = {}
    for key, u in uuid_keys(path.read_text(encoding="utf-8")):
        want = old.get(key) or str(uuid.uuid5(
            _UUID_NS, json.dumps([name, *key], default=str)))
        if want != u:
            mapping[u] = want
    _rewrite_uuids(path, mapping)
    return mapping


def apply_pin_number_fixups(fixups: list[dict], sch_path: Path) -> None:
    """kicad-sch-api renumbers alphanumeric pin numbers (e.g. USB shield
    "SH" -> "6"), breaking netlist-vs-footprint pad matching. Repair the
    saved file text. Entries: {"lib_id", "pin_name", "wrong", "right"}.
    (Proven at S1: tests/golden/generators/sch_build.py.)"""
    if not fixups:
        return
    text = sch_path.read_text(encoding="utf-8")
    for fx in fixups:
        # 1. embedded lib symbol: the (number "wrong") right after the pin
        #    whose (name "pin_name") matches
        ni = text.find(f'(name "{fx["pin_name"]}"')
        if ni < 0:
            raise ValueError(f"pin fixup: name {fx['pin_name']} not found")
        old = f'(number "{fx["wrong"]}"'
        no = text.find(old, ni)
        if no < 0:
            raise ValueError(f"pin fixup: number {fx['wrong']} not found "
                             f"after name {fx['pin_name']}")
        text = text[:no] + f'(number "{fx["right"]}"' + text[no + len(old):]
        # 2. every instance of that lib_id: per-pin uuid entries (pin "wrong")
        pos = 0
        needle = f'(lib_id "{fx["lib_id"]}")'
        while True:
            li = text.find(needle, pos)
            if li < 0:
                break
            nxt = text.find('(lib_id "', li + len(needle))
            end = nxt if nxt > 0 else len(text)
            seg = text[li:end].replace(
                f'(pin "{fx["wrong"]}"', f'(pin "{fx["right"]}"')
            text = text[:li] + seg + text[end:]
            pos = li + len(seg)
    sch_path.write_text(text, encoding="utf-8")


def apply_hier_shapes(shapes: dict[str, str], sch_path: Path) -> None:
    """kicad-sch-api writes every hierarchical label as `(shape input)` whatever
    it was given. Put back the shape each `hier_pin` declared, after the last
    whole-file re-save, so the sheet and its pin on the root agree."""
    text = sch_path.read_text(encoding="utf-8")

    def fix(m: re.Match) -> str:
        return m.group(1) + shapes.get(m.group(2), m.group(3)) + m.group(4)

    sch_path.write_text(re.sub(r'(\(hierarchical_label "((?:[^"\\]|\\.)*)"\s*\(shape )(\w+)(\))',
                               fix, text), encoding="utf-8")


def apply_native_dnp(refs: list[str], sch_path: Path) -> None:
    """kicad-sch-api 0.5.6 hard-codes `(dnp no)` on every saved symbol
    instance - there is no writable `dnp` field on a schematic symbol in
    this library version (LEARNINGS [kicad-sch-api][schematic][bom]
    2026-08-07). Repair the saved file text for `refs`: flip that instance's
    own `(dnp no)` to `(dnp yes)`. Each symbol block emits `(dnp no)`
    shortly BEFORE its own `(property "Reference" "REF" ...)` (only
    `fields_autoplaced`/`uuid` in between, neither containing the substring
    `(dnp `), so the nearest preceding `(dnp no)` before that property
    belongs unambiguously to this instance - the same nearest-neighbour
    technique `apply_pin_number_fixups` uses. Leaves in_bom/on_board and
    every custom field (e.g. `Variant`) untouched. A multi-unit part is
    one ref on several symbols (#378): each unit's instance is flipped."""
    if not refs:
        return
    text = sch_path.read_text(encoding="utf-8")
    old = "(dnp no)"
    for ref in refs:
        needle = f'(property "Reference" "{ref}"'
        hits = [m.start() for m in re.finditer(re.escape(needle), text)]
        if not hits:
            raise ValueError(f"dnp fixup: reference {ref} not found")
        # several hits are one part only as distinct units of one symbol;
        # two parts sharing a ref have no instance that is the ref's own
        units = []
        for ri in hits:
            li = text.rfind("(lib_id ", 0, ri)
            head = text[li:ri] if li >= 0 else ""
            lid = re.match(r'\(lib_id "([^"]*)"', head)
            un = re.search(r"\(unit (\d+)\)", head)
            units.append((lid.group(1) if lid else "", un.group(1) if un else "1"))
        if len(set(units)) < len(units) or len({u[0] for u in units}) > 1:
            raise ValueError(f"dnp fixup: reference {ref} is not unique")
        for ri in reversed(hits):   # from the end: earlier offsets hold
            dn = text.rfind(old, 0, ri)
            # another instance's (dnp ...) is not this one's
            if dn < 0 or text.rfind("(property \"Reference\"", dn, ri) >= 0:
                raise ValueError(f"dnp fixup: no preceding '{old}' for {ref}")
            text = text[:dn] + "(dnp yes)" + text[dn + len(old):]
    sch_path.write_text(text, encoding="utf-8")


def _forms(text: str, start: int = 0, end: int | None = None):
    """(head, open, close) of each top-level form in text[start:end],
    `close` one past its ")". Quoted strings are skipped."""
    end = len(text) if end is None else end
    depth, i, open_at = 0, start, -1
    while i < end:
        ch = text[i]
        if ch == '"':
            i += 1
            while i < end and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif ch == "(":
            if depth == 0:
                open_at = i
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                m = re.match(r"\((\S+)", text[open_at:open_at + 40])
                yield (m.group(1) if m else "", open_at, i + 1)
            if depth < 0:
                return
        i += 1


def _body(text: str):
    """(start, end) of the inside of the kicad_sch form."""
    top = next(_forms(text))
    return top[1] + 1, top[2] - 1


def apply_label_justify(sch_path: Path) -> int:
    """kicad-sch-api writes `(justify left bottom)` on every label whatever
    its angle, while KiCad writes a 180/270-degree label right-justified:
    left-justified, its text runs back over its own wire and pin. Repair
    the saved file text; returns how many labels changed."""
    text = sch_path.read_text(encoding="utf-8")
    n = 0

    def fix(m):
        nonlocal n
        n += 1
        return m.group(1) + "(justify right"

    new = re.sub(r'(\((?:label|hierarchical_label) "(?:[^"\\]|\\.)*"\s*'
                 r'\(at \S+ \S+ (?:180|270)(?:\.0+)?\)[^()]*'
                 r'\(effects[^()]*\(font[^()]*\([^()]*\)[^()]*\)\s*)'
                 r'\(justify left', fix, text)
    if n:
        sch_path.write_text(new, encoding="utf-8")
    return n


def hide_pin_names(sch_path: Path) -> list[str]:
    """Hide pin NAMES on embedded symbols where they only add clutter: a
    two-pin part (R, C, LED, a 2-pin header: KiCad's own Device symbols
    hide them too; the symbol's drawing and the pin numbers say all there
    is) and any symbol whose every pin name is empty or its own number.
    Left shown, an LED's "K" and "A" print over each other inside its
    body. Pin numbers stay. Returns the lib_ids changed."""
    text = sch_path.read_text(encoding="utf-8")
    b0, b1 = _body(text)
    lib = next((f for f in _forms(text, b0, b1) if f[0] == "lib_symbols"),
               None)
    if lib is None:
        return []
    edits, changed = [], []
    for head, s0, s1 in _forms(text, lib[1] + 1, lib[2] - 1):
        if head != "symbol":
            continue
        block = text[s0:s1]
        pins = re.findall(r'\(pin \S+ \S+.*?\(name "((?:[^"\\]|\\.)*)"'
                          r'.*?\(number "((?:[^"\\]|\\.)*)"', block, re.S)
        if not pins:
            continue
        redundant = all(nm in ("", "~", num) for nm, num in pins)
        if len(pins) > 2 and not redundant:
            continue
        lib_id = re.match(r'\(symbol "((?:[^"\\]|\\.)*)"', block).group(1)
        inner = list(_forms(text, s0 + 1, s1 - 1))
        pn = next((f for f in inner if f[0] == "pin_names"), None)
        if pn is None:
            at = inner[0][1] if inner else s1 - 1
            ind = text[text.rfind("\n", 0, at) + 1:at]
            new = (f"(pin_names\n{ind}\t(hide yes)\n{ind})\n{ind}"
                   if ind.isspace() else "(pin_names (hide yes)) ")
            edits.append((at, at, new))
        else:
            form = text[pn[1]:pn[2]]
            if re.search(r"\(hide yes\)|\bhide\b", form):
                continue
            edits.append((pn[2] - 1, pn[2] - 1, " (hide yes)"))
        changed.append(lib_id)
    for a, z, s in sorted(edits, reverse=True):
        text = text[:a] + s + text[z:]
    if edits:
        sch_path.write_text(text, encoding="utf-8")
    return changed


# Landscape paper, smallest first (mm), and KiCad's default page layout:
# a 10 mm frame margin, the title block in the bottom-right corner.
PAPERS = (("A5", 210.0, 148.0), ("A4", 297.0, 210.0), ("A3", 420.0, 297.0),
          ("A2", 594.0, 420.0), ("A1", 841.0, 594.0), ("A0", 1189.0, 841.0))
FRAME = 10.0
TITLE_BLOCK = (110.0, 36.0)   # w, h of the default title block, mm
FIT_MARGIN = 5.08             # clear space kept around the drawing, mm


def drawn_bounds(sch_path: Path) -> tuple[float, float, float, float] | None:
    """(x0, y0, x1, y1) of everything drawn on the sheet: symbol bodies and
    their pin text, visible fields, wires, labels, junctions, sheets."""
    import schem_refdes as sr  # sibling script; deferred (shapely)
    from shapely.ops import unary_union
    sheet = sr.Sheet(sch_path)
    geoms = [g for _k, g, _n in sheet.statics]
    for sym in sheet.symbols:
        ob = sheet.body_obstacle(sym)
        if ob is not None:
            geoms.append(ob)
        for p in sym["props"].values():
            if p["hidden"] or p["at"] is None or not p["value"]:
                continue
            geoms.append(sr.text_box(p["value"], p["at"], p["size"],
                                     p["thick"], p["justify"], p["angle"]))
    geoms = [g for g in geoms if not g.is_empty]
    return unary_union(geoms).bounds if geoms else None


def _usable(w: float, h: float) -> tuple[float, float, float, float]:
    """The page area a drawing may use: inside the frame, above the title
    block (x0, y0, x1, y1)."""
    return (FRAME, FRAME, w - FRAME, h - FRAME - TITLE_BLOCK[1])


def fit_sheet(sch_path: Path, paper: str = "auto") -> dict:
    """Size the sheet to its drawing and centre the drawing on it. With
    paper "auto", the smallest landscape A-size whose area above the title
    block holds the drawing (plus FIT_MARGIN all round); any other paper is
    kept. Everything placed on the sheet moves by one grid-snapped offset,
    so connectivity is unchanged; the embedded library is not touched."""
    bb = drawn_bounds(sch_path)
    if bb is None:
        return {"paper": paper, "moved": [0.0, 0.0]}
    dw = bb[2] - bb[0] + 2 * FIT_MARGIN
    dh = bb[3] - bb[1] + 2 * FIT_MARGIN
    sizes = {n: (w, h) for n, w, h in PAPERS}
    if paper == "auto":
        paper = next((n for n, w, h in PAPERS
                      if dw <= w - 2 * FRAME
                      and dh <= h - 2 * FRAME - TITLE_BLOCK[1]),
                     PAPERS[-1][0])
    out = {"paper": paper, "drawing": [round(dw, 2), round(dh, 2)]}
    text = sch_path.read_text(encoding="utf-8")
    text = re.sub(r'\(paper "[^"]*"\)', f'(paper "{paper}")', text, count=1)
    dx = dy = 0.0
    if paper in sizes:
        ux0, uy0, ux1, uy1 = _usable(*sizes[paper])
        if dw <= ux1 - ux0 and dh <= uy1 - uy0:
            dx = snap((ux0 + ux1) / 2 - (bb[0] + bb[2]) / 2)
            dy = snap((uy0 + uy1) / 2 - (bb[1] + bb[3]) / 2)
    out["moved"] = [dx, dy]
    if dx or dy:
        text = _translate(text, dx, dy)
    sch_path.write_text(text, encoding="utf-8")
    return out


def _num(v: float) -> str:
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _translate(text: str, dx: float, dy: float) -> str:
    """Move every placed item of a .kicad_sch by (dx, dy): each point form
    outside lib_symbols (library coordinates are the symbol's own)."""
    pt = re.compile(r"\((at|xy|start|end|center|mid) (-?[\d.]+) (-?[\d.]+)")

    def mv(m):
        return (f"({m.group(1)} {_num(float(m.group(2)) + dx)} "
                f"{_num(float(m.group(3)) + dy)}")

    b0, b1 = _body(text)
    out, pos = [], 0
    for head, f0, f1 in _forms(text, b0, b1):
        if head in ("lib_symbols", "title_block", "paper", "uuid",
                    "sheet_instances", "embedded_fonts"):
            continue
        out.append(text[pos:f0])
        out.append(pt.sub(mv, text[f0:f1]))
        pos = f1
    out.append(text[pos:])
    return "".join(out)


def write_project(name: str, out_dir: Path | str) -> Path:
    """Minimal .kicad_pro. The project file is the ERC/DRC severity
    authority; keep it MINIMAL - unexpected keys can make KiCad reject the
    whole file and silently fall back to defaults (LEARNINGS [kicad]).
    Library-configuration checks are ignored on purpose: generated designs
    embed their symbols and must not break on library updates.

    An existing project file is MERGED, not replaced (#440): after P5 it
    carries board_init's design settings and stackup, rules_gen's net
    classes and the DRC floors, and a schematic rebuild must not wipe them.
    Every key already there is kept; the only thing the schematic forces is
    its ERC library-check severities. An unreadable file is replaced."""
    out_dir = Path(out_dir)
    pro = {
        "board": {
            "design_settings": {
                "rule_severities": {
                    "lib_footprint_issues": "ignore",
                    "lib_footprint_mismatch": "ignore",
                },
                "rules": {
                    "min_track_width": 0.127,
                },
            },
        },
        "erc": {
            "erc_exclusions": [],
            "meta": {"version": 0},
            "rule_severities": {
                "lib_symbol_issues": "ignore",
                "lib_symbol_mismatch": "ignore",
                "footprint_link_issues": "ignore",
            },
        },
        "meta": {"filename": f"{name}.kicad_pro", "version": 3},
        "schematic": {"legacy_lib_dir": "", "legacy_lib_list": []},
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{name}.kicad_pro"
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        old = None
    if isinstance(old, dict):
        forced = pro["erc"]["rule_severities"]
        pro = _merge_kept(pro, old)
        pro["erc"].setdefault("rule_severities", {}).update(forced)
        pro["meta"]["filename"] = f"{name}.kicad_pro"
    path.write_text(json.dumps(pro, indent=2) + "\n", encoding="utf-8")
    return path


def _merge_kept(new: dict, old: dict) -> dict:
    """new's keys deep-merged under old's: on a clash old wins."""
    out = dict(old)
    for k, v in new.items():
        if k not in out:
            out[k] = v
        elif isinstance(v, dict) and isinstance(out[k], dict):
            out[k] = _merge_kept(v, out[k])
    return out


def lib_units(lib_id: str) -> dict[int, list[tuple[str, float, float]]]:
    """{unit: [(pin number, x, y)]} of the library symbol `lib_id` as the
    kicad-sch-api cache has it (fplib.unit_pins; an `extends` alias reads
    its parent's), or {} when it is unknown. Unit 0: pins every unit shares."""
    try:
        with _quiet():
            cache = ksa.get_symbol_cache()
            sym = cache.get_symbol(lib_id)
            units = fplib.unit_pins(getattr(sym, "raw_kicad_data", None))
            if not units and getattr(sym, "extends", None):
                nick = lib_id.split(":", 1)[0]
                parent = cache.get_symbol(f"{nick}:{sym.extends}")
                units = fplib.unit_pins(getattr(parent, "raw_kicad_data", None))
    except Exception:  # noqa: BLE001 - unknown lib/symbol: no units
        return {}
    return units


def missing_units(sch_path: Path) -> dict[str, list[int]]:
    """{ref: [unit, ...]} for each part on a saved sheet drawn as several
    units (its embedded lib_symbols copy says how many) but not placed as
    all of them (#378): those units' pins are in no net, and KiCad's ERC
    says missing_unit. Power symbols (#...) are skipped."""
    top = next((f for f in _sexp(sch_path.read_text(encoding="utf-8"))
                if isinstance(f, list) and f and f[0] == "kicad_sch"), None)
    if top is None:
        return {}
    libs = _child(top, "lib_symbols") or []
    want = {}
    for s in libs[1:]:
        if isinstance(s, list) and len(s) > 1 and s[0] == "symbol":
            # sub-symbols NAME_<unit>_<body style>; unit 0 is shared
            n = {int(m.group(1)) for sub in s[2:]
                 if isinstance(sub, list) and len(sub) > 1
                 and sub[0] == "symbol"
                 for m in [re.search(r"_(\d+)_\d+$", _unq(sub[1]))]
                 if m and int(m.group(1)) > 0}
            if len(n) > 1:
                want[_unq(s[1])] = n
    have: dict[tuple[str, str], set[int]] = {}
    for f in top[1:]:
        if not (isinstance(f, list) and f and f[0] == "symbol"):
            continue
        lid, ref = _child(f, "lib_id"), _prop(f, "Reference") or ""
        if lid is None or ref.startswith("#") or _unq(lid[1]) not in want:
            continue
        unit = _child(f, "unit")
        have.setdefault((ref, _unq(lid[1])), set()).add(
            int(_unq(unit[1])) if unit else 1)
    return {ref: sorted(want[lid] - got)
            for (ref, lid), got in sorted(have.items()) if want[lid] - got}


def lib_footprint(lib_id: str) -> str | None:
    """The Footprint property of the library symbol `lib_id` as the
    kicad-sch-api cache has it (a project lib must be registered first:
    Sheet.add_component does), or None when it has none or is unknown."""
    try:
        with _quiet():
            sym = ksa.get_symbol_cache().get_symbol(lib_id)
    except Exception:  # noqa: BLE001 - unknown lib/symbol: no default
        return None
    for node in (getattr(sym, "raw_kicad_data", None) or []):
        if isinstance(node, list) and len(node) > 2 \
                and str(node[0]) == "property" and node[1] == "Footprint":
            val = str(node[2]).strip()
            if not val:
                return None
            nick = lib_id.split(":", 1)[0]
            return val if ":" in val or not nick else f"{nick}:{val}"
    return None


# bom_cpl's LCSC field names: `LCSC Part` (easyeda2kicad), `LCSC`,
# `LCSC Part #`, `LCSC#`
_LCSC_FIELD = re.compile(r"^LCSC(?:\s*PART)?(?:\s*(?:#|NO\.?|NUMBER))?$", re.I)


def lib_lcsc(lib_id: str) -> tuple[str, str] | None:
    """(field name, LCSC number) of the library symbol `lib_id`'s LCSC
    property as the kicad-sch-api cache has it, or None when it has none or
    is unknown."""
    try:
        with _quiet():
            sym = ksa.get_symbol_cache().get_symbol(lib_id)
    except Exception:  # noqa: BLE001 - unknown lib/symbol: no default
        return None
    for node in (getattr(sym, "raw_kicad_data", None) or []):
        if isinstance(node, list) and len(node) > 2 \
                and str(node[0]) == "property" \
                and _LCSC_FIELD.match(str(node[1]).strip()):
            val = str(node[2]).strip()
            if val:
                return str(node[1]), val
    return None


def lib_flags(lib_id: str) -> dict[str, bool]:
    """{"in_bom": bool, "on_board": bool} for the flags the library symbol
    `lib_id` states, as the kicad-sch-api cache has it; {} when it states
    none or is unknown."""
    try:
        with _quiet():
            sym = ksa.get_symbol_cache().get_symbol(lib_id)
    except Exception:  # noqa: BLE001 - unknown lib/symbol: no default
        return {}
    out = {}
    for node in (getattr(sym, "raw_kicad_data", None) or []):
        if isinstance(node, list) and len(node) == 2 \
                and str(node[0]) in ("in_bom", "on_board"):
            out[str(node[0])] = str(node[1]) != "no"
    return out


def pin_table(lib_id: str, lib_paths: list | None = None) -> dict:
    """Symbol pin table for grounding (number/name/type where available).

    lib_paths: extra .kicad_sym files/dirs to register first - kicad-sch-api's
    global cache never reads a project's sym-lib-table (S14 finding: project
    libs like `aiee:...` were invisible to --pins without this).
    """
    with _quiet():
        if lib_paths:
            cache = ksa.get_symbol_cache()
            for p in lib_paths:
                cache.add_library_path(str(p))
        env.register_stock_libs(ksa.get_symbol_cache(), [lib_id])
        sch =ksa.create_schematic("_pins")
        c = sch.components.add(lib_id, reference="X1", value="x",
                               position=(127.0, 63.5))
    unit_of = {num: u for u, ps in lib_units(lib_id).items()
               for num, _, _ in ps}
    pins = []
    for p in c.pins:
        row = {"number": p.number, "name": p.name}
        if p.number in unit_of:
            row["unit"] = unit_of[p.number]
        for attr in ("electrical_type", "type", "pin_type"):
            v = getattr(p, attr, None)
            if v is not None:
                row["type"] = str(getattr(v, "value", v))
                break
        pins.append(row)
    # units: how many a board places (unit 0 pins are on every one)
    units = len({u for u in unit_of.values() if u > 0}) or 1
    return {"lib_id": lib_id, "pin_count": len(pins), "units": units,
            "pins": pins}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pins", metavar="LIB_ID",
                    help="print the symbol's pin table as JSON")
    ap.add_argument("--lib", action="append", default=[],
                    help="extra .kicad_sym file/dir to register (repeatable; "
                    "project libs are invisible to the global cache)")
    ap.add_argument("--out", help="write JSON here instead of stdout")
    args = ap.parse_args(argv)
    if not args.pins:
        ap.error("nothing to do: give --pins LIB_ID")
    try:
        payload = {"script": _SCRIPT, "status": "pass",
                   **pin_table(args.pins, lib_paths=args.lib)}
    except Exception as exc:  # noqa: BLE001  (contract: any error -> exit 2)
        print(json.dumps({"script": _SCRIPT, "status": "error",
                          "error": f"{type(exc).__name__}: {exc}"}))
        return 2
    text = json.dumps(payload, indent=1, ensure_ascii=True)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
