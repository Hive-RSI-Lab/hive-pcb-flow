"""check_irdrop.py - DC IR-drop + sheet current density per power net (layout sim).

2.5D resistor-grid FDM (the KiPIDA/OpenROAD-psm formulation class - approach
only, no code shared; KiPIDA is AGPL and was used strictly as approach
validation). Per constrained power net:

 - each layer's net copper (geom.net_copper: tracks+pads+vias+zone fill) is
   rasterized onto ONE common grid frame (all layers share origin/pitch so via
   cells align); every in-copper cell is a node, adjacent cells couple with the
   layer's sheet conductance (1 square per cell edge, so g = 1/Rs independent
   of cell size), Rs = rho_cu / t_cu with rho_cu = 1.72e-8 Ohm-m
   (1 oz / 0.035 mm -> ~0.49 mOhm/sq);
 - layers couple through via resistors: cylinder R = rho*L/A on the drill
   diameter with an ASSUMED 0.025 mm plating wall (annulus area), L = stackup
   height between the coupled coppers; the barrel conductance is split across
   all cell pairs inside the via radius (kills the single-cell injection
   spike). Multi-layer (THT) pads are tied the same way with an assumed drill
   of 0.6 x min(pad size) - geom carries no pad drill;
 - sparse SPD system (graph Laplacian, Dirichlet nodes eliminated) solved with
   scipy.sparse.linalg.spsolve up to DIRECT_MAX_UNKNOWNS unknowns; above it
   with CG to a 1e-10 relative residual, preconditioned by smoothed-
   aggregation multigrid on the grid's own 2x2 cell blocks (memory linear in
   the cells, where an 800k-cell net's LU fill took 4.5 GB). If CG doesn't
   converge, spsolve is the fallback. Each net's facts say which ran
   ("grid.solver"). Grids hold node ids only for in-copper cells and the
   rasterizer tests a block of rows at a time: on a board with that net the
   whole check went from a 4.5 GB peak to 0.75 GB, with the same report.

INJECTION MODEL (v1, worst-case by default - document to consumers):
 - If the constraints entry carries "source_ref", the source is that
   component's pads (Dirichlet V=0 across all their cells; a THT source pad is
   clamped on every layer it touches). Optional "sinks": [{ref, current_a}]
   attributes return currents per component (split equally over that ref's
   pads, then equally over each pad's cells); overrides are IGNORED in this
   explicit mode (explicit attribution wins).
 - Otherwise WORST-CASE UNIFORM: the source is the component with the largest
   net pad copper area (the "largest pad cluster" - normally the regulator or
   connector), the full current_a returns uniformly across all other
   components' net pads (uniform PER PAD), honoring check_current's per-region
   "overrides" semantics: sink pads whose center falls in an override region
   collectively draw that region's current_a (split equally inside the
   region), the remainder spreads uniformly over the rest. Per-branch
   attribution beyond this is a declared v1 limit.

GRID SIZING (documented floor/ceiling):
 - target cell = min_feature / 8 (>= 8 cells across the narrowest neck, per
   the negative-result guidance: no published rule, 8-10 cells ~5-10% local
   accuracy); min_feature = the net's narrowest track width (fallback:
   smallest pad dimension, then 0.5 mm);
 - floor CELL_MIN_MM = 0.015, ceiling CELL_MAX_MM = 0.4;
 - cell-count ceiling: if the estimated in-copper cell count exceeds
   BASE_CELL_CAP the cell grows to fit (the reported cells_across_min_feature
   fact then exposes the shortfall and the Richardson gate judges it);
 - an entry may pin "cell_mm" explicitly (floor/ceiling still clamp).

RICHARDSON GATE: solve, then double the grid once (cell/2) and report both
resistances + the relative delta (fact richardson_delta); when the doubled
grid would exceed REFINE_CELL_CAP cells the pair is taken against a 2x
COARSER grid instead (fact refine: "coarsened") so runtime stays bounded.
delta > 10% -> warning kind grid_unconverged. Reported numbers always come
from the finer grid of the pair.

Outputs (checklib report): per net - total resistance source->worst sink
(worst sink mean drop / injected current), worst-node IR drop mV, sheet
current density maxima (A/mm) with [x, y] + layer, neck locations. Violations
only when the entry carries optional "irdrop_mv_max" (kind irdrop_excess) or
"jmax_a_per_mm" (kind current_density_excess); otherwise facts-only advisory.
Entries with "pdn": false are skipped (same opt-out as check_pdn).

CLI: --pcb board.kicad_pcb --constraints constraints.json [--out report.json]
     exit 0/1/2 per SPEC section 6.

RUNNING IT: call it as `pcb-flow check_irdrop ...` so it runs in the module's
venv; plain `python3 scripts/check_irdrop.py` fails with "No module named
'shapely'". Runtime, measured 2026-11-14: a 4-layer board (236 KB pcb, one
constrained net, ~39k cells at 0.019 mm, direct solver on ~34k unknowns plus
the ~8k-unknown Richardson pair) takes about 6 s. A job queue in front of it
(e.g. a memory-slot wrapper) can add minutes; time the check itself, not the
wait. Nets past DIRECT_MAX_UNKNOWNS switch to multigrid CG and an 800k-cell
net peaks near 0.75 GB.
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import numpy as np
import shapely
from scipy import sparse
from scipy.sparse.csgraph import connected_components
from scipy.sparse.linalg import LinearOperator, cg, splu, spsolve
from scipy.spatial import cKDTree

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import checklib  # noqa: E402
import geom  # noqa: E402
from checklib import CheckError, violation  # noqa: E402

SCRIPT = "check_irdrop"

RHO_CU_OHM_M = 1.72e-8     # contract C1 (Bogatin/EDN band, do not use 0.7 mOhm/sq)
PLATING_MM = 0.025         # assumed via barrel plating wall (JLC-class 20-25 um)
THT_DRILL_FRACTION = 0.6   # assumed THT drill = 0.6 x min(pad size) (no drill in geom)
NECK_CELLS = 8.0           # target cells across the narrowest feature
CELL_MIN_MM = 0.015        # documented floor
CELL_MAX_MM = 0.4          # documented ceiling
BASE_CELL_CAP = 250_000    # in-copper cell budget for the base grid
REFINE_CELL_CAP = 800_000  # doubling allowed only below this (else coarsen)
RICHARDSON_WARN = 0.10     # grid_unconverged threshold
NECK_MIN_SEP_MM = 1.5      # reported neck maxima are at least this far apart
NECK_KEEP = 5              # top-N reported necks
DIRECT_MAX_UNKNOWNS = 50_000  # sparse LU up to here; multigrid CG above
CG_RTOL = 1e-10            # CG's relative residual target
CG_MAXITER = 500           # then the sparse LU is the fallback
MG_COARSE_UNKNOWNS = 3000  # multigrid's coarsest level, solved by LU
MG_MAX_LEVELS = 16
RASTER_POINTS = 4_000_000  # cell centers tested per contains_xy call


def sheet_res_ohm_sq(cu_mm: float) -> float:
    """Sheet resistance (Ohm/square) of copper cu_mm thick."""
    return RHO_CU_OHM_M / (cu_mm * 1e-3)


def via_res_ohm(drill_mm: float, span_mm: float,
                plating_mm: float = PLATING_MM) -> float:
    """Plated-barrel resistance: rho * L / A on the annulus drill->drill+wall."""
    r = max(drill_mm, 0.05) / 2.0
    area_mm2 = math.pi * ((r + plating_mm) ** 2 - r ** 2)
    return RHO_CU_OHM_M * (span_mm * 1e-3) / (area_mm2 * 1e-6)


# ============================================================ raster grid

class NetGrid:
    """All layers of one net rasterized on a single common frame."""

    def __init__(self, bg: geom.BoardGeom, net: str, layers: list[str],
                 cell: float):
        self.bg = bg
        self.net = net
        self.layers = layers
        self.cell = cell
        polys = {l: bg.net_copper(net, l) for l in layers}
        bounds = [polys[l].bounds for l in layers if not polys[l].is_empty]
        if not bounds:
            raise CheckError(f"net {net!r} has no copper to solve")
        minx = min(b[0] for b in bounds) - cell
        miny = min(b[1] for b in bounds) - cell
        maxx = max(b[2] for b in bounds) + cell
        maxy = max(b[3] for b in bounds) + cell
        self.x0, self.y0 = minx, miny
        self.nx = max(int(math.ceil((maxx - minx) / cell)), 1)
        self.ny = max(int(math.ceil((maxy - miny) / cell)), 1)
        self.xs = minx + (np.arange(self.nx) + 0.5) * cell
        self.ys = miny + (np.arange(self.ny) + 0.5) * cell
        self.mask: dict[str, np.ndarray] = {
            l: self._raster(polys[l]) for l in layers}
        self._number()
        self._trees: dict[str, tuple[cKDTree, np.ndarray]] = {}

    def _number(self):
        """Give every in-copper cell its node id: layer after layer, row-major.

        A layer keeps its cells' flat indices (i * nx + j, sorted) and its
        first id, not a dense id array: a thin net's frame can be 38 M cells
        a layer around 0.1 M nodes, and an int64 per frame cell was 300 MB a
        layer. Call it again after changing a mask."""
        self.keys: dict[str, np.ndarray] = {}
        self.n0: dict[str, int] = {}
        n = 0
        for l in self.layers:
            self.keys[l] = np.flatnonzero(self.mask[l])
            self.n0[l] = n
            n += len(self.keys[l])
        self.n_nodes = n

    def node_ids(self, layer: str, ii, jj) -> np.ndarray:
        """Node ids of the in-copper cells (ii, jj) on layer."""
        flat = (np.asarray(ii, dtype=np.int64) * self.nx
                + np.asarray(jj, dtype=np.int64))
        return self.n0[layer] + np.searchsorted(self.keys[layer], flat)

    def layer_edges(self, layer: str):
        """The layer's neighbour pairs: [(a, b, ka, kb)] for the horizontal
        pairs, then the vertical, each in row-major order of its first cell;
        a, b are node ids, ka, kb flat cell indices."""
        keys, n0, nx = self.keys[layer], self.n0[layer], self.nx
        ph = np.flatnonzero((keys[1:] == keys[:-1] + 1)
                            & (keys[:-1] % nx != nx - 1))
        below = keys + nx
        q = np.searchsorted(keys, below)
        hit = q < len(keys)
        hit[hit] = keys[q[hit]] == below[hit]
        pv = np.flatnonzero(hit)
        qv = q[pv]
        return [(n0 + ph, n0 + ph + 1, keys[ph], keys[ph + 1]),
                (n0 + pv, n0 + qv, keys[pv], keys[qv])]

    def _raster(self, poly) -> np.ndarray:
        """Cells whose center lies in poly, tested a block of rows at a time
        so the test points never take more than RASTER_POINTS."""
        m = np.zeros((self.ny, self.nx), dtype=bool)
        if poly.is_empty:
            return m
        minx, miny, maxx, maxy = poly.bounds
        ix = np.nonzero((self.xs >= minx) & (self.xs <= maxx))[0]
        iy = np.nonzero((self.ys >= miny) & (self.ys <= maxy))[0]
        if not len(ix) or not len(iy):
            return m
        shapely.prepare(poly)
        j0, j1 = int(ix[0]), int(ix[-1]) + 1
        xs = self.xs[j0:j1]
        step = max(1, RASTER_POINTS // len(xs))
        for i0 in range(int(iy[0]), int(iy[-1]) + 1, step):
            i1 = min(i0 + step, int(iy[-1]) + 1)
            xx, yy = np.meshgrid(xs, self.ys[i0:i1])
            hits = shapely.contains_xy(poly, xx.ravel(), yy.ravel())
            m[i0:i1, j0:j1] = hits.reshape(i1 - i0, j1 - j0)
        return m

    def cell_center(self, i: int, j: int) -> tuple[float, float]:
        return (float(self.xs[j]), float(self.ys[i]))

    def disk_cells(self, layer: str, cx: float, cy: float, r: float):
        """(i, j) index arrays of in-copper cells with center within r."""
        h = self.cell
        j0 = max(int((cx - r - self.x0) / h) - 1, 0)
        j1 = min(int((cx + r - self.x0) / h) + 2, self.nx)
        i0 = max(int((cy - r - self.y0) / h) - 1, 0)
        i1 = min(int((cy + r - self.y0) / h) + 2, self.ny)
        if j0 >= j1 or i0 >= i1:
            return np.empty(0, int), np.empty(0, int)
        sub = self.mask[layer][i0:i1, j0:j1]
        xx, yy = np.meshgrid(self.xs[j0:j1], self.ys[i0:i1])
        near = ((xx - cx) ** 2 + (yy - cy) ** 2 <= r * r) & sub
        ii, jj = np.nonzero(near)
        return ii + i0, jj + j0

    def poly_cells(self, layer: str, poly):
        """Nodes of in-copper cells whose center lies inside poly."""
        minx, miny, maxx, maxy = poly.bounds
        ix = np.nonzero((self.xs >= minx) & (self.xs <= maxx))[0]
        iy = np.nonzero((self.ys >= miny) & (self.ys <= maxy))[0]
        if not len(ix) or not len(iy):
            return np.empty(0, dtype=np.int64)
        shapely.prepare(poly)
        xx, yy = np.meshgrid(self.xs[ix], self.ys[iy])
        hits = shapely.contains_xy(poly, xx.ravel(), yy.ravel())
        hits = hits.reshape(len(iy), len(ix))
        si, sj = np.nonzero(self.mask[layer][np.ix_(iy, ix)] & hits)
        return self.node_ids(layer, iy[si], ix[sj])

    def nearest_node(self, layers: list[str], cx: float, cy: float,
                     max_dist: float):
        """Nearest in-copper node to (cx, cy) on any given layer, or None."""
        best = None
        for l in layers:
            if l not in self.mask:
                continue
            if l not in self._trees:
                if not len(self.keys[l]):
                    continue
                ii, jj = np.divmod(self.keys[l], self.nx)
                pts = np.column_stack([self.xs[jj], self.ys[ii]])
                self._trees[l] = (cKDTree(pts), self.n0[l]
                                  + np.arange(len(ii), dtype=np.int64))
            tree, ids = self._trees[l]
            d, k = tree.query([cx, cy])
            if d <= max_dist and (best is None or d < best[0]):
                best = (float(d), int(ids[k]))
        return None if best is None else best[1]

    def node_coords(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(layer index, row, column) of every node, in node-id order."""
        lay, rows, cols = [], [], []
        for k, l in enumerate(self.layers):
            ii, jj = np.divmod(self.keys[l], self.nx)   # in node-id order
            lay.append(np.full(len(ii), k, dtype=np.int64))
            rows.append(ii.astype(np.int64))
            cols.append(jj.astype(np.int64))
        return (np.concatenate(lay), np.concatenate(rows),
                np.concatenate(cols))


