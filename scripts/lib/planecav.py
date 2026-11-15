"""planecav.py - plane-pair cavity on the real plane shape, by segmentation.

check_pdn_z's closed form holds for a rectangle. A real plane pair is an L, a
split or a cut-out, so this module cuts the overlap polygon into rectangles,
gives each the same modal sum, and joins them along their shared edges (the
segmentation method: Okoshi & Takeuchi; Okoshi, 'Planar Circuits'; Sharma &
Gupta for power planes).

JOIN. A shared edge becomes pairs of strip ports (q on one rectangle, r on the
other), with mean voltage equal and current opposite across each pair:

    Z_eff = Z_pp - Z_pc A^-1 Z_cp
    Z_pc  = Z_pq - Z_pr ,  A = Z_qq - Z_qr - Z_rq + Z_rr

SPEED (mode acceleration). A sum over modes costs ports^2 * modes per frequency
and the strips need many modes. Modes far above the band vary slowly, so
1/(lam - k^2) ~ 1/lam + k^2/lam^2 folds them into two fixed matrices. Only the
low modes are summed per frequency. The error goes as (k^2/lam)^3.

MODES. With no loss the join is a constrained modal problem, and its count of
eigenvalues below s = k^2 is exact (Wittrick-Williams, from the inertia of the
bordered matrix):

    N(s) = N_free(s) - n_neg(C (Lam - s)^-1 C^T)

Bisecting N(s) cannot skip a mode, even one with a node on every probe or one
whose field vanishes on the join. This is why modes do not come from peak
picking on Z(f). Mode shapes come from the response at the mode's frequency
with a small loss, driven from several probes (an SVD separates degenerate
pairs).

SHAPE. `decompose` uses a grid through the polygon's own vertices, so a
rectilinear plane is exact whatever `h_mm` is, and a thin slot stays a slot.
Slanted edges become staircases, whose modes are measured against the right
triangle's exact ones in tests/test_planecav.py: worst of the first four -2.3 %
at 16 cells a side, -0.8 % at 32, +0.4 % at 64. Palace meshes the true polygon,
so use it to check a plane with long diagonal edges.

BOUNDARY. Open plane edges are perfect magnetic walls, as in the closed form,
so fringing is left out. Fields are uniform in z, so d only scales Z, and the
modes do not depend on d. Vias and stitching between the planes are not in the
model.

Units: rectangles and ports are in metres in the caller's frame, except where
a name says mm.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field

# Hundreds of small solves: OpenBLAS's pool busy-waits on them and stalls on a
# loaded machine (see lib/solvers.py), and one thread costs nothing here.
for _v in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, "1")

import numpy as np  # noqa: E402 (after the BLAS thread pin)
import shapely  # noqa: E402
from scipy.interpolate import CubicSpline  # noqa: E402
from shapely.geometry import Polygon  # noqa: E402

MU0 = 4e-7 * math.pi
EPS0 = 8.8541878128e-12
C0 = 299792458.0

K_STRIP = 3          # modes per strip width along an edge: M >= K_STRIP * a / w
MIN_MODES = 8        # per axis, per rectangle
LOW_FACTOR = 16.0    # modes under this * k_max^2 are summed exactly
MAX_COORDS = 160     # per axis, before the polygon is simplified harder
MAX_STRIPS = 200     # joins wider than this get wider strips: cost goes as cube
SWEEP_STEP = 5       # below half the first mode, solve every this point of a sweep
RECT_FILL = 0.97     # model area / bounding box at or above this is a rectangle
PROBE_LOSS = 2e-3    # relative imaginary part of k^2 when reading a mode shape
DEGENERATE = 1e-3    # modes closer than this (relative f) form one cluster


def sinc(x):
    return np.sinc(np.asarray(x, dtype=float) / np.pi)


def components(geom) -> list[Polygon]:
    """The separate polygons of a fill, largest first."""
    if geom is None or geom.is_empty:
        return []
    polys = list(geom.geoms) if hasattr(geom, "geoms") else [geom]
    polys = [p for p in polys if isinstance(p, Polygon) and p.area > 0]
    return sorted(polys, key=lambda p: -p.area)


def _grid_coords(values, lo, hi, h, merge):
    vals = sorted({lo, hi, *[v for v in values if lo <= v <= hi]})
    kept = [vals[0]]
    for v in vals[1:]:
        if v - kept[-1] >= merge:
            kept.append(v)
    if kept[-1] != hi:
        kept[-1] = hi
    out = [kept[0]]
    for v in kept[1:]:
        n = max(1, math.ceil((v - out[-1]) / h - 1e-9))
        step = (v - out[-1]) / n
        base = out[-1]
        out.extend(base + step * (i + 1) for i in range(n - 1))
        out.append(v)
    return out


def _clean(poly: Polygon, h: float) -> Polygon:
    """Drop holes that a cell would not see: antipads and thermal windows.
    A hole under a quarter cell goes, whatever its shape; that is a slot under
    about a cell long at the narrowest."""
    keep = [r for r in poly.interiors if Polygon(r).area >= 0.25 * h * h]
    return Polygon(poly.exterior, keep)


def outline(poly: Polygon, h: float, tol: float = 0.05) -> Polygon:
    """The polygon a mesher should see: no holes a cell would miss, small
    wiggles removed."""
    return _clean(poly, h).simplify(tol, preserve_topology=True)


def _long_edge_coords(p: Polygon, min_len: float, merge: float):
    """x and y of the ends of axis-aligned edges at least min_len long: a
    plane's splits and cut-outs set the grid, and an antipad's facets do not."""
    xs, ys = [], []
    for ring in [p.exterior, *p.interiors]:
        c = np.asarray(ring.coords)
        d = np.abs(np.diff(c, axis=0))
        keep = ((d[:, 0] <= merge) & (d[:, 1] >= min_len)
                | (d[:, 1] <= merge) & (d[:, 0] >= min_len))
        for i in np.nonzero(keep)[0]:
            xs += [c[i, 0], c[i + 1, 0]]
            ys += [c[i, 1], c[i + 1, 1]]
    return xs, ys


def decompose(poly: Polygon, h_mm: float, merge_mm: float = 0.05) -> dict:
    """Rectangles (x0, y0, a, b in mm) that cover one polygon, plus the raster
    facts. A cell is copper when at least half of it is."""
    s = _clean(poly, h_mm).simplify(merge_mm, preserve_topology=True)
    minx, miny, maxx, maxy = poly.bounds
    min_len = 0.5 * h_mm
    while True:
        xv, yv = _long_edge_coords(s, min_len, merge_mm)
        xs = _grid_coords(xv, minx, maxx, h_mm, merge_mm)
        ys = _grid_coords(yv, miny, maxy, h_mm, merge_mm)
        if max(len(xs), len(ys)) <= MAX_COORDS:
            break
        min_len *= 2.0
    xs_a, ys_a = np.asarray(xs), np.asarray(ys)
    nx, ny = len(xs) - 1, len(ys) - 1
    x0 = np.repeat(xs_a[:-1], ny)
    x1 = np.repeat(xs_a[1:], ny)
    y0 = np.tile(ys_a[:-1], nx)
    y1 = np.tile(ys_a[1:], nx)
    cells = shapely.box(x0, y0, x1, y1)
    frac = shapely.area(shapely.intersection(cells, s)) / shapely.area(cells)
    occ = (frac >= 0.5).reshape(nx, ny)
    occ = _largest_blob(occ)
    rects = _merge_cells(occ, xs, ys)
    area_model = sum(r[2] * r[3] for r in rects)
    return {"rects": rects, "area_model_mm2": area_model,
            "area_true_mm2": poly.area,
            "bbox_mm2": (maxx - minx) * (maxy - miny),
            "fill": area_model / max((maxx - minx) * (maxy - miny), 1e-12)}


def _largest_blob(occ: np.ndarray) -> np.ndarray:
    """Keep the largest edge-connected set of cells: a neck thinner than half
    a cell cuts the plane in the raster and the rest has no join to it."""
    seen = np.zeros_like(occ, dtype=bool)
    best: list[tuple[int, int]] = []
    nx, ny = occ.shape
    for i, j in zip(*np.nonzero(occ)):
        if seen[i, j]:
            continue
        stack, blob = [(int(i), int(j))], []
        seen[i, j] = True
        while stack:
            a, b = stack.pop()
            blob.append((a, b))
            for c, e in ((a + 1, b), (a - 1, b), (a, b + 1), (a, b - 1)):
                if 0 <= c < nx and 0 <= e < ny and occ[c, e] and not seen[c, e]:
                    seen[c, e] = True
                    stack.append((c, e))
        if len(blob) > len(best):
            best = blob
    out = np.zeros_like(occ, dtype=bool)
    for a, b in best:
        out[a, b] = True
    return out


def _merge_cells(occ, xs, ys) -> list[tuple[float, float, float, float]]:
    """Occupied cells to rectangles: runs along y per column, then merge equal
    runs of neighbouring columns."""
    nx, ny = occ.shape
    open_runs: dict[tuple[int, int], int] = {}
    rects = []
    for i in range(nx + 1):
        runs = []
        if i < nx:
            j = 0
            while j < ny:
                if occ[i, j]:
                    k = j
                    while k < ny and occ[i, k]:
                        k += 1
                    runs.append((j, k))
                    j = k
                else:
                    j += 1
        current = set(runs)
        for run in [r for r in open_runs if r not in current]:
            start = open_runs.pop(run)
            rects.append((xs[start], ys[run[0]], xs[i] - xs[start],
                           ys[run[1]] - ys[run[0]]))
        for run in runs:
            open_runs.setdefault(run, i)
    return rects


@dataclass
class Rect:
    x0: float
    y0: float
    a: float
    b: float

    def contains(self, x, y, eps=1e-9) -> bool:
        return (self.x0 - eps <= x <= self.x0 + self.a + eps
                and self.y0 - eps <= y <= self.y0 + self.b + eps)

    def dist(self, x, y) -> float:
        dx = max(self.x0 - x, 0.0, x - self.x0 - self.a)
        dy = max(self.y0 - y, 0.0, y - self.y0 - self.b)
        return math.hypot(dx, dy)


def _shared_edges(rects: list[Rect], eps: float):
    """(i, j, axis, pos, lo, hi): rectangles i and j touch along a line."""
    out = []
    for i, r in enumerate(rects):
        for j in range(i + 1, len(rects)):
            s = rects[j]
            for p, q, ii, jj in ((r, s, i, j), (s, r, j, i)):
                if abs(p.x0 + p.a - q.x0) < eps:
                    lo, hi = max(p.y0, q.y0), min(p.y0 + p.b, q.y0 + q.b)
                    if hi - lo > eps:
                        out.append((ii, jj, "x", q.x0, lo, hi))
                if abs(p.y0 + p.b - q.y0) < eps:
                    lo, hi = max(p.x0, q.x0), min(p.x0 + p.a, q.x0 + q.a)
                    if hi - lo > eps:
                        out.append((ii, jj, "y", q.y0, lo, hi))
    return out


@dataclass
class Cavity:
    """A plane pair cut into rectangles. `w` is the strip width on a join and
    `k_cut` the wavenumber the mode sum reaches (radians/m)."""
    rects: list[Rect]
    d: float
    eps_r: float
    w: float
    k_cut: float
    joins: list = field(default_factory=list)   # (rect q, rect r, axis, s, ds)
    _modes: list = field(default_factory=list)

    def __post_init__(self):
        eps = 1e-9
        self.joins = []
        for i, j, axis, pos, lo, hi in _shared_edges(self.rects, eps):
            n = max(1, math.ceil((hi - lo) / self.w - 1e-9))
            ds = (hi - lo) / n
            for t in range(n):
                self.joins.append((i, j, axis, pos, lo + (t + 0.5) * ds, ds))
        self._modes = [self._rect_modes(k, r)
                       for k, r in enumerate(self.rects)]

    @property
    def area(self) -> float:
        return sum(r.a * r.b for r in self.rects)

    def _strip_ports(self, idx: int):
        """(row in C, sign, port) of each strip on rectangle idx."""
        out = []
        for row, (i, j, axis, pos, s, ds) in enumerate(self.joins):
            if idx not in (i, j):
                continue
            sign = 1.0 if idx == i else -1.0
            port = (pos, s, 0.0, ds) if axis == "x" else (s, pos, ds, 0.0)
            out.append((row, sign, port))
        return out

    def _rect_modes(self, idx: int, r: Rect) -> dict:
        nm = max(MIN_MODES, math.ceil(r.a * self.k_cut / math.pi),
                 math.ceil(K_STRIP * r.a / self.w))
        nn = max(MIN_MODES, math.ceil(r.b * self.k_cut / math.pi),
                 math.ceil(K_STRIP * r.b / self.w))
        kx = np.arange(nm + 1) * math.pi / r.a
        ky = np.arange(nn + 1) * math.pi / r.b
        return {"kx": kx, "ky": ky,
                "lam": (kx[:, None] ** 2 + ky[None, :] ** 2).ravel(),
                "wt": np.outer(np.where(kx > 0, 2.0, 1.0),
                               np.where(ky > 0, 2.0, 1.0)).ravel() ** 0.5
                / math.sqrt(r.a * r.b)}

    def _rows(self, idx: int, ports) -> np.ndarray:
        """Mode amplitudes at ports, (P, modes); modes are orthonormal over
        the rectangle's area."""
        r, md = self.rects[idx], self._modes[idx]
        rows = []
        for (x, y, tx, ty) in ports:
            # A port off this rectangle (raster gap, another grid) sits on its
            # nearest point: the cosines mean nothing outside.
            u = min(max(x - r.x0, 0.0), r.a)
            v = min(max(y - r.y0, 0.0), r.b)
            fx = np.cos(md["kx"] * u) * sinc(md["kx"] * tx / 2.0)
            fy = np.cos(md["ky"] * v) * sinc(md["ky"] * ty / 2.0)
            rows.append(np.outer(fx, fy).ravel() * md["wt"])
        return np.vstack(rows) if rows else np.zeros((0, md["lam"].size))

    def _locate(self, x, y) -> int:
        inside = [i for i, r in enumerate(self.rects) if r.contains(x, y)]
        if inside:
            return inside[0]
        return int(np.argmin([r.dist(x, y) for r in self.rects]))

    def snap(self, x, y) -> tuple[float, float, float]:
        """(x, y) moved onto the plane, and how far it moved."""
        i = self._locate(x, y)
        r = self.rects[i]
        sx = min(max(x, r.x0), r.x0 + r.a)
        sy = min(max(y, r.y0), r.y0 + r.b)
        return sx, sy, math.hypot(x - sx, y - sy)

    def _prepare(self, ports, s_max: float):
        """Per rectangle: where its ports sit in the global (ext, q, r)
        numbering, its low-mode rows and lambdas, and the folded high modes."""
        n_ext, m = len(ports), len(self.joins)
        owner = [self._locate(p[0], p[1]) for p in ports]
        cut = max(LOW_FACTOR * s_max, 1.0)
        blocks = []
        for idx in range(len(self.rects)):
            ext = [k for k in range(n_ext) if owner[k] == idx]
            strips = self._strip_ports(idx)
            bm = self._rows(idx, [ports[k] for k in ext]
                            + [p for _, _, p in strips])
            lam = self._modes[idx]["lam"]
            low = lam <= cut
            bh, lh = bm[:, ~low], lam[~low]
            blocks.append({
                "sel": np.array(ext + [n_ext + row + (0 if sign > 0 else m)
                                       for row, sign, _ in strips], dtype=int),
                "bl": bm[:, low], "ll": lam[low],
                "s0": (bh / lh) @ bh.T, "s1": (bh / lh ** 2) @ bh.T})
        return blocks

    @staticmethod
    def _green(blocks, size: int, sigma, dtype=complex) -> np.ndarray:
        full = np.zeros((size, size), dtype=dtype)
        for b in blocks:
            full[np.ix_(b["sel"], b["sel"])] = (
                (b["bl"] / (b["ll"] - sigma)) @ b["bl"].T
                + b["s0"] + sigma * b["s1"])
        return full

    def _solve(self, blocks, n_ext: int, sigma: complex) -> np.ndarray:
        """Eliminate the joins: the modal part of Z_eff at k^2 = sigma, in
        1/m^2 (multiply by j w mu0 d to get ohm), for the external ports."""
        m = len(self.joins)
        full = self._green(blocks, n_ext + 2 * m, sigma)
        if m == 0:
            return full
        q = np.arange(n_ext, n_ext + m)
        r = q + m
        zc = full[:, q] - full[:, r]
        a = zc[q] - zc[r]
        cp = full[q][:, :n_ext] - full[r][:, :n_ext]
        return full[:n_ext, :n_ext] - zc[:n_ext] @ np.linalg.solve(a, cp)

    def z(self, freqs_hz, ports, lossy_k=None) -> np.ndarray:
        """Impedance matrix (F, P, P) between ports (x, y, tx, ty) in metres.
        `lossy_k(w)` gives the complex wavenumber; default: no loss."""
        freqs = np.asarray(freqs_hz, dtype=float)
        k_max = 2 * math.pi * freqs.max() * math.sqrt(self.eps_r) / C0
        blocks = self._prepare(ports, k_max * k_max)
        out = np.empty((len(freqs), len(ports), len(ports)), dtype=complex)
        for i, f in enumerate(freqs):
            w = 2 * math.pi * f
            k = lossy_k(w) if lossy_k else w * math.sqrt(self.eps_r) / C0
            out[i] = 1j * w * MU0 * self.d * self._solve(blocks, len(ports), k * k)
        return out

    def z_sweep(self, freqs_hz, ports, lossy_k=None) -> np.ndarray:
        """As z() on a fine sweep at a fifth of the solves: under half the first
        mode Z * j w C00 - 1 is smooth in log f, so a spline carries it across
        the skipped points. From there up every point is solved: a resonance's
        width is under the grid."""
        freqs = np.asarray(freqs_hz, dtype=float)
        f_lim = 0.5 * self.mode_freqs(1)[0]
        n_lo = int(np.searchsorted(freqs, f_lim))
        pick = sorted({*range(0, n_lo, SWEEP_STEP), *range(max(n_lo - 1, 0), len(freqs))})
        if len(pick) >= len(freqs) or n_lo < 2 * SWEEP_STEP:
            return self.z(freqs, ports, lossy_k=lossy_k)
        got = self.z(freqs[pick], ports, lossy_k=lossy_k)
        c00 = EPS0 * self.eps_r * self.area / self.d
        norm = 1j * 2 * math.pi * c00
        x = np.log(freqs[pick])
        scale = (norm * freqs[pick])[:, None, None]
        spline = CubicSpline(x, got * scale - 1.0, axis=0)
        out = (spline(np.log(freqs)) + 1.0) / (norm * freqs)[:, None, None]
        out[pick] = got
        return out

    # ------------------------------------------------------------ modes

    def _count(self, blocks, sigma: float) -> int:
        n_free = sum(int((b["ll"] < sigma).sum()) for b in blocks)
        m = len(self.joins)
        if m == 0:
            return n_free
        full = self._green(blocks, 2 * m, sigma, float)
        q, r = np.arange(m), np.arange(m, 2 * m)
        zc = full[:, q] - full[:, r]
        a = zc[q] - zc[r]
        a = 0.5 * (a + a.T)
        return n_free - int((np.linalg.eigvalsh(a) < 0).sum())

    def mode_sigmas(self, n: int) -> list[float]:
        """The first n non-zero eigenvalues k^2 (1/m^2)."""
        s_hi = 4.0 * math.pi * (n + 2) / self.area     # Weyl, a little over
        for _ in range(8):
            blocks = self._prepare([], s_hi)
            if self._count(blocks, s_hi) >= n + 1:
                break
            s_hi *= 2.0
        out = []
        for i in range(1, n + 1):          # index 0 is the static mode
            lo, hi = (out[-1] if out else 0.0), s_hi
            # the mode at or above `lo`: N(s) counts the static mode too
            for _ in range(46):
                mid = 0.5 * (lo + hi)
                if self._count(blocks, mid) >= i + 1:
                    hi = mid
                else:
                    lo = mid
                if hi - lo < 1e-7 * hi:
                    break
            out.append(0.5 * (lo + hi))
        return out

    def mode_freqs(self, n: int) -> list[float]:
        """The first n non-zero mode frequencies in Hz (TM_mn0, no loss)."""
        return [math.sqrt(s) * C0 / (2 * math.pi * math.sqrt(self.eps_r))
                for s in self.mode_sigmas(n)]

    def samples(self, per_side: int = 17) -> list[tuple[float, float]]:
        """Points on the plane for reading a mode shape, edges and corners
        included: a magnetic-wall cavity's antinodes sit there."""
        x0 = min(r.x0 for r in self.rects)
        y0 = min(r.y0 for r in self.rects)
        x1 = max(r.x0 + r.a for r in self.rects)
        y1 = max(r.y0 + r.b for r in self.rects)
        pts = {(round(float(x), 7), round(float(y), 7))
               for x in np.linspace(x0, x1, per_side)
               for y in np.linspace(y0, y1, per_side)
               if any(r.contains(x, y) for r in self.rects)}
        for r in self.rects[:40]:
            for x in (r.x0, r.x0 + r.a):
                for y in (r.y0, r.y0 + r.b):
                    pts.add((round(x, 7), round(y, 7)))
        return sorted(pts)

    def mode_report(self, n: int, marks=None) -> list[dict]:
        """Per mode: f_hz, the antinode (x, y in metres) and |V| at `marks`
        [(x, y), ...] relative to the antinode's."""
        marks = list(marks or [])
        sigmas = self.mode_sigmas(n)
        rng = np.random.default_rng(498)
        probes = []
        for r in self.rects[:6]:
            for _ in range(2):
                u, v = rng.uniform(0.11, 0.89, 2)
                probes.append((r.x0 + u * r.a, r.y0 + v * r.b, 0.0, 0.0))
        pts = self.samples()
        ports = [(x, y, 0.0, 0.0) for x, y in pts + marks] + probes
        blocks = self._prepare(ports, max(sigmas))
        out = []
        n_s, n_m = len(pts), len(marks)
        i = 0
        while i < len(sigmas):
            j = i
            while (j + 1 < len(sigmas)
                   and sigmas[j + 1] - sigmas[i] < 2 * DEGENERATE * sigmas[i]):
                j += 1
            z = self._solve(blocks, len(ports),
                            sigmas[i] * (1 - 1j * PROBE_LOSS))
            resp = z[:n_s + n_m, n_s + n_m:]
            u, _, _ = np.linalg.svd(resp, full_matrices=False)
            for k in range(i, j + 1):
                v = np.abs(u[:, min(k - i, u.shape[1] - 1)])
                peak = int(np.argmax(v[:n_s]))
                out.append({
                    "f_hz": (math.sqrt(sigmas[k]) * C0
                             / (2 * math.pi * math.sqrt(self.eps_r))),
                    "antinode": pts[peak],
                    "v_marks": (v[n_s:] / max(v[peak], 1e-300)).tolist()})
            i = j + 1
        return out


