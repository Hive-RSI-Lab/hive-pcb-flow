"""check_pdn_z.py - plane-pair cavity PDN impedance |Z|(f) (layout sim).

Rectangular plane-pair cavity-resonator model (Lei/Techentin/Gilbert TMTT
47(5) 1999; Novak & Miller ch.4; Xu/Wang/Fan NSF 10213240 - formulas
reimplemented from the published equations, no third-party code):

  Z_ij(w) = (j*w*mu0*d / (a*b)) * SUM_{m=0..M} SUM_{n=0..N}
            [ c_m^2 * c_n^2 * f_i * f_j ] / ( k_xm^2 + k_yn^2 - k^2 )

  k_xm = m*pi/a ; k_yn = n*pi/b ; c_m^2 = 1 (m=0) else 2  => weights 1/2/4
  f_i  = cos(k_xm*x_i)*cos(k_yn*y_i)*sinc(k_xm*t_xi/2)*sinc(k_yn*t_yi/2)
  sinc(x) = sin(x)/x, sinc(0) = 1 (point ports: sinc -> 1)

Light-loss wavenumber with the MANDATORY low-frequency clip (Novak-Miller
eq 4.29) - without the clip the low end departs from the true 1/(j*w*C00):

  k = w*sqrt(mu0*eps0*eps_r) * (1 - j*(tan_delta + delta_mod/d)/2)
  delta_s   = sqrt(2/(w*mu0*sigma_cu)),  sigma_cu = 5.8e7 S/m
  delta_mod = 1/(1/delta_s + 1/t),  t = plane copper thickness

The (0,0) term is exactly 1/(j*w*C00), C00 = eps0*eps_r*a*b/d. The model is
quasi-static (only (m,n,0) modes) - valid through the whole PCB PDN band for
0.1-1 mm gaps - and non-causal (fine for |Z|(f) profiles).

TRUNCATION: M=N = max(mode-coverage bound for f_max with 2.5x margin, 30);
peaks converge fast, MINIMA slowly, so the sum is recomputed once at 2M/2N
and the first-impedance-minimum shift is reported (warning kind
cavity_unconverged above 5%).

PLANE PAIRS (v1): adjacent copper layers where two DIFFERENT nets both carry
zone fills >= 15% of the board outline area; one of them must be a rail named
in decoupling.json. a x b x d from geom's stackup.

REAL SHAPES (#498, lib/planecav.py). A pair whose overlap fills its bounding
box (planecav.RECT_FILL, on a raster that ignores antipads) keeps the closed
form above: `"geometry": "bounding_rect_assumed"`, same numbers as before. Any
other (an L, a cut-out, a split) is cut into rectangles and joined by
segmentation, on the separate piece of the overlap under the observation port:
`"geometry": "segmented"`. A decap off that piece joins at its nearest point
through SPREAD_NH_PER_MM of path. Such a pair also reports `bounding_rect`,
what the old model says, and a verdict that moves gets a warning,
pdn_verdict_moved, so it never moves silently. `shape.convergence` compares
against a run with half the cells and strips and twice the modes (warning
segmentation_unconverged). Every pair reports `cavity_modes`: frequency,
antinode, and |V| at the observation port and decaps relative to the antinode.
`--palace-check` also runs Palace eigenmode on the true polygon of each
segmented pair (slow; warning palace_disagrees over PALACE_TOL_PCT).

PORTS: port 0 = the observation port at the served IC (the most-associated IC
for the rail in decoupling.json; position = centroid of its associated rail
pin pads), then one port per decap (rail pad center). Port dims = pad bounds
(the cos*sinc port factors above).

DECAP BRANCHES: Z_c = ESR + j*w*ESL + 1/(j*w*C). C parses from the
association value (check_decoupling.parse_farads). ESL uses check_decoupling's
loop-inductance heuristic constants (kicad-happy DC-003: 0.7 nH/mm of leg +
1 nH per via): leg = distance from the cap's rail/return pad to the nearest
same-net via (each leg that ends in a via counts one via), plus the part's
body ESL (pdnlib.ESL_BODY_NH by size). ESR from pdnlib's class table by
capacitance; C is mlcc.c_eff at the rail's DC bias (voltage from the
constraints entry or the net name; unknown -> nominal). Loading identity (NSF eqs 4-5):
Z_loaded = (E + Z*Y_C)^-1 * Z with Y_C diagonal, 0 at the observation port.

OUTPUT: |Z|(f) facts on a log grid 10 kHz - 200 MHz (301 points): peaks
(antiresonances) with frequency/magnitude, C00, first minimum. The full curve
goes to a sidecar JSON via --curve-out (never inline).

TARGET + FULL CHAIN (#490, scripts/lib/pdnlib.py). A constraints power entry
with `transient_a` gets a derived target and is judged on the whole chain:

  {"net": "+1V1", "transient_a": 0.3, "ripple_pct": 3, "voltage": 1.1,
   "edge_ns": 2 | "f_max_mhz": 50,
   "vrm": {"r_out_mohm": 8, "l_out_nh": 20 | "f_bw_khz": 60},
   "package": {"l_pkg_nh": 0.2, "c_die_nf": 20}}

  Z_t = V * ripple% / transient_a  (ripple_pct default 5 %, V from
  `voltage` or the net name), flat up to f_hi = min(0.35/edge, f_max,
  package corner 1/(2 pi sqrt(L_pkg C_die))). The package comes from
  `package`, else reference/pkg_pdn.yaml by the served IC's text/footprint
  (flagged assumed); the VRM from `vrm`, else a flagged default (10 mOhm to
  a 50 kHz loop bandwidth). Decap C is mlcc.c_eff at the rail voltage,
  ESR/ESL from pdnlib's class tables plus the mounting heuristic above.
  Board-node Z = (loaded cavity at the observation port, when the rail has
  a plane pair, else a lumped ladder with SPREAD_NH_PER_MM of path from each
  cap to the IC) || VRM; the die-side Z (through the package) is reported.
  Every frequency in [10 kHz, f_hi] is judged (the VRM removes the low-end
  artifact and f_hi stops before the package's): kind pdn_z_excess, one per
  band over the mask. `pdn_target_mohm` still overrides the value; alone (no
  transient_a) it keeps the old peaks-only gate. Rails with neither are
  listed in `targets_missing`; a transient_a rail whose voltage can't be
  read gets a pdn_target_underived warning.

LICENSE NOTE: KiPIDA (AGPL) was consulted as approach validation only; all
code here is written from the published equations above.

CLI: --pcb board.kicad_pcb --metadata decoupling.json
     [--constraints constraints.json] [--curve-out curve.json] [--out report.json]
     exit 0/1/2 per SPEC section 6.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import sys
import tempfile
from pathlib import Path

# Hundreds of small solves: OpenBLAS's pool busy-waits on them and stalls on a
# loaded machine (see lib/solvers.py), and one thread costs nothing here.
for _v in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, "1")

import numpy as np  # noqa: E402 (after the BLAS thread pin)
import shapely  # noqa: E402
import shapely.affinity  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import checklib  # noqa: E402
import geom  # noqa: E402
import pdnlib  # noqa: E402
import planecav  # noqa: E402
from check_decoupling import (NH_PER_MM, NH_PER_VIA, nearest_via_mm,  # noqa: E402
                              parse_farads)
from checklib import CheckError, violation  # noqa: E402

SCRIPT = "check_pdn_z"

MU0 = 4e-7 * math.pi
EPS0 = 8.8541878128e-12
C_LIGHT = 299792458.0
SIGMA_CU = 5.8e7            # S/m
TAN_DELTA = 0.02            # FR4 default loss tangent
F_MIN_HZ = 1e4
F_MAX_HZ = 2e8
N_FREQ = 301
LARGE_FILL_FRAC = 0.15      # "large fill" = this fraction of outline area
M_FLOOR = 30                # truncation floor per the contract recipe
COVERAGE_MARGIN = 2.5       # mode-coverage margin over f_max
FMIN_SHIFT_WARN = 0.05      # first-minimum shift gate on the 2M check
N_MODES = 6                 # cavity modes reported per pair
SEG_CELLS = 32              # raster cells on a segmented plane's long side
SEG_F_STEP = 10             # of the sweep, every this point in the 2x check
SNAP_MM = 1.0               # a port this far off the plane gets a path L
SLIVER_FRAC = 0.01          # overlap pieces under this share are not islands
MODE_SHIFT_WARN = 0.02      # first-mode shift gate on the 2x check
PALACE_TOL_PCT = 2.0        # segmentation vs Palace, per the issue


# ============================================================ cavity model

def sinc(x):
    """sin(x)/x with sinc(0)=1 (np.sinc is the normalized variant)."""
    return np.sinc(np.asarray(x, dtype=float) / np.pi)


def skin_depth_m(w: float) -> float:
    return math.sqrt(2.0 / (w * MU0 * SIGMA_CU))


def delta_mod_m(w: float, t_cu_m: float) -> float:
    """Novak-Miller eq 4.29 low-frequency clip of the skin depth."""
    ds = skin_depth_m(w)
    return 1.0 / (1.0 / ds + 1.0 / t_cu_m)


def lossy_k(w: float, eps_r: float, d_m: float, t_cu_m: float,
            tan_delta: float = TAN_DELTA) -> complex:
    dm = delta_mod_m(w, t_cu_m)
    return (w * math.sqrt(MU0 * EPS0 * eps_r)
            * (1.0 - 1j * (tan_delta + dm / d_m) / 2.0))


def cavity_z(freqs_hz, a_m: float, b_m: float, d_m: float, eps_r: float,
             ports, m_max: int, n_max: int, tan_delta: float = TAN_DELTA,
             t_cu_m: float = 35e-6) -> np.ndarray:
    """Bare plane-pair impedance matrix, shape (F, P, P) complex.

    `ports` = [(x_m, y_m, tx_m, ty_m), ...] in the rectangle frame (meters).
    """
    freqs = np.asarray(freqs_hz, dtype=float)
    kx = np.arange(m_max + 1) * math.pi / a_m
    ky = np.arange(n_max + 1) * math.pi / b_m
    c2m = np.where(np.arange(m_max + 1) > 0, 2.0, 1.0)
    c2n = np.where(np.arange(n_max + 1) > 0, 2.0, 1.0)
    sqw = np.sqrt(np.outer(c2m, c2n))          # sqrt(c_m^2 * c_n^2)
    b_rows = []
    for (px, py, tx, ty) in ports:
        f_p = (np.outer(np.cos(kx * px) * sinc(kx * tx / 2.0),
                        np.cos(ky * py) * sinc(ky * ty / 2.0)))
        b_rows.append((f_p * sqw).ravel())
    bmat = np.vstack(b_rows)                   # (P, modes)
    kx2ky2 = (kx[:, None] ** 2 + ky[None, :] ** 2).ravel()
    n_p = len(ports)
    z = np.empty((len(freqs), n_p, n_p), dtype=complex)
    for i, f in enumerate(freqs):
        w = 2.0 * math.pi * f
        k = lossy_k(w, eps_r, d_m, t_cu_m, tan_delta)
        amp = 1.0 / (kx2ky2 - k * k)
        z[i] = (1j * w * MU0 * d_m / (a_m * b_m)) * ((bmat * amp) @ bmat.T)
    return z


def branch_z(freqs_hz, c_f: float, esl_h: float, esr_ohm: float) -> np.ndarray:
    w = 2.0 * math.pi * np.asarray(freqs_hz, dtype=float)
    return esr_ohm + 1j * w * esl_h + 1.0 / (1j * w * c_f)


def load_decaps(z: np.ndarray, y_ports: np.ndarray) -> np.ndarray:
    """Z_loaded = (E + Z*Y_C)^-1 * Z per frequency (NSF eqs 4-5).

    y_ports: (F, P) branch admittances, 0 at observation ports."""
    n_p = z.shape[1]
    eye = np.eye(n_p, dtype=complex)
    a = eye[None, :, :] + z * y_ports[:, None, :]   # column-scaled Z
    return np.linalg.solve(a, z)


def c00_farads(a_m: float, b_m: float, d_m: float, eps_r: float) -> float:
    return EPS0 * eps_r * a_m * b_m / d_m


def mode_count(dim_m: float, eps_r: float, f_max_hz: float) -> int:
    """Truncation per axis: coverage bound with margin, floored at M_FLOOR."""
    m = math.ceil(2.0 * dim_m * math.sqrt(eps_r)
                  * f_max_hz * COVERAGE_MARGIN / C_LIGHT)
    return max(m, M_FLOOR)


def local_maxima(zmag: np.ndarray) -> list[int]:
    out = []
    for i in range(1, len(zmag) - 1):
        if zmag[i] > zmag[i - 1] and zmag[i] >= zmag[i + 1]:
            out.append(i)
    return out


def first_local_min(zmag: np.ndarray):
    for i in range(1, len(zmag) - 1):
        if zmag[i] < zmag[i - 1] and zmag[i] <= zmag[i + 1]:
            return i
    return None


# ============================================================ board -> pairs

def find_pairs(bg: geom.BoardGeom, rails: set[str]) -> list[dict]:
    """Adjacent-layer (rail, reference) plane pairs with large fills."""
    outline_area = bg.outline.area
    if outline_area <= 0:
        outline_area = max((bg.zone_fill(n, l).area for n in bg.nets
                            for l in bg.layers_with_zone(n)), default=0.0)
    if outline_area <= 0:
        return []
    floor = LARGE_FILL_FRAC * outline_area
    fills: dict[str, list[tuple[str, object]]] = {}
    for net in sorted(n for n in bg.nets if n):
        for layer in bg.layers_with_zone(net):
            f = bg.zone_fill(net, layer)
            if f.area >= floor:
                fills.setdefault(layer, []).append((net, f))
    pairs = []
    for la, lb in zip(bg.copper_layers, bg.copper_layers[1:]):
        for na, fa in fills.get(la, []):
            for nb, fb in fills.get(lb, []):
                if na == nb:
                    continue
                if na in rails and nb not in rails:
                    rail, ref = na, nb
                    rail_layer, ref_layer = la, lb
                    rail_fill, ref_fill = fa, fb
                elif nb in rails and na not in rails:
                    rail, ref = nb, na
                    rail_layer, ref_layer = lb, la
                    rail_fill, ref_fill = fb, fa
                else:
                    continue
                inter = rail_fill.intersection(ref_fill)
                if inter.is_empty or inter.area < 0.25 * min(rail_fill.area,
                                                             ref_fill.area):
                    continue
                minx, miny, maxx, maxy = inter.bounds
                if maxx - minx < 1.0 or maxy - miny < 1.0:
                    continue
                pairs.append({
                    "rail": rail, "reference": ref, "poly": inter,
                    "rail_layer": rail_layer, "ref_layer": ref_layer,
                    "origin": (minx, miny),
                    "a_mm": maxx - minx, "b_mm": maxy - miny,
                    "d_mm": bg.stackup.height_between(la, lb),
                    "eps_r": bg.stackup.epsilon_between(la, lb),
                    "t_cu_mm": min(bg.stackup.copper_thickness[la],
                                   bg.stackup.copper_thickness[lb]),
                })
    return pairs


def cap_esl_nh(bg: geom.BoardGeom, cap: str, rail: str, ref_net: str):
    """Mounting ESL from the check_decoupling heuristic constants
    (0.7 nH/mm leg + 1 nH per via; kicad-happy DC-003 + spec via heuristic)."""
    legs = 0.0
    vias = 0
    detail = {}
    for net, key in ((rail, "rail_leg_mm"), (ref_net, "return_leg_mm")):
        pads = bg.pads_of(net=net, ref=cap)
        leg = nearest_via_mm(bg, net, pads[0].center) if pads else None
        if leg is not None:
            legs += leg
            vias += 1
            detail[key] = checklib.rnd(leg)
        else:
            detail[key] = None
    return NH_PER_MM * legs + NH_PER_VIA * vias, detail


def rail_volts(rail: str, entry: dict | None) -> float | None:
    if entry and isinstance(entry.get("voltage"), (int, float)):
        return float(entry["voltage"])
    from check_ratings import volts_from_name  # noqa: PLC0415
    return volts_from_name(rail)


def make_cap(bg: geom.BoardGeom, a: dict, c_f: float, rail: str,
             ref_net: str, v_rail: float | None,
             extra_l_nh: float = 0.0) -> tuple[pdnlib.Cap, dict]:
    """The decap branch: pdnlib's derated C + class ESR/ESL, mounting L from
    the via-leg heuristic (plus `extra_l_nh` of path when lumped)."""
    cap = a["cap"]
    mount_nh, detail = cap_esl_nh(bg, cap, rail, ref_net)
    fp = (getattr(bg, "footprints", None) or {}).get(cap, {})
    props = fp.get("props") or {}
    mpn = " ".join(str(props.get(k, "")) for k in ("MPN", "LCSC", "Part"))
    c = pdnlib.cap_from_part(cap, a.get("value") or "", c_f,
                             v_rail or 0.0, footprint=fp.get("lib", ""),
                             mpn=mpn, l_mount_nh=mount_nh + extra_l_nh)
    facts = {"cap": cap, "value": a.get("value"), "c_f": c_f,
             "c_eff_f": checklib.rnd(c.c_f * 1e9, 4) * 1e-9,
             "derate": c.meta["derate"],
             "esl_nh": checklib.rnd(c.l_total_h * 1e9),
             "esl_body_nh": checklib.rnd(c.esl_h * 1e9),
             "l_mount_nh": checklib.rnd(mount_nh),
             "esr_mohm": checklib.rnd(c.esr_ohm * 1e3), **detail}
    if extra_l_nh:
        facts["l_path_nh"] = checklib.rnd(extra_l_nh)
    return c, facts


def observation(bg: geom.BoardGeom, rail: str, rail_assocs: list[dict]):
    """(obs_ic, pin_pads) - the IC with most associations on the rail."""
    counts: dict[str, int] = {}
    for a in rail_assocs:
        counts[a["ic"]] = counts.get(a["ic"], 0) + 1
    obs_ic = sorted(counts, key=lambda r: (-counts[r], r))[0]
    pin_nums = {str(a.get("pin")) for a in rail_assocs if a["ic"] == obs_ic}
    pin_pads = [p for p in bg.pads_of(net=rail, ref=obs_ic)
                if p.number in pin_nums] or bg.pads_of(net=rail, ref=obs_ic)
    return obs_ic, pin_nums, pin_pads


def build_ports(bg: geom.BoardGeom, pair: dict, assocs: list[dict],
                v_rail: float | None = None):
    """(ports_m, obs, decaps, skipped, caps) - port 0 is the observation
    port; caps are the pdnlib.Cap branches, in decaps' order."""
    rail = pair["rail"]
    ox, oy = pair["origin"]
    a_m = pair["a_mm"] * 1e-3
    b_m = pair["b_mm"] * 1e-3

    def to_port(center, size) -> tuple[float, float, float, float]:
        x = min(max((center[0] - ox) * 1e-3, 0.0), a_m)
        y = min(max((center[1] - oy) * 1e-3, 0.0), b_m)
        return (x, y, size[0] * 1e-3, size[1] * 1e-3)

    rail_assocs = [a for a in assocs if a.get("rail") == rail]
    if not rail_assocs:
        return None, None, [], [], []
    obs_ic, pin_nums, pin_pads = observation(bg, rail, rail_assocs)
    if not pin_pads:
        return None, None, [], [], []
    cx = sum(p.center[0] for p in pin_pads) / len(pin_pads)
    cy = sum(p.center[1] for p in pin_pads) / len(pin_pads)
    psize = (max(p.size[0] for p in pin_pads), max(p.size[1] for p in pin_pads))
    ports = [to_port((cx, cy), psize)]
    obs = {"ic": obs_ic, "pos": [checklib.rnd(cx), checklib.rnd(cy)],
           "pins": sorted(pin_nums)}

    decaps: list[dict] = []
    caps: list[pdnlib.Cap] = []
    skipped: list[dict] = []
    seen: set[str] = set()
    for a in rail_assocs:
        cap = a.get("cap")
        if not cap or cap in seen:
            continue
        seen.add(cap)
        pads = bg.pads_of(net=rail, ref=cap)
        c_f = parse_farads(a.get("value"))
        if not pads or not c_f:
            skipped.append({"cap": cap,
                            "reason": "no rail pad" if not pads
                            else f"unparseable value {a.get('value')!r}"})
            continue
        pad = pads[0]
        c, facts = make_cap(bg, a, c_f, rail, pair["reference"], v_rail)
        ports.append(to_port(pad.center, pad.size))
        caps.append(c)
        decaps.append({**facts, "pos": [checklib.rnd(pad.center[0]),
                                        checklib.rnd(pad.center[1])]})
    return ports, obs, decaps, skipped, caps


