"""engcalc.py - deterministic engineering calculations (no engine, no board).

The hand calculations a reviewer does on a schematic, as small pure
functions, so sim_board.py and eng_calc.py give the same number every time
and a test pins each one to a worked example:

  parse_value         "4k7" -> 4700, "2.2uF" -> 2.2e-6, "100R" -> 100,
                      "10uH 8A" -> 1e-5 (first token); "1M" is mega here
  part_value          parse_value plus a package code ("0805,C0.1"), and a
                      bare number on a capacitor in uF ("0.1" -> 1e-7)
  package_code        footprint -> imperial chip size ("0603"), or None
  resistor_rating_w   the usual thick-film rating for a chip size (P70)
  resistor_umax_v     a chip size's limiting element voltage (Umax)
  resistor_derate     P(T)/P70: flat to 70 C, linear to 0 at 155 C
  fuse_rerate         a fuse's current rerate k_T at an ambient
  pptc_hold_factor    a PPTC's Ihold(T)/Ihold(23 C)
  parse_amps          '500mA' -> 0.5, '1.1A' -> 1.1
  parse_temp_range    '-40℃~+85℃' -> (-40, 85, False); '@(Tj)' -> True
  divider_out         Vout of a two-resistor divider
  fb_vout             a regulator's Vout from its FB divider and Vref,
                      with the worst-case spread over R and Vref tolerance
  rc_corner_hz        1 / (2 pi R C)
  rc_cross_s          time for an RC step to reach a fraction of its swing
  i2c_pullup          Rp min (sink current) / max (rise time) and tr
  crystal_load_pf     the load a crystal sees from its two caps + stray
  led_vf              a forward voltage by colour, when no datasheet says
  ldo_tj              an LDO's dissipation and junction temperature
  holdup_s            how long a bulk cap holds a rail above a floor
  friis_cascade       an RF chain's gain, NF, Te and IIP3 (Friis)
  rx_sensitivity      kTB + NF noise floor and sensitivity at an SNR
  diode_detector      a square-law detector's NEP, MDS and TSS
  pll_loop_design     a charge-pump PLL's passive 2nd-order loop filter
  pll_loop_analyse    that filter's fc, phase margin and closed-loop BW
  line_wavelength     lambda0, guided lambda, quarter wave, phase of a line
  microstrip_dispersion  eps_eff at frequency (Getsinger)

Every default here (package ratings, LED Vf by colour, I2C pin
capacitance, crystal stray) is a rule of thumb, not a datasheet: callers
report which inputs were assumed so a review can see what it doesn't know.
"""
from __future__ import annotations

import math
import re

# ------------------------------------------------------------------ values

_SI = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6,
       "m": 1e-3, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "R": 1.0,
       "r": 1.0, "Ω": 1.0}
_VAL = re.compile(
    r"^(\d+(?:\.\d+)?)\s*(meg|MEG|Meg|[pnuµμmkKMGRrΩ])?(\d+)?"
    r"\s*(?:[FHfh]|ohms?|Ohms?|Ω)?$")


def parse_value(text) -> float | None:
    """A component value as a number in base units, or None.

    The first token is read: "10uH 8A" -> 1e-5, "470uF/50V" -> 4.7e-4. The SI letter may
    sit in the decimal point's place ("4k7" = 4.7k, "2R2" = 2.2). "M" is mega
    and "m" milli, as on a schematic (SPICE's own "M" = milli trap is
    simlib's to handle). "0R" is zero ohms."""
    if text is None:
        return None
    tok = re.split(r"[\s/,;]+", str(text).strip())
    if not tok or not tok[0]:
        return None
    m = _VAL.match(tok[0])
    if not m:
        return None
    num, unit, frac = m.group(1), m.group(2), m.group(3)
    if frac:
        if "." in num or not unit:
            return None
        num = f"{num}.{frac}"
    mult = 1e6 if unit and unit.lower() == "meg" else _SI.get(unit or "", 1.0)
    return float(num) * mult


# an imperial chip size written into a value field ("0805", "C0603")
_PKG_TOKEN = re.compile(r"^[CRL]?(?:01005|0201|0402|0603|0805|1206|1210|1812|"
                        r"2010|2512)$", re.I)