def build(rects_mm, d_m: float, eps_r: float, f_max_hz: float,
          strip_mm: float | None = None, k_cut: float | None = None,
          max_strips: int = MAX_STRIPS) -> Cavity:
    """A Cavity from rectangles in mm. The strip width defaults to a 32nd
    of the largest side or a tenth of a wavelength at f_max, the smaller."""
    rects = [Rect(x * 1e-3, y * 1e-3, a * 1e-3, b * 1e-3)
             for x, y, a, b in rects_mm]
    span = max(max(r.x0 + r.a for r in rects) - min(r.x0 for r in rects),
               max(r.y0 + r.b for r in rects) - min(r.y0 for r in rects))
    lam = C0 / (f_max_hz * math.sqrt(eps_r))
    w = strip_mm * 1e-3 if strip_mm else min(span / 32.0, lam / 10.0)
    kc = k_cut or 2.5 * 2 * math.pi * f_max_hz * math.sqrt(eps_r) / C0
    total = sum(e[5] - e[4] for e in _shared_edges(rects, 1e-9))
    return Cavity(rects, d_m, eps_r, max(w, total / max_strips), kc)


def from_polygon(poly: Polygon, d_m: float, eps_r: float, f_max_hz: float,
                 cells: int = 32, refine: int = 1,
                 k_cut: float | None = None) -> tuple[Cavity, dict]:
    """(cavity, raster facts) of one polygon, in the polygon's own frame (mm
    in, metres out). `cells` per long side; `refine` halves cells and strips."""
    minx, miny, maxx, maxy = poly.bounds
    span = max(maxx - minx, maxy - miny)
    h = span / (cells * refine)
    facts = decompose(poly, h)
    cav = build(facts["rects"], d_m, eps_r, f_max_hz,
                strip_mm=min(span / (32.0 * refine),
                             C0 / (f_max_hz * math.sqrt(eps_r)) * 100.0 / refine),
                k_cut=k_cut, max_strips=MAX_STRIPS * refine)
    return cav, facts