# ============================================================ analysis

def _load(freqs, z, ports, caps) -> tuple[np.ndarray, np.ndarray]:
    y = np.zeros((len(freqs), len(ports)), dtype=complex)
    for k, c in enumerate(caps):
        y[:, k + 1] = 1.0 / c.z(freqs)
    zl = load_decaps(z, y)
    return zl[:, 0, 0], z[:, 0, 0]


def _zin(freqs, pair, ports, caps, m, n) -> tuple[np.ndarray, np.ndarray]:
    """(Z_loaded, Z_bare) at the observation port, complex ohm."""
    z = cavity_z(freqs, pair["a_mm"] * 1e-3, pair["b_mm"] * 1e-3,
                 pair["d_mm"] * 1e-3, pair["eps_r"], ports, m, n,
                 t_cu_m=pair["t_cu_mm"] * 1e-3)
    return _load(freqs, z, ports, caps)


def _zin_cav(freqs, pair, cav, ports, caps,
             sweep: bool = True) -> tuple[np.ndarray, np.ndarray]:
    """As _zin, on a segmented cavity. `sweep` interpolates under the first
    mode: for a fine log grid only, so never on the 2x run's coarse one."""
    d_m, t_m = pair["d_mm"] * 1e-3, pair["t_cu_mm"] * 1e-3
    solve = cav.z_sweep if sweep else cav.z
    z = solve(freqs, ports,
              lossy_k=lambda w: lossy_k(w, pair["eps_r"], d_m, t_m))
    return _load(freqs, z, ports, caps)