_BARE_NUM = re.compile(r"^\d+(?:\.\d+)?$")


def part_value(text, kind: str | None = None) -> float | None:
    """A two-pin part's value in base units, reading the forms a BOM writes
    beyond parse_value's: a package code before the value ("0805,C0.1",
    "0603 100n"), the kind letter before a capacitor's number ("C0.1"), and
    a bare number on a capacitor in uF ("0.1" = 100 nF, "10" = 10 uF).
    kind is "C", "R" or "L" (None: parse_value's reading). None when no
    value can be read, so a caller can say not judged, not off-value."""
    if text is None:
        return None
    toks = [x for x in re.split(r"[\s/,;]+", str(text).strip()) if x]
    toks = [x for x in toks if not _PKG_TOKEN.match(x)] if len(toks) > 1 \
        else toks
    if not toks or (kind and len(toks) == 1 and _PKG_TOKEN.match(toks[0])):
        return None
    tok = toks[0]
    if kind == "C":
        if re.match(r"^C\d", tok, re.I):
            tok = tok[1:]
        if _BARE_NUM.match(tok):
            return float(tok) * 1e-6
    return parse_value(tok)


_VOLT = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*V(?![a-zA-Z])")


def parse_volts(text) -> float | None:
    """A voltage written in free text ("10uF 25V", "50V") -> 25.0, or None."""
    m = _VOLT.search(str(text or ""))
    return float(m.group(1)) if m else None


_WATT = re.compile(r"(\d+(?:\.\d+)?)\s*(m?)W\b")


def parse_watts(text) -> float | None:
    """'100mW' -> 0.1, '1/4W' -> 0.25, '0.125W' -> 0.125, else None."""
    s = str(text or "")
    m = re.search(r"(\d+)\s*/\s*(\d+)\s*W\b", s)
    if m and float(m.group(2)):
        return float(m.group(1)) / float(m.group(2))
    m = _WATT.search(s)
    if not m:
        return None
    return float(m.group(1)) * (1e-3 if m.group(2) else 1.0)


