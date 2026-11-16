"""check_si.py - signal integrity on the nets that need it (SPEC 6.3).

check_diffpair and check_return_path judge the nets a designer declared;
this check finds the nets that need signal-integrity care on its own, by
interface, and asks of each the questions Johnson's and Bogatin's rules ask:

 1. WHICH NETS. Each net is classed by its name (CLASSES below: oscillator
    and serial clocks, RMII, I2S, SPI, USB, Ethernet MDI) unless
    constraints.json["si"]["nets"] says otherwise. A clock's class follows
    its driver: an oscillator part (a Y/X/OSC part with a supply pin) on the
    net or behind its series resistor makes it osc_clock, else an MCU's
    clock; a net on a crystal (no supply pin) is a crystal net, a
    resonator's sine with no line rules but an analog victim to every
    aggressor. A class carries the fastest edge the interface's drivers
    make (t_rise_ns) - the edge, not the clock rate, sets every limit below.
 2. ELECTRICAL LENGTH. The net's line length is its longest pad-to-pad path
    on netconn's graph (branches don't add to it). Each part of the net is
    solved by lib/impedance.py's field solver for Z0 and eps_eff on the
    board's own stackup, so the delay is the board's, not a rule of thumb's.
    A net is lumped while its delay is under t_rise / 6 (Johnson & Graham,
    High-Speed Digital Design, sec. 1.6 / 4.1: the critical length); past it
    reflections are visible and the net is a transmission line.
 3. TERMINATION (si_unterminated_clock). A transmission-line net whose class
    clocks on its edges (a ring through the threshold is a double clock)
    needs a series resistor at its driver, R + R_drv ~ Z0 (Johnson,
    "Series Termination", sigcon.com). A 0-ohm link is no termination. A
    data net settles before it is sampled, so it is not asked.
 4. IMPEDANCE (si_impedance_step, si_impedance_target). On a transmission-
    line net, two parts of the net each at least a critical length long
    whose Z0 differ by more than STEP_GAMMA in reflection coefficient
    (a neck, a layer change onto a different stack) warn. A target -
    constraints si.nets[*].z0_ohm, or a pair class's Zdiff (USB 90,
    100BASE-TX 100) - missed by more than its tolerance warns, twice it errors.
 5. RETURN PATH. Every transmission-line net is put through
    check_return_path's corridor and return-via tests with its class's rise
    time (the via radius follows from t_rise), unless it is already declared
    in high_speed (check_return_path reports those itself). Its findings keep
    check_return_path's kinds (corridor_void, missing_return_via, ...) so
    their remediations reach the fixer; a corridor_void counts only where
    the trace's centreline crosses the gap MIN_CROSSING_MM or more. A net
    with a z0_ohm target over a hatched plane also gets check_return_path's
    hatch_impedance estimate, or its not_judged reason (#1010).
 6. CROSSTALK (si_crosstalk). Each fast net is an aggressor to every other
    signal net running beside it on the same layer within CROSSTALK_GAP_H x
    the dielectric height. Along the aggressor every SAMPLE_MM the nearest
    near-parallel victim track's edge gap is solved (two-line even/odd field
    solve, lib/impedance.coupling) for Bogatin's kb and kf, and
        NEXT = min(max kb, sum(kb dx) / L_sat),  L_sat = v t_r / 2
        FEXT = |sum(kf dx)| / (v t_r)
    (Bogatin, Signal and Power Integrity - Simplified, ch. 10; Johnson,
    "Crosstalk in Microstrip", sigcon.com). The larger, times the aggressor's
    swing, is held against the victim's budget: VICTIM_BUDGET of the swing
    for a digital victim (Bogatin's 5% of a noise budget), tighter for one
    the constraints or its name mark analog.
 7. TIMING (si_skew). Nets of one source-synchronous group (RMII TX, RMII
    RX with REF_CLK, I2S) are held to SKEW_UI of their unit interval in
    flight-time spread.

Every SI net's figures are in the report (`nets`), so a net that passes
still shows its length, delay, Z0 and critical length.

 8. CROSSTALK SIMULATED (si_crosstalk_sim). The top N estimates (--xtalk-sim N,
    si.xtalk_sim_top, default XTALK_SIM_TOP; 0 = none) at or past
    XTALK_SIM_FLOOR of their budget go through sim_si.simulate_pair: the routed
    pair as coupled lines with its real terminations. `crosstalk_sim` holds
    estimate vs simulated for each. The estimate is the launched-step formula
    at the full swing on matched ends, so the two differ by the driver's divider
    and by what the ends reflect. No ngspice here: `not_run` says so and the
    estimates stand.

CLI: --pcb board.kicad_pcb [--constraints constraints.json] [--out report.json]
     [--xtalk-sim N]. exit 0/1/2 per SPEC section 6.

constraints.json["si"] (all optional):
    {"nets": {"/ETH_CLK": {"class": "osc_clock", "t_rise_ns": 1.5,
                           "z0_ohm": 50, "z0_tol_pct": 15,
                           "driver_ohm": 20, "swing_v": 3.3}},
     "ignore": ["/SLOW_CLK"],          # not an SI net, whatever its name
     "victims": {"/VREF": "analog"},   # analog | digital | ignore
     "groups": [{"name": "rmii_tx", "nets": [...], "ui_ns": 20}],
     "xtalk_sim_top": 3}
    A net's entry also takes driver ("U1" or "U1.3"), c_in_pf, r_in_ohm and
    noise_margin_v for the simulation (sim_si.py).
"""
from __future__ import annotations

import argparse
import math
import re
import sys
from pathlib import Path