def plane_shape(pair: dict, obs_xy) -> dict | None:
    """The piece of the overlap to analyse, in the pair's frame (mm from its
    origin), or None when the bounding rectangle is the shape.
    `obs_xy` is the observation port in that frame, in mm."""
    poly = pair.get("poly")
    if poly is None:
        return None
    frame = shapely.affinity.translate(poly, -pair["origin"][0],
                                       -pair["origin"][1])
    pieces = planecav.components(frame)
    total = sum(p.area for p in pieces)
    real = [p for p in pieces if p.area >= SLIVER_FRAC * total]
    if not real:
        return None
    obs = shapely.Point(obs_xy)
    piece = min(real, key=lambda p: (p.distance(obs), -p.area))
    facts = planecav.decompose(piece, max(pair["a_mm"], pair["b_mm"]) / SEG_CELLS)
    if len(real) == 1 and facts["fill"] >= planecav.RECT_FILL:
        return None
    return {"piece": piece, "others": [p for p in real if p is not piece]}


def segment_ports(cav, piece, ports, obs, decaps, caps):
    """Ports put on the plane: unclamped from the board positions and moved to
    the model's nearest copper. A decap off the true copper (a raster cell
    is not copper) gets the path as series L."""
    ports_out, caps_out = [], []
    for k, port in enumerate(ports):
        x, y, _ = cav.snap(port[0], port[1])
        moved = piece.distance(shapely.Point(port[0] * 1e3, port[1] * 1e3)) * 1e-3
        ports_out.append((x, y, port[2], port[3]))
        if k == 0:
            if moved * 1e3 > SNAP_MM:
                obs["off_plane_mm"] = checklib.rnd(moved * 1e3)
            continue
        cap, fact = caps[k - 1], decaps[k - 1]
        if moved * 1e3 > SNAP_MM:
            extra = pdnlib.SPREAD_NH_PER_MM * moved * 1e3 * 1e-9
            cap = dataclasses.replace(cap, l_mount_h=cap.l_mount_h + extra)
            fact["off_plane_mm"] = checklib.rnd(moved * 1e3)
            fact["l_path_nh"] = checklib.rnd(extra * 1e9)
        caps_out.append(cap)
    return ports_out, caps_out