def parse_tolerance(text) -> float | None:
    """'+-1%' / '±1%' / '1%' -> 0.01, else None."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", str(text or ""))
    return float(m.group(1)) / 100.0 if m else None


# ---------------------------------------------------------------- packages

_METRIC = {"0603": "0201", "1005": "0402", "1608": "0603", "2012": "0805",
           "3216": "1206", "3225": "1210", "5025": "2010", "6332": "2512"}
# Common thick-film chip resistor ratings at 70 C (Yageo RC / UniOhm series);
# a part's own datasheet or parts.json "Power(Watts)" wins over these.
RESISTOR_RATING_W = {"01005": 0.031, "0201": 0.05, "0402": 0.0625,
                     "0603": 0.1, "0805": 0.125, "1206": 0.25,
                     "1210": 0.5, "2010": 0.75, "2512": 1.0}


def package_code(footprint: str | None) -> str | None:
    """The imperial chip size a footprint name carries, or None.
    'Resistor_SMD:R_0603_1608Metric' -> '0603'; 'aiee:R0402' -> '0402';
    an EasyEDA 'R0603' or a metric '1608Metric' alone also reads."""
    fp = str(footprint or "")
    name = fp.split(":")[-1]
    m = re.search(r"(\d{4})Metric", name)
    if m and m.group(1) in _METRIC:
        return _METRIC[m.group(1)]
    m = re.search(r"(?<!\d)(01005|0201|0402|0603|0805|1206|1210|2010|2512)"
                  r"(?!\d)", name)
    return m.group(1) if m else None


def resistor_rating_w(footprint: str | None) -> float | None:
    code = package_code(footprint)
    return RESISTOR_RATING_W.get(code) if code else None


# Limiting element voltage (Umax, max continuous working voltage) per chip
# size: Vishay D/CRCW e3 (doc 20035) for 0402-2512; 0201 and 01005 from
# Yageo RC (25 V, 15 V), which D/CRCW does not list. A resistor's voltage is
# min(sqrt(P*R), Umax): above the critical resistance Umax^2/P70 (56 kOhm
# for an 0603) voltage limits the part before power does
# (docs/ingest/safety-components.md R-RAT-1).
RESISTOR_UMAX_V = {"01005": 15.0, "0201": 25.0, "0402": 50.0, "0603": 75.0,
                   "0805": 150.0, "1206": 200.0, "1210": 200.0,
                   "2010": 200.0, "2512": 500.0}


def resistor_umax_v(footprint: str | None) -> float | None:
    code = package_code(footprint)
    return RESISTOR_UMAX_V.get(code) if code else None


def resistor_derate(t_amb: float | None) -> float:
    """Thick-film rated power at an ambient as a fraction of P70: full to
    70 C, falling linearly to 0 at 155 C (D/CRCW e3 derating chart,
    R-RAT-2). 85 C -> 0.82."""
    if t_amb is None or t_amb <= 70.0:
        return 1.0
    return max(0.0, (155.0 - t_amb) / 85.0)


def _interp(table: list[tuple[float, float]], t: float) -> float:
    if t <= table[0][0]:
        return table[0][1]
    for (t0, k0), (t1, k1) in zip(table, table[1:]):
        if t <= t1:
            return k0 + (k1 - k0) * (t - t0) / (t1 - t0)
    return table[-1][1]


# A fuse's current rerate with ambient: Littelfuse 881 / chip-fuse curves
# (0.90 at 70 C); no credit below 25 C. On top of it the fuse runs at no more
# than 75 % of its rating (Littelfuse Fuseology guide; R-RAT-3).
FUSE_RERATE = [(25.0, 1.0), (70.0, 0.90), (85.0, 0.85), (100.0, 0.80),
               (125.0, 0.70)]
FUSE_LOAD_FRACTION = 0.75
# A PPTC's hold current against ambient, as a fraction of Ihold at 23 C: the
# typical table of Bourns Multifuse MF-MSMF/MF-NSMF and Littelfuse
# PolySwitch chip devices (S8, S9). The part's own table wins; Ihold(T)
# should clear the operating current by 10 % (R-RAT-4).
PPTC_HOLD = [(-40.0, 1.48), (-20.0, 1.33), (0.0, 1.17), (23.0, 1.0),
             (40.0, 0.87), (50.0, 0.80), (60.0, 0.73), (70.0, 0.68),
             (85.0, 0.56)]
PPTC_MARGIN = 1.1


def fuse_rerate(t_amb: float | None) -> float:
    return 1.0 if t_amb is None else _interp(FUSE_RERATE, t_amb)


def pptc_hold_factor(t_amb: float | None) -> float:
    return 1.0 if t_amb is None else _interp(PPTC_HOLD, t_amb)


_AMP = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*(m|u|µ)?A(?![a-zA-Z])")


def parse_amps(text) -> float | None:
    """'500mA' -> 0.5, '1.1A' -> 1.1, '750 mA' -> 0.75, else None."""
    m = _AMP.search(str(text or ""))
    if not m:
        return None
    return float(m.group(1)) * {"m": 1e-3, "u": 1e-6, "µ": 1e-6}.get(
        m.group(2) or "", 1.0)


_TRANGE = re.compile(r"([+-]?\s*\d+(?:\.\d+)?)\s*(?:℃|°\s*C|C)?\s*(?:~|to|\.\.|"
                     r"\s-\s|-(?=\s*[+-]?\d))\s*([+-]?\s*\d+(?:\.\d+)?)"
                     r"\s*(?:℃|°\s*C|C)", re.I)


def parse_temp_range(text) -> tuple[float, float, bool] | None:
    """An operating temperature range as LCSC writes it: '-40℃~+85℃' ->
    (-40.0, 85.0, False); '-40℃~+125℃@(TJ)' -> (-40.0, 125.0, True), the
    flag saying the bound is the junction's alone, not the air's ('@(TA/TJ)'
    binds the air too). None when the
    text names no range."""
    s = str(text or "")
    m = _TRANGE.search(s)
    if not m:
        return None
    lo, hi = (float(g.replace(" ", "")) for g in m.groups())
    if lo > hi:
        return None
    tj = bool(re.search(r"T\s*J\b|junction", s, re.I)) and \
        not re.search(r"T\s*A\b|ambient", s, re.I)
    return lo, hi, tj


# ---------------------------------------------------------------- circuits

def divider_out(vin: float, r_top: float, r_bot: float) -> float:
    return vin * r_bot / (r_top + r_bot)


def fb_vout(vref: float, r_top: float, r_bot: float, r_tol: float = 0.01,
            vref_tol: float = 0.02) -> dict:
    """Vout = Vref (1 + Rtop/Rbot), and its worst-case corners: Vref and
    both resistors each at the tolerance edge that pushes Vout out."""
    nom = vref * (1 + r_top / r_bot)
    lo = vref * (1 - vref_tol) * (1 + r_top * (1 - r_tol) / (r_bot * (1 + r_tol)))
    hi = vref * (1 + vref_tol) * (1 + r_top * (1 + r_tol) / (r_bot * (1 - r_tol)))
    return {"nom": nom, "min": lo, "max": hi}


def rc_corner_hz(r: float, c: float) -> float:
    return 1.0 / (2 * math.pi * r * c)


def rc_cross_s(tau: float, fraction: float) -> float:
    """Time for a first-order RC step to cover `fraction` of its swing."""
    return -tau * math.log(1.0 - fraction)


# I2C (NXP UM10204 rev 7, 7.1): Rp(min) = (VDD - VOL) / IOL with VOL 0.4 V
# at IOL 3 mA (Sm/Fm) or 20 mA (Fm+); Rp(max) = tr / (0.8473 Cb), the 30-70%
# rise of an RC; tr max 1000 ns (Sm 100 kHz), 300 ns (Fm 400 kHz), 120 ns
# (Fm+ 1 MHz); Cb max 400 pF (Sm/Fm), 550 pF (Fm+).
I2C_MODES = {100: {"tr_ns": 1000.0, "iol_a": 3e-3, "cb_max_pf": 400.0},
             400: {"tr_ns": 300.0, "iol_a": 3e-3, "cb_max_pf": 400.0},
             1000: {"tr_ns": 120.0, "iol_a": 20e-3, "cb_max_pf": 550.0}}
I2C_PIN_PF = 10.0     # UM10204 Ci max per I/O pin
I2C_TRACE_PF = 10.0   # a short on-board trace, a guess


def i2c_pullup(vdd: float, rp: float, cb_pf: float, khz: int = 400) -> dict:
    mode = I2C_MODES.get(khz) or I2C_MODES[400]
    rmin = (vdd - 0.4) / mode["iol_a"]
    rmax = mode["tr_ns"] * 1e-9 / (0.8473 * cb_pf * 1e-12)
    tr_ns = 0.8473 * rp * cb_pf * 1e-12 * 1e9
    return {"rmin": rmin, "rmax": rmax, "tr_ns": tr_ns,
            "tr_max_ns": mode["tr_ns"], "sink_ma": (vdd - 0.4) / rp * 1e3,
            "ok": rmin <= rp <= rmax}


CRYSTAL_STRAY_PF = 4.0  # pin + trace, the usual 2-5 pF guess


def crystal_load_pf(c1_pf: float, c2_pf: float,
                    stray_pf: float = CRYSTAL_STRAY_PF) -> float:
    return c1_pf * c2_pf / (c1_pf + c2_pf) + stray_pf


# LED forward voltage by colour at a few mA, when no datasheet figure is
# known (typical InGaN for green/blue/white, AlInGaP for red..yellow).
LED_VF = {"ir": 1.3, "red": 1.9, "orange": 2.0, "amber": 2.0, "yellow": 2.0,
          "yellow-green": 2.1, "green": 2.9, "blue": 3.0, "white": 3.0,
          "uv": 3.3}


def led_color(*texts) -> str | None:
    s = " ".join(str(t or "") for t in texts).lower()
    if "yellow-green" in s or "yellowgreen" in s:
        return "yellow-green"
    for c in ("white", "blue", "green", "yellow", "amber", "orange", "red",
              "uv", "ir"):
        if re.search(rf"(?<![a-z]){c}(?![a-z])", s):
            return c
    return None


def led_vf(color: str | None) -> float:
    return LED_VF.get(color or "", 2.0)


def ldo_tj(vin: float, vout: float, iout: float, theta_ja: float,
           t_amb: float = 25.0, iq: float = 0.0) -> dict:
    pd = (vin - vout) * iout + vin * iq
    return {"pd_w": pd, "tj_c": t_amb + pd * theta_ja}


def holdup_s(c: float, v_start: float, v_min: float, i_load: float) -> float:
    """Time a capacitor alone holds a constant-current load above v_min."""
    return c * (v_start - v_min) / i_load


# ---------------------------------------------------------------------- RF
#
# The RF budget a radar, receiver or synthesiser review writes down.
# Reference temperature T0 = 290 K (IEEE noise figure); kT0 = -173.98 dBm/Hz.

K_BOLTZMANN = 1.380649e-23
T0_K = 290.0
C0_M_S = 299_792_458.0
MU0 = 4e-7 * math.pi


def db_to_lin(db: float) -> float:
    return 10.0 ** (db / 10.0)


def lin_to_db(x: float) -> float:
    return 10.0 * math.log10(x)


def noise_floor_dbm(bw_hz: float, t_k: float = T0_K) -> float:
    """kTB in dBm."""
    if bw_hz <= 0 or t_k <= 0:
        raise ValueError("bandwidth and temperature must be positive")
    return lin_to_db(K_BOLTZMANN * t_k * bw_hz * 1e3)


def friis_cascade(stages: list[dict]) -> dict:
    """Friis noise and gain cascade, input-referred IIP3 when stages give
    OIP3. Each stage: {name, gain_db, nf_db[, oip3_dbm]}; a passive loss of
    L dB is gain -L, NF L (at T0). F = F1 + (F2-1)/G1 + (F3-1)/(G1 G2) ...;
    1/IIP3 = sum(G_before_i / IIP3_i) in mW, IIP3_i = OIP3_i / G_i. A stage
    without oip3_dbm is taken as perfectly linear. `share` is each stage's
    fraction of the cascade's excess noise (F - 1)."""
    if not stages:
        raise ValueError("no stages")
    g_before, f_tot, inv_iip3 = 1.0, 1.0, 0.0
    rows, adds = [], []
    for st in stages:
        g = db_to_lin(float(st["gain_db"]))
        f = db_to_lin(float(st["nf_db"]))
        if f < 1.0:
            raise ValueError(f"stage {st.get('name')}: NF below 0 dB")
        add = (f - 1.0) / g_before
        f_tot += add
        adds.append(add)
        if st.get("oip3_dbm") is not None:
            iip3_mw = db_to_lin(float(st["oip3_dbm"])) / g
            inv_iip3 += g_before / iip3_mw
        g_before *= g
        rows.append({"name": st.get("name"), "gain_db": float(st["gain_db"]),
                     "nf_db": float(st["nf_db"]),
                     "cum_gain_db": lin_to_db(g_before),
                     "cum_nf_db": lin_to_db(f_tot)})
    excess = f_tot - 1.0
    for row, add in zip(rows, adds):
        row["share"] = add / excess if excess > 0 else 0.0
    out = {"gain_db": lin_to_db(g_before), "nf_db": lin_to_db(f_tot),
           "te_k": T0_K * excess, "stages": rows}
    if inv_iip3 > 0:
        out["iip3_dbm"] = lin_to_db(1.0 / inv_iip3)
        out["oip3_dbm"] = out["iip3_dbm"] + out["gain_db"]
    return out


