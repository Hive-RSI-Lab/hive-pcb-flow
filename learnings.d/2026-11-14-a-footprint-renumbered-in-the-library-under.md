## 2026-11-14 [place_edit][board_update][footprint] A footprint renumbered in the library under the same name never reaches a routed board - re-pad it with part_repair
Found building scripts/part_repair.py for haos-voice item 8 (2026-11-14).
Y301 (SiT1602, QFN-4) and MK901 (T5848, LGA-8) had the right pad sizes and
spacing but the wrong pad numbers, so fp_verify passed both. The fix goes in
the library file under the same footprint name, and the schematic does not
change. So the netlist names the footprint id the board already has and gives
each pad number the net it already has: board_update plans nothing for the
part (no swap_new_fp, no rewire), and the board keeps the wrong pads with all
its routing. Only DRC's lib_footprint_mismatch warning says the board's copy
differs from the library.

On a routed board the fix is part_repair.py with place_edit's swap_fp (the
fixed .kicad_mod; each pad takes the net of its new number, so all four of
Y301's pads change net), or renumber where no graphic marks pin 1. It rips
the copper on the changed pads in a window round the part, swaps, routes
every net back to as few islands as it had, and keeps the result only if DRC
is no worse. A rip-and-swap by hand is what it replaces: place_edit alone
refuses while copper of another net touches a new pad, because KiCad would
carry that copper onto the pad's net when it saves.

Triage: now L1 | target L2 | owner scripts/part_repair.py | status open | note the miss shows only as DRC's lib_footprint_mismatch warning; part_repair is the fix, and no gate fails on the warning yet