def board_ports(pair: dict, ports, obs, decaps):
    """Ports in the pair's frame without build_ports' clamp to the rectangle:
    a real shape needs to know how far off the copper a pad really is."""
    ox, oy = pair["origin"]
    out = [((obs["pos"][0] - ox) * 1e-3, (obs["pos"][1] - oy) * 1e-3,
            ports[0][2], ports[0][3])]
    for port, fact in zip(ports[1:], decaps):
        out.append(((fact["pos"][0] - ox) * 1e-3, (fact["pos"][1] - oy) * 1e-3,
                    port[2], port[3]))
    return out


def mode_facts(cav, pair: dict, ports, obs, decaps) -> list[dict]:
    """The cavity's modes against where the decaps sit."""
    ox, oy = pair["origin"]
    marks = [(p[0], p[1]) for p in ports]
    out = []
    for md in cav.mode_report(N_MODES, marks):
        ax, ay = md["antinode"]
        v = md["v_marks"]
        entry = {"f_hz": checklib.rnd(md["f_hz"], 1),
                 "in_band": md["f_hz"] <= F_MAX_HZ,
                 "antinode": [checklib.rnd(ax * 1e3 + ox),
                              checklib.rnd(ay * 1e3 + oy)],
                 "v_obs": checklib.rnd(v[0], 3)}
        if decaps:
            dist = [math.hypot(p[0] - ax, p[1] - ay) * 1e3 for p in ports[1:]]
            near = int(np.argmin(dist))
            best = int(np.argmax(v[1:]))
            entry["nearest_decap"] = {"cap": decaps[near]["cap"],
                                      "mm": checklib.rnd(dist[near])}
            entry["best_coupled_decap"] = {"cap": decaps[best]["cap"],
                                           "v": checklib.rnd(v[best + 1], 3)}
        out.append(entry)
    return out


def palace_facts(piece, pair: dict, cav, modes) -> tuple[dict, list[dict]]:
    """Palace eigenmode on the true polygon against the segmented modes."""
    import sim_solve  # noqa: PLC0415
    from lib import solvers  # noqa: PLC0415   (the class sim_solve raises)
    cell = max(pair["a_mm"], pair["b_mm"]) / SEG_CELLS
    clean = planecav.outline(piece, cell, tol=cell / 4)
    n = min(4, len(modes))
    f_seg = [m["f_hz"] * 1e-9 for m in modes[:n]]
    lam_mm = (planecav.C0 / (f_seg[-1] * 1e9 * math.sqrt(pair["eps_r"]))) * 1e3
    size = min(lam_mm / 8, max(pair["a_mm"], pair["b_mm"]) / 16)
    rail = pair["rail"]
    try:
        with tempfile.TemporaryDirectory(prefix="hwde-pdn-palace-") as d:
            got = sim_solve.palace_plane_modes(Path(d), clean, pair["eps_r"], n,
                                               f_seg[0], size)
    except (solvers.SolverError, OSError, ValueError) as e:
        return {"status": "not_run", "why": str(e)[-200:]}, [
            violation(SCRIPT, "warning", None, pair["rail_layer"], rail, [],
                      f"{rail}: Palace cross-check did not run: {e}"[:240],
                      SCRIPT, kind="palace_not_run")]
    errs = [(s / p - 1) * 100 for s, p in zip(f_seg, got["f_ghz"])]
    facts = {"status": "run", "f_ghz": [checklib.rnd(f, 5) for f in got["f_ghz"]],
             "err_pct": [checklib.rnd(e, 3) for e in errs],
             "size_mm": checklib.rnd(got["size_mm"]), "order": got["order"],
             "seconds": got["seconds"]}
    worst = max((abs(e) for e in errs), default=0.0)
    if worst > PALACE_TOL_PCT or len(errs) < n:
        return facts, [violation(
            SCRIPT, "warning", None, pair["rail_layer"], rail, [],
            f"{rail}: segmented cavity modes differ from Palace by "
            f"{worst:.1f}% (limit {PALACE_TOL_PCT}%, {len(errs)} of {n} modes "
            "found)", SCRIPT, kind="palace_disagrees",
            err_pct=checklib.rnd(worst))]
    return facts, []