from shapely.geometry import Point
from shapely.strtree import STRtree

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import checklib  # noqa: E402
import geom  # noqa: E402
import impedance  # noqa: E402
import netconn  # noqa: E402
from checklib import CheckError, violation  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_return_path  # noqa: E402

SCRIPT = "check_si"
C_MM_PER_PS = 0.299792458
LUMPED_FRACTION = 6.0          # lumped while delay < t_rise / 6 (Johnson)
STEP_GAMMA = 0.10              # |reflection| between two long parts of a net
Z_TOL_PCT_SE = 20.0            # single-ended target tolerance
Z_TOL_PCT_DIFF = 15.0          # USB 2.0 90 ohm +-15%; 100BASE-TX 100 +-15%
DRIVER_OHM = 20.0              # CMOS output impedance a series R adds to
SERIES_MATCH_GAMMA = 0.35      # (R + R_drv) vs Z0 miss tolerated at the source
SAMPLE_MM = 0.25               # crosstalk sampling step along an aggressor
CROSSTALK_GAP_H = 6.0          # victims looked for within this x height
PARALLEL_DEG = 30.0            # victim within this of the aggressor's heading
GAP_BIN_MM = 0.05              # edge gaps are solved on this grid
SWING_V = 3.3
VICTIM_BUDGET = {"digital": 0.05, "analog": 0.01}
SKEW_UI = 0.10                 # flight-time spread allowed, fraction of UI
DEFAULT_ER = 4.5
MIN_CROSSING_MM = 0.25         # plane-gap crossing an inferred net is held to
XTALK_SIM_TOP = 3              # worst estimates simulated
XTALK_SIM_FLOOR = 0.25         # ... that reach this fraction of their budget

# class -> fastest edge and what the class asks. t_rise_ns is the 10-90 edge
# the class's drivers can make (an oscillator's ~1 ns, an MCU GPIO at full
# drive ~2 ns, USB FS's 4 ns minimum - USB 2.0 7.1.1.1, 100BASE-TX's 3 ns -
# ANSI X3.263 TP-PMD 9.1.6); clock: the net is edge-sensitive; zdiff: the
# pair's standard differential impedance; ui_ns: the unit interval a group's
# skew is held against.
CLASSES = {
    "crystal": {"t_rise_ns": 2.0, "aggressor": False, "victim": "analog"},
    "osc_clock": {"t_rise_ns": 1.0, "clock": True, "aggressor": True},
    "clock": {"t_rise_ns": 2.0, "clock": True, "aggressor": True},
    "rmii": {"t_rise_ns": 2.0, "aggressor": True, "ui_ns": 20.0},
    "i2s": {"t_rise_ns": 2.0, "aggressor": True},
    "spi": {"t_rise_ns": 2.0, "aggressor": True},
    "usb_fs": {"t_rise_ns": 4.0, "aggressor": True, "zdiff": 90.0},
    "usb_hs": {"t_rise_ns": 0.5, "aggressor": True, "zdiff": 90.0},
    "eth_mdi": {"t_rise_ns": 3.0, "aggressor": True, "zdiff": 100.0},
}

# (leaf-name regex, class), first match wins; the leaf is the net name after
# its last "/", upper-cased. I2C, MDC/MDIO, UART and reset lines are slow by
# design and match nothing.
NAME_RULES = [
    (r"^(ETH|RMII|REF|PHY|ENET)_?(REF_?)?CLK(_?IN|_?OUT)?$", "osc_clock"),
    (r"(^|_)(OSC|XO|TCXO)(_?OUT)?$", "osc_clock"),
    (r"(^|_)(MCLK|SYSCLK|CLKOUT|CLK_OUT|MCO\d?)$", "osc_clock"),
    (r"^RMII_(TXD\d|RXD\d|TX_?EN|CRS_?DV|RX_?ER|RX_?DV)$", "rmii"),
    (r"(^|_)(BCK|BCLK|SCK|SCLK|SPI_?CLK|CLK)$", "clock"),
    (r"^I2S\d?_", "i2s"),
    (r"(^|_)(LRCK|LRCLK|WS|SDATA|SDIN|SDOUT)$", "i2s"),
    (r"(^|_)(MOSI|MISO|SDO|SDI|COPI|CIPO)$", "spi"),
    (r"^USB\d?_?(D[PM]|DP|DN|D_?P|D_?N|D\+|D-)$", "usb_fs"),
    (r"^(ETH|PHY|MDI)\d?_?(TX|RX|TD|RD)[PN+-]$", "eth_mdi"),
    # 1000BASE-T's four bidirectional pairs A-D (#510): MDI1_A_P, BI_DA+,
    # TRD0_P, TXRX1P_A (Microchip switches), MDI0+ (Realtek), and the line
    # side after the magnetics as a port's L1_A_P
    (r"^((ETH|PHY)\d?_?)?MDI\d?_?([A-D]|[0-3])_?[PN+-]$", "eth_mdi"),
    (r"^((ETH|PHY)\d?_?)?(BI_?D[A-D][PN+-]|D[A-D][+-])$", "eth_mdi"),
    (r"^((ETH|PHY)\d?_?)?TRD\d_?[PN+-]$", "eth_mdi"),
    (r"^TXRX\d?_?[PM]_[A-D]$", "eth_mdi"),
    (r"^L\d_[A-D]_[PN]$", "eth_mdi"),
]

POWER_RE = re.compile(r"^(GND|AGND|DGND|PGND|EARTH|VSS|VCC|VDD|VBUS|VIN|VBAT|"
                      r"PVDD|AVDD|DVDD)|^([+-]?\d+V\d*[A-Z]?|\+.*)$", re.I)