# ============================================================ linear solve

class _Multigrid:
    """Smoothed-aggregation multigrid, used as CG's preconditioner.

    The aggregates are the grid's own 2x2 cell blocks, layer by layer, so
    the setup needs no strength-of-connection graph. The prolongator is the
    block indicator smoothed by one damped Jacobi step (omega = 4/3 over a
    Gershgorin bound on rho(D^-1 A)), the coarse operators are Galerkin
    (P^T A P), and the V-cycle smooths with two damped Jacobi sweeps each way,
    so the preconditioner is symmetric positive definite, as CG needs.
    Deterministic: no random start vectors.
    """

    def __init__(self, a: sparse.csr_matrix, coords):
        lay, ii, jj = (np.asarray(c, dtype=np.int64) for c in coords)
        self.levels: list[tuple] = []
        while (a.shape[0] > MG_COARSE_UNKNOWNS
               and len(self.levels) < MG_MAX_LEVELS):
            n = a.shape[0]
            ii, jj = ii >> 1, jj >> 1
            span_i, span_j = int(ii.max()) + 1, int(jj.max()) + 1
            key = (lay * span_i + ii) * span_j + jj
            _, first, agg = np.unique(key, return_index=True,
                                      return_inverse=True)
            nc = len(first)
            if nc > 0.8 * n:
                break                       # cells too scattered to coarsen
            tent = sparse.csr_matrix((np.ones(n), (np.arange(n), agg)),
                                     shape=(n, nc))
            dinv = 1.0 / a.diagonal()
            rho = float(np.max(np.asarray(abs(a).sum(axis=1)).ravel() * dinv))
            w = 4.0 / (3.0 * rho)
            p = (tent - sparse.diags(w * dinv) @ (a @ tent)).tocsr()
            self.levels.append((a, p, w * dinv))
            a = (p.T @ (a @ p)).tocsr()
            lay, ii, jj = lay[first], ii[first], jj[first]
        self.coarse = splu(a.tocsc())
        self.coarse_unknowns = a.shape[0]

    def vcycle(self, b: np.ndarray, k: int = 0) -> np.ndarray:
        if k == len(self.levels):
            return self.coarse.solve(b)
        a, p, wd = self.levels[k]
        x = wd * b
        x += wd * (b - a @ x)
        x += p @ self.vcycle(p.T @ (b - a @ x), k + 1)
        x += wd * (b - a @ x)
        x += wd * (b - a @ x)
        return x


