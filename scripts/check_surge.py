"""check_surge.py - IEC 61000-4-5 surge and IEC 61000-4-4 EFT on declared
cable ports (hive-hwde #492).

check_esd looks at nanosecond strikes from a finger. A cable brings something
slower and far more energetic: the combination wave delivers 500 A per kV
into a short for 8/20 us. A clamp sized for ESD dies on it, and a clamp that
survives may still let through more than the part behind it can take.

Only ports declared in constraints are judged; the check says nothing about
a board that declares none, since what a port is cabled to isn't in the board:

  "surge": {
    "environment": "residential",        // or industrial
    "ports": {"J1": {"kind": "dc_power", // dc_power|signal|ethernet|mains
                     "cable_m": 2, "outdoor": false,
                     "earthed": false,   // le modes on dc_power/mains
                     "nets": ["VBUS"],   // default by kind (below)
                     "level_kv": 0.5,    // override the assumed level
                     "eft_kv": 0.5}},
    "clamps": {"D1": {"vbr": 23.3, "vc": 32.4, "ipp_a": 100,
                      "bidirectional": false, "vf_v": 3.5, "vf_a": 50}},
    "abs_max": {"U1": 32, "U2.3": 30},
    "abs_min": {"U1": -0.3},
    "series": {"R4": {"pulse_w": 150}, "F1": {"i2t_a2s": 0.02},
               "L2": {"ipk_a": 30}}
  }

Levels come from reference/surge_levels.yaml by kind, environment and cable
length (below the table's min_cable_m and not outdoor the level doesn't
apply, unless level_kv forces one); every finding marks its level as an
assumption from the declaration, since the product's standard decides.
Nets: dc_power takes the port's power-named nets, signal and ethernet its
other nets, mains all; never ground-named or unconnected ones.

For each line and coupling mode, walking from the connector through series
parts (R <= SERIES_MAX_OHM, L, FB, fuse; a bigger R isolates what's behind):

    Z = 2 ohm + coupling + R_series up to the clamp
    I = (V - Vbr) / (Z + Rdyn),  Vc(I) = Vbr + Rdyn * I

(V/Z, the usual selection-guide figure, is reported too as i_simple_a.)

Findings:
  surge_unclamped           error: an applicable surge and no clamp on the
                            line (ethernet: warning surge_relies_on_magnetics,
                            the transformer takes it unless a clamp is there)
  surge_clamp_overstress    error: I over the clamp's Ipp(8/20) or P over its
                            Ppk(8/20)
  surge_clamp_unrated       warning: a clamp nothing gives ratings for
  surge_vc_over_abs_max     error: Vc(I) over the abs max of an IC pin on the
                            protected stretch. A secondary clamp behind a
                            series element is judged at its own Vc instead;
                            the message says which of the two is missing
  surge_eft_over_abs_max    warning: the EFT burst's Vc over a pin's abs max
                            (5/50 ns, 50 ohm; its energy is too small to judge
                            against an 8/20 rating, so no overstress finding)
  surge_negative_under_abs_min
                            error: the same surge, negative: a bidirectional
                            clamp holds -Vc, a unidirectional one conducts
                            forward at -Vf(I) = -(0.7 + rf I), rf from the
                            datasheet's Vf at its test current; under the
                            abs min of an IC pin on the stretch
  surge_series_overstress   error: a series resistor's I^2 R over its
                            single-pulse rating at 8/20 (its chip size in
                            reference/resistor_pulse.yaml, or constraints
                            surge.series pulse_w), or any series part's peak
                            over constraints ipk_a
  surge_fuse_opens          warning: the pulse's I^2 t (12.17 us * Ipk^2 for
                            8/20) over a series fuse or PPTC's constraints
                            i2t_a2s

Series current: a part before the first clamp carries the whole surge
current, one between the first and a secondary clamp the secondary's; each
part is judged once, at the worst current over all ports, modes and both
polarities. Parts nothing rates go in the report as series_unrated.

Pin abs max: constraints surge.abs_max (ref or ref.pin) first, then the part's
extraction (<parts>/<LCSC>.json), then reference/pin_ratings.yaml's abs_max_v
by pin name, or by pad number (pin_numbers) when the board's pads carry no
pin names. Pins nothing rates go in the report as unrated_pins, not findings.
Pin abs min: constraints surge.abs_min, then the extraction's ground-referred
lower bound; pins without one go in unrated_pins_negative. A clamp whose
direction or forward drop nothing gives (constraints bidirectional / vf_v,
or a family in surgelib.FORWARD_VF) leaves the negative pulse unjudged and
is listed in negative_not_judged.

Not modelled: bulk capacitors on the line (they eat some of the 1.2/50
energy, so this errs high), a series resistor's pulse voltage limit, the EFT
burst's negative polarity, forward drops for families other than SMBJ,
10/700 us telecom waves, a MOV + fuse coordination. --spice runs the wave into each clamp's diode model in ngspice
and adds spice_i_a / spice_vc_v to the report; findings stay on the closed form.

CLI exit 0 pass / 1 violations / 2 error, per SPEC section 6.
    bin/hwde check_surge --pcb board.kicad_pcb --constraints c.json [--parts D]
"""
from __future__ import annotations