ANALOG_RE = re.compile(r"(AUDIO|AMP_IN|_IN\d?[PMN]$|DAC[LR]_|ADC_|LIN_|LINE|"
                       r"MIC|VREF|REF[PQN]?$|SENSE|ISNS|ISENSE|OP[LR]_|"
                       r"(^|_)(MAIN|SUB\d?)_?(L|R|OUT|DAC)$|TAP)", re.I)


def leaf(net: str) -> str:
    return net.rsplit("/", 1)[-1].upper()


def classify(net: str) -> str | None:
    name = leaf(net)
    for rx, cls in NAME_RULES:
        if re.search(rx, name):
            return cls
    return None


def is_power(net: str, power_nets: set) -> bool:
    return net in power_nets or bool(POWER_RE.match(leaf(net)))


# ------------------------------------------------------------ line model

class LineModel:
    """Z0 and eps_eff per (layer, width) on the board's stackup, and the
    crosstalk coefficients per (layer, width, edge gap). Solves are cached
    (the field solver's own lru_cache keys the cross-section)."""

    def __init__(self, bg: geom.BoardGeom):
        self.bg = bg
        self.st = bg.stackup
        self.planar = len(bg.copper_layers) >= 2 and bool(self.st.dielectrics)
        self._cache: dict = {}

    def _geom(self, layer):
        st = self.st
        above, below = st.adjacent(layer)
        t = st.copper_thickness.get(layer, 0.035)
        if above is None or below is None:
            ref = below or above
            return ("ms", st.height_between(layer, ref), t,
                    st.epsilon_between(layer, ref))
        (hb, eb), (hf, ef) = impedance.stripline_sides(st.side(layer, above),
                                                       st.side(layer, below))
        return ("sl", hb, hf, t, eb, ef)

    def height(self, layer) -> float:
        if not self.planar:
            return self.bg.thickness
        g = self._geom(layer)
        return g[1] if g[0] == "ms" else min(g[1], g[2])

    def line(self, layer, w) -> dict:
        """{z0, eps_eff, tpd_ps_mm, model}."""
        key = ("line", layer, round(w, 4))
        if key in self._cache:
            return self._cache[key]
        out = None
        if self.planar:
            g = self._geom(layer)
            try:
                if g[0] == "ms":
                    r = impedance.field_microstrip(w, g[1], g[2], g[3])
                    model = "coated microstrip"
                else:
                    r = impedance.field_stripline(w, g[1], g[2], g[3], g[4], g[5])
                    model = "stripline"
                out = {"z0": r["z0"], "eps_eff": r["eps_eff"], "model": model}
            except (ValueError, RuntimeError, ZeroDivisionError):
                out = None
        if out is None:
            out = {"z0": None, "eps_eff": (DEFAULT_ER + 1) / 2, "model": "no plane"}
        out["tpd_ps_mm"] = math.sqrt(out["eps_eff"]) / C_MM_PER_PS
        self._cache[key] = out
        return out

    def coupled(self, layer, w, gap) -> dict | None:
        """Two identical lines w wide, `gap` edge to edge: kb, kf."""
        gap = max(GAP_BIN_MM, round(gap / GAP_BIN_MM) * GAP_BIN_MM)
        key = ("pair", layer, round(w, 4), round(gap, 4))
        if key in self._cache:
            return self._cache[key]
        out = None
        if self.planar:
            g = self._geom(layer)
            try:
                if g[0] == "ms":
                    r = impedance.field_microstrip(w, g[1], g[2], g[3], gap, even=True)
                else:
                    r = impedance.field_stripline(w, g[1], g[2], g[3], g[4], g[5],
                                                  gap, even=True)
                out = impedance.coupling(r)
            except (ValueError, RuntimeError, ZeroDivisionError):
                out = None
        self._cache[key] = out
        return out


# ------------------------------------------------------------ per net

def line_length(bg, net) -> tuple[float, bool]:
    """Longest pad-to-pad path on the net's copper graph; (total copper,
    False) when fewer than two pads connect."""
    pads = bg.pads_of(net)
    g = netconn.build(bg, net)
    nodes = list(dict.fromkeys(n for n in (netconn.pad_node(g, p) for p in pads) if n))
    best = None
    for i, a in enumerate(nodes):
        for b in nodes[i + 1:]:
            d = netconn.shortest_path_len(g, a, b)
            if d is not None and (best is None or d > best):
                best = d
    if best is None:
        return sum(t.length for t in bg.tracks_of(net)), False
    return best, True


def parts_of(bg, lm: LineModel, net) -> list[dict]:
    """The net's copper grouped by (layer, width), longest first."""
    by: dict = {}
    for t in bg.tracks_of(net):
        k = (t.layer, round(t.width, 3))
        by[k] = by.get(k, 0.0) + t.length
    out = []
    for (layer, w), length in sorted(by.items(), key=lambda kv: -kv[1]):
        ln = lm.line(layer, w)
        out.append({"layer": layer, "width_mm": w, "length_mm": length,
                    "z0": ln["z0"], "eps_eff": ln["eps_eff"],
                    "tpd_ps_mm": ln["tpd_ps_mm"], "model": ln["model"]})
    return out


def ohms(value) -> float | None:
    """A resistor's Value field in ohms: 33, 33R, 4R7, 1k, 0R, 0."""
    if not value:
        return None
    s = str(value).strip().upper().replace("Ω", "").replace("OHM", "")
    s = s.split()[0] if s.split() else s
    m = re.match(r"^(\d*)([RKM])(\d*)$", s)
    if m and (m.group(1) or m.group(3)):
        mult = {"R": 1.0, "K": 1e3, "M": 1e6}[m.group(2)]
        return float(f"{m.group(1) or 0}.{m.group(3) or 0}") * mult
    m = re.match(r"^(\d+(?:\.\d+)?)([KM]?)", s)
    if m:
        return float(m.group(1)) * {"": 1.0, "K": 1e3, "M": 1e6}[m.group(2)]
    return None