def rx_sensitivity(nf_db: float, bw_hz: float, snr_db: float = 0.0,
                   t_k: float = T0_K) -> dict:
    """A receiver's noise floor (kTB + NF) and the input that gives snr_db
    over it: MDS at snr 0 dB, sensitivity at the demodulator's SNR."""
    floor = noise_floor_dbm(bw_hz, t_k) + nf_db
    return {"noise_floor_dbm": floor, "sensitivity_dbm": floor + snr_db,
            "ktb_dbm_hz": noise_floor_dbm(1.0, t_k)}


def diode_detector(bw_hz: float, responsivity_v_w: float | None = None,
                   rv_ohm: float | None = None, i_s: float | None = None,
                   n: float = 1.05, i_bias: float = 0.0,
                   en_v_rthz: float = 0.0, r_load: float | None = None,
                   t_k: float = 300.0) -> dict:
    """A square-law (Schottky) video detector's sensitivity.

    Responsivity and video resistance come from the datasheet, or from the
    diode's Is (+ bias) as the ideal matched-input limit: Rv = n Vt /
    (Is + Ib), beta = Rv / (2 n Vt) = 1 / (2 (Is + Ib)) V/W - a real
    detector with input mismatch and junction capacitance at 10-35 GHz
    gets several dB less, so a datasheet figure at the frequency wins.
    A load r_load divides the video voltage by Rv / (Rv + RL).
    Noise at the video output: Rv's thermal noise sqrt(4kT Rv) (shot
    noise of a biased diode is 1/sqrt(2) of that and is folded in as the
    same figure, the usual approximation) plus the video amp's en.
    NEP = vn / beta; MDS = NEP sqrt(B) (video SNR 1); TSS ~ 2.5 NEP
    sqrt(B) - the rule of thumb for the tangential (~8 dB) criterion.
    Square law holds below about -20 dBm; above that the output goes linear."""
    vt = K_BOLTZMANN * t_k / 1.602176634e-19
    ideal = responsivity_v_w is None
    if ideal:
        if i_s is None:
            raise ValueError("give --responsivity and --rv, or --is")
        rv_ohm = n * vt / (i_s + i_bias)
        responsivity_v_w = rv_ohm / (2 * n * vt)
    if rv_ohm is None or rv_ohm <= 0 or responsivity_v_w <= 0 or bw_hz <= 0:
        raise ValueError("responsivity, rv and bandwidth must be positive")
    beta = responsivity_v_w
    if r_load:
        beta *= r_load / (rv_ohm + r_load)
    vn = math.sqrt(4 * K_BOLTZMANN * t_k * rv_ohm + en_v_rthz ** 2)
    nep = vn / beta
    mds_w = nep * math.sqrt(bw_hz)
    return {"responsivity_v_w": beta, "responsivity_ideal": ideal,
            "rv_ohm": rv_ohm, "vn_v_rthz": vn, "nep_w_rthz": nep,
            "nep_dbm_rthz": lin_to_db(nep * 1e3),
            "mds_dbm": lin_to_db(mds_w * 1e3),
            "tss_dbm": lin_to_db(2.5 * mds_w * 1e3),
            "square_law_max_dbm": -20.0}


