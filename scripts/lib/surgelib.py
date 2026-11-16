"""surgelib.py - IEC 61000-4-5 surge arithmetic and the combination-wave
generator, for check_surge (and hand decks).

Generator: the Ed.3 topology (Cc charged to U, Rs1 across it, Lr + Rm in
series to the output, Rs2 across the output) with element values fitted in
ngspice to the standard's waveform, since the standard gives shapes, not
values: open circuit 1.2/50 us, short circuit 8/20 us, Voc/Isc = 2 ohm.
T1/Td are measured the way Ed.3 Table 2/3 define them: voltage T1 = 1.67 x
(t90 - t30), Td = the 50 % width; current T1 = 1.25 x (t90 - t10), Td = 1.18
x the 50 % width. tests/test_check_surge.py pins them to the standard's
tolerance (+-30 %, +-20 %, +-20 %, +-20 %; peaks +-10 %).

TVS family rule: a SMBJ20A's name gives everything a surge check needs.
Standoff from the name; Vbr nominal = 1.1675 x Vrwm (Vbr min 22.2, max 24.5
for 20 V); Vc = 1.62 x Vrwm at Ipp = Ppk/Vc on 10/1000 us, the family's
datasheet line (32.4 V at 18.5 A). The dynamic resistance through those two
points gives Vc(I) = Vbr + Rdyn * I. Datasheets give no 8/20 rating for these
families; Ppk falls as t^-0.5 on their peak-power vs pulse-width curve, so
the rating at 8/20 (Td 20 us) is Ppk * sqrt(1000/20) = 7.07 x Ppk: an
ESTIMATE, flagged as such in the report. A datasheet's 8/20 number goes in
reference/surge_clamps.yaml or constraints surge.clamps.

The 8/20 wave as a closed form (#577): i(t) = Ipk (t/3tau)^3 e^(3 - t/tau),
tau = 3.911 us, peaks at 3 tau and measures 8.0/19.1 us by Ed.3's T1 and Td.
Its I2t is 6! A^2 (tau/2)^7 = I2T_PER_A2 x Ipk^2 (12.17 us), the same as a
rectangular pulse of Ipk lasting 12.17 us: the width at which a resistor's
single-pulse chart is read and against which a fuse's melting I2t is set.
tests/test_check_surge.py pins it to a numeric integral and to the
generator's own short-circuit wave in ngspice.

Reverse polarity: a unidirectional TVS (family name without C, as SMBJ20A)
conducts forward on the negative pulse. Its forward drop is modelled as
0.7 V + rf x I through the datasheet's maximum, Vf 3.5 V at 50 A for single
die and 5.0 V for stacked die (SMBJ300A and up), Littelfuse SMBJ datasheet
(Revised 06/03/20) note 4. Only SMBJ is sourced, so no other family's
forward drop is assumed. A bidirectional part (SMBJ20CA) clamps at its own
Vc either way.
"""
from __future__ import annotations

import math
import re
from pathlib import Path

import yaml

REFERENCE = Path(__file__).resolve().parents[2] / "reference"
LEVELS_YAML = REFERENCE / "surge_levels.yaml"
CLAMPS_YAML = REFERENCE / "surge_clamps.yaml"
PULSE_YAML = REFERENCE / "resistor_pulse.yaml"

GENERATOR_OHM = 2.0
EFT_OHM = 50.0
# fitted, see the docstring; CHARGE_PER_V makes Voc = 973 V per kV of level
# and Isc = 515 A per kV, both inside the +-10 %
GEN = {"cc": 6.2e-6, "rs1": 19.57, "lr": 10.43e-6, "rm": 0.7417, "rs2": 25.23}
CHARGE_PER_V = 1.03

# 10/1000 us peak pulse power by family (same numbers as sim_inrush)
FAMILY_W = {"SMAJ": 400.0, "P4SMA": 400.0, "SMBJ": 600.0, "P6KE": 600.0,
            "SMCJ": 1500.0, "SMF": 200.0}