def series_resistors(bg, net, power_nets) -> list[dict]:
    """Two-pin resistors with one pin on `net` and the other on another
    signal net: in series with the line (a pull-up's far pin is a rail)."""
    out = []
    for p in bg.pads_of(net):
        if not re.match(r"^R\d", p.ref):
            continue
        pads = bg.pads_of(ref=p.ref)
        if len(pads) != 2:
            continue
        other = next(q for q in pads if q is not p)
        if not other.net or other.net == net or is_power(other.net, power_nets):
            continue
        out.append({"ref": p.ref, "other_net": other.net,
                    "value": (bg.footprints.get(p.ref) or {}).get("value"),
                    "ohm": ohms((bg.footprints.get(p.ref) or {}).get("value")),
                    "pos": p.center})
    return out


def rep_point(bg, net):
    tks = bg.tracks_of(net)
    if tks:
        c = max(tks, key=lambda t: t.length).shape.interpolate(0.5, normalized=True)
        return (c.x, c.y)
    pads = bg.pads_of(net)
    return pads[0].center if pads else None


# ------------------------------------------------------------ crosstalk

def _heading(line, d):
    a = line.interpolate(max(0.0, d - 0.05))
    b = line.interpolate(min(line.length, d + 0.05))
    return math.atan2(b.y - a.y, b.x - a.x)


def _seg_heading(line, pt):
    """Heading of the straight piece of `line` nearest pt."""
    d = line.project(pt)
    return _heading(line, d)


def crosstalk(bg, lm: LineModel, aggressor: str, t_r_ps: float, victims_ok,
              index) -> dict:
    """Victim net -> {next, fext, coupled_mm, min_gap_mm, layer, pos}."""
    acc: dict = {}
    for at in bg.tracks_of(aggressor):
        layer = at.layer
        tree, items = index.get(layer, (None, []))
        if tree is None or at.length <= 0:
            continue
        h = lm.height(layer)
        reach = CROSSTALK_GAP_H * h + at.width
        v_mm_ps = 1.0 / lm.line(layer, at.width)["tpd_ps_mm"]
        n = max(1, int(at.length / SAMPLE_MM))
        dx = at.length / n
        for i in range(n):
            d = (i + 0.5) * dx
            pt = at.shape.interpolate(d)
            ha = _heading(at.shape, d)
            best: dict = {}
            for j in tree.query(pt.buffer(reach + 0.5)):
                vt = items[int(j)]
                if vt.net == aggressor or not victims_ok(vt.net):
                    continue
                dist = vt.shape.distance(pt)
                gap = dist - (at.width + vt.width) / 2.0
                if gap <= 0 or gap > reach:
                    continue
                hv = _seg_heading(vt.shape, pt)
                dang = abs((math.degrees(ha - hv) + 90.0) % 180.0 - 90.0)
                if dang > PARALLEL_DEG:
                    continue
                if vt.net not in best or gap < best[vt.net]:
                    best[vt.net] = gap
            for vnet, gap in best.items():
                c = lm.coupled(layer, at.width, gap)
                if c is None:
                    continue
                a = acc.setdefault(vnet, {"kb_dx": 0.0, "kf_dx": 0.0, "kb_max": 0.0,
                                          "coupled_mm": 0.0, "min_gap_mm": gap,
                                          "layer": layer, "pos": (pt.x, pt.y),
                                          "v": v_mm_ps})
                a["kb_dx"] += c["kb"] * dx
                a["kf_dx"] += c["kf"] * dx
                a["coupled_mm"] += dx
                if c["kb"] > a["kb_max"]:
                    a["kb_max"] = c["kb"]
                if gap < a["min_gap_mm"]:
                    a.update(min_gap_mm=gap, layer=layer, pos=(pt.x, pt.y), v=v_mm_ps)
    out = {}
    for vnet, a in acc.items():
        l_sat = a["v"] * t_r_ps / 2.0
        nxt = min(a["kb_max"], a["kb_dx"] / l_sat)
        fxt = abs(a["kf_dx"]) / (a["v"] * t_r_ps)
        out[vnet] = {"next": nxt, "fext": fxt, "coupled_mm": a["coupled_mm"],
                     "min_gap_mm": a["min_gap_mm"], "layer": a["layer"],
                     "pos": a["pos"]}
    return out


# ------------------------------------------------------------ the check

OSC_REF = re.compile(r"^(Y|X|XO|OSC|XTAL)\d")


def oscillator_parts(bg, net, power_nets) -> tuple[list, list]:
    """(oscillators, crystals) on the net, directly or through a series
    resistor (Y2 -> R60 -> /ETH_CLK). A Y/X part with a pin on a supply
    rail is an oscillator driving a logic edge; one without is a crystal
    (its pins are the resonator and GND)."""
    nets = [net] + [s["other_net"] for s in series_resistors(bg, net, power_nets)]
    osc, xtal = [], []
    for n in nets:
        for p in bg.pads_of(n):
            if not OSC_REF.match(p.ref) or p.ref in osc + xtal:
                continue
            supplied = any(q.net and is_power(q.net, power_nets)
                           and not re.match(r"^(A|D|P)?GND", leaf(q.net))
                           for q in bg.pads_of(ref=p.ref))
            (osc if supplied else xtal).append(p.ref)
    return osc, xtal