def solve_spd(a: sparse.csr_matrix, b: np.ndarray, coords):
    """Solve the grid's SPD system: (x, solver facts).

    Up to DIRECT_MAX_UNKNOWNS, sparse LU (spsolve). Above it, CG with the
    multigrid preconditioner to CG_RTOL: its memory grows linearly, where
    the LU's fill on an 800k-cell net took 4.5 GB and failed under a 2.5 GB
    address-space limit (SUPERLU_MALLOC). If CG doesn't converge in
    CG_MAXITER iterations, the LU is the fallback.
    """
    n = a.shape[0]
    if n <= DIRECT_MAX_UNKNOWNS:
        return spsolve(a.tocsc(), b), {"method": "direct"}
    mg = _Multigrid(a.tocsr(), coords)
    pre = LinearOperator(a.shape, matvec=mg.vcycle, dtype=float)
    its = [0]

    def count(_x):
        its[0] += 1

    x, info = cg(a, b, rtol=CG_RTOL, atol=0.0, maxiter=CG_MAXITER, M=pre,
                 callback=count)
    relres = float(np.linalg.norm(b - a @ x) / max(np.linalg.norm(b), 1e-300))
    facts = {"method": "multigrid_cg", "levels": len(mg.levels) + 1,
             "iterations": its[0], "relres": float(f"{relres:.2g}")}
    if info != 0:
        return spsolve(a.tocsc(), b), {
            "method": "direct", "note": f"CG stopped after {its[0]} "
                                        f"iterations at relres {relres:.2g}"}
    return x, facts