FAMILY_RE = re.compile(r"(SMAJ|P4SMA|SMBJ|P6KE|SMCJ|SMF)(\d+(?:\.\d+)?)", re.I)
VC_PER_VRWM = 1.62
VBR_PER_VRWM = 1.1675
PULSE_SCALE_8_20 = math.sqrt(1000.0 / 20.0)

# the 8/20 wave i = A t^3 e^(-t/tau), A = e^3 / (27 tau^3) per amp of peak
TAU_8_20 = 3.911e-6
I2T_PER_A2 = math.factorial(6) * (math.e ** 3 / (27 * TAU_8_20 ** 3)) ** 2 \
    * (TAU_8_20 / 2) ** 7
# Littelfuse SMBJ (Revised 06/03/20) note 4: Vf max at 50 A, unidirectional
FORWARD_KNEE_V = 0.7
FORWARD_VF = {"SMBJ": (50.0, 3.5, 5.0, 300.0)}  # at A, single, stacked, Vrwm>=


def wave_8_20(t: float, i_peak: float = 1.0) -> float:
    """The 8/20 short-circuit current at t seconds, peak i_peak at 3 tau."""
    x = t / TAU_8_20
    return i_peak * (x / 3.0) ** 3 * math.exp(3.0 - x) if t > 0 else 0.0


def pulse_i2t(i_peak: float) -> float:
    """I2t (A^2 s) of an 8/20 pulse peaking at i_peak."""
    return I2T_PER_A2 * i_peak * i_peak


def load_pulse_table(path: Path = PULSE_YAML) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def resistor_pulse_w(code: str | None, ti_s: float = I2T_PER_A2,
                     table: dict | None = None) -> float | None:
    """A chip size's single rectangular pulse power at width ti_s, log-log
    between the chart's points; None for a size the chart doesn't give."""
    table = table or load_pulse_table()
    row = (table.get("sizes") or {}).get(code or "")
    if not row:
        return None
    us = [float(x) for x in table["ti_us"]]
    t = min(max(ti_s * 1e6, us[0]), us[-1])
    for (t0, p0), (t1, p1) in zip(zip(us, row), zip(us[1:], row[1:])):
        if t <= t1:
            f = math.log(t / t0) / math.log(t1 / t0)
            return math.exp(math.log(p0) + f * (math.log(p1) - math.log(p0)))
    return float(row[-1])


def clamp_direction(texts: list[str], override: dict | None = None
                    ) -> str | None:
    """'uni', 'bi' or None (not known) for a clamp. Constraints decide
    first: bidirectional true/false, or a forward drop vf_v, which only a
    unidirectional clamp has."""
    if override and "bidirectional" in override:
        return "bi" if override["bidirectional"] else "uni"
    if override and override.get("vf_v") is not None:
        return "uni"
    for t in texts:
        m = re.search(FAMILY_RE.pattern + r"(C?)A?\b", t or "", re.I)
        if m:
            return "bi" if m.group(3) else "uni"
    return None


def forward_model(texts: list[str], override: dict | None = None
                  ) -> dict | None:
    """A unidirectional clamp's forward conduction as Vf(I) = 0.7 + rf I, in
    clamp_model's shape so clamped() can drive it; None when no source gives
    the forward drop. Constraints give it as vf_v at vf_a."""
    if override and override.get("vf_v") is not None:
        at, vf, src = (float(override.get("vf_a", 50.0)),
                       float(override["vf_v"]), "constraints surge.clamps")
    else:
        m = next((FAMILY_RE.search(t or "") for t in texts
                  if FAMILY_RE.search(t or "")), None)
        if not m or m.group(1).upper() not in FORWARD_VF:
            return None
        fam = m.group(1).upper()
        at, single, stacked, from_v = FORWARD_VF[fam]
        vf = stacked if float(m.group(2)) >= from_v else single
        src = (f"{fam} datasheet note 4 (Littelfuse, Revised 06/03/20): "
               f"Vf < {vf:g} V at {at:g} A")
    rf = max(vf - FORWARD_KNEE_V, 0.0) / at
    return {"vbr": FORWARD_KNEE_V, "rdyn": rf, "vc": vf, "ipp_a": at,
            "vf_at_a": at, "source": src, "forward": True}