def resolve(bg, cons, power_nets=frozenset()) -> dict:
    """net -> spec (class defaults, then the constraints' own entry). A
    clock's class follows its driver: an oscillator's edge is ~1 ns, an
    MCU pin's ~2 ns, whatever the net is called."""
    si = (cons or {}).get("si") or {}
    ignore = set(si.get("ignore") or [])
    given = si.get("nets") or {}
    out = {}
    for net in sorted(n for n in bg.nets if n):
        if net in ignore:
            continue
        spec = dict(given.get(net) or {})
        if net not in given and is_power(net, power_nets):
            continue
        cls = spec.get("class") or classify(net)
        if not spec.get("class") and (cls in ("osc_clock", "clock") or cls is None):
            osc, xtal = oscillator_parts(bg, net, power_nets)
            if xtal and not osc:
                cls = "crystal"
            elif cls is not None:
                cls = "osc_clock" if osc else "clock"
        if cls is None and net not in given:
            continue
        base = dict(CLASSES.get(cls, {"t_rise_ns": 2.0, "aggressor": True}))
        base.update(spec)
        base["class"] = cls or "declared"
        out[net] = base
    return out


def victim_class(net, si, power_nets, specs=None) -> str:
    v = (si.get("victims") or {}).get(net) or ((specs or {}).get(net) or {}).get("victim")
    if v:
        return v
    if is_power(net, power_nets):
        return "ignore"
    return "analog" if ANALOG_RE.search(leaf(net)) else "digital"


def pair_of(net, specs) -> str | None:
    """The other half of a differential-class net, by name."""
    name = net
    for a, b in (("DP", "DM"), ("DP", "DN"), ("D+", "D-"), ("TXP", "TXN"),
                 ("RXP", "RXN"), ("TDP", "TDN"), ("RDP", "RDN"), ("TX+", "TX-"),
                 ("RX+", "RX-"), ("_P", "_N"), ("+", "-"), ("P", "N")):
        for x, y in ((a, b), (b, a)):
            if name.upper().endswith(x):
                cand = name[: -len(x)] + (y if name[-len(x):].isupper() else y.lower())
                if cand in specs:
                    return cand
    # a pair letter after the polarity: TXRX1P_A / TXRX1M_A (#510)
    m = re.search(r"([PMpm])(_[A-Da-d])$", name)
    if m:
        y = {"P": "M", "M": "P", "p": "m", "m": "p"}[m.group(1)]
        cand = name[: m.start()] + y + m.group(2)
        if cand in specs:
            return cand
    return None