def _zin_mohm(freqs, pair, ports, caps, m, n) -> tuple[np.ndarray, np.ndarray]:
    """(|Z_loaded|, |Z_bare|) at the observation port, in mOhm."""
    zl, zb = _zin(freqs, pair, ports, caps, m, n)
    return np.abs(zl) * 1e3, np.abs(zb) * 1e3


def rail_spec(entry: dict) -> dict | None:
    """The target recipe of one constraints power entry, or None when it
    asks for nothing (no transient_a, no pdn_target_mohm)."""
    if entry.get("pdn") is False or not entry.get("net"):
        return None
    override = entry.get("pdn_target_mohm")
    transient = entry.get("transient_a")
    if override is None and transient is None:
        return None
    return {"net": entry["net"], "entry": entry,
            "override_mohm": None if override is None else float(override),
            "derived": transient is not None}


def derive_target(spec: dict, bg: geom.BoardGeom, obs_ic: str | None):
    """(target dict, VRM, Package) for a derived spec; (None, None, None)
    plus a reason string in target when it cannot be derived."""
    e = spec["entry"]
    v = rail_volts(spec["net"], e)
    ripple = float(e.get("ripple_pct", pdnlib.RIPPLE_PCT_DEFAULT))
    i_step = float(e["transient_a"])
    if spec["override_mohm"] is not None:
        z_t, z_from = spec["override_mohm"] * 1e-3, "pdn_target_mohm"
    elif v is None:
        return {"error": "no voltage: set `voltage` or name the rail "
                         "(+1V1)"}, None, None
    else:
        z_t, z_from = pdnlib.target_ohm(v, ripple, i_step), "V*ripple/dI"
    fp = (getattr(bg, "footprints", None) or {}).get(obs_ic or "", {})
    props = fp.get("props") or {}
    texts = [fp.get("value", "")] + [str(x) for x in props.values()]
    pkg = pdnlib.package_for(texts, fp.get("lib", ""), e.get("package"))
    vrm = pdnlib.VRM.from_spec(e.get("vrm"))
    f_max = e.get("f_max_mhz")
    band = pdnlib.target_band(z_t, e.get("edge_ns"),
                              None if f_max is None else f_max * 1e6, pkg)
    f_hi = min(band["f_hi_hz"], F_MAX_HZ)
    target = {
        "z_t_mohm": checklib.rnd(z_t * 1e3), "from": z_from,
        "voltage": v, "ripple_pct": ripple, "transient_a": i_step,
        "f_hi_hz": checklib.rnd(f_hi, 1), "f_hi_from": band["f_hi_from"],
        "f_hi_terms_hz": {k: checklib.rnd(f, 1)
                          for k, f in band["terms"].items()},
        "vrm": {"r_out_mohm": checklib.rnd(vrm.r_out_ohm * 1e3),
                "l_out_nh": checklib.rnd(vrm.l_out_h * 1e9),
                "assumed": vrm.assumed, "source": vrm.source},
        "package": {"l_pkg_nh": checklib.rnd(pkg.l_pkg_h * 1e9),
                    "c_die_nf": checklib.rnd(pkg.c_die_f * 1e9),
                    "f_corner_hz": checklib.rnd(pkg.f_corner_hz(), 1),
                    "assumed": pkg.assumed, "source": pkg.source},
    }
    return target, vrm, pkg


def judge_mask(freqs, z_board, z_die, target: dict, rail: str, where,
               layer, obs_ic: str, n_caps: int, model: str):
    """(mask facts, violations): every point of [F_MIN, f_hi] vs Z_t."""
    z_t = target["z_t_mohm"] * 1e-3
    mm = pdnlib.mask_margin(freqs, z_board, z_t, F_MIN_HZ,
                            target["f_hi_hz"])
    facts = {"worst_ratio": checklib.rnd(mm["worst_ratio"], 4),
             "f_worst_hz": checklib.rnd(mm["f_worst_hz"], 1),
             "z_worst_mohm": checklib.rnd(mm["z_worst_ohm"] * 1e3),
             "over": [[checklib.rnd(a, 1), checklib.rnd(b, 1),
                       checklib.rnd(z * 1e3)] for a, b, z in mm["over"]],
             "model": model}
    if z_die is not None:
        dmag = np.abs(z_die)
        i = int(np.argmax(dmag))
        facts["die_z_max"] = {"mohm": checklib.rnd(dmag[i] * 1e3),
                              "f_hz": checklib.rnd(freqs[i], 1),
                              "note": "through the package; above f_hi it "
                                      "is the package's, not the board's"}
    vs = []
    for f0, f1, zmax in mm["over"][:4]:
        vs.append(violation(
            SCRIPT, "error", where, layer, rail, [obs_ic],
            f"{rail} PDN |Z| reaches {zmax * 1e3:.1f} mOhm between "
            f"{f0 / 1e6:.3g} and {f1 / 1e6:.3g} MHz, over the target "
            f"{target['z_t_mohm']:.1f} mOhm ({target['from']}; flat to "
            f"{target['f_hi_hz'] / 1e6:.3g} MHz by {target['f_hi_from']}; "
            f"{n_caps} decaps, {model})", SCRIPT,
            kind="pdn_z_excess", z_mohm=checklib.rnd(zmax * 1e3),
            f_hz=checklib.rnd(f0, 1), f_to_hz=checklib.rnd(f1, 1),
            target_mohm=target["z_t_mohm"], derived=True))
    return facts, vs


def mask_verdict(freqs, zl_c, spec_target, vrm, pkg) -> dict:
    """Over or under a derived target, for a loaded cavity."""
    ch = pdnlib.chain_z(freqs, vrm=vrm, pkg=pkg, extra_board=[zl_c])
    mm = pdnlib.mask_margin(freqs, ch["board"], spec_target["z_t_mohm"] * 1e-3,
                            F_MIN_HZ, spec_target["f_hi_hz"])
    return {"worst_ratio": checklib.rnd(mm["worst_ratio"], 4),
            "f_worst_hz": checklib.rnd(mm["f_worst_hz"], 1),
            "over": bool(mm["over"])}