# ============================================================ injection plan

def pad_groups(bg: geom.BoardGeom, net: str) -> dict[str, list]:
    groups: dict[str, list] = {}
    for p in bg.pads_of(net):
        groups.setdefault(p.ref, []).append(p)
    return groups


def pick_source_ref(groups: dict[str, list]) -> str:
    """Worst-case source proxy: component with the largest net pad area."""
    return max(sorted(groups),
               key=lambda r: sum(p.poly.area for p in groups[r]))


def sink_currents(entry: dict, groups: dict[str, list], source_ref: str):
    """[(ref, pad, amps)] per sink pad (uniform-per-pad + overrides, or the
    explicit sinks list when present)."""
    out: list[tuple[str, object, float]] = []
    if entry.get("sinks"):
        for s in entry["sinks"]:
            ref = s.get("ref")
            if ref not in groups:
                raise CheckError(f"sink ref {ref!r} has no pads on the net")
            amps = float(s["current_a"]) / len(groups[ref])
            out.extend((ref, p, amps) for p in groups[ref])
        return out
    pads = [(r, p) for r in sorted(groups) if r != source_ref
            for p in groups[r]]
    if not pads:
        raise CheckError("no sink pads (single-component net)")
    total = float(entry["current_a"])
    assigned: dict[int, float] = {}
    remaining = list(range(len(pads)))
    for ov in entry.get("overrides", []):
        near = ov.get("near")
        if not near:
            continue
        r = float(ov.get("radius_mm", 2.0))
        idxs = [i for i in remaining
                if math.hypot(pads[i][1].center[0] - near[0],
                              pads[i][1].center[1] - near[1]) <= r]
        if not idxs:
            continue
        share = float(ov["current_a"]) / len(idxs)
        for i in idxs:
            assigned[i] = share
        remaining = [i for i in remaining if i not in idxs]
    rest = max(0.0, total - sum(assigned.values()))
    for i in remaining:
        assigned[i] = rest / len(remaining)
    return [(pads[i][0], pads[i][1], assigned[i]) for i in range(len(pads))]


# ============================================================ system assembly

def _grid_edges(grid: NetGrid, rows, cols, vals):
    for l in grid.layers:
        g = 1.0 / sheet_res_ohm_sq(grid.bg.stackup.copper_thickness[l])
        for a, b, _ka, _kb in grid.layer_edges(l):
            rows.append(a)
            cols.append(b)
            vals.append(np.full(len(a), g))


def _barrel_edges(grid: NetGrid, cx, cy, radius, drill, span_layers,
                  rows, cols, vals) -> bool:
    """Couple consecutive layer pairs of one barrel; True if fully linked."""
    bg = grid.bg
    ok = True
    for la, lb in zip(span_layers, span_layers[1:]):
        span = bg.stackup.height_between(la, lb)
        g_total = 1.0 / via_res_ohm(drill, max(span, 1e-3))
        ia, ja = grid.disk_cells(la, cx, cy, radius)
        ib, jb = grid.disk_cells(lb, cx, cy, radius)
        pa = set(zip(ia.tolist(), ja.tolist()))
        pb = set(zip(ib.tolist(), jb.tolist()))
        common = sorted(pa & pb)
        if common:
            ii = np.array([c[0] for c in common], dtype=int)
            jj = np.array([c[1] for c in common], dtype=int)
            rows.append(grid.node_ids(la, ii, jj))
            cols.append(grid.node_ids(lb, ii, jj))
            vals.append(np.full(len(common), g_total / len(common)))
            continue
        reach = radius + 2.0 * grid.cell
        na = grid.nearest_node([la], cx, cy, reach)
        nb = grid.nearest_node([lb], cx, cy, reach)
        if na is not None and nb is not None and na != nb:
            rows.append(np.array([na]))
            cols.append(np.array([nb]))
            vals.append(np.array([g_total]))
        else:
            ok = False
    return ok


def assemble(grid: NetGrid):
    """Adjacency matrix (conductances) + via bookkeeping."""
    bg, net = grid.bg, grid.net
    rows: list[np.ndarray] = []
    cols: list[np.ndarray] = []
    vals: list[np.ndarray] = []
    _grid_edges(grid, rows, cols, vals)
    used = skipped = 0
    for v in bg.vias_of(net):
        span = [l for l in bg.copper_layers
                if l in v.layers and l in grid.mask]
        if len(span) < 2:
            continue
        if _barrel_edges(grid, v.at[0], v.at[1], v.diameter / 2.0,
                         v.drill or v.diameter / 2.0, span,
                         rows, cols, vals):
            used += 1
        else:
            skipped += 1
    for p in bg.pads_of(net):
        span = [l for l in bg.copper_layers
                if l in p.layers and l in grid.mask]
        if len(span) < 2:
            continue
        drill = THT_DRILL_FRACTION * min(p.size)
        if _barrel_edges(grid, p.center[0], p.center[1],
                         max(p.size) / 2.0, drill, span, rows, cols, vals):
            used += 1
        else:
            skipped += 1
    if rows:
        r = np.concatenate(rows)
        c = np.concatenate(cols)
        g = np.concatenate(vals)
    else:
        r = c = np.empty(0, int)
        g = np.empty(0)
    n = grid.n_nodes
    adj = sparse.coo_matrix((np.concatenate([g, g]),
                             (np.concatenate([r, c]),
                              np.concatenate([c, r]))), shape=(n, n)).tocsr()
    return adj, used, skipped