def run(argv=None):
    ap = argparse.ArgumentParser(description="Signal integrity of the nets that need it.")
    ap.add_argument("--pcb", required=True)
    ap.add_argument("--constraints", help="constraints.json (optional si block)")
    ap.add_argument("--out")
    ap.add_argument("--xtalk-sim", type=int, metavar="N",
                    help="simulate the N worst crosstalk estimates (0 = none)")
    args = ap.parse_args(argv)

    cons = checklib.load_json(args.constraints, "constraints") \
        if args.constraints and Path(args.constraints).is_file() else {}
    bg = geom.load_board(Path(args.pcb))
    si = cons.get("si") or {}
    power_nets = {p.get("net") for p in cons.get("power", []) if isinstance(p, dict)}
    # a teardrop is a zone too, but not a pour: counting it made every
    # teardropped signal net a power net and skipped it (#633)
    power_nets |= {z.net for z in bg.zones_of() if z.net and not z.teardrop}
    lm = LineModel(bg)
    specs = resolve(bg, cons, power_nets)
    declared_hs = {e.get("net") for e in cons.get("high_speed", []) if isinstance(e, dict)}

    index = {}
    for layer in bg.copper_layers:
        items = [t for t in bg.tracks_of(layer=layer) if t.length > 0]
        index[layer] = (STRtree([t.shape for t in items]) if items else None, items)

    violations: list[dict] = []
    nets_out: list[dict] = []
    done_pairs: set = set()
    estimates: list[dict] = []
    not_judged: list[str] = []
    for net, spec in specs.items():
        if not bg.tracks_of(net):
            not_judged.append(f"{net} ({spec['class']}): not routed")
            continue
        if spec["class"] == "crystal":
            # a resonator's sine, not an edge: no line rules, but every
            # aggressor is held to the analog budget beside it
            nets_out.append({"net": net, "class": "crystal",
                             "length_mm": checklib.rnd(sum(t.length for t in bg.tracks_of(net)), 2),
                             "vias": len(bg.vias_of(net))})
            continue
        t_r_ps = float(spec["t_rise_ns"]) * 1000.0
        parts = parts_of(bg, lm, net)
        length, ok = line_length(bg, net)
        tot = sum(p["length_mm"] for p in parts) or 1.0
        tpd = sum(p["tpd_ps_mm"] * p["length_mm"] for p in parts) / tot
        delay = length * tpd
        l_crit = t_r_ps / (LUMPED_FRACTION * tpd)
        tline = length > l_crit
        main = parts[0]
        pos = rep_point(bg, net)
        series = series_resistors(bg, net, power_nets)
        facts = {"net": net, "class": spec["class"], "t_rise_ns": spec["t_rise_ns"],
                 "length_mm": checklib.rnd(length, 2), "path": "pad-to-pad" if ok else "total copper",
                 "delay_ps": checklib.rnd(delay, 1), "critical_mm": checklib.rnd(l_crit, 1),
                 "transmission_line": tline,
                 "z0_ohm": checklib.rnd(main["z0"], 1) if main["z0"] else None,
                 "model": main["model"],
                 "parts": [{"layer": p["layer"], "width_mm": p["width_mm"],
                            "length_mm": checklib.rnd(p["length_mm"], 2),
                            "z0_ohm": checklib.rnd(p["z0"], 1) if p["z0"] else None}
                           for p in parts],
                 "series_r": [{k: v for k, v in s.items() if k != "pos"} for s in series]}
        common = {"si_class": spec["class"], "length_mm": checklib.rnd(length, 2),
                  "critical_mm": checklib.rnd(l_crit, 1), "t_rise_ns": spec["t_rise_ns"]}

        # 3. termination of an edge-sensitive transmission line
        if tline and spec.get("clock") and main["z0"]:
            real = [s for s in series if s["ohm"] is None or s["ohm"] > 1.0]
            r_drv = float(spec.get("driver_ohm", DRIVER_OHM))
            if not real:
                violations.append(violation(
                    SCRIPT, "warning", pos, main["layer"], net, [s["ref"] for s in series],
                    f"{net} ({spec['class']}, t_r {spec['t_rise_ns']} ns) runs "
                    f"{length:.0f} mm, past its {l_crit:.0f} mm critical length, with "
                    f"no series termination: its edges ring and can double-clock. "
                    f"Put ~{max(0.0, main['z0'] - r_drv):.0f} ohm in series at the "
                    f"driver (Z0 {main['z0']:.0f} ohm less ~{r_drv:.0f} ohm driver)",
                    SCRIPT, kind="si_unterminated_clock", z0_ohm=checklib.rnd(main["z0"], 1),
                    **common))
                facts["termination"] = "none"
            else:
                r = real[0]
                if r["ohm"] is not None:
                    src = r["ohm"] + r_drv
                    gam = (src - main["z0"]) / (src + main["z0"])
                    facts["termination"] = {"ref": r["ref"], "ohm": r["ohm"],
                                            "source_gamma": checklib.rnd(gam, 3)}
                    if abs(gam) > SERIES_MATCH_GAMMA:
                        violations.append(violation(
                            SCRIPT, "warning", r["pos"], main["layer"], net, [r["ref"]],
                            f"{net}: series {r['ref']} {r['value']} plus ~{r_drv:.0f} ohm "
                            f"driver is {src:.0f} ohm against a {main['z0']:.0f} ohm line "
                            f"(source reflection {gam:+.2f}); make it "
                            f"~{max(0.0, main['z0'] - r_drv):.0f} ohm",
                            SCRIPT, kind="si_series_mismatch", **common))
                else:
                    facts["termination"] = {"ref": r["ref"], "ohm": None}

        # 4. impedance steps and targets
        if tline:
            long_parts = [p for p in parts if p["z0"] and p["length_mm"] >= l_crit]
            if len(long_parts) >= 2:
                zs = sorted(long_parts, key=lambda p: p["z0"])
                lo, hi = zs[0], zs[-1]
                gam = (hi["z0"] - lo["z0"]) / (hi["z0"] + lo["z0"])
                facts["impedance_step_gamma"] = checklib.rnd(gam, 3)
                if gam > STEP_GAMMA:
                    violations.append(violation(
                        SCRIPT, "warning", pos, hi["layer"], net, [],
                        f"{net}: {lo['width_mm']} mm on {lo['layer']} is "
                        f"{lo['z0']:.0f} ohm and {hi['width_mm']} mm on {hi['layer']} "
                        f"{hi['z0']:.0f} ohm, both longer than the {l_crit:.0f} mm "
                        f"critical length: a {gam:.2f} reflection at the step",
                        SCRIPT, kind="si_impedance_step", gamma=checklib.rnd(gam, 3),
                        **common))
            target = spec.get("z0_ohm")
            if target and main["z0"]:
                tol = float(spec.get("z0_tol_pct", Z_TOL_PCT_SE))
                miss = 100.0 * (main["z0"] - float(target)) / float(target)
                if abs(miss) > tol:
                    violations.append(violation(
                        SCRIPT, "error" if abs(miss) > 2 * tol else "warning", pos,
                        main["layer"], net, [],
                        f"{net}: {main['width_mm']} mm on {main['layer']} is "
                        f"{main['z0']:.1f} ohm against its {float(target):.0f} ohm "
                        f"target ({miss:+.0f}%, tolerance {tol:.0f}%)",
                        SCRIPT, kind="si_impedance_target",
                        z0_ohm=checklib.rnd(main["z0"], 1), target_ohm=float(target),
                        miss_pct=checklib.rnd(miss, 1), **common))
            mate = pair_of(net, specs) if spec.get("zdiff") else None
            if spec.get("zdiff") and not mate:
                not_judged.append(f"{net} ({spec['class']}): has a Zdiff "
                                  "target but no other half by name, so no "
                                  "Zdiff was judged")
            if mate and tuple(sorted((net, mate))) not in done_pairs:
                done_pairs.add(tuple(sorted((net, mate))))
                if bg.tracks_of(mate):
                    violations.extend(pair_target(bg, net, mate, spec, common, pos))
                else:
                    not_judged.append(f"{net}/{mate}: {mate} is not routed, "
                                      "so no Zdiff was judged")

        # 5. return path of a transmission line
        if tline and net not in declared_hs and lm.planar:
            entry = {"net": net, "t_rise_ns": spec["t_rise_ns"],
                     "reference": spec.get("reference", "GND")}
            if spec.get("z0_ohm"):
                # over a hatch, check_return_path estimates the Z0 the
                # solid-plane line model above cannot see (#1010)
                entry.update(z0_ohm=spec["z0_ohm"], z0_tol_pct=float(
                    spec.get("z0_tol_pct", Z_TOL_PCT_SE)))
            try:
                vs, rp = check_return_path.check_net(bg, entry, check_return_path.K_DEFAULT)
            except CheckError:
                vs, rp = [], None
            # the corridor's flanks brushing a void are not a crossing: an
            # inferred net is held to a plane gap its centreline really
            # crosses (check_return_path holds a declared net to more)
            vs = [v for v in vs if v.get("kind") != "corridor_void"
                  or v.get("crossing_len_mm", 0.0) >= MIN_CROSSING_MM]
            for v in vs:
                v["check"] = v["source"] = SCRIPT
                v.update(si_class=spec["class"])
            violations.extend(vs)
            facts["return_via_radius_mm"] = rp and rp.get("return_via_radius_mm")
            if rp and spec.get("z0_ohm"):
                not_judged.extend(rp.get("not_judged", []))

        # 6. crosstalk from this net
        if spec.get("aggressor", True):
            swing = float(spec.get("swing_v", SWING_V))
            xt = crosstalk(bg, lm, net, t_r_ps,
                           lambda v: victim_class(v, si, power_nets, specs) != "ignore", index)
            worst = []
            for vnet, x in sorted(xt.items(), key=lambda kv: -max(kv[1]["next"], kv[1]["fext"])):
                noise = max(x["next"], x["fext"])
                vcls = victim_class(vnet, si, power_nets, specs)
                budget = VICTIM_BUDGET.get(vcls, VICTIM_BUDGET["digital"])
                rec = {"victim": vnet, "victim_class": vcls,
                       "next_pct": checklib.rnd(100 * x["next"], 2),
                       "fext_pct": checklib.rnd(100 * x["fext"], 2),
                       "noise_mv": checklib.rnd(1000 * swing * noise, 1),
                       "coupled_mm": checklib.rnd(x["coupled_mm"], 1),
                       "min_gap_mm": checklib.rnd(x["min_gap_mm"], 3)}
                if len(worst) < 5:
                    worst.append(rec)
                estimates.append({"aggressor": net, "victim": vnet, "budget": budget,
                                  "noise": noise, "swing": swing, "next": x["next"],
                                  "fext": x["fext"], "pos": x["pos"], "layer": x["layer"]})
                if noise > budget:
                    violations.append(violation(
                        SCRIPT, "warning", x["pos"], x["layer"], vnet, [],
                        f"{net} ({spec['class']}, t_r {spec['t_rise_ns']} ns) couples "
                        f"{100 * noise:.1f}% of its {swing:g} V swing "
                        f"(~{1000 * swing * noise:.0f} mV; NEXT {100 * x['next']:.1f}%, "
                        f"FEXT {100 * x['fext']:.1f}%) into {vcls} {vnet} over "
                        f"{x['coupled_mm']:.1f} mm on {x['layer']}, "
                        f"{x['min_gap_mm']:.2f} mm apart at the closest; the "
                        f"budget is {100 * budget:.0f}%. Space them out (3x the "
                        f"height to the plane) or shorten the run",
                        SCRIPT, kind="si_crosstalk", aggressor=net, **rec,
                        budget_pct=100 * budget))
            facts["crosstalk"] = worst
        nets_out.append(facts)

    # 7. group timing
    groups = list(si.get("groups") or []) or auto_groups(specs)
    by_net = {f["net"]: f for f in nets_out}
    group_out = []
    for g in groups:
        members = [by_net[n] for n in g.get("nets", []) if n in by_net]
        if len(members) < 2 or not g.get("ui_ns"):
            continue
        delays = {m["net"]: m["delay_ps"] for m in members}
        spread = max(delays.values()) - min(delays.values())
        budget = SKEW_UI * float(g["ui_ns"]) * 1000.0
        group_out.append({"name": g.get("name"), "ui_ns": g["ui_ns"],
                          "spread_ps": checklib.rnd(spread, 1),
                          "budget_ps": checklib.rnd(budget, 1)})
        if spread > budget:
            slow = max(delays, key=delays.get)
            violations.append(violation(
                SCRIPT, "warning", rep_point(bg, slow), None, slow, [],
                f"group {g.get('name')}: flight times spread {spread:.0f} ps, past "
                f"{100 * SKEW_UI:.0f}% of its {g['ui_ns']} ns unit interval "
                f"({budget:.0f} ps); {slow} is the slowest",
                SCRIPT, kind="si_skew", group=g.get("name"),
                spread_ps=checklib.rnd(spread, 1), budget_ps=checklib.rnd(budget, 1)))

    # 8. the worst estimates, simulated
    top = args.xtalk_sim if args.xtalk_sim is not None else si.get("xtalk_sim_top", XTALK_SIM_TOP)
    sim_out = simulate_worst(bg, lm, estimates, int(top), specs, si, power_nets, violations,
                             not_judged)

    payload = checklib.report(SCRIPT, args.pcb, violations, nets=nets_out,
                              groups=group_out, stackup_assumed=bool(bg.stackup.assumed),
                              crosstalk_sim=sim_out, not_judged=not_judged,
                              nothing_to_judge=None if nets_out else
                              "no classified net is routed" if specs else
                              "no net matches a signal class by name or si.nets")
    return payload, args.out