import argparse
import fnmatch
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "lib"))
import check_esd  # noqa: E402
import checklib  # noqa: E402
import engcalc  # noqa: E402
import geom  # noqa: E402
import surgelib  # noqa: E402

SCRIPT = "check_surge"
SOURCE = SCRIPT
KINDS = ("dc_power", "signal", "ethernet", "mains")
SERIES_MAX_OHM = 100.0
IC_PREFIXES = ("U", "IC")


def _ref_letters(ref: str) -> str:
    return ref.rstrip("0123456789").upper()


def _texts(fp: dict, part: dict | None) -> list[str]:
    return [t for t in ((part or {}).get("mpn"), fp.get("value")) if t]


def load_parts(parts_dir: Path | None) -> dict:
    """ref -> parts.json entry."""
    pj = parts_dir / "parts.json" if parts_dir else None
    if not pj or not pj.is_file():
        return {}
    out = {}
    for p in checklib.load_json(pj, "parts.json").get("parts") or []:
        for r in p.get("refdes") or []:
            out[r] = p
    return out


def port_levels(port: dict, env_table: dict) -> tuple[list[dict], float | None]:
    """The port's surge modes [{mode, kv, z_ohm}] and its EFT kV after the
    table, cable length, earth and overrides. Empty / None = doesn't apply."""
    row = env_table[port["kind"]]
    cable = float(port.get("cable_m", 0))
    outdoor = bool(port.get("outdoor"))
    forced = port.get("level_kv")
    modes = []
    if forced is not None or outdoor or cable > row["surge"]["min_cable_m"] \
            or row["surge"]["min_cable_m"] == 0:
        for m in row["surge"]["modes"]:
            if m["mode"] == "le" and port["kind"] in ("dc_power", "mains") \
                    and not port.get("earthed"):
                continue
            modes.append({**m, "kv": float(forced if forced is not None
                                           else m["kv"])})
    eft = port.get("eft_kv")
    if eft is None and (outdoor or cable > row["eft"]["min_cable_m"]
                        or row["eft"]["min_cable_m"] == 0):
        eft = row["eft"]["kv"]
    return modes, None if eft is None else float(eft)


def _port_nets(bg, ref: str, port: dict) -> list[tuple]:
    """(net, connector pad) for the lines the declaration takes."""
    out, seen = [], set()
    for cp in bg.pads_of(ref=ref):
        net = cp.net
        if not net or net in seen or check_esd.is_ground(net) \
                or "unconnected" in net.lower():
            continue
        if not [p for p in bg.pads_of(net) if p.ref != ref]:
            continue
        if port.get("nets"):
            if not any(fnmatch.fnmatch(net, g) for g in port["nets"]):
                continue
        elif port["kind"] == "dc_power" and not check_esd.is_power(net):
            continue
        elif port["kind"] in ("signal", "ethernet") and check_esd.is_power(net):
            continue
        seen.add(net)
        out.append((net, cp))
    return out


def _series_ohms(ref: str, fp: dict) -> float | None:
    """A series part's resistance, 0 for an inductor, bead or fuse; None when
    it isn't a series part or is too big to carry the surge onward."""
    letters = _ref_letters(ref)
    if letters in ("L", "FB", "F"):
        return 0.0
    if letters == "R":
        v = engcalc.parse_value(fp.get("value"))
        if v is not None and v <= SERIES_MAX_OHM:
            return v
    return None