# ============================================================ solve one grid

def solve_grid(bg: geom.BoardGeom, entry: dict, net: str, layers: list[str],
               cell: float):
    """Build + solve the resistor grid at one cell size. Returns a dict of
    raw results (voltages folded into summary numbers, no arrays)."""
    grid = NetGrid(bg, net, layers, cell)
    adj, vias_used, vias_skipped = assemble(grid)

    groups = pad_groups(bg, net)
    if not groups:
        raise CheckError(f"net {net!r} has no component pads")
    source_ref = entry.get("source_ref")
    source_mode = "constraints"
    if source_ref:
        if source_ref not in groups:
            raise CheckError(f"source_ref {source_ref!r} has no pads on {net!r}")
    else:
        source_ref = pick_source_ref(groups)
        source_mode = "largest_pad_area"

    def pad_nodes(p) -> np.ndarray:
        pools = [grid.poly_cells(l, p.poly) for l in p.layers
                 if l in grid.mask]
        pool = (np.unique(np.concatenate(pools))
                if pools and sum(len(x) for x in pools) else
                np.empty(0, dtype=np.int64))
        if len(pool):
            return pool
        reach = max(p.size) / 2.0 + 3.0 * cell
        near = grid.nearest_node([l for l in p.layers if l in grid.mask],
                                 p.center[0], p.center[1], reach)
        return (np.array([near], dtype=np.int64) if near is not None
                else np.empty(0, dtype=np.int64))

    src_nodes = (np.unique(np.concatenate(
        [pad_nodes(p) for p in groups[source_ref]] or
        [np.empty(0, dtype=np.int64)])))
    if not len(src_nodes):
        raise CheckError(f"source {source_ref!r} pads land on no copper cells")

    # connected components: only what the source can reach is solvable
    n_comp, labels = connected_components(adj, directed=False)
    keep_labels = set(labels[src_nodes].tolist())
    kept = np.isin(labels, sorted(keep_labels))

    b = np.zeros(grid.n_nodes)
    warnings: list[dict] = []
    injected = 0.0
    sink_pad_count = 0
    sink_drop_nodes: dict[str, list[np.ndarray]] = {}
    for ref, pad, amps in sink_currents(entry, groups, source_ref):
        if amps <= 0:
            continue
        nodes = pad_nodes(pad)
        nodes = nodes[kept[nodes]] if len(nodes) else nodes
        if not len(nodes):
            warnings.append(violation(
                SCRIPT, "warning", pad.center, pad.layers[0], net, [ref],
                f"{net} sink pad {ref} pad {pad.number} at "
                f"({pad.center[0]:.2f}, {pad.center[1]:.2f}) is not connected "
                f"to the source copper on the solver grid (open or "
                f"rasterization artifact); its {amps:.3f} A was dropped",
                SCRIPT, kind="sink_disconnected"))
            continue
        b[nodes] -= amps / len(nodes)
        injected += amps
        sink_pad_count += 1
        sink_drop_nodes.setdefault(ref, []).append(nodes)
    if injected <= 0:
        raise CheckError(
            f"all sink pads on {net!r} are disconnected from the source "
            "copper on the solver grid; nothing to solve")

    is_src = np.zeros(grid.n_nodes, dtype=bool)
    is_src[src_nodes] = True
    unknown = np.nonzero(kept & ~is_src)[0]
    deg = np.asarray(adj.sum(axis=1)).ravel()
    lap = (sparse.diags(deg) - adj).tocsr()
    lap_uu = lap[unknown][:, unknown].tocsr()
    lay, rows, cols = grid.node_coords()
    v_u, solver = solve_spd(lap_uu, b[unknown],
                            (lay[unknown], rows[unknown], cols[unknown]))
    v = np.zeros(grid.n_nodes)
    v[unknown] = v_u
    drop = -v  # volts below the source

    sinks = []
    for ref in sorted(sink_drop_nodes):
        nodes = np.concatenate(sink_drop_nodes[ref])
        sinks.append((ref, float(drop[nodes].mean())))
    worst_ref, worst_sink_v = max(sinks, key=lambda t: t[1])
    worst_node = int(np.argmax(np.where(kept, drop, -np.inf)))

    jmax, jmax_pos, jmax_layer, necks = current_density(grid, v)

    return {
        "cell": cell,
        "cells": grid.n_nodes,
        "kept_cells": int(kept.sum()),
        "source_ref": source_ref,
        "source_mode": source_mode,
        "injected_a": injected,
        "sink_pads": sink_pad_count,
        "resistance_ohm": worst_sink_v / injected,
        "worst_sink": {"ref": worst_ref, "drop_v": worst_sink_v},
        "sink_drops": dict(sinks),  # ref -> mean drop (V), check_rail_budget
        "worst_drop_v": float(drop[worst_node]),
        "jmax": jmax, "jmax_pos": jmax_pos, "jmax_layer": jmax_layer,
        "necks": necks,
        "vias_used": vias_used, "vias_skipped": vias_skipped,
        "solver": dict(solver, unknowns=int(len(unknown))),
        "warnings": warnings,
    }