def load_levels(path: Path = LEVELS_YAML) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def load_clamp_table(path: Path = CLAMPS_YAML) -> list[dict]:
    rows = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get(
        "parts") or []
    return [{**r, "_m": re.compile(r["match"], re.I)} for r in rows]


def clamp_model(vbr: float, vc: float, ipp_a: float, ppk_w: float | None = None,
                vrwm: float | None = None, source: str = "",
                estimated: bool = False) -> dict:
    """A clamp as Vc(I) = vbr + rdyn * I, rated ipp_a (8/20) and ppk_w."""
    return {"vbr": vbr, "rdyn": max(vc - vbr, 0.0) / ipp_a, "vc": vc,
            "ipp_a": ipp_a, "ppk_w": ppk_w if ppk_w else vc * ipp_a,
            "vrwm": vrwm, "source": source, "estimated": estimated}


def family_clamp(text: str) -> dict | None:
    """The clamp a TVS family name implies, or None. rdyn comes from the
    10/1000 line; the 8/20 power rating is scaled and marked estimated."""
    m = FAMILY_RE.search(text or "")
    if not m:
        return None
    fam, vrwm = m.group(1).upper(), float(m.group(2))
    ppk = FAMILY_W[fam]
    vc = VC_PER_VRWM * vrwm
    vbr = VBR_PER_VRWM * vrwm
    ipp_1000 = ppk / vc
    rdyn = max(vc - vbr, 0.0) / ipp_1000
    ppk_20 = ppk * PULSE_SCALE_8_20
    # the current at which Vc(I) * I reaches the scaled power
    ipp_20 = (-vbr + math.sqrt(vbr * vbr + 4 * rdyn * ppk_20)) / (2 * rdyn) \
        if rdyn else ppk_20 / vbr
    return {"vbr": vbr, "rdyn": rdyn, "vc": vc, "ipp_1000_a": ipp_1000,
            "ipp_a": ipp_20, "ppk_w": ppk_20, "vrwm": vrwm, "estimated": True,
            "source": f"{fam} family: {ppk:g} W 10/1000 us, Vc = "
                      f"{VC_PER_VRWM} x Vrwm; 8/20 rating = t^-0.5 estimate"}


def find_clamp(texts: list[str], table: list[dict],
               override: dict | None = None) -> dict | None:
    """Ratings for a clamp: constraints override, then the table, then the
    family rule; None when nothing says."""
    if override:
        return clamp_model(float(override["vbr"]), float(override["vc"]),
                           float(override["ipp_a"]),
                           override.get("ppk_w"), override.get("vrwm"),
                           "constraints surge.clamps")
    for r in table:
        if any(r["_m"].search(t or "") for t in texts):
            return clamp_model(float(r["vbr"]), float(r["vc"]),
                               float(r["ipp_a"]), r.get("ppk_w"),
                               r.get("vrwm"),
                               "reference/surge_clamps.yaml: "
                               f"{r.get('source', '')}"
                               + ("" if r.get("verified") else ", not re-read"))
    for t in texts:
        c = family_clamp(t)
        if c:
            return c
    return None


def clamped(v_peak: float, z_ohm: float, clamp: dict) -> dict:
    """Current and voltage with the clamp conducting, from the source's peak
    and impedance: I = (V - Vbr) / (Z + Rdyn), Vc = Vbr + Rdyn * I. Slightly
    less than V/Z, which ignores the clamp's own drop."""
    i = max(v_peak - clamp["vbr"], 0.0) / (z_ohm + clamp["rdyn"])
    vc = min(clamp["vbr"] + clamp["rdyn"] * i, v_peak)
    return {"i_a": i, "vc_v": vc, "p_w": i * vc}