def walk(bg, start: str, stop_refs: set) -> dict:
    """net -> (ohms from the connector, [series refs]) across series parts."""
    best = {start: (0.0, [])}
    todo = [start]
    while todo:
        todo.sort(key=lambda n: best[n][0])
        net = todo.pop(0)
        ohms, via = best[net]
        for p in bg.pads_of(net):
            if p.ref in stop_refs or p.ref in via:
                continue
            r = _series_ohms(p.ref, bg.footprints[p.ref])
            pads = bg.pads_of(ref=p.ref)
            if r is None or len(pads) != 2:
                continue
            other = next(q for q in pads if q.number != p.number).net
            if not other or other == net or check_esd.is_ground(other):
                continue
            if other not in best or ohms + r < best[other][0]:
                best[other] = (ohms + r, via + [p.ref])
                todo.append(other)
    return best


def _extraction_ratings(part: dict | None, parts_dir: Path | None) -> dict:
    lcsc = (part or {}).get("lcsc")
    ext = parts_dir / f"{lcsc}.json" if parts_dir and lcsc else None
    if not ext or not ext.is_file():
        return {}
    import check_ratings
    return check_ratings.part_ratings(checklib.load_json(ext, "extraction"))


def pin_limit(pad, fp, part, parts_dir, table, overrides) -> tuple:
    """(abs max volts, source) for an IC pad, or (None, "")."""
    for key in (f"{pad.ref}.{pad.number}", pad.ref):
        if key in overrides:
            return float(overrides[key]), "constraints surge.abs_max"
    rows = [r for r in (_extraction_ratings(part, parts_dir).get(pad.number)
                        or {}).get(("voltage", "abs")) or []
            if r["ref"] is None and isinstance(r["hi"], (int, float))]
    if rows:
        r = min(rows, key=lambda r: r["hi"])
        return float(r["hi"]), f"{part.get('lcsc')}.json {r['src']}"
    names = _texts(fp, part)
    for r in table:
        if not any(r["_m"].search(x) for x in names):
            continue
        if (pad.pinfunction and r["_p"].match(pad.pinfunction)) or (
                not pad.pinfunction and r.get("_n")
                and r["_n"].match(pad.number)):
            return float(r["abs_max_v"]), ("reference/pin_ratings.yaml: "
                                           f"{str(r.get('source', '')).strip()}")
    return None, ""


def pin_limit_lo(pad, part, parts_dir, overrides) -> tuple:
    """(abs min volts, source) for an IC pad, or (None, ""): constraints
    surge.abs_min (ref or ref.pin), then the extraction's ground-referred
    lower bound (the tightest, e.g. -0.3 of "-0.3 to VDD+0.3")."""
    for key in (f"{pad.ref}.{pad.number}", pad.ref):
        if key in overrides:
            return float(overrides[key]), "constraints surge.abs_min"
    rows = [r for r in (_extraction_ratings(part, parts_dir).get(pad.number)
                        or {}).get(("voltage", "abs")) or []
            if r["ref"] is None and isinstance(r["lo"], (int, float))
            and not isinstance(r["lo"], bool)]
    if rows:
        r = max(rows, key=lambda r: r["lo"])
        return float(r["lo"]), f"{part.get('lcsc')}.json {r['src']}"
    return None, ""


def _pin_table() -> list[dict]:
    import sim_board
    rows = [r for r in sim_board.load_pin_ratings() if r.get("abs_max_v")]
    return [{**r, "_n": re.compile(r["pin_numbers"])} if r.get("pin_numbers")
            else r for r in rows]


def _clamps_on(bg, reach: dict, clamps: set, ratings) -> list[dict]:
    """The clamps on the reached nets with a pad to ground, nearest first."""
    out = {}
    for net, (ohms, via) in reach.items():
        for p in bg.pads_of(net):
            if p.ref not in clamps:
                continue
            if not any(check_esd.is_ground(q.net)
                       for q in bg.pads_of(ref=p.ref)):
                continue
            if p.ref not in out or ohms < out[p.ref]["ohms"]:
                out[p.ref] = {"ref": p.ref, "net": net, "ohms": ohms,
                              "via": via, "pos": p.copper_center,
                              "model": ratings(p.ref)}
    return sorted(out.values(), key=lambda c: (c["ohms"], c["ref"]))