def _peak_facts(freqs, zmag):
    return [{"f_hz": checklib.rnd(freqs[i], 1), "z_mohm": checklib.rnd(zmag[i])}
            for i in local_maxima(zmag)][:8]


def analyze_pair(bg: geom.BoardGeom, pair: dict, assocs: list[dict],
                 target_mohm, spec: dict | None = None,
                 palace_check: bool = False):
    """(facts, violations, curve) for one plane pair; facts is None when the
    pair has no observation port (no associations for its rail).
    `target_mohm` alone = the legacy peaks-only gate; a derived `spec`
    (rail_spec with transient_a) judges the full chain instead."""
    v_rail = rail_volts(pair["rail"], spec["entry"] if spec else None)
    ports, obs, decaps, skipped, caps = build_ports(bg, pair, assocs, v_rail)
    if ports is None:
        return None, [], None
    freqs = np.logspace(math.log10(F_MIN_HZ), math.log10(F_MAX_HZ), N_FREQ)
    m = mode_count(pair["a_mm"] * 1e-3, pair["eps_r"], F_MAX_HZ)
    n = mode_count(pair["b_mm"] * 1e-3, pair["eps_r"], F_MAX_HZ)
    zl_c, zb_c = _zin(freqs, pair, ports, caps, m, n)
    k_cut = max(m * math.pi / (pair["a_mm"] * 1e-3),
                n * math.pi / (pair["b_mm"] * 1e-3))
    raw = board_ports(pair, ports, obs, decaps)
    shape = plane_shape(pair, (raw[0][0] * 1e3, raw[0][1] * 1e3))
    rect_zl = seg = None
    if shape is None:
        zin2, _ = _zin_mohm(freqs, pair, ports, caps, 2 * m, 2 * n)
        cav = planecav.Cavity([planecav.Rect(0.0, 0.0, pair["a_mm"] * 1e-3,
                                             pair["b_mm"] * 1e-3)],
                              pair["d_mm"] * 1e-3, pair["eps_r"],
                              1.0, k_cut)
        modes_at = ports
    else:
        rect_zl = zl_c
        d_m = pair["d_mm"] * 1e-3
        cav, raster = planecav.from_polygon(shape["piece"], d_m, pair["eps_r"],
                                            F_MAX_HZ, SEG_CELLS, 1, k_cut)
        ports, caps = segment_ports(cav, shape["piece"], raw, obs,
                                    decaps, caps)
        modes_at = ports
        zl_c, zb_c = _zin_cav(freqs, pair, cav, ports, caps)
        fine, _ = planecav.from_polygon(shape["piece"], d_m, pair["eps_r"],
                                        F_MAX_HZ, SEG_CELLS, 2, 2 * k_cut)
        sub = freqs[::SEG_F_STEP]
        z_fine, _ = _zin_cav(sub, pair, fine, ports, caps, sweep=False)
        z_here = zl_c[::SEG_F_STEP]
        f1, f1_fine = cav.mode_freqs(1)[0], fine.mode_freqs(1)[0]
        seg = {"raster": raster, "n_rect": len(cav.rects),
               "n_strips": len(cav.joins),
               "z_shift_max": float(np.max(np.abs(np.abs(z_fine)
                                                  / np.abs(z_here) - 1))),
               "f1_shift": abs(f1_fine - f1) / f1,
               "z_here": z_here, "z_fine": z_fine, "sub": sub}
    zin, zbare = np.abs(zl_c) * 1e3, np.abs(zb_c) * 1e3

    i_min = first_local_min(zin)
    # The 2x run of a segmented plane has a coarser sweep: read both on it.
    if seg is None:
        check_f, check_a, check_b = freqs, zin, zin2
    else:
        check_f = seg["sub"]
        check_a = np.abs(seg["z_here"]) * 1e3
        check_b = np.abs(seg["z_fine"]) * 1e3
    i_a, i_min2 = first_local_min(check_a), first_local_min(check_b)
    shift = None
    if i_a is not None and i_min2 is not None:
        shift = abs(check_f[i_min2] - check_f[i_a]) / check_f[i_a]
    peaks = _peak_facts(freqs, zin)
    i_worst = int(np.argmax(zin))
    c00 = c00_farads(pair["a_mm"] * 1e-3, pair["b_mm"] * 1e-3,
                     pair["d_mm"] * 1e-3, pair["eps_r"])
    if seg is not None:
        c00 = EPS0 * pair["eps_r"] * cav.area / (pair["d_mm"] * 1e-3)

    violations: list[dict] = []
    if shift is not None and shift > FMIN_SHIFT_WARN:
        violations.append(violation(
            SCRIPT, "warning", None, None, pair["rail"], [],
            f"{pair['rail']}/{pair['reference']} cavity sum not converged: "
            f"first |Z| minimum moved {shift:.1%} between M={m} and M={2 * m}"
            if seg is None else
            f"first |Z| minimum moved {shift:.1%} with cells, strips and modes "
            "doubled",
            SCRIPT, kind="cavity_unconverged",
            shift=checklib.rnd(shift)))
    if seg is not None and seg["f1_shift"] > MODE_SHIFT_WARN:
        violations.append(violation(
            SCRIPT, "warning", None, None, pair["rail"], [],
            f"{pair['rail']}/{pair['reference']} segmented cavity not "
            f"converged: first mode moved {seg['f1_shift']:.1%} with cells and "
            "strips doubled", SCRIPT, kind="segmentation_unconverged",
            shift=checklib.rnd(seg["f1_shift"])))
    target = mask = None
    z_chain = None
    before = None
    if spec and spec["derived"]:
        target, vrm, pkg = derive_target(spec, bg, obs["ic"])
        if vrm is None:
            violations.append(violation(
                SCRIPT, "warning", tuple(obs["pos"]), None, pair["rail"],
                [obs["ic"]], f"{pair['rail']}: no PDN target derived: "
                f"{target['error']}", SCRIPT, kind="pdn_target_underived"))
        else:
            ch = pdnlib.chain_z(freqs, vrm=vrm, pkg=pkg, extra_board=[zl_c])
            z_chain = ch["board"]
            mask, vs = judge_mask(freqs, ch["board"], ch["die"], target,
                                  pair["rail"], tuple(obs["pos"]),
                                  pair["rail_layer"], obs["ic"], len(caps),
                                  f"cavity {pair['rail_layer']}/"
                                  f"{pair['ref_layer']} + VRM")
            violations.extend(vs)
            if rect_zl is not None:
                before = mask_verdict(freqs, rect_zl, target, vrm, pkg)
                after_over = bool(mask["over"])
                if before["over"] != after_over:
                    violations.append(moved_warning(
                        pair, obs, "mask", before["over"], after_over))
    # Legacy (pdn_target_mohm with no transient_a): the target gates the
    # modeled ANTIRESONANCE PEAKS only: with no VRM branch (low end) and no
    # package/die capacitance (high end), the band-max always sits at a band
    # edge - a model validity limit, not a layout property (finding B-1).
    elif target_mohm is not None:
        for pk in peaks:
            if pk["z_mohm"] > float(target_mohm):
                violations.append(violation(
                    SCRIPT, "error", tuple(obs["pos"]), pair["rail_layer"],
                    pair["rail"], [obs["ic"]],
                    f"{pair['rail']} PDN antiresonance |Z| = "
                    f"{pk['z_mohm']:.1f} mOhm at {pk['f_hz'] / 1e6:.2f} MHz "
                    f"exceeds pdn_target_mohm {float(target_mohm):.1f} "
                    f"(plane pair {pair['rail_layer']}/{pair['ref_layer']}, "
                    f"{len(decaps)} decaps)", SCRIPT,
                    kind="pdn_z_excess", z_mohm=pk["z_mohm"],
                    f_hz=pk["f_hz"], target_mohm=float(target_mohm)))
        if rect_zl is not None:
            old_peaks = _peak_facts(freqs, np.abs(rect_zl) * 1e3)
            before = {"over": any(p["z_mohm"] > float(target_mohm)
                                  for p in old_peaks)}
            after_over = any(p["z_mohm"] > float(target_mohm) for p in peaks)
            if before["over"] != after_over:
                violations.append(moved_warning(
                    pair, obs, "peaks", before["over"], after_over))

    cavity_modes = mode_facts(cav, pair, modes_at, obs, decaps)
    facts = {
        "rail": pair["rail"], "reference": pair["reference"],
        "layers": [pair["rail_layer"], pair["ref_layer"]],
        "geometry": "bounding_rect_assumed" if seg is None else "segmented",
        "origin": [checklib.rnd(pair["origin"][0]),
                   checklib.rnd(pair["origin"][1])],
        "a_mm": checklib.rnd(pair["a_mm"]), "b_mm": checklib.rnd(pair["b_mm"]),
        "d_mm": checklib.rnd(pair["d_mm"], 5),
        "epsilon_r": checklib.rnd(pair["eps_r"]),
        "t_cu_mm": checklib.rnd(pair["t_cu_mm"], 5),
        "c00_nf": checklib.rnd(c00 * 1e9, 5),
        "modes": {"m": m, "n": n, "m_check": 2 * m,
                  "first_min": None if i_min is None else
                  {"f_hz": checklib.rnd(freqs[i_min], 1),
                   "z_mohm": checklib.rnd(zin[i_min])},
                  "first_min_check": None if i_min2 is None else
                  {"f_hz": checklib.rnd(check_f[i_min2], 1),
                   "z_mohm": checklib.rnd(check_b[i_min2])},
                  "first_min_shift": None if shift is None
                  else checklib.rnd(shift)},
        "cavity_modes": cavity_modes,
        "obs": obs,
        "v_rail": v_rail,
        "decaps": decaps,
        "skipped_caps": skipped,
        "peaks": peaks,
        "z_max": {"mohm": checklib.rnd(zin[i_worst]),
                  "f_hz": checklib.rnd(freqs[i_worst], 1),
                  "note": "cavity + decaps only (no VRM/package) - judge "
                          "peaks/first_min or the derived `mask`"},
        "target_mohm": None if target_mohm is None else float(target_mohm),
    }
    if target is not None:
        facts["target"] = target
        facts["mask"] = mask
    if seg is not None:
        rmag = np.abs(rect_zl) * 1e3
        i_r = int(np.argmax(rmag))
        facts["shape"] = {
            "n_rect": seg["n_rect"], "n_strips": seg["n_strips"],
            "area_true_mm2": checklib.rnd(seg["raster"]["area_true_mm2"]),
            "area_model_mm2": checklib.rnd(seg["raster"]["area_model_mm2"]),
            "other_islands_mm2": [checklib.rnd(p.area) for p in shape["others"]],
            "convergence": {"z_shift_max": checklib.rnd(seg["z_shift_max"], 4),
                            "f1_shift": checklib.rnd(seg["f1_shift"], 4)}}
        facts["bounding_rect"] = {
            "a_mm": checklib.rnd(pair["a_mm"]), "b_mm": checklib.rnd(pair["b_mm"]),
            "c00_nf": checklib.rnd(c00_farads(
                pair["a_mm"] * 1e-3, pair["b_mm"] * 1e-3,
                pair["d_mm"] * 1e-3, pair["eps_r"]) * 1e9, 5),
            "peaks": _peak_facts(freqs, rmag),
            "z_max": {"mohm": checklib.rnd(rmag[i_r]),
                      "f_hz": checklib.rnd(freqs[i_r], 1)},
            "verdict": before}
        if palace_check:
            facts["palace"], vs = palace_facts(shape["piece"], pair, cav,
                                               cavity_modes)
            violations.extend(vs)
    curve = {
        "rail": pair["rail"], "reference": pair["reference"],
        "layers": [pair["rail_layer"], pair["ref_layer"]],
        "freq_hz": [checklib.rnd(f, 3) for f in freqs],
        "z_mohm": [checklib.rnd(v) for v in zin],
        "z_bare_mohm": [checklib.rnd(v) for v in zbare],
    }
    if z_chain is not None:
        curve["z_chain_mohm"] = [checklib.rnd(v)
                                 for v in np.abs(z_chain) * 1e3]
    return facts, violations, curve