def simulate_worst(bg, lm, estimates, top, specs, si, power_nets, violations,
                   not_judged=None) -> dict:
    """Module doc item 8: estimate vs simulated for the top pairs. A deck that
    runs past its limit (a loaded machine) gets one retry at sim_si's longer
    limit; a pair the simulator still couldn't run goes in not_judged, so a
    lost simulation is never silent (#1377)."""
    if top <= 0:
        return {"not_run": "off"}
    worst = sorted((e for e in estimates
                    if e["noise"] >= XTALK_SIM_FLOOR * e["budget"]
                    and pair_of(e["aggressor"], specs) != e["victim"]),
                   key=lambda e: (-e["noise"] / e["budget"], e["aggressor"], e["victim"]))[:top]
    if not worst:
        return {"pairs": []}
    import sim_si
    import solvers
    why = sim_si.available()
    if why:
        return {"not_run": why, "pairs_waiting": [[e["aggressor"], e["victim"]] for e in worst]}
    pairs = []
    for e in worst:
        a, v = e["aggressor"], e["victim"]
        rec = {"aggressor": a, "victim": v,
               "estimate_mv": checklib.rnd(1000 * e["swing"] * e["noise"], 1),
               "estimate_next_mv": checklib.rnd(1000 * e["swing"] * e["next"], 1),
               "estimate_fext_mv": checklib.rnd(1000 * e["swing"] * e["fext"], 1)}
        failed = None
        try:
            try:
                r = sim_si.simulate_pair(bg, lm, a, v, specs, si, power_nets)
            except solvers.SolverTimeout as ex:
                rec["retried"] = str(ex)[:200]
                r = sim_si.simulate_pair(bg, lm, a, v, specs, si, power_nets,
                                         timeout=sim_si.DECK_RETRY_S)
        except (CheckError, solvers.SolverError, ValueError, FloatingPointError) as ex:
            failed = str(ex)[:300]
            r = {"not_simulated": failed}
        if failed is not None and not_judged is not None:
            not_judged.append(f"{a} -> {v}: crosstalk not simulated ({failed}); "
                              f"only the estimate judged it")
        rec.update(r)
        if "noise_v" in r:
            rec.pop("noise_v")
            rec["sim_over_estimate"] = checklib.rnd(r["noise_mv"] / rec["estimate_mv"], 3) \
                if rec["estimate_mv"] else None
            if r["over_budget"]:
                far = max(r["ends"].items(), key=lambda kv: abs(kv[1]["peak_mv"])
                          if kv[1]["receiver"] else -1.0)
                over = "past" if r["over_margin"] else "inside"
                violations.append(violation(
                    SCRIPT, "warning", e["pos"], e["layer"], v, [],
                    f"{a} (t_r {r['t_rise_ns']:g} ns, {r['swing_v']:g} V) puts "
                    f"{r['noise_mv']:.0f} mV on {v} at receiver {far[0]}, simulated with "
                    f"the routed terminations (estimate {rec['estimate_mv']:.0f} mV): over "
                    f"the {r['budget_mv']:.0f} mV budget and {over} the "
                    f"{r['margin_mv']:.0f} mV noise margin. Space them out or shorten "
                    f"the {r['coupled_mm']:.0f} mm run", SCRIPT, kind="si_crosstalk_sim",
                    aggressor=a, noise_mv=r["noise_mv"], estimate_mv=rec["estimate_mv"],
                    budget_mv=r["budget_mv"], margin_mv=r["margin_mv"],
                    over_margin=r["over_margin"], coupled_mm=r["coupled_mm"]))
        pairs.append(rec)
    return {"pairs": pairs}