# Charge-pump PLL, passive 2nd-order loop filter (a series R + C_series
# from the pump to ground, C_shunt across both). Open loop
#   G(s) = Icp Kvco Z(s) / (s N),  Icp in A, Kvco in Hz/V
# (Kd = Icp / 2pi A/rad times Kv = 2pi Kvco rad/s/V), and
#   Z(s) = (1 + s T2) / (s (C_series + C_shunt) (1 + s T1)),
#   T2 = R C_series, T1 = R C_series C_shunt / (C_series + C_shunt).
# Design follows Banerjee, "PLL Performance, Simulation and Design", ch. 9:
# put the phase-margin peak at fc.

def _pll_open_loop(w: float, icp: float, kvco: float, n: float, r: float,
                   c_series: float, c_shunt: float) -> complex:
    s = 1j * w
    ctot = c_series + c_shunt
    t2 = r * c_series
    t1 = r * c_series * c_shunt / ctot
    z = (1 + s * t2) / (s * ctot * (1 + s * t1))
    return icp * kvco * z / (s * n)


def pll_loop_design(icp: float, kvco: float, n: float, fc_hz: float,
                    pm_deg: float) -> dict:
    """R, C_series, C_shunt for loop bandwidth fc and phase margin pm."""
    if not 0 < pm_deg < 90:
        raise ValueError("phase margin must be between 0 and 90 degrees")
    if min(icp, kvco, n, fc_hz) <= 0:
        raise ValueError("icp, kvco, n and fc must be positive")
    wc = 2 * math.pi * fc_hz
    phi = math.radians(pm_deg)
    t1 = (1 / math.cos(phi) - math.tan(phi)) / wc
    t2 = 1 / (wc * wc * t1)
    a0 = (icp * kvco / (wc * wc * n)) * math.sqrt(
        (1 + (wc * t2) ** 2) / (1 + (wc * t1) ** 2))
    c_shunt = a0 * t1 / t2
    c_series = a0 - c_shunt
    return {"r_ohm": t2 / c_series, "c_series_f": c_series,
            "c_shunt_f": c_shunt, "t1_s": t1, "t2_s": t2}