def diode_card(clamp: dict, name: str = "dtvs") -> str:
    """The clamp as a SPICE diode: breakdown at Vbr, RS = Rdyn."""
    return (f".model {name} D(IS=1e-12 N=1 BV={clamp['vbr']:.4g} IBV=1m "
            f"RS={max(clamp['rdyn'], 1e-3):.4g})")


def generator_lines(level_kv: float, out: str = "o") -> list[str]:
    """The combination wave generator charged for level_kv, its output on
    node `out`. Needs `.tran ... uic`."""
    u = level_kv * 1000.0 * CHARGE_PER_V
    g = GEN
    return [f"Cgen gen_a 0 {g['cc']:.5g} IC={u:.6g}",
            f"Rgen1 gen_a 0 {g['rs1']:.5g}",
            f"Lgen gen_a gen_b {g['lr']:.5g}",
            f"Rgenm gen_b {out} {g['rm']:.5g}",
            f"Rgen2 {out} 0 {g['rs2']:.5g}"]


def load_deck(level_kv: float, r_load: float, tstop: float = 200e-6) -> str:
    """The generator into a plain resistor (open or short circuit)."""
    lines = ["* combination wave generator"] + generator_lines(level_kv)
    lines += [f"Rload o 0 {r_load:g}", f".tran 20n {tstop:g} uic", ".end", ""]
    return "\n".join(lines)


def clamp_deck(level_kv: float, z_ohm: float, clamp: dict,
               tstop: float = 200e-6) -> str:
    """The generator (its own 2 ohm is inside) through z_ohm of coupling and
    series parts into the clamp; the clamp's current is the voltage over a 1 mohm shunt to ground."""
    lines = ["* combination wave into a clamp"] + generator_lines(level_kv)
    lines += [f"Rz o c {max(z_ohm, 1e-3):.5g}",
              "Dtvs s c dtvs", "Rshunt s 0 1m", diode_card(clamp),
              f".tran 20n {tstop:g} uic", ".end", ""]
    return "\n".join(lines)


# ------------------------------------------------------ waveform measurement

def _cross(t, y, level: float, rising: bool) -> float:
    """The time y crosses level before (rising) or after (falling) its peak."""
    k = max(range(len(y)), key=lambda n: y[n])
    span = range(1, k + 1) if rising else range(k + 1, len(y))
    for n in span:
        if (rising and y[n] >= level) or (not rising and y[n] <= level):
            frac = (level - y[n - 1]) / (y[n] - y[n - 1])
            return t[n - 1] + frac * (t[n] - t[n - 1])
    raise ValueError(f"waveform never crosses {level:g}")


def voltage_shape(t, v) -> dict:
    """Open-circuit voltage: peak, T1 = 1.67 (t90 - t30), Td = 50 % width."""
    pk = max(v)
    return {"peak": pk,
            "t1_s": 1.67 * (_cross(t, v, 0.9 * pk, True)
                            - _cross(t, v, 0.3 * pk, True)),
            "td_s": _cross(t, v, 0.5 * pk, False) - _cross(t, v, 0.5 * pk, True)}


def current_shape(t, i) -> dict:
    """Short-circuit current: peak, T1 = 1.25 (t90 - t10), Td = 1.18 x 50 %
    width."""
    pk = max(i)
    return {"peak": pk,
            "t1_s": 1.25 * (_cross(t, i, 0.9 * pk, True)
                            - _cross(t, i, 0.1 * pk, True)),
            "td_s": 1.18 * (_cross(t, i, 0.5 * pk, False)
                            - _cross(t, i, 0.5 * pk, True))}
