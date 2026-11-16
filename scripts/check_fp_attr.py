"""check_fp_attr.py - every placed footprint's placement type (attr) matches
its pads (P8 verify suite).

The hit: EasyEDA's XR0850 8x8 mm power inductor footprint ships
`(attr through_hole)` on two SMD pads. lite-main's L1-L4 took it, so
bom_cpl classed four reflowed inductors `tht` ("JLC THT assembly ... not on
the SMT line") and `kicad-cli pcb export stats` counted them as THT; KiCad's
own library linter (KLC F6.1/F7.1) flags the footprint. The other way round,
an `smd` part whose pins all go through holes is placed by the SMT line with
nothing to hold.

lib/fpattr.py is the rule: SMD pads only -> smd, plated pins only ->
through_hole, both (USB-C shell legs, pin-in-paste, THT parts with SMD tabs)
-> its own attr stands. A plated hole inside an SMD pad is a thermal via, not
a pin (lite-main U8's HTSSOP-32 stays smd). Parts marked board_only,
exclude_from_bom or virtual aren't judged. fpfix rule G fixes it in the
library (lib_pull runs it on every pull) and `pcb-flow fpfix --pcb` on a placed
board; this check fails what the fix didn't reach, one error per ref, kind:
  fp_attr_mismatch     the pads give one type and (attr) says the other or
                       none (fixable: the pads settle it)
  fp_attr_unspecified  SMD pads and plated pins and no type at all: the
                       position export drops it and only the part's drawing
                       says how it is fitted

CLI: --pcb board.kicad_pcb [--out report.json]
     exit 0 pass / 1 violations / 2 error, per SPEC section 6.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "lib"))
import check_model_paths  # noqa: E402  (workspace_of)
import checklib  # noqa: E402
import fpattr  # noqa: E402
import fpfix  # noqa: E402

SCRIPT = "check_fp_attr"


def check(pcb: Path) -> tuple[list[dict], int]:
    """(violations, footprints judged)."""
    ws = check_model_paths.workspace_of(pcb)
    text = pcb.read_text(encoding="utf-8")
    viols, n = [], 0
    for head, s, e in fpfix.top_level_nodes(text):
        if head != "footprint":
            continue
        root = fpfix._parse(text[s:e])
        at = fpfix._nums(fpfix._kid(root, "at"))
        v = fpattr.judge(root, -(at[2] if len(at) >= 3 else 0.0))
        n += 1
        if not v["mismatch"]:
            continue
        ref = next((str(fpfix._tok(c[2])) for c in root[1:]
                    if isinstance(c, list) and len(c) >= 3
                    and fpfix._tok(c[0]) == "property"
                    and str(fpfix._tok(c[1])) == "Reference"), "?")
        name = str(fpfix._tok(root[1]))
        lib, _, fp = name.rpartition(":")
        layer = fpfix._kid(root, "layer")
        lib_dir = ws / "lib" / f"{lib or '<lib>'}.pretty"
        if v["fix"]:
            fix = (f"pcb-flow fpfix --lib {lib_dir} --only {fp} sets (attr "
                   f"{v['fix']}) in the library, then pcb-flow fpfix --pcb {pcb} "
                   f"(or board_update) brings it onto the placed part")
            kind = "fp_attr_mismatch"
        else:
            fix = (f"read the part's drawing: set (attr smd) if it is "
                   f"reflowed with THT legs, (attr through_hole) if it is "
                   f"fitted in the THT step, in {lib_dir}/{fp}.kicad_mod and "
                   f"on the board")
            kind = "fp_attr_unspecified"
        msg = f"{ref} ({name}): {fpattr.why(v)}. Fix: {fix}"
        viols.append(checklib.violation(
            SCRIPT, "error", at[:2] if len(at) >= 2 else None,
            str(fpfix._tok(layer[1])) if layer is not None else None, None,
            [ref], msg, SCRIPT, kind=kind, footprint=name,
            attr=v["declared"], pads_say=v["pads_say"], smd_pads=v["smd"],
            tht_pins=v["tht"], thermal_vias=v["thermal_vias"],
            fixable=bool(v["fix"]), fix=fix))
    return viols, n


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcb", required=True)
    ap.add_argument("--out", help="write the JSON report here")
    args = ap.parse_args(argv)
    pcb = Path(args.pcb)
    if not pcb.is_file():
        raise checklib.CheckError(f"board not found: {pcb}")
    viols, n = check(pcb)
    return checklib.report(SCRIPT, pcb, viols, footprints=n), args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    sys.exit(main())