def judge_mode(ctx, ref, net, cp, mode, cls, reach, facts) -> None:
    """One coupling mode of one line: the clamp's stress, then the pins."""
    v_kv, z_mode = mode["kv"], mode["z_ohm"]
    v = v_kv * 1000.0
    vios = ctx["vios"]
    assumed = (f"{v_kv:g} kV {mode['mode'].upper()} is the level assumed from "
               f"{ref}'s declaration as {ctx['ports'][ref]['kind']}, not a "
               "requirement")
    row = {"mode": mode["mode"], "level_kv": v_kv, "z_mode_ohm": 2 + z_mode}
    facts["modes"].append(row)
    if not cls:
        kind = ctx["ports"][ref]["kind"]
        relies = kind == "ethernet"
        vios.append(checklib.violation(
            SCRIPT, "warning" if relies else "error", cp.copper_center, None,
            net, [ref],
            f"{ref} pin {cp.number} ({net}): no clamp on the line; "
            + ("the surge rests on the transformer's isolation alone"
               if relies else
               f"a surge drives {v / (2 + z_mode):.0f} A into whatever is "
               "there: add a TVS rated for it")
            + f" ({assumed})", SOURCE,
            kind="surge_relies_on_magnetics" if relies else "surge_unclamped",
            level_kv=v_kv, assumption=True))
        return
    rated = [c for c in cls if c["model"]]
    if not rated:
        return
    first = rated[0]
    z = surgelib.GENERATOR_OHM + z_mode + first["ohms"]
    got = surgelib.clamped(v, z, first["model"])
    m = first["model"]
    row.update(clamp=first["ref"], z_ohm=checklib.rnd(z, 3),
               i_a=checklib.rnd(got["i_a"], 1),
               i_simple_a=checklib.rnd(v / z, 1),
               vc_v=checklib.rnd(got["vc_v"], 1),
               p_w=checklib.rnd(got["p_w"], 0),
               ipp_8_20_a=checklib.rnd(m["ipp_a"], 1),
               ppk_8_20_w=checklib.rnd(m["ppk_w"], 0),
               rating_estimated=bool(m.get("estimated")))
    if ctx["spice"] is not None:
        ctx["spice"].append((row, v_kv, z - surgelib.GENERATOR_OHM, m))
    second, v2, i2 = _secondary(rated, first, got["vc_v"], lambda c: c["model"])
    if second:
        row["secondary"] = second["ref"]
        row["secondary_vc_v"] = checklib.rnd(v2, 1)
    _carry(ctx, first, second, got["i_a"], i2, ref, net, v_kv, mode, "+")
    if got["i_a"] > m["ipp_a"] or got["p_w"] > m["ppk_w"]:
        vios.append(checklib.violation(
            SCRIPT, "error", first["pos"], None, net, [first["ref"], ref],
            f"{first['ref']} on {ref} {net}: {got['i_a']:.0f} A / "
            f"{got['p_w'] / 1000:.1f} kW through Z = {z:g} ohm exceeds its "
            f"8/20 us rating {m['ipp_a']:.0f} A / {m['ppk_w'] / 1000:.1f} kW"
            + (" (rating estimated from the 10/1000 us line)"
               if m.get("estimated") else "")
            + f": the clamp fails short. {assumed}", SOURCE,
            kind="surge_clamp_overstress", level_kv=v_kv, assumption=True,
            i_a=row["i_a"], ipp_a=row["ipp_8_20_a"]))
    judge_pins(ctx, ref, net, reach, first, second, got["vc_v"], v2, assumed,
               row, vios, kind="surge_vc_over_abs_max", severity="error",
               kv=v_kv)
    judge_negative(ctx, ref, net, reach, mode, z, rated, assumed, row)


def _secondary(rated, first, vc, model_of) -> tuple:
    """(second clamp, its Vc, the current between the two) for the first
    rated clamp further along than `first`; (None, None, None) without one,
    and (clamp, None, None) when model_of gives it no model."""
    second = next((c for c in rated[1:] if c["ohms"] > first["ohms"]), None)
    if not second:
        return None, None, None
    m2 = model_of(second)
    if not m2:
        return second, None, None
    got = surgelib.clamped(vc, second["ohms"] - first["ohms"], m2)
    return second, got["vc_v"], got["i_a"]


