## 2026-11-15 [kicad-cli][drc][rules_gen] KiCad 10 drops a whole .kicad_dru over one value it cannot read, and kicad-cli says nothing
Found closing #1273 (PR #1309) for the haos-voice board, 2026-11-15;
measured on KiCad 10.0.6. LEARNINGS.md line 214 says kicad-cli loads the
.kicad_dru beside the board by itself. It also drops it by itself, without a
word: KiCad 10 reads each (min / opt / max ...) value of a rule through its
expression evaluator, and one value it rejects is a parse error that drops
the whole file. 0.5MM, 20mils, 500um, 1e-2mm, .5mm, a bare 0.2 on a length,
2mm on via_count, 1ps on a clearance and a tab inside a sum each do it. DRC
then runs on the netclass, zone and board-setup values alone, so a board can
pass DRC against the wrong limits.

The evaluator's rules, from drc_rule_parser.cpp and 190 probe files: the
units are mm, mil and in, lower case. A value's kind is the kind of its last
unit, and deg or no unit counts as unitless, so 1mm + 1deg is unitless. One
(constraint ...) clause takes one kind: 100ps after 1mm in the same clause,
or -1 after 0.1mm, drops the file. ps is taken only on length and skew, and
via_count, track_angle and the like take a plain number.

kc.run_drc now puts a rules_file_ignored error first, so the gate fails the
board, and rules_gen.py --check-dru fails the file before it compares
floors. routelib.dru_unit_errors names each value KiCad would reject; the
probe verdicts are in tests/fixtures/dru_values/kicad10_probe.json.

Triage: now L2 | target L2 | owner scripts/kc.py | status done | note run_drc fails the board (rules_file_ignored) and check_dru fails the file; drops for other reasons (file structure, layers, unknown clauses, conditions, a non-ASCII blank) are #1308
