"""route_close: the chunked driver's loop against a stand-in router
(route_close_fake.py), and the route_finish flags it runs a chunk with."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))
sys.path.insert(0, str(REPO / "tests"))
import route_close as rc  # noqa: E402
import route_finish as rf  # noqa: E402
import test_route_finish as trf  # noqa: E402

FAKE = REPO / "tests" / "route_close_fake.py"

HDR = """(kicad_pcb (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (setup)
  (net 0 "") (net 1 "SIG") (net 2 "PAIR_P") (net 3 "PAIR_N")
"""
PAIR_TRACK = ('  (segment (start 5 5) (end 9 5) (width 0.2) (layer "F.Cu")'
              ' (net "PAIR_P") (uuid "aaaaaaaa-0000-0000-0000-000000000001"))\n')


def _setup(tmp_path, world: dict, *, opened=None, tracks="", constraints=None):
    """A board with `opened` connections, and the driver's command for it."""
    b = tmp_path / "b"
    b.mkdir(exist_ok=True)
    pcb = b / "t.kicad_pcb"
    ids = sorted(opened if opened is not None
                 else [k for k in world if ":" in k])
    pcb.write_text(HDR + tracks + "  (fake_open "
                   + " ".join(f'"{i}"' for i in ids) + ")\n)\n")
    (tmp_path / "world.json").write_text(json.dumps(world))
    if constraints:
        (b / "constraints.json").write_text(json.dumps(constraints))
    return pcb


def _run(tmp_path, pcb, *extra, chunk_conns=4, check=True):
    env = dict(os.environ, FAKE_WORLD=str(tmp_path / "world.json"),
               FAKE_CALLS=str(tmp_path / "calls.jsonl"))
    out = tmp_path / "out.json"
    out.unlink(missing_ok=True)
    p = subprocess.run(
        [sys.executable, str(SCRIPTS / "route_close.py"), "--pcb", str(pcb),
         "--runner", f"{sys.executable} {FAKE}",
         "--chunk-conns", str(chunk_conns), "--out-report", str(out),
         *extra], env=env, capture_output=True, text=True)
    if check and p.returncode not in (0, 1):
        pytest.fail(f"rc {p.returncode}\n{p.stdout}\n{p.stderr}")
    facts = json.loads(out.read_text())["facts"] if out.exists() else None
    return p, facts


def _calls(tmp_path):
    return [json.loads(x) for x in
            (tmp_path / "calls.jsonl").read_text().splitlines()]


def _open(pcb):
    import re
    return re.findall(r'"([^"]+)"',
                      re.search(r'\(fake_open([^)]*)\)', pcb.read_text())[1])


# The fake takes these from the world by id: a<id> sort first, like the short
# connections that used to use up every run.

def test_failing_connections_do_not_starve_the_long_ones(tmp_path):
    world = {f"a{i:02d}:x": "fail" for i in range(20)}
    world.update({f"z{i}:y": "ok" for i in range(5)})
    pcb = _setup(tmp_path, world)
    p, f = _run(tmp_path, pcb)
    assert p.returncode == 1
    assert sorted(_open(pcb)) == sorted(k for k in world if k[0] == "a")
    assert f["stop_reason"] == "no_progress"
    assert f["unconnected_start"] == 25 and f["unconnected_left"] == 20
    assert f["routed"] == 5
    # two rounds failed them: the one that closed the five, and the one that
    # closed nothing
    assert f["failed_nets"]["a00"]["fails"] == 2
    assert f["failed_nets"]["a00"]["reasons"] == {"no path on the grid": 2}
    assert len(f["still_failing"]) == 20
    assert f["rounds"] == 2
    assert "placement or topology" in f["next"]


def test_a_walled_in_connection_is_set_aside_for_every_round(tmp_path):
    """#682: a pad walled in by fixed copper fails the same way every round,
    so it is tried once; a plain failure is tried again each round."""
    world = {"a:x": "walled", "b:x": "fail", "z:y": "ok"}
    pcb = _setup(tmp_path, world)
    p, f = _run(tmp_path, pcb)
    assert sorted(_open(pcb)) == ["a:x", "b:x"]
    assert f["rounds"] == 2
    assert f["failed_nets"]["b"]["fails"] == 2
    assert f["failed_nets"]["a"]["fails"] == 1
    assert [w["net"] for w in f["walled"]] == ["a"]
    assert "walled in" in f["walled"][0]["reason"]


def test_all_closed_and_later_chunks_reuse_the_last_report(tmp_path):
    pcb = _setup(tmp_path, {f"n{i}:x": "ok" for i in range(9)})
    p, f = _run(tmp_path, pcb)
    assert p.returncode == 0 and f["stop_reason"] == "closed"
    assert f["unconnected_left"] == 0
    calls = _calls(tmp_path)
    assert [c["measure"] for c in calls] == [True] + [False] * (len(calls) - 1)
    assert all(c["start"] for c in calls[1:])
    # a chunk never uses the board's rip-up unless asked
    assert not any(c["rip"] for c in calls[1:])


def test_a_chunk_that_dies_is_undone_and_its_search_blamed(tmp_path):
    world = {"a:x": "ok", "b:x": "die", "c:x": "ok", "d:x": "ok"}
    pcb = _setup(tmp_path, world)
    p, f = _run(tmp_path, pcb)
    assert _open(pcb) == ["b:x"]
    assert "chunk died" in next(iter(f["failed_nets"]["b"]["reasons"]))
    assert f["stop_reason"] == "no_progress"
    # the first chunk's a:x went with it and was routed again later
    assert f["routed"] == 3


def test_chunk_size_halves_on_death_and_grows_back(tmp_path):
    world = {"a:x": "die"}
    world.update({f"b{i}:x": "ok" for i in range(12)})
    pcb = _setup(tmp_path, world)
    _run(tmp_path, pcb, chunk_conns=8)
    sizes = [c["conns"] for c in _calls(tmp_path) if not c["measure"]]
    assert sizes[0] == "8" and sizes[1] == "4"
    assert "8" in sizes[2:]


def test_a_rejected_group_is_halved_until_the_culprit_is_alone(tmp_path):
    world = {f"b{i}:x": "ok" for i in range(7)}
    world["b3:x"] = "poison"
    pcb = _setup(tmp_path, world)
    p, f = _run(tmp_path, pcb, chunk_conns=8)
    assert _open(pcb) == ["b3:x"]
    assert "undone" in next(iter(f["failed_nets"]["b3"]["reasons"]))
    assert f["routed"] == 6


def test_a_size_one_chunk_rejecting_two_routes_doesnt_repeat(tmp_path):
    pcb = _setup(tmp_path, {"a:x": "chain", "b:x": "ok"})
    p, f = _run(tmp_path, pcb, chunk_conns=1)
    assert _open(pcb) == ["a:x"]
    assert f["stop_reason"] == "no_progress"
    assert f["chunks"] < 8


def test_rip_up_doesnt_shrink_on_a_rejected_group(tmp_path):
    world = {f"a{i}:x": "chain" for i in range(6)}
    world["b:x"] = "ok"
    pcb = _setup(tmp_path, world)
    p, f = _run(tmp_path, pcb, "--rip-board", "always", chunk_conns=12)
    assert f["stop_reason"] == "no_progress"
    assert f["chunks"] < 8
    state = json.loads((Path(f["state_dir"]) / "state.json").read_text())
    assert state["conns"] == 12


def test_a_rip_up_run_undone_at_its_end_sets_its_routes_aside(tmp_path):
    pcb = _setup(tmp_path, {"a:x": "ripundone"})
    p, f = _run(tmp_path, pcb, "--rip-board", "always")
    assert f["stop_reason"] == "no_progress"
    assert f["routed"] == 0
    assert "undone" in next(iter(f["failed_nets"]["a"]["reasons"]))
    assert f["chunks"] <= 4


def test_a_chunk_that_changes_nothing_stops_the_run(tmp_path):
    pcb = _setup(tmp_path, {"a:x": "overtime"})
    p, f = _run(tmp_path, pcb)
    assert f["stop_reason"] == "chunks_dying"
    assert "--chunk-timeout" in f["died_why"]
    assert f["chunks"] <= 5


def test_resumes_after_being_killed(tmp_path):
    world = {f"n{i}:x": "ok" for i in range(8)}
    world["kill_at"] = 3
    pcb = _setup(tmp_path, world)
    p, f = _run(tmp_path, pcb, chunk_conns=2, check=False)
    assert p.returncode != 0 and f is None
    left = _open(pcb)
    assert 0 < len(left) < 8
    world["kill_at"] = -1
    (tmp_path / "world.json").write_text(json.dumps(world))
    n = len(_calls(tmp_path))
    p, f = _run(tmp_path, pcb, chunk_conns=2)
    assert f["stop_reason"] == "closed" and _open(pcb) == []
    # it picked up from disk: no new measure, and the original start count
    assert not any(c["measure"] for c in _calls(tmp_path)[n:])
    assert f["unconnected_start"] == 8


def test_fresh_and_a_changed_board_measure_again(tmp_path):
    world = {"a:x": "fail", "b:x": "ok"}
    pcb = _setup(tmp_path, world)
    _run(tmp_path, pcb)
    n = len(_calls(tmp_path))
    pcb.write_text(pcb.read_text().replace('(fake_open', '(fake_open "c:y"'))
    p, f = _run(tmp_path, pcb)
    calls = _calls(tmp_path)[n:]
    assert calls[0]["measure"]
    assert _open(pcb) == ["a:x"]
    n = len(_calls(tmp_path))
    p, f = _run(tmp_path, pcb, "--fresh")
    assert _calls(tmp_path)[n]["measure"]
    assert f["unconnected_start"] == 1


def test_broken_chunks_stop_with_the_reason(tmp_path):
    pcb = _setup(tmp_path, {"a:x": "ok"})
    _run(tmp_path, pcb)     # measure and close
    world = {"broken": 1}
    pcb = _setup(tmp_path, world, opened=["a:x"])
    p, f = _run(tmp_path, pcb, check=False)
    assert p.returncode == 2 and "kicad-cli not found" in p.stdout


def test_chunks_dying_after_a_good_measure(tmp_path):
    # dies before tracing anything: nothing to blame
    world = {"a:x": "ok"}
    pcb = _setup(tmp_path, world)
    closer = _closer(tmp_path, pcb)
    closer.setup()
    closer.report = {"violations": [
        {"source": "unconnected", "net": "a", "severity": "error"}],
        "counts": {"total": 1}}
    closer.st.update(round=1, round_start=1, board_sha=rc.sha(pcb),
                     start_left=1)
    (closer.dir / "report.json").write_text(json.dumps(closer.report))
    closer.runner = ["false"]
    assert closer.run_all() == "chunks_dying"
    assert closer.st["chunk"] == rc.DEAD_LIMIT
    assert closer.st["conns"] == 1
    assert _open(pcb) == ["a:x"]
    assert "chunks die" in closer.payload("chunks_dying")["facts"]["next"].lower()


def _closer(tmp_path, pcb, **kw):
    import argparse
    defaults = dict(pcb=str(pcb), nets=None, layers="auto", grid=0.1,
                    margin=4.0, min_width=0.2, clearance_margin=0.02,
                    via_size=None, via_drill=None, max_nodes=3_000_000,
                    search_weight=1.0, via_in_pad_refs=None, budget=600.0,
                    chunk_timeout=30, chunk_conns=4, runner="none",
                    state_dir=None, fresh=False, rip_board="never",
                    protect_nets=None, protect_board=None, allow_nets=None,
                    constraints=None, no_protect_pairs=False, out_report=None)
    defaults.update(kw)
    return rc.Closer(argparse.Namespace(**defaults))


def test_out_of_budget_saves_and_says_so(tmp_path):
    pcb = _setup(tmp_path, {"a:x": "ok"})
    p, f = _run(tmp_path, pcb, "--budget", "10")
    assert f["stop_reason"] == "budget"
    assert "same command" in f["next"]
    assert _open(pcb) == ["a:x"]


def test_a_search_longer_than_a_chunk_is_set_aside(tmp_path):
    pcb = _setup(tmp_path, {"a:x": "cut", "b:x": "ok"})
    p, f = _run(tmp_path, pcb, chunk_conns=1)
    assert _open(pcb) == ["a:x"]
    assert "outlasted" in next(iter(f["failed_nets"]["a"]["reasons"]))


def test_rip_up_waits_for_a_stall_when_asked(tmp_path):
    pcb = _setup(tmp_path, {"a:x": "riponly", "b:x": "ok"})
    p, f = _run(tmp_path, pcb, "--rip-board", "stalled")
    assert p.returncode == 0 and f["stop_reason"] == "closed"
    rips = [c["rip"] for c in _calls(tmp_path) if not c["measure"]]
    assert rips[0] is False and rips[-1] is True


def test_no_rip_up_by_default_stops_and_offers_it(tmp_path):
    pcb = _setup(tmp_path, {"a:x": "riponly"})
    p, f = _run(tmp_path, pcb)
    assert f["stop_reason"] == "no_progress"
    assert "--rip-board stalled" in f["next"]


# ---------------------------------------------------------- protected copper

PAIRS = {"diff_pairs": [{"p": "PAIR_P", "n": "PAIR_N"}]}


def test_a_chunk_that_loses_pair_copper_is_undone(tmp_path):
    pcb = _setup(tmp_path, {"a:x": "lose", "b:x": "ok"}, tracks=PAIR_TRACK,
                 constraints=PAIRS)
    before = PAIR_TRACK in pcb.read_text()
    p, f = _run(tmp_path, pcb)
    assert before and PAIR_TRACK in pcb.read_text()
    assert _open(pcb) == ["a:x"]
    assert "protected copper" in next(iter(f["failed_nets"]["a"]["reasons"]))
    assert f["undone_for_protected_copper"] >= 1
    assert f["routed"] == 1


def test_pair_nets_are_not_routed_and_are_listed(tmp_path):
    pcb = _setup(tmp_path, {"PAIR_P:1": "ok", "b:x": "ok"}, constraints=PAIRS)
    p, f = _run(tmp_path, pcb)
    assert _open(pcb) == ["PAIR_P:1"]
    assert f["stop_reason"] == "closed"
    assert f["protected_open"] == {"PAIR_P": 1}
    assert "pair router" in f["next"]
    calls = [c for c in _calls(tmp_path) if not c["measure"]]
    assert all("PAIR_P" not in c["nets"].split(",") for c in calls)
    assert "PAIR_P" in calls[0]["protect"]


def test_allow_nets_routes_a_protected_stub(tmp_path):
    pcb = _setup(tmp_path, {"PAIR_P:1": "ok"}, constraints=PAIRS)
    p, f = _run(tmp_path, pcb, "--allow-nets", "PAIR_P")
    assert _open(pcb) == [] and f["stop_reason"] == "closed"


def test_copper_added_to_a_protected_net_is_undone(tmp_path):
    pcb = _setup(tmp_path, {"a:x": "grabs"}, constraints=PAIRS)
    p, f = _run(tmp_path, pcb)
    assert _open(pcb) == ["a:x"] and "PAIR_P" not in pcb.read_text().split(
        "(fake_open")[1]
    assert "(segment" not in pcb.read_text()


def test_no_protect_pairs_turns_it_off(tmp_path):
    pcb = _setup(tmp_path, {"PAIR_P:1": "ok"}, constraints=PAIRS)
    p, f = _run(tmp_path, pcb, "--no-protect-pairs")
    assert _open(pcb) == []


def test_protect_board_start_covers_every_net(tmp_path):
    sig = ('  (segment (start 15 5) (end 19 5) (width 0.2) (layer "F.Cu")'
           ' (net "SIG") (uuid "aaaaaaaa-0000-0000-0000-000000000002"))\n')
    pcb = _setup(tmp_path, {"a:x": "lose", "b:x": "ok"}, tracks=sig)
    p, f = _run(tmp_path, pcb, "--protect-board", "start")
    assert sig in pcb.read_text() and _open(pcb) == ["a:x"]
    # and without it the same chunk would have been kept
    pcb = _setup(tmp_path, {"a:x": "lose", "b:x": "ok"}, tracks=sig)
    p, f = _run(tmp_path, pcb, "--fresh")
    assert sig not in pcb.read_text()


def test_guard_goes_by_geometry_not_uuid(tmp_path):
    a = tmp_path / "a.kicad_pcb"
    b = tmp_path / "b.kicad_pcb"
    a.write_text(HDR + PAIR_TRACK + ")\n")
    b.write_text(HDR + PAIR_TRACK.replace("aaaaaaaa", "bbbbbbbb") + ")\n")
    base = rc.copper_keys(a)
    assert rc.guard(b, base, {"PAIR_P"}, set()) == {"lost": 0, "added_nets": []}
    c = tmp_path / "c.kicad_pcb"
    c.write_text(HDR + PAIR_TRACK.replace("end 9 5", "end 9 6") + ")\n")
    assert rc.guard(c, base, {"PAIR_P"}, set()) == {"lost": 1,
                                                    "added_nets": ["PAIR_P"]}
    assert rc.guard(c, base, {"PAIR_P"}, {"PAIR_P"})["added_nets"] == []


# ---------------------------------------------------------- the runner

def test_runner_choice(monkeypatch):
    monkeypatch.delenv("HWDE_ROUTE_RUNNER", raising=False)
    monkeypatch.setattr(rc.shutil, "which", lambda name: "/x/heavy")
    assert rc.default_runner() == ["heavy", "--quick"]
    monkeypatch.setattr(rc.shutil, "which", lambda name: None)
    assert rc.default_runner() == []
    monkeypatch.setenv("HWDE_ROUTE_RUNNER", "nice -n 5")
    assert rc.default_runner() == ["nice", "-n", "5"]
    monkeypatch.setenv("HWDE_ROUTE_RUNNER", "none")
    assert rc.default_runner() == []


def test_two_runs_cannot_share_a_state_dir(tmp_path):
    pcb = _setup(tmp_path, {"a:x": "ok"})
    one = _closer(tmp_path, pcb)
    one.setup()
    two = _closer(tmp_path, pcb)
    with pytest.raises(rc.CheckError, match="another route_close"):
        two.setup()


# ---------------------------------------------------------- route_finish's side

BOARD = (trf._pad("U1", 5, 5, "SIG") + trf._pad("U2", 25, 5, "SIG")
         + trf._pad("U3", 5, 15, "SIG") + trf._pad("U4", 8, 15, "SIG"))
PAIRS_OF_BOARD = [trf._pair("SIG", (5, 5), (25, 5)),
                  trf._pair("SIG", (5, 15), (8, 15))]


def test_conn_key_ignores_which_end_comes_first():
    assert rf.conn_key("A", [1, 2], [3, 4]) == rf.conn_key("A", [3, 4], [1, 2])
    assert rf.conn_key("A", [1, 2], [3, 4]) != rf.conn_key("B", [1, 2], [3, 4])


def test_a_set_aside_connection_does_not_use_up_max_conns(tmp_path):
    pcb = trf._board(tmp_path, BOARD)
    _, jobs = trf._plan(pcb, PAIRS_OF_BOARD, max_conns=1)
    short = next(j for j in jobs if j["status"] == "routed")
    longer = next(j for j in jobs if j["status"] == "skipped")
    assert longer["reason"] == "over --max-conns"
    skip = {rf.conn_key(short["net"], short["a"], short["b"])}
    _, jobs = trf._plan(pcb, PAIRS_OF_BOARD, max_conns=1, skip=skip)
    by = {tuple(j["a"]): j for j in jobs}
    assert by[tuple(longer["a"])]["status"] == "routed"
    assert by[tuple(short["a"])]["reason"] == "set aside"


def test_a_past_deadline_searches_nothing_and_says_so(tmp_path):
    pcb = trf._board(tmp_path, BOARD)
    tried = []
    _, jobs = trf._plan(pcb, PAIRS_OF_BOARD, deadline=0.0, on_try=tried.append)
    assert not tried
    assert {j["reason"] for j in jobs} == {"over the deadline"}


def test_on_try_names_the_connection_before_its_search(tmp_path):
    pcb = trf._board(tmp_path, BOARD)
    tried = []
    trf._plan(pcb, PAIRS_OF_BOARD, on_try=lambda j: tried.append(
        rf.conn_key(j["net"], j["a"], j["b"])))
    assert len(tried) == 2


def test_protect_nets_are_not_ripped(tmp_path):
    body = trf._pad("U1", 5, 15, "SIG") + trf._pad("U2", 25, 15, "SIG")
    body += trf._seg(15, 0.1, 15, 29.9, 0.25, "F.Cu", "+3V3", "u-wf")
    body += trf._seg(15, 0.1, 15, 29.9, 0.25, "B.Cu", "+3V3", "u-wb")
    pcb = trf._board(tmp_path, body)
    pairs = [trf._pair("SIG", (5, 15), (25, 15))]
    _, jobs = trf._plan(pcb, pairs, rip_board=True)
    assert jobs[0]["status"] == "routed"
    _, jobs = trf._plan(pcb, pairs, rip_board=True, keep_nets_extra={"+3V3"})
    assert jobs[0]["status"] == "failed"
    assert "rip_uuids" not in jobs[0]


def test_routing_flags_match_the_parser():
    import argparse
    ap = argparse.ArgumentParser()
    rf.add_routing_args(ap)
    assert {a.dest for a in ap._actions} - {"help"} == set(rf.ROUTING_FLAGS)


# ------------------------------------------------------------ group step (#938)

def test_group_end_reads_drc_items():
    assert rc.group_end({"msg": "Pad 1 [C] of C1 on F.Cu",
                         "pos": [5, 12]}) == "C1.1"
    assert rc.group_end({"msg": "Pad A3 [D0] of U1 on F.Cu",
                         "pos": [5, 12]}) == "U1.A3"
    assert rc.group_end({"msg": "Track [GND] on B.Cu, length 6.8 mm",
                         "pos": [15.0, 1.5]}) == "15.0,1.5@B.Cu"
    assert rc.group_end({"msg": "Via [GND] on F.Cu - B.Cu",
                         "pos": [1, 2]}) == "1,2"
    assert rc.group_end({"msg": "Zone [GND] on F.Cu", "pos": [1, 2]}) is None


def _group_tight(tmp_path):
    import shutil
    src = REPO / "tests" / "fixtures" / "mazeroute"
    for f in src.glob("group_tight.*"):
        shutil.copy2(f, tmp_path / f.name)
    return tmp_path / "group_tight.kicad_pcb"


def _live_close(tmp_path):
    pcb = _group_tight(tmp_path)
    payload, _ = rc.run(["--pcb", str(pcb), "--layers", "F.Cu",
                         "--runner", "none", "--budget", "280"])
    return payload["facts"]


@pytest.fixture
def kicad():
    import env
    cli = env.find_kicad_cli()
    if cli is None or env.find_kicad_python(cli) is None:
        pytest.skip("KiCad bundled python not installed")
    return cli


def test_live_group_step_closes_a_shared_channel(kicad, tmp_path):
    """Three nets through one 1.33 mm gap: one at a time C never fits, the
    group step routes A, B and C together. GND (the wall's own gap) stays:
    power nets are never grouped."""
    f = _live_close(tmp_path)
    assert f["grouped"] == 1
    assert set(f["open_by_net"]) == {"GND"}
    assert f["errors_after"] == {}


def test_live_without_the_group_step_c_stays_open(kicad, tmp_path,
                                                  monkeypatch):
    """The planted fault: no group grids, no group route - one of A, B, C
    (which one depends on the order) is left."""
    monkeypatch.setattr(rc, "GROUP_GRIDS", ())
    f = _live_close(tmp_path)
    assert f["grouped"] == 0
    assert set(f["open_by_net"]) & {"A", "B", "C"}