def _carry(ctx, first, second, i1, i2, ref, net, kv, mode, sign) -> None:
    """Note the current each series part carries, keeping the worst: up to
    the first clamp the whole surge current, between the two clamps the
    secondary's."""
    legs = [(r, i1) for r in first["via"]]
    if second and i2 is not None:
        legs += [(r, i2) for r in second["via"] if r not in first["via"]]
    for r, i in legs:
        old = ctx["series"].get(r)
        if old is None or i > old["i_a"]:
            ctx["series"][r] = {"i_a": i, "port": ref, "net": net,
                                "level_kv": kv, "mode": mode["mode"],
                                "polarity": sign}


def judge_negative(ctx, ref, net, reach, mode, z, rated, assumed, row) -> None:
    """The same surge, negative: a bidirectional clamp at -Vc, a
    unidirectional one forward at -Vf(I); pins against their abs min."""
    v = mode["kv"] * 1000.0
    first = rated[0]

    def neg_model(c):
        texts = _texts(ctx["bg"].footprints[c["ref"]], ctx["parts"].get(c["ref"]))
        over = ctx["clamp_over"].get(c["ref"])
        d = surgelib.clamp_direction(texts, over)
        if d == "bi":
            return c["model"]
        return surgelib.forward_model(texts, over) if d == "uni" else None

    m = neg_model(first)
    if not m:
        row["negative"] = {"judged": False, "why": (
            f"{first['ref']}: neither its direction nor its forward drop is "
            "known")}
        ctx["neg_unjudged"].add(first["ref"])
        return
    got = surgelib.clamped(v, z, m)
    second, v2, i2 = _secondary(rated, first, got["vc_v"], neg_model)
    neg = {"judged": True, "clamp": first["ref"],
           "conducts": "forward" if m.get("forward") else "reverse",
           "i_a": checklib.rnd(got["i_a"], 1),
           "vc_v": -checklib.rnd(got["vc_v"], 1),
           "source": m.get("source", "")}
    if second:
        neg["secondary"] = second["ref"]
        if v2 is None:
            ctx["neg_unjudged"].add(second["ref"])
        else:
            neg["secondary_vc_v"] = -checklib.rnd(v2, 1)
    row["negative"] = neg
    _carry(ctx, first, second, got["i_a"], i2, ref, net, mode["kv"], mode, "-")
    assumed = assumed.replace(" is the level", ", negative, is the level", 1)
    judge_pins(ctx, ref, net, reach, first, second, got["vc_v"], v2, assumed,
               neg, ctx["vios"], kind="surge_negative_under_abs_min",
               severity="error", kv=mode["kv"], sign=-1)