def _cut_vector(grid: NetGrid, layer: str, at, u, half: float):
    """The flux functional of one cross-section of a track: c with
    c . v = the current (A) crossing the line through `at` normal to the
    unit direction `u`, within `half` mm of `at`, on `layer`, counted
    positive along u. None when the section isn't clean: copper of the
    net lies just beyond `half` beside it (the line would cut a pour or a
    neighbour as well as the track), or fewer than two cell edges cross."""
    h = grid.cell
    keys, n0 = grid.keys[layer], grid.n0[layer]
    if not len(keys):
        return None
    px, py = at
    ux, uy = u
    ii, jj = np.divmod(keys, grid.nx)
    du = (grid.xs[jj] - px) * ux + (grid.ys[ii] - py) * uy
    dn = -(grid.xs[jj] - px) * uy + (grid.ys[ii] - py) * ux
    beside = (np.abs(du) <= h) & (np.abs(dn) > half) \
        & (np.abs(dn) <= half + 1.5 * h)
    if beside.any():
        return None
    g = 1.0 / sheet_res_ohm_sq(grid.bg.stackup.copper_thickness[layer])
    rows, vals = [], []
    for a, b, _ka, _kb in grid.layer_edges(layer):
        da, db = du[a - n0], du[b - n0]
        mid = 0.5 * (dn[a - n0] + dn[b - n0])
        cross = ((da < 0) != (db < 0)) & (np.abs(mid) <= half)
        if not cross.any():
            continue
        sgn = np.sign(db[cross] - da[cross])
        rows += [a[cross], b[cross]]
        vals += [g * sgn, -g * sgn]
    if not rows or sum(len(r) for r in rows) < 4:
        return None
    return (np.concatenate(rows), np.concatenate(vals))


def cut_transfer(bg: geom.BoardGeom, net: str, layers: list[str],
                 cell: float, cuts: list, source_refs: list,
                 rhs_cap: int = 64, mg_rhs_cap: int = 16):
    """Transfer fractions for check_current's grid bound (#1169). For each
    source ref s, T[c, k] is the current (A) crossing cut c, counted along
    its direction, per ampere drawn by ref k with s's pads held at 0 V and
    every other ref drawing nothing. The net is rasterized and assembled
    once for all sources. cuts: [(layer, (x, y), (ux, uy), half_mm)].
    T = C^T L^-1 E, solved on whichever side has fewer columns (one solve
    per cut, or per sink): sparse LU up to DIRECT_MAX_UNKNOWNS unknowns and
    rhs_cap columns, multigrid CG above it up to mg_rhs_cap columns.

    Returns ({source: (T, refs) | "why it couldn't be solved"}, facts):
    refs names T's columns (every other ref with pads on the net); a NaN
    row is a cut that isn't clean (_cut_vector) or not on the source's
    copper, a NaN column a ref the grid doesn't connect to the source."""
    grid = NetGrid(bg, net, layers, cell)
    adj, _used, _skipped = assemble(grid)
    groups = pad_groups(bg, net)
    deg = np.asarray(adj.sum(axis=1)).ravel()
    lap = (sparse.diags(deg) - adj).tocsr()
    lay, rows, cols = grid.node_coords()
    _n, labels = connected_components(adj, directed=False)

    def pad_nodes(p) -> np.ndarray:
        pools = [grid.poly_cells(l, p.poly) for l in p.layers
                 if l in grid.mask]
        pool = (np.unique(np.concatenate(pools))
                if pools and sum(len(x) for x in pools) else
                np.empty(0, dtype=np.int64))
        if len(pool):
            return pool
        near = grid.nearest_node([l for l in p.layers if l in grid.mask],
                                 p.center[0], p.center[1],
                                 max(p.size) / 2.0 + 3.0 * cell)
        return (np.array([near], dtype=np.int64) if near is not None
                else np.empty(0, dtype=np.int64))

    ref_nodes = {}
    for ref in sorted(groups):
        nodes = [pad_nodes(p) for p in groups[ref]]
        ref_nodes[ref] = np.unique(np.concatenate(
            nodes + [np.empty(0, dtype=np.int64)]))
    sections = [(_cut_vector(grid, layer, at, u, half)
                 if layer in grid.mask else None)
                for layer, at, u, half in cuts]

    out: dict = {}
    facts = {"cell_mm": checklib.rnd(cell, 5), "cells": int(grid.n_nodes),
             "solves": 0, "method": "direct"}
    for source in source_refs:
        src = ref_nodes.get(source, np.empty(0, dtype=np.int64))
        if not len(src):
            out[source] = f"source {source} lands on no copper cells"
            continue
        kept = np.isin(labels, np.unique(labels[src]))
        is_src = np.zeros(grid.n_nodes, dtype=bool)
        is_src[src] = True
        unknown = np.nonzero(kept & ~is_src)[0]
        pos = np.full(grid.n_nodes, -1, dtype=np.int64)
        pos[unknown] = np.arange(len(unknown))
        nu = len(unknown)
        refs = [r for r in sorted(groups) if r != source]
        t = np.full((len(cuts), len(refs)), np.nan)
        cmat = []                             # (cut index, rows, vals)
        for ci, cv in enumerate(sections):
            if cv is None or not kept[cv[0]].all():
                continue
            r, v = cv
            sel = pos[r] >= 0                 # source nodes sit at 0 V
            cmat.append((ci, pos[r[sel]], v[sel]))
        emat = []                             # (ref index, rows, vals)
        for si, ref in enumerate(refs):
            nodes = ref_nodes[ref]
            if not len(nodes) or not kept[nodes].all():
                continue
            p = pos[nodes]
            p = p[p >= 0]
            emat.append((si, p, np.full(len(p), -1.0 / len(nodes))))
        if not cmat or not emat or not nu:
            out[source] = "no clean section or connected sink to solve"
            continue

        def as_matrix(cols_):
            r = np.concatenate([x[1] for x in cols_])
            c = np.concatenate([np.full(len(x[1]), j)
                                for j, x in enumerate(cols_)])
            v = np.concatenate([x[2] for x in cols_])
            return sparse.csc_matrix((v, (r, c)), shape=(nu, len(cols_)))

        cm, em = as_matrix(cmat), as_matrix(emat)
        adjoint = len(cmat) <= len(emat)
        rhs = cm if adjoint else em
        direct = nu <= DIRECT_MAX_UNKNOWNS
        cap = rhs_cap if direct else mg_rhs_cap
        if rhs.shape[1] > cap:
            out[source] = (f"{rhs.shape[1]} right-hand sides on "
                           f"{nu} unknowns exceed the cap of {cap}")
            continue
        a = lap[unknown][:, unknown].tocsr()
        if direct:
            x = splu(a.tocsc()).solve(rhs.toarray())
        else:
            facts["method"] = "multigrid_cg"
            mg = _Multigrid(a, (lay[unknown], rows[unknown], cols[unknown]))
            pre = LinearOperator(a.shape, matvec=mg.vcycle, dtype=float)
            x = np.zeros(rhs.shape)
            failed = None
            for j in range(rhs.shape[1]):
                b = rhs[:, j].toarray().ravel()
                its = [0]

                def count(_x, its=its):
                    its[0] += 1
                x[:, j], info = cg(a, b, rtol=CG_RTOL, atol=0.0,
                                   maxiter=CG_MAXITER, M=pre,
                                   callback=count)
                if info != 0:
                    failed = f"CG did not converge in {its[0]} iterations"
                    break
                facts["iterations_max"] = max(
                    facts.get("iterations_max", 0), its[0])
            if failed:
                out[source] = failed
                continue
        facts["solves"] += int(rhs.shape[1])
        # adjoint: x = L^-1 C (nu x m), so E^T x is K x m; else x = L^-1 E
        # and C^T x is m x K. prod is m x K either way.
        prod = np.asarray((em.T @ x).T) if adjoint else np.asarray(cm.T @ x)
        for a_i, (ci, _r, _v) in enumerate(cmat):
            for b_i, (si, _r2, _v2) in enumerate(emat):
                t[ci, si] = prod[a_i, b_i]
        out[source] = (t, refs)
    return out, facts


