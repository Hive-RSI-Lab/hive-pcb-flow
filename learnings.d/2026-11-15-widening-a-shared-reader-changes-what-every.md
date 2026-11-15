## 2026-11-15 [python][review][rules_gen] Widening a shared reader changes what every caller picks up - test the callers on the generator's own output
Found in the independent review of PR #1309 (#1273), 2026-11-15.
#1309 widened route_critical.parse_dru_rules to read every clause of a rule
as KiCad does: a min beside opt and max, a second constraint, mil and in.
Its own tests passed, and so did CI. The review found the blocker: the
widened reader now handed rules_gen's stripline windows (a pair's width held
on one inner layer only, with any BGA field cut out) to dru_net_floors as a
net-wide floor and to dru_neck_rules as a courtyard neck. No test built its
rules from rules_gen, so none saw what the real rules files hold.

The fix tests the callers, not just the reader: test_route_critical.py now
builds its .kicad_dru with rules_gen (diff_pair_rules, impedance_rules,
render_dru) and checks that neither reader takes a layered window; two of
its three cases fail without the fix. When a shared reader starts to accept
more, list its callers (grep) and give each one a case built from the
generator's own output.

Triage: now L0 | target L2 | owner tests/test_route_critical.py | status open | note #1309 added rules_gen-built cases for route_critical's neck and floor readers only; the other callers of parse_dru_rules (route_finish, route_cleanup, stitch_vias, check_creepage, board_update) were not reviewed for one
