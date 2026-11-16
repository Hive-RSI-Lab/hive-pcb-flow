"""check_model_paths.py - every 3D model path in a board survives a checkout
move (P8 verify suite).

The hit: eleven boards in the boards repo name their footprints' models by
absolute path into the track worktree that built them
(`/.../worktrees/boards/<track>/<ws>/lib/aiee.3dshapes/X.wrl`). Once that
worktree is removed the path is dead, STEP export drops the part and a doc's
render shows the USB-C wrong or missing. lib_pull.py now writes
${KIPRJMOD}-relative paths; this check catches every board made before that,
and anything else that bakes a path in.

A model path passes when it is
  ${KICAD<n>_3DMODEL_DIR}/...   the stock KiCad library, or
  ${KIPRJMOD}/...               relative to the board's dir and staying inside
                                the board's workspace (state.json above it;
                                without one, the dir holding lib/).
Anything else is an error, one finding per distinct path (refs = every
footprint that uses it, pos = the first one), kind:
  model_path_absolute           /home/.., C:/.., \\\\host\\..
  model_path_outside_workspace  ${KIPRJMOD}/ climbing out of the workspace
  model_path_unanchored         a bare relative path, or any other ${VAR}
  model_path_embed_missing      kicad-embed://<name> with no embedded file of
                                that name in the board (kicad-embed:// paths
                                are portable: the file is in the board)
Whether the file exists today does not matter: a path into a live worktree
resolves until the day that worktree goes. A path whose file is missing is
render's concern (kc.model_audit), not this check's.

--fix rewrites each failing path whose file the workspace's own
lib/*.3dshapes holds, by name, to ${KIPRJMOD}/<rel>/lib/<x>.3dshapes/<name>,
in the board and in the workspace's lib/*.pretty footprints (so the next
footprint update does not bring the old path back). It uses the name lookup
render's relink uses (kc.model_libs / kc.model_lib_file). The report is then
taken on the rewritten board; `fixed` lists what changed. A path with no file
in the lib stays and is still reported.

--fix also keeps the board's passed gates fresh (`restamped` in the report).
Gate freshness hashes the whole board, so the rewrite would otherwise stale
place, drc_routed, verify and dfm although no copper moved. The hash keeps
model paths on purpose: this check runs in the verify suite, so a path edit
by hand must still stale verify. Instead, when the workspace's state.json
records a passed gate against the board exactly as it was before the
rewrite, and the rewrite changed nothing but (model "...") strings, the
gate's recorded board hash moves to the new one (State.restamp_input). The
rewrite can only clear model-path findings, so a gate that passed before
still passes. A gate that was already stale, or failed, stays as it was.
The board is read once and fixed under its writer lock (the one the SWIG
workers take), so the hash the gates move from is of the text the rewrite
started from. If state.json changed meanwhile, its save refuses and the
report says so under `restamped.skipped`; the board stays fixed.

CLI: --pcb board.kicad_pcb [--fix] [--out report.json]
     exit 0 pass / 1 violations / 2 error, per SPEC section 6.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import sexpdata

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "lib"))
import checklib  # noqa: E402
import kc  # noqa: E402
import safelib  # noqa: E402
import statelib  # noqa: E402

SCRIPT = "check_model_paths"
KINDS = {"absolute": "model_path_absolute",
         "outside_workspace": "model_path_outside_workspace",
         "unanchored": "model_path_unanchored",
         "embed_missing": "model_path_embed_missing"}


def _head(node) -> str | None:
    if isinstance(node, list) and node and isinstance(node[0], sexpdata.Symbol):
        return node[0].value()
    return None


def _sval(x) -> str:
    return x.value() if isinstance(x, sexpdata.Symbol) else str(x)


def board_models(pcb: Path) -> list[tuple[str, tuple, str]]:
    """(ref, (x, y), model path) for every model of every footprint, in
    board order."""
    data = sexpdata.loads(_read(pcb))
    out = []
    for node in data[1:]:
        if _head(node) != "footprint":
            continue
        ref, pos, models = None, None, []
        for sub in node[1:]:
            h = _head(sub)
            if h == "property" and len(sub) >= 3 \
                    and _sval(sub[1]) == "Reference":
                ref = _sval(sub[2])
            elif h == "fp_text" and len(sub) >= 3 and ref is None \
                    and _sval(sub[1]) == "reference":  # KiCad 6 (#1429)
                ref = _sval(sub[2])
            elif h == "at" and len(sub) >= 3:
                pos = (float(sub[1]), float(sub[2]))
            elif h == "model" and len(sub) >= 2:
                models.append(_sval(sub[1]))
        out += [(ref or "?", pos, m) for m in models]
    return out


def workspace_of(pcb: Path) -> Path:
    """The board's workspace: the dir holding state.json above it, else the
    dir holding lib/ (the board's own or its parent), else the board's dir."""
    ws = statelib.find_workspace(pcb)
    if ws:
        return ws
    base = pcb.resolve().parent
    return next((r for r in (base, base.parent) if (r / "lib").is_dir()), base)


def _read(p: Path) -> str:
    # newline="" keeps a CRLF board byte-identical outside the rewritten paths
    with open(p, encoding="utf-8", newline="") as fh:
        return fh.read()


def _write(p: Path, text: str) -> None:
    with open(p, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def fix(pcb: Path, ws: Path, pcb_text: str | None = None
        ) -> tuple[dict, str | None]:
    """Rewrite the board's and the workspace lib footprints' failing model
    paths that the workspace lib holds by name. `pcb_text` is the board as
    the caller already read it (else it is read here). Returns
    ({file: {old: new}}, the board text written or None)."""
    project = pcb.resolve().parent
    libs = kc.model_libs(project)
    files = [pcb] + sorted({f for d in libs
                            for f in d.parent.glob("*.pretty/*.kicad_mod")})
    done: dict[str, dict[str, str]] = {}
    board_new = None
    for f in files:
        text = pcb_text if f is pcb and pcb_text is not None else _read(f)
        new, changed = kc.model_paths_portable(text, project, ws, libs)
        if changed:
            _write(f, new)
            done[str(f)] = changed
            if f is pcb:
                board_new = new
    return done, board_new


def _blank_models(text: str) -> str:
    return kc._MODEL_RE.sub('(model ""', text)


def _state_pcb(pcb: Path, ws: Path, old_bytes: bytes):
    """(State, norm, hash of `old_bytes`) when ws/state.json is a v2 state
    whose pcb kind is this board, else a reason string. The hash is of the
    bytes the caller read, never a second read of the file."""
    import state as state_mod
    sp = ws / "state.json"
    if not sp.is_file():
        return "no state.json"
    try:
        st = state_mod.State.load(sp)
    except checklib.CheckError as exc:
        return f"state.json unreadable: {exc}"
    imap = statelib.load_map()
    rel = statelib.kind_path("pcb", st.data.get("board") or "", imap,
                             st.data["artifacts"], ws)
    if (ws / rel).resolve() != pcb.resolve():
        return f"the workspace's board is {rel}, not this file"
    norm = imap["artifact_kinds"]["pcb"]["norm"]
    return st, norm, statelib.hash_bytes(old_bytes, norm)


def restamp(pre, old_text: str, new_text: str) -> dict:
    """Move the passed gates recorded against the pre-fix board to the
    rewritten one (module doc). `pre` is _state_pcb() of the bytes `old_text`
    was decoded from; `new_text` is what fix() wrote."""
    if isinstance(pre, str):
        return {"gates": [], "skipped": pre}
    if _blank_models(new_text) != _blank_models(old_text):
        return {"gates": [], "skipped": "the rewrite changed more than "
                                        "model paths"}
    st, norm, old = pre
    new = statelib.hash_bytes(new_text.encode("utf-8"), norm)
    moved = st.restamp_input("pcb", old, new, "check_model_paths --fix: "
                             "3D model paths only")
    if moved:
        try:
            st.save()
        except (safelib.StaleWriteError, safelib.LockBusy) as exc:
            return {"gates": [], "skipped": f"state.json not updated, the "
                    f"gates stay stale: {exc}"}
    return {"gates": moved}


def check(pcb: Path, ws: Path) -> list[dict]:
    project = pcb.resolve().parent
    libs = kc.model_libs(project)
    by_path: dict[str, dict] = {}
    text = _read(pcb)
    embedded = set(re.findall(r'\(embedded_files(?:(?!\n\t\(footprint ).)*?\(name "([^"]*)"', text, re.S)) \
        | set(re.findall(r'\(file\s*\(name "([^"]*)"', text))
    for ref, pos, path in board_models(pcb):
        why = kc.model_path_kind(path, project, ws)
        if why is None and path.startswith(kc.EMBED_PREFIX) \
                and path[len(kc.EMBED_PREFIX):] not in embedded:
            why = "embed_missing"
        if why is None:
            continue
        hit = by_path.setdefault(path, {"why": why, "refs": [], "pos": pos})
        if ref not in hit["refs"]:
            hit["refs"].append(ref)
    viols = []
    for path, h in by_path.items():
        name = path.replace("\\", "/").rsplit("/", 1)[-1]
        lib_file = kc.model_lib_file(path, libs)
        if lib_file:
            new = kc.portable_model_path(lib_file, project)
            fix_line = (f"check_model_paths.py --pcb {pcb} --fix rewrites it "
                        f"to {new}")
        else:
            fix_line = (f"put {name} in {ws / 'lib'}/<lib>.3dshapes/ (re-pull "
                        f"the part with lib_pull.py, or copy it in), then run "
                        f"check_model_paths.py --pcb {pcb} --fix; a stock "
                        f"KiCad model takes ${{KICAD10_3DMODEL_DIR}}/...")
        if h["why"] == "embed_missing":
            fix_line = (f"embed {name} in the board (KiCad: Board Setup > Embedded Files), "
                        f"or point the model at a library file")
        kind = KINDS[h["why"]]
        refs = h["refs"]
        what = ("names an embedded file the board doesn't hold" if h["why"] == "embed_missing"
                else f"is {h['why'].replace('_', ' ')} - it breaks once that checkout moves")
        msg = (f"{', '.join(refs[:6])}{' ...' if len(refs) > 6 else ''}: "
               f"3D model path {path} {what}. Fix: {fix_line}")
        viols.append(checklib.violation(
            SCRIPT, "error", h["pos"], None, None, refs, msg, SCRIPT,
            kind=kind, model_path=path, fixable=bool(lib_file),
            fix=fix_line))
    return viols


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcb", required=True)
    ap.add_argument("--fix", action="store_true",
                    help="rewrite failing paths the workspace lib holds by "
                         "name, in the board and its lib footprints")
    ap.add_argument("--out", help="write the JSON report here")
    args = ap.parse_args(argv)
    pcb = Path(args.pcb)
    if not pcb.is_file():
        raise checklib.CheckError(f"board not found: {pcb}")
    ws = workspace_of(pcb)
    fixed = restamped = None
    if args.fix:
        # one read under the board lock the SWIG writers take: the hash the
        # gates move FROM is of exactly the text the rewrite starts from
        with safelib.writer_lock(pcb.resolve(), what="board"):
            old_bytes = pcb.read_bytes()
            old_text = old_bytes.decode("utf-8")
            pre = _state_pcb(pcb, ws, old_bytes)
            fixed, new_text = fix(pcb, ws, old_text)
            if new_text is not None:
                restamped = restamp(pre, old_text, new_text)
    viols = check(pcb, ws)
    facts = {"workspace": str(ws),
             "model_libs": [str(d) for d in kc.model_libs(pcb.resolve().parent)]}
    if fixed is not None:
        facts["fixed"] = fixed
    if restamped is not None:
        facts["restamped"] = restamped
    none = "(model " not in pcb.read_text(encoding="utf-8", errors="replace")
    return checklib.report(SCRIPT, pcb, viols, **facts,
                           nothing_to_judge="no 3D model on the board"
                           if none else None), args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    sys.exit(main())