def current_density(grid: NetGrid, v: np.ndarray):
    """Sheet current density |K| (A/mm) per cell from the solved voltages.
    Returns (jmax, [x, y], layer, top-neck list). K on the edge between two
    cells is the edge current / cell size; a cell's K averages its incident
    edge currents per axis, so interior necks are exact and boundary cells
    read slightly low (documented; necks are interior by construction).
    Worked per node, not per frame cell, so it costs memory in the copper."""
    h = grid.cell
    jmax, jmax_pos, jmax_layer = 0.0, None, None
    cands: list[tuple[float, tuple[float, float], str]] = []
    for l in grid.layers:
        keys, n0 = grid.keys[l], grid.n0[l]
        cnt = len(keys)
        if not cnt:
            continue
        g = 1.0 / sheet_res_ohm_sq(grid.bg.stackup.copper_thickness[l])
        half = []
        for a, b, _ka, _kb in grid.layer_edges(l):
            w = 0.5 * (g * (v[a] - v[b]))
            half.append(np.bincount(a - n0, weights=w, minlength=cnt)
                        + np.bincount(b - n0, weights=w, minlength=cnt))
        jj = np.hypot(half[0], half[1]) / h
        peak = float(jj.max())
        if peak > jmax:
            i, j = divmod(int(keys[int(np.argmax(jj))]), grid.nx)
            jmax = peak
            jmax_pos = grid.cell_center(i, j)
            jmax_layer = l
        sel = (np.flatnonzero(jj >= 0.3 * peak) if peak > 0
               else np.empty(0, dtype=np.int64))
        vals = jj[sel]
        order = np.argsort(-vals, kind="stable")[:2000]
        ci, cj = np.divmod(keys[sel[order]], grid.nx)
        cands.extend((float(vals[o]), grid.cell_center(int(i), int(j)), l)
                     for o, i, j in zip(order, ci, cj))
    necks: list[dict] = []
    cands.sort(key=lambda t: (-t[0], t[1], t[2]))
    for val, pos, layer in cands:
        if val < 0.3 * jmax or len(necks) >= NECK_KEEP:
            break
        if all(n["layer"] != layer or
               math.hypot(pos[0] - n["pos"][0], pos[1] - n["pos"][1])
               >= NECK_MIN_SEP_MM for n in necks):
            necks.append({"a_per_mm": checklib.rnd(val),
                          "pos": [checklib.rnd(pos[0]), checklib.rnd(pos[1])],
                          "layer": layer})
    return jmax, jmax_pos, jmax_layer, necks


# ============================================================ per-net check

def pick_cell_mm(bg: geom.BoardGeom, net: str, layers: list[str],
                 entry: dict):
    """(cell_mm, min_feature_mm) per the documented sizing rules."""
    tracks = bg.tracks_of(net)
    if tracks:
        feat = min(t.width for t in tracks)
    else:
        pads = bg.pads_of(net)
        feat = min(min(p.size) for p in pads) if pads else 0.5
    if entry.get("cell_mm"):
        h = min(max(float(entry["cell_mm"]), CELL_MIN_MM), CELL_MAX_MM)
        return h, feat
    h = min(max(feat / NECK_CELLS, CELL_MIN_MM), CELL_MAX_MM)
    area = sum(bg.net_area(net, l) for l in layers)
    if area / (h * h) > BASE_CELL_CAP:
        h = math.sqrt(area / BASE_CELL_CAP)
    return h, feat