def pll_loop_analyse(icp: float, kvco: float, n: float, r: float,
                     c_series: float, c_shunt: float,
                     f_ref: float | None = None) -> dict:
    """The loop's crossover fc, phase margin there, closed-loop -3 dB
    bandwidth and peaking, from the parts on the board."""
    if min(icp, kvco, n, r, c_series, c_shunt) <= 0:
        raise ValueError("every pll input must be positive")
    g = lambda w: _pll_open_loop(w, icp, kvco, n, r, c_series, c_shunt)  # noqa: E731

    def cross(fn, target):
        lo, hi = 1e-3, 1e12          # rad/s, |fn| falls through target once
        for _ in range(200):
            mid = math.sqrt(lo * hi)
            if fn(mid) > target:
                lo = mid
            else:
                hi = mid
        return math.sqrt(lo * hi)

    wc = cross(lambda w: abs(g(w)), 1.0)
    pm = 180.0 + math.degrees(math.atan2(g(wc).imag, g(wc).real))
    pm = (pm + 180.0) % 360.0 - 180.0
    cl = lambda w: abs(g(w) / (1 + g(w)))  # noqa: E731
    w3 = cross(lambda w: cl(w) if w > wc / 100 else 2.0, 1 / math.sqrt(2))
    peak = max(cl(wc * 10 ** (k / 100.0)) for k in range(-200, 201))
    out = {"fc_hz": wc / (2 * math.pi), "phase_margin_deg": pm,
           "f3db_hz": w3 / (2 * math.pi),
           "peaking_db": 20 * math.log10(max(peak, 1.0)),
           "pole_hz": 1 / (2 * math.pi * r * c_series * c_shunt
                           / (c_series + c_shunt)),
           "zero_hz": 1 / (2 * math.pi * r * c_series)}
    if f_ref:
        # a sampled loop stays continuous-time-like below about fref/10
        out["fref_over_fc"] = f_ref / out["fc_hz"]
        out["fc_ok"] = out["fref_over_fc"] >= 10.0
    return out