def judge_pins(ctx, ref, net, reach, first, second, vc, v2, assumed, row,
               vios, kind, severity, kv, sign=1) -> None:
    """Pins on the stretch against the voltage that reaches them: the first
    clamp's Vc, or a secondary clamp's when the pin sits on or behind it.
    sign -1 is the negative pulse: vc and v2 are magnitudes, the limit is
    the pin's abs min, and a pin behind a secondary clamp with no negative
    model (v2 None) is not judged."""
    pins = []
    for pnet, (ohms, via) in sorted(reach.items()):
        for p in ctx["bg"].pads_of(pnet):
            if p.ref in ctx["clamps"] or \
                    _ref_letters(p.ref) not in IC_PREFIXES:
                continue
            fp = ctx["bg"].footprints[p.ref]
            part = ctx["parts"].get(p.ref)
            behind = bool(second) and ohms >= second["ohms"]
            at_pin = v2 if behind else vc
            if sign < 0:
                lim, src = pin_limit_lo(p, part, ctx["parts_dir"],
                                        ctx["abs_min"])
                if lim is None or at_pin is None:
                    ctx["unrated_lo"].add(f"{p.ref}.{p.number}")
                    continue
                pins.append({"ref": p.ref, "pin": p.number, "net": pnet,
                             "abs_min_v": lim,
                             "at_pin_v": -checklib.rnd(at_pin, 1),
                             "source": src})
                if -at_pin >= lim:
                    continue
            else:
                lim, src = pin_limit(p, fp, part, ctx["parts_dir"],
                                     ctx["table"], ctx["abs_max"])
                if lim is None:
                    ctx["unrated"].add(f"{p.ref}.{p.number}")
                    continue
                pins.append({"ref": p.ref, "pin": p.number, "net": pnet,
                             "abs_max_v": lim,
                             "at_pin_v": checklib.rnd(at_pin, 1),
                             "source": src})
                if at_pin <= lim:
                    continue
            behind_series = ohms > first["ohms"] or len(via) > len(first["via"])
            hint = ("the secondary clamp still lets it through" if behind else
                    "the series element has no clamp behind it: add a "
                    "secondary clamp" if behind_series else
                    "add a series element and a secondary clamp, or a "
                    "lower-Vc clamp")
            if sign < 0:
                fwd = row.get("conducts") == "forward" and not behind
                vios.append(checklib.violation(
                    SCRIPT, severity, p.copper_center, None, pnet,
                    [p.ref, first["ref"], ref],
                    f"{p.ref} pin {p.number} ({pnet}): {-at_pin:.1f} V during "
                    f"the negative pulse on {ref} {net}, under its abs min "
                    f"{lim:g} V ({src})"
                    + (f": {first['ref']} is unidirectional and conducts "
                       "forward; a bidirectional clamp, or " if fwd else ": ")
                    + (f"{hint}" if not fwd else "a series element and a "
                       "secondary clamp")
                    + f". {assumed}", SOURCE, kind=kind, level_kv=kv,
                    assumption=True, at_pin_v=-checklib.rnd(at_pin, 1),
                    abs_min_v=lim, pin=p.number))
                continue
            vios.append(checklib.violation(
                SCRIPT, severity, p.copper_center, None, pnet,
                [p.ref, first["ref"], ref],
                f"{p.ref} pin {p.number} ({pnet}): {at_pin:.1f} V during the "
                f"pulse on {ref} {net}, over its abs max {lim:g} V ({src}): "
                f"{hint}. {assumed}", SOURCE, kind=kind, level_kv=kv,
                assumption=True, at_pin_v=checklib.rnd(at_pin, 1),
                abs_max_v=lim, pin=p.number))
    row["pins"] = pins


def judge_eft(ctx, ref, net, kv, cls, reach, facts) -> None:
    facts["eft_kv"] = kv
    rated = [c for c in cls if c["model"]]
    if not rated:
        return
    first = rated[0]
    got = surgelib.clamped(kv * 1000.0, surgelib.EFT_OHM + first["ohms"],
                           first["model"])
    row = {"level_kv": kv, "clamp": first["ref"],
           "i_a": checklib.rnd(got["i_a"], 1),
           "vc_v": checklib.rnd(got["vc_v"], 1)}
    facts["eft"] = row
    assumed = (f"{kv:g} kV EFT is the level assumed from {ref}'s declaration "
               f"as {ctx['ports'][ref]['kind']}, not a requirement")
    second, v2, _i2 = _secondary(rated, first, got["vc_v"],
                                 lambda c: c["model"])
    if second:
        row["secondary"] = second["ref"]
        row["secondary_vc_v"] = checklib.rnd(v2, 1)
    judge_pins(ctx, ref, net, reach, first, second, got["vc_v"], v2, assumed,
               row, ctx["vios"], kind="surge_eft_over_abs_max",
               severity="warning", kv=kv)