def moved_warning(pair, obs, what, was_over, now_over):
    word = {True: "fails", False: "passes"}
    return violation(
        SCRIPT, "warning", tuple(obs["pos"]), pair["rail_layer"], pair["rail"],
        [obs["ic"]],
        f"{pair['rail']} {word[was_over]} its PDN {what} on the bounding "
        f"rectangle and {word[now_over]} on the real plane shape; the real "
        "shape is the verdict (see `bounding_rect` in this pair's facts)",
        SCRIPT, kind="pdn_verdict_moved", was_over=was_over, now_over=now_over)


def analyze_lumped(bg: geom.BoardGeom, spec: dict, assocs: list[dict]):
    """(facts, violations, curve) for a derived rail with no plane pair:
    the lumped ladder (caps with mounting + path L) || VRM, package to die.
    facts carries `skipped` instead when the rail cannot be modelled."""
    rail = spec["net"]
    rail_assocs = [a for a in assocs if a.get("rail") == rail]
    if not rail_assocs:
        return {"rail": rail, "skipped": "no decoupling associations for "
                                         "this rail"}, [], None
    obs_ic, pin_nums, pin_pads = observation(bg, rail, rail_assocs)
    if not pin_pads:
        return {"rail": rail, "skipped": f"{obs_ic} has no {rail} pads on "
                                         "the board"}, [], None
    cx = sum(p.center[0] for p in pin_pads) / len(pin_pads)
    cy = sum(p.center[1] for p in pin_pads) / len(pin_pads)
    from check_ratings import is_ground  # noqa: PLC0415
    v_rail = rail_volts(rail, spec["entry"])
    caps, decaps, skipped, seen = [], [], [], set()
    for a in rail_assocs:
        cap = a.get("cap")
        if not cap or cap in seen:
            continue
        seen.add(cap)
        pads = bg.pads_of(net=rail, ref=cap)
        c_f = parse_farads(a.get("value"))
        if not pads or not c_f:
            skipped.append({"cap": cap,
                            "reason": "no rail pad" if not pads
                            else f"unparseable value {a.get('value')!r}"})
            continue
        ret = [p.net for p in bg.pads_of(ref=cap)
               if p.net and p.net != rail and is_ground(p.net)]
        dist = math.hypot(pads[0].center[0] - cx, pads[0].center[1] - cy)
        c, facts = make_cap(bg, a, c_f, rail, ret[0] if ret else "GND",
                            v_rail, pdnlib.SPREAD_NH_PER_MM * dist)
        facts["dist_mm"] = checklib.rnd(dist)
        caps.append(c)
        decaps.append(facts)
    obs = {"ic": obs_ic, "pos": [checklib.rnd(cx), checklib.rnd(cy)],
           "pins": sorted(pin_nums)}
    target, vrm, pkg = derive_target(spec, bg, obs_ic)
    if vrm is None:
        return {"rail": rail, "obs": obs, "skipped": target["error"]}, [
            violation(SCRIPT, "warning", tuple(obs["pos"]), None, rail,
                      [obs_ic], f"{rail}: no PDN target derived: "
                      f"{target['error']}", SCRIPT,
                      kind="pdn_target_underived")], None
    freqs = np.logspace(math.log10(F_MIN_HZ), math.log10(F_MAX_HZ), N_FREQ)
    ch = pdnlib.chain_z(freqs, caps, vrm=vrm, pkg=pkg)
    mask, vs = judge_mask(freqs, ch["board"], ch["die"], target, rail,
                          tuple(obs["pos"]), pin_pads[0].layers[0]
                          if pin_pads[0].layers else None, obs_ic,
                          len(caps), "lumped ladder + VRM (no plane pair)")
    facts = {"rail": rail, "model": "lumped", "obs": obs, "v_rail": v_rail,
             "decaps": decaps, "skipped_caps": skipped, "target": target,
             "mask": mask}
    curve = {"rail": rail, "model": "lumped",
             "freq_hz": [checklib.rnd(f, 3) for f in freqs],
             "z_chain_mohm": [checklib.rnd(v)
                              for v in np.abs(ch["board"]) * 1e3],
             "z_die_mohm": [checklib.rnd(v)
                            for v in np.abs(ch["die"]) * 1e3]}
    return facts, vs, curve