def check_net(bg: geom.BoardGeom, entry: dict):
    net = entry.get("net")
    if not net:
        raise CheckError('power entry without "net"')
    if net not in bg.nets:
        raise CheckError(f"power net {net!r} not on board "
                         f"(nets: {sorted(n for n in bg.nets if n)})")
    if "current_a" not in entry:
        raise CheckError(f'power entry {net!r} without "current_a"')
    layers = [l for l in bg.copper_layers if bg.net_area(net, l) > 0]
    if not layers:
        raise CheckError(f"net {net!r} has no copper on any layer")

    cell, feat = pick_cell_mm(bg, net, layers, entry)
    base = solve_grid(bg, entry, net, layers, cell)
    if 4 * base["cells"] <= REFINE_CELL_CAP:
        other = solve_grid(bg, entry, net, layers, cell / 2.0)
        fine, refine = other, "doubled"
    else:
        other = solve_grid(bg, entry, net, layers, cell * 2.0)
        fine, refine = base, "coarsened"
    coarse = base if fine is not base else other
    r_fine = fine["resistance_ohm"]
    r_coarse = coarse["resistance_ohm"]
    delta = abs(r_fine - r_coarse) / max(abs(r_fine), 1e-12)

    violations = list(fine["warnings"])
    if delta > RICHARDSON_WARN:
        violations.append(violation(
            SCRIPT, "warning", None, None, net, [],
            f"{net} IR-drop grid not converged: source->worst-sink resistance "
            f"moved {delta:.1%} between {coarse['cell']:.4f} and "
            f"{fine['cell']:.4f} mm grids "
            f"({r_coarse * 1e3:.3f} -> {r_fine * 1e3:.3f} mOhm)", SCRIPT,
            kind="grid_unconverged", richardson_delta=checklib.rnd(delta)))

    worst_mv = fine["worst_drop_v"] * 1e3
    limit_mv = entry.get("irdrop_mv_max")
    if limit_mv is not None and worst_mv > float(limit_mv):
        violations.append(violation(
            SCRIPT, "error", fine["jmax_pos"], fine["jmax_layer"], net,
            [fine["worst_sink"]["ref"]],
            f"{net} worst IR drop {worst_mv:.1f} mV at "
            f"{fine['injected_a']:.2f} A exceeds irdrop_mv_max "
            f"{float(limit_mv):.1f} mV (worst sink "
            f"{fine['worst_sink']['ref']})", SCRIPT, kind="irdrop_excess",
            drop_mv=checklib.rnd(worst_mv), limit_mv=float(limit_mv)))
    limit_j = entry.get("jmax_a_per_mm")
    if limit_j is not None and fine["jmax"] > float(limit_j):
        violations.append(violation(
            SCRIPT, "error", fine["jmax_pos"], fine["jmax_layer"], net, [],
            f"{net} sheet current density {fine['jmax']:.2f} A/mm at "
            f"({fine['jmax_pos'][0]:.2f}, {fine['jmax_pos'][1]:.2f}) on "
            f"{fine['jmax_layer']} exceeds jmax_a_per_mm "
            f"{float(limit_j):.2f}", SCRIPT, kind="current_density_excess",
            a_per_mm=checklib.rnd(fine["jmax"]),
            limit_a_per_mm=float(limit_j)))

    facts = {
        "net": net,
        "requested_a": float(entry["current_a"]),
        "injected_a": checklib.rnd(fine["injected_a"]),
        "source_ref": fine["source_ref"],
        "source_mode": fine["source_mode"],
        "sink_pads": fine["sink_pads"],
        "layers": layers,
        "min_feature_mm": checklib.rnd(feat),
        "grid": {
            "cell_mm": checklib.rnd(fine["cell"], 5),
            "cells": fine["cells"],
            "cells_across_min_feature": checklib.rnd(feat / fine["cell"], 2),
            "refine": refine,
            "cell_mm_pair": checklib.rnd(coarse["cell"], 5),
            "cells_pair": coarse["cells"],
            "solver": fine["solver"],
            "solver_pair": coarse["solver"],
        },
        "resistance_mohm": checklib.rnd(r_fine * 1e3),
        "resistance_pair_mohm": [checklib.rnd(r_fine * 1e3),
                                 checklib.rnd(r_coarse * 1e3)],
        "richardson_delta": checklib.rnd(delta),
        "worst_drop_mv": checklib.rnd(worst_mv),
        "worst_sink": {"ref": fine["worst_sink"]["ref"],
                       "drop_mv": checklib.rnd(
                           fine["worst_sink"]["drop_v"] * 1e3)},
        "jmax": {"a_per_mm": checklib.rnd(fine["jmax"]),
                 "pos": [checklib.rnd(fine["jmax_pos"][0]),
                         checklib.rnd(fine["jmax_pos"][1])]
                 if fine["jmax_pos"] else None,
                 "layer": fine["jmax_layer"]},
        "necks": fine["necks"],
        "vias": {"used": fine["vias_used"], "skipped": fine["vias_skipped"]},
    }
    return violations, facts


# ============================================================ CLI

def run(argv=None):
    ap = argparse.ArgumentParser(
        description="DC IR-drop + sheet current density per power net "
                    "(2.5D resistor-grid FDM).")
    ap.add_argument("--pcb", required=True, help="path to .kicad_pcb")
    ap.add_argument("--constraints", required=True,
                    help="constraints.json with a power list")
    ap.add_argument("--out", help="write JSON report here instead of stdout")
    args = ap.parse_args(argv)

    cons = checklib.load_json(args.constraints, "constraints")
    entries = cons.get("power", [])
    bg = geom.load_board(Path(args.pcb))
    bg.assert_fresh()

    violations: list[dict] = []
    checked: list[dict] = []
    for entry in entries:
        if entry.get("pdn") is False:
            checked.append({"net": entry.get("net"),
                            "skipped": "pdn:false (width-only power entry)"})
            continue
        if "current_a" in entry and float(entry["current_a"]) <= 0:
            checked.append({"net": entry.get("net"),
                            "skipped": "current_a <= 0 (placeholder entry; "
                                       "nothing to solve)"})
            continue
        vs, facts = check_net(bg, entry)
        violations.extend(vs)
        checked.append(facts)

    payload = checklib.report(SCRIPT, args.pcb, violations, checked=checked,
                              stackup_assumed=bg.stackup.assumed,
                              model={
                                  "kind": "2.5D resistor-grid FDM",
                                  "rho_cu_ohm_m": RHO_CU_OHM_M,
                                  "via_plating_mm": PLATING_MM,
                                  "injection": "worst-case uniform per pad "
                                               "unless source_ref/sinks given",
                                  "solver": f"sparse LU up to "
                                            f"{DIRECT_MAX_UNKNOWNS} unknowns, "
                                            f"multigrid-preconditioned CG "
                                            f"(rtol {CG_RTOL:g}) above",
                              })
    return payload, args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())