def microstrip_eeff_static(w_mm: float, h_mm: float, er: float) -> float:
    """Hammerstad's quasi-static eps_eff of a bare, zero-thickness
    microstrip (the fallback when impedance.py's field solver is absent)."""
    u = w_mm / h_mm
    e = (er + 1) / 2 + (er - 1) / 2 / math.sqrt(1 + 12 / u)
    if u < 1:
        e += (er - 1) / 2 * 0.04 * (1 - u) ** 2
    return e


def microstrip_dispersion(er: float, eeff0: float, z0: float, h_mm: float,
                          f_hz: float) -> float:
    """Getsinger's microstrip dispersion: eps_eff(f) = er - (er - eeff0) /
    (1 + G (f / fp)^2), fp = Z0 / (2 mu0 h), G = 0.6 + 0.009 Z0. Within a
    few % of Kirschning-Jansen up to ~fp; matters from ~10 GHz on FR4."""
    fp = z0 / (2 * MU0 * h_mm * 1e-3)
    g = 0.6 + 0.009 * z0
    return er - (er - eeff0) / (1 + g * (f_hz / fp) ** 2)


def line_wavelength(f_hz: float, eps_eff: float,
                    length_mm: float | None = None) -> dict:
    """Free-space and guided wavelength, the quarter-wave, the lambda/10
    'electrically short' limit, delay per mm and a length's phase."""
    if f_hz <= 0 or eps_eff < 1.0:
        raise ValueError("f must be positive and eps_eff >= 1")
    lam0 = C0_M_S / f_hz * 1e3
    lamg = lam0 / math.sqrt(eps_eff)
    out = {"lambda0_mm": lam0, "lambdag_mm": lamg,
           "quarter_wave_mm": lamg / 4, "half_wave_mm": lamg / 2,
           "short_limit_mm": lamg / 10, "eps_eff": eps_eff,
           "vp_c": 1 / math.sqrt(eps_eff),
           "delay_ps_per_mm": math.sqrt(eps_eff) / C0_M_S * 1e12 * 1e-3}
    if length_mm is not None:
        out["length_mm"] = length_mm
        out["phase_deg"] = 360.0 * length_mm / lamg
        out["delay_ps"] = length_mm * out["delay_ps_per_mm"]
    return out