# ============================================================ CLI

def run(argv=None):
    ap = argparse.ArgumentParser(
        description="Plane-pair cavity PDN impedance |Z|(f) and the "
                    "derived target-impedance mask per rail.")
    ap.add_argument("--pcb", required=True, help="path to .kicad_pcb")
    ap.add_argument("--metadata", required=True,
                    help="decoupling.json (cap<->pin associations)")
    ap.add_argument("--constraints",
                    help="constraints.json (power[]: transient_a, ripple_pct,"
                         " vrm, package, edge_ns; or pdn_target_mohm)")
    ap.add_argument("--curve-out",
                    help="write the |Z|(f) curves to this sidecar JSON")
    ap.add_argument("--palace-check", action="store_true",
                    help="cross-check each segmented plane's modes with Palace "
                         "eigenmode on the true polygon (slow)")
    ap.add_argument("--out", help="write JSON report here instead of stdout")
    args = ap.parse_args(argv)

    meta = checklib.load_json(args.metadata, "decoupling metadata")
    assocs = meta.get("associations", [])
    specs: dict[str, dict] = {}
    missing: list[str] = []
    if args.constraints:
        from check_ratings import is_ground  # noqa: PLC0415
        cons = checklib.load_json(args.constraints, "constraints")
        for entry in cons.get("power", []):
            spec = rail_spec(entry)
            if spec:
                specs[spec["net"]] = spec
            elif (entry.get("net") and entry.get("pdn") is not False
                  and not is_ground(entry["net"])):
                missing.append(entry["net"])

    bg = geom.load_board(Path(args.pcb))
    bg.assert_fresh()

    rails = {a.get("rail") for a in assocs if a.get("rail")}
    pairs = find_pairs(bg, rails)
    paired_rails = {p["rail"] for p in pairs}
    skipped_rails = [{"rail": r,
                      "reason": "no plane pair detected (rail has decoupling "
                                "associations but no adjacent-layer fill "
                                "pair)"}
                     for r in sorted(rails - paired_rails)]
    violations: list[dict] = []
    checked: list[dict] = []
    curves: list[dict] = []
    skipped_pairs: list[dict] = []
    judged: set[str] = set()
    for pair in pairs:
        spec = specs.get(pair["rail"])
        # one plane pair per rail carries the derived judgement
        if spec and pair["rail"] in judged:
            spec = {**spec, "derived": False}
        facts, vs, curve = analyze_pair(
            bg, pair, assocs,
            spec["override_mohm"] if spec else None, spec, args.palace_check)
        if facts is None:
            skipped_pairs.append({"rail": pair["rail"],
                                  "reference": pair["reference"],
                                  "layers": [pair["rail_layer"],
                                             pair["ref_layer"]],
                                  "reason": "no decoupling associations "
                                            "for this rail"})
            continue
        if "mask" in facts:
            judged.add(pair["rail"])
        violations.extend(vs)
        checked.append(facts)
        curves.append(curve)

    lumped: list[dict] = []
    for net in sorted(specs):
        spec = specs[net]
        if not spec["derived"] or net in judged:
            continue
        facts, vs, curve = analyze_lumped(bg, spec, assocs)
        violations.extend(vs)
        lumped.append(facts)
        if curve:
            curves.append(curve)

    if args.curve_out:
        Path(args.curve_out).write_text(
            json.dumps({"script": SCRIPT, "board": Path(args.pcb).name,
                        "f_min_hz": F_MIN_HZ, "f_max_hz": F_MAX_HZ,
                        "points": N_FREQ, "pairs": curves}, indent=1),
            encoding="utf-8")

    payload = checklib.report(
        SCRIPT, args.pcb, violations, pairs=checked, rails=lumped,
        skipped_pairs=skipped_pairs, skipped_rails=skipped_rails,
        targets_missing=sorted(set(missing)),
        stackup_assumed=bg.stackup.assumed,
        model={"kind": "plane-pair cavity sum: closed form on a filled "
                       "rectangle, segmentation on any other shape; lumped "
                       "ladder (pdnlib) for target rails with no plane pair",
               "tan_delta": TAN_DELTA, "sigma_cu_s_per_m": SIGMA_CU,
               "esr": "pdnlib class table by capacitance",
               "spread_nh_per_mm": pdnlib.SPREAD_NH_PER_MM,
               "band_hz": [F_MIN_HZ, F_MAX_HZ], "points": N_FREQ,
               "curve_out": args.curve_out or None})
    return payload, args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())