def judge_series(ctx, cfg) -> list[dict]:
    """Each series part against the worst surge current it carries (an 8/20
    pulse, I2t = I2T_PER_A2 * Ipk^2): a resistor's pulse power P = I^2 R
    against its size's single-pulse rating or constraints pulse_w; any part
    against constraints i2t_a2s (a fuse or PPTC opens) and ipk_a."""
    over = cfg.get("series") or {}
    table = surgelib.load_pulse_table()
    rows = []
    for r, hit in sorted(ctx["series"].items()):
        fp = ctx["bg"].footprints[r]
        o = over.get(r) or {}
        i = hit["i_a"]
        i2t = surgelib.pulse_i2t(i)
        row = {"ref": r, "i_a": checklib.rnd(i, 1),
               "i2t_a2s": checklib.rnd(i2t, 4), "port": hit["port"],
               "net": hit["net"], "level_kv": hit["level_kv"],
               "mode": hit["mode"], "polarity": hit["polarity"]}
        where = (f"{hit['level_kv']:g} kV {hit['mode'].upper()} "
                 f"({hit['polarity']}) on {hit['port']} {hit['net']}")
        assumed = (f"{hit['level_kv']:g} kV is the level assumed from "
                   f"{hit['port']}'s declaration, not a requirement")
        pos = next(iter(ctx["bg"].pads_of(ref=r))).copper_center
        judged = False
        ohms = _series_ohms(r, fp)
        if _ref_letters(r) == "R" and ohms:
            p_w = i * i * ohms
            if o.get("pulse_w") is not None:
                lim, src = float(o["pulse_w"]), "constraints surge.series"
            else:
                code = engcalc.package_code(fp.get("lib"))
                lim = surgelib.resistor_pulse_w(code, table=table)
                src = (f"{code} thick film, reference/resistor_pulse.yaml"
                       if lim else "")
            row["p_w"] = checklib.rnd(p_w, 1)
            if lim:
                judged = True
                row.update(pulse_w=checklib.rnd(lim, 1), pulse_source=src)
                if p_w > lim:
                    ctx["vios"].append(checklib.violation(
                        SCRIPT, "error", pos, None, hit["net"], [r, hit["port"]],
                        f"{r} ({fp.get('value')} ohm): {i:.0f} A through it "
                        f"during {where} is {p_w:.0f} W for 8/20 us, over its "
                        f"single-pulse rating {lim:.0f} W ({src}): a bigger "
                        "or pulse-rated resistor, or a lower-Vc first clamp. "
                        f"{assumed}", SOURCE, kind="surge_series_overstress",
                        level_kv=hit["level_kv"], assumption=True,
                        p_w=checklib.rnd(p_w, 1), pulse_w=checklib.rnd(lim, 1)))
        if o.get("i2t_a2s") is not None:
            judged = True
            lim = float(o["i2t_a2s"])
            if i2t > lim:
                ctx["vios"].append(checklib.violation(
                    SCRIPT, "warning", pos, None, hit["net"], [r, hit["port"]],
                    f"{r}: {i:.0f} A for 8/20 us during {where} is "
                    f"{i2t:.3g} A2s, over its I2t {lim:g} A2s (constraints "
                    "surge.series): it opens, and the port stays down until "
                    f"it is replaced or cools. {assumed}", SOURCE,
                    kind="surge_fuse_opens", level_kv=hit["level_kv"],
                    assumption=True, i2t_a2s=checklib.rnd(i2t, 4)))
        if o.get("ipk_a") is not None:
            judged = True
            lim = float(o["ipk_a"])
            if i > lim:
                ctx["vios"].append(checklib.violation(
                    SCRIPT, "error", pos, None, hit["net"], [r, hit["port"]],
                    f"{r}: {i:.0f} A peak during {where}, over its pulse "
                    f"current {lim:g} A (constraints surge.series). {assumed}",
                    SOURCE, kind="surge_series_overstress",
                    level_kv=hit["level_kv"], assumption=True,
                    i_a=checklib.rnd(i, 1)))
        if not judged:
            ctx["series_unrated"].add(r)
        rows.append(row)
    return rows


def run_spice(items: list, timeout: float = 120.0) -> str | None:
    """The wave into each clamp's diode model; the peaks into its report row.
    -> the reason it couldn't run, or None."""
    if not items:
        return None
    import env as envlib
    import sim_board
    dll = envlib.find_ngspice_dll()
    if dll is None:
        return "no ngspice library"
    decks = [surgelib.clamp_deck(kv, z, m) for _r, kv, z, m in items]
    plots, _ver = sim_board.run_engine(decks, dll, timeout)
    for (row, _kv, _z, _m), plot in zip(items, plots):
        row["spice_i_a"] = checklib.rnd(max(plot["s"]) / 1e-3, 1)
        row["spice_vc_v"] = checklib.rnd(max(plot["c"]), 1)
    return None