def auto_groups(specs) -> list[dict]:
    """RMII's TX and RX halves, each with the reference clock."""
    clk = [n for n, s in specs.items() if re.search(r"(REF_?)?CLK", leaf(n))
           and s["class"] == "osc_clock" and re.match(r"(ETH|RMII|REF|PHY|ENET)", leaf(n))]
    out = []
    for half in ("TX", "RX"):
        nets = [n for n, s in specs.items() if s["class"] == "rmii"
                and (leaf(n).startswith(f"RMII_{half}")
                     or (half == "RX" and "CRS" in leaf(n)))]
        if nets:
            out.append({"name": f"rmii_{half.lower()}", "nets": nets + clk,
                        "ui_ns": CLASSES["rmii"]["ui_ns"]})
    return out


def pair_target(bg, p, n, spec, common, pos) -> list[dict]:
    """A transmission-line differential pair against its standard Zdiff."""
    import check_diffpair
    _, _, stats = check_diffpair.coupling_stats(bg, p, n, 1.0)
    gap = stats.get("nominal_gap_mm")
    imp = check_diffpair.pair_impedance(bg, p, n, gap)
    z = imp.get("zdiff_ohm")
    target = float(spec.get("zdiff_ohm", spec["zdiff"]))
    tol = float(spec.get("zdiff_tol_pct", Z_TOL_PCT_DIFF))
    if z is None:
        return [violation(
            SCRIPT, "warning", pos, imp.get("signal_layer"), p, [],
            f"{p}/{n} are a transmission line ({common['length_mm']} mm, critical "
            f"{common['critical_mm']} mm) but are not routed as a coupled pair "
            f"({imp.get('note', 'no gap')}): their {target:.0f} ohm differential "
            f"impedance is not held", SCRIPT, kind="si_pair_uncoupled", pair=[p, n],
            **common)]
    miss = 100.0 * (z - target) / target
    if abs(miss) <= tol:
        return []
    return [violation(
        SCRIPT, "error" if abs(miss) > 2 * tol else "warning", pos,
        imp.get("signal_layer"), p, [],
        f"{p}/{n}: {imp['width_mm']} mm / {imp.get('gap_mm')} mm gap on "
        f"{imp['signal_layer']} is {z:.0f} ohm differential against "
        f"{target:.0f} ({miss:+.0f}%, tolerance {tol:.0f}%)", SCRIPT,
        kind="si_impedance_target", pair=[p, n], zdiff_ohm=z, target_ohm=target,
        miss_pct=checklib.rnd(miss, 1), **common)]


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())