def check(pcb: Path, constraints: dict | None = None,
          parts_dir: Path | None = None, spice: bool = False
          ) -> tuple[list, dict]:
    cfg = (constraints or {}).get("surge") or {}
    ports = cfg.get("ports") or {}
    if not ports:
        return [], {"ports_declared": 0, "lines": [],
                    "note": "no surge.ports in constraints: nothing to judge"}
    levels = surgelib.load_levels()
    env = cfg.get("environment", "residential")
    if env not in levels["environments"]:
        raise checklib.CheckError(f"surge.environment {env!r}: want one of "
                                  f"{sorted(levels['environments'])}")
    bg = geom.load_board(pcb)
    fps = bg.footprints
    parts = load_parts(parts_dir)
    table = surgelib.load_clamp_table()
    overrides = cfg.get("clamps") or {}
    clamps = {r for r, fp in fps.items()
              if check_esd.is_clamp(fp, set(overrides), r)}
    models: dict = {}

    def ratings(ref):
        if ref not in models:
            models[ref] = surgelib.find_clamp(
                _texts(fps[ref], parts.get(ref)), table, overrides.get(ref))
        return models[ref]

    ctx = {"bg": bg, "ports": ports, "clamps": clamps, "parts": parts,
           "parts_dir": parts_dir, "table": _pin_table(),
           "abs_max": cfg.get("abs_max") or {},
           "abs_min": cfg.get("abs_min") or {}, "clamp_over": overrides,
           "vios": [], "unrated": set(), "unrated_lo": set(),
           "neg_unjudged": set(), "series": {}, "series_unrated": set(),
           "spice": [] if spice else None}
    lines, unrated_clamps = [], set()
    for ref, port in sorted(ports.items()):
        if port.get("kind") not in KINDS:
            raise checklib.CheckError(f"surge.ports.{ref}.kind "
                                      f"{port.get('kind')!r}: want one of {KINDS}")
        if ref not in fps:
            raise checklib.CheckError(f"surge.ports.{ref}: no such part")
        modes, eft = port_levels(port, levels["environments"][env])
        for net, cp in _port_nets(bg, ref, port):
            reach = walk(bg, net, {ref})
            cls = _clamps_on(bg, reach, clamps, ratings)
            facts = {"port": ref, "kind": port["kind"], "net": net,
                     "environment": env, "assumption": True, "modes": [],
                     "clamps": [c["ref"] for c in cls],
                     "reach": {n: checklib.rnd(o, 3)
                               for n, (o, _v) in sorted(reach.items())}}
            lines.append(facts)
            for c in cls:
                if not c["model"] and c["ref"] not in unrated_clamps:
                    unrated_clamps.add(c["ref"])
                    ctx["vios"].append(checklib.violation(
                        SCRIPT, "warning", c["pos"], None, c["net"], [c["ref"]],
                        f"{c['ref']} clamps {ref} {net} but nothing gives its "
                        "8/20 us ratings: add it to reference/surge_clamps."
                        "yaml or constraints surge.clamps", SOURCE,
                        kind="surge_clamp_unrated"))
            for mode in modes:
                judge_mode(ctx, ref, net, cp, mode, cls, reach, facts)
            if eft is not None:
                judge_eft(ctx, ref, net, eft, cls, reach, facts)
    series = judge_series(ctx, cfg)
    report = {"ports_declared": len(ports), "environment": env,
              "levels_source": str(levels.get("source", "")).strip(),
              "levels_verified": bool(levels.get("verified")),
              "lines": lines, "unrated_pins": sorted(ctx["unrated"]),
              "unrated_pins_negative": sorted(ctx["unrated_lo"]),
              "negative_not_judged": sorted(ctx["neg_unjudged"]),
              "series": series,
              "series_unrated": sorted(ctx["series_unrated"]),
              "clamp_ratings": {
                  r: {k: (checklib.rnd(v, 3) if isinstance(v, float) else v)
                      for k, v in m.items()}
                  for r, m in sorted(models.items()) if m}}
    if spice:
        why = run_spice(ctx["spice"])
        if why:
            report["spice_skipped"] = why
    return ctx["vios"], report


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcb", required=True)
    ap.add_argument("--constraints")
    ap.add_argument("--parts", help="parts dir (parts.json, <LCSC>.json)")
    ap.add_argument("--spice", action="store_true",
                    help="also run the wave into each clamp in ngspice")
    ap.add_argument("--out", help="write the JSON report here")
    args = ap.parse_args(argv)
    pcb = Path(args.pcb)
    if not pcb.is_file():
        raise checklib.CheckError(f"board not found: {pcb}")
    cons = (checklib.load_json(args.constraints, "constraints")
            if args.constraints else None)
    viols, facts = check(pcb, cons, Path(args.parts) if args.parts else None,
                         args.spice)
    return checklib.report(SCRIPT, pcb, viols, checked=[facts],
                           nothing_to_judge=None if facts.get("ports_declared")
                           else "no surge.ports in constraints"), args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    sys.exit(main())
