#!/usr/bin/env python
"""report_gen.py - assemble a board's design document (LaTeX -> PDF) from its run workspace.

Reads state.json (read-only) plus the run's markdown/JSON/render artifacts and
writes reports/design_doc/<board>-design-doc.tex (other --kind: below), then compiles it to PDF with
lualatex (HWDE_LUALATEX pin, else PATH; two passes, staged in a system temp dir
so no .aux/.log/.toc litter ever lands in the git-tracked workspace). Every
kind is set in pdf-material-builder's house style: housestyle.sty and its
vendored Source Serif 4 / IBM Plex Mono are used by path from
HWDE_HOUSE_STYLE, else ~/.claude/skills/pdf-material-builder/references/
house-style (put on TEXINPUTS for the compile, never copied into hwde). Sections are conditional on the run's
phase: not-yet-due sections render a one-line "Pending" stub; due-but-absent
CORE artifacts (schematic.pdf, a board render, bom_rows, order.json) are
violations, everything else absent is a warning. All external text goes
through latex_escape (total function; the final .tex is asserted pure ASCII).

Asset paths embedded in run JSON are unreliably backslashed and mixed
repo-/workspace-relative, so every asset is resolved by this script's own
ladder relative to the workspace root and emitted with forward slashes.

The Run Record carries the model cost of generating the board from
reports/cost.json (gen_cost.py): the total, the split by step or the reason
there is none. A missing cost.json is a warning and a "not recorded" line.

The DFM section states the board's castellated pads from bom_cpl.json (else
order.json's spec snapshot): the count and refs when there are some, "none"
when there are none - and then a brief/brief.md or architecture/*.md line
that claims castellation (and does not deny it) is a warning.

A finished PDF is filed with `cc-docs file` only when asked: `--file` (the
command a person or session runs to finish a report) or DOC_PROJECT set in the
environment. A board document already in the library (report_gen.
filed_document: its source in reports/<kind>/ of this board's directory, or
its last revision describing this part number) is filed under its own
project and exact title, so the rebuild is its next revision even from
another worktree, where cc-docs' source-path match misses. Else it files
into DOC_PROJECT, or the project "Boards" (002), as "<PN> <board> design
doc" ("<board> design doc" with no part number).
With neither, the PDF is built and nothing is filed, so test runs and
scratch builds never reach the register. A filing that succeeds leaves
reports/design_doc/.filed.json (a hash of the .tex, its "generated" time
left out, and of the images it includes, plus the project and the cc-docs
library, CC_DOCS_ROOT or "" for the default); a rebuild whose stamp matches
files nothing and sets the payload's `unchanged`, so filing into a scratch
library never stops the live one from getting the revision. A failed filing,
or cc-docs missing from PATH, only warns and leaves the stamp alone.
The filing names the board's part number with --describes PCB-NNNN-R when
the boards register lists the workspace (lib/boardreg.py; hwde never
allocates one), and passes --cost <step>=<usd> once per step of
reports/cost.json. The part number is also printed under the title, in
every page's running head beside the board name, and as a row of the metadata
table ("not in the boards register" otherwise). The payload's `filed` is
cc-docs' first output line (the number and path) when this run filed, else
null; `unchanged` is true when a matching stamp skipped the filing.
A filing also puts the board's fab set on the revision it filed or found
unchanged, every name led by the workspace directory's name <ws> (owner,
#ai-ee: "name the files all with the prefix of the project name"):
fab/<ws>_gerbers.zip, fab/BOM.csv and fab/CPL.csv as <ws>_BOM.csv and
<ws>_CPL.csv (copied under those names in a temp dir; the JLC tooling keeps
the bare ones), and fab/<ws>_BOM_digikey.csv, <ws>_BOM_mouser.csv and
<ws>_BOM_cost.json, each only when present. It is a second call,
`cc-docs attach <number> --replace`, because `cc-docs file --attach`
refuses a changed file on an unchanged revision and refuses the PDF with it
when a file is over a cap. The stamp carries each attached file's hash, so a
fab set that changed under an unchanged PDF still goes up; a failed attach
warns and leaves the stamp alone. The payload's `attached` lists the names
put up this run. A board with no fab set files as before. The workspace may be named by its directory, the board's old name or
its part number (lib/boardreg.py resolves the last two).
--note TEXT (only with --file) is the filed revision's note, passed as
`cc-docs file --note`; an unchanged rebuild files nothing and warns that
the note was dropped.

The Verification section (and the highlight's Checks and Cost) lists the
board's accepted verify waivers, one item each: what fires (check, kind),
where (net, refs, pos), the reason, `approved` (who accepted it and when)
and `expires`, each only when the entry has it. They are read as the verify
gate reads them (releaselib.waivers_for_input + gate.load_waivers); a file
the gate would refuse is a warning and a line saying so, and a board with
no reports/verify-waivers.json gets nothing. The highlight cuts each reason
at HL_REASON_CAP characters.

--kind picks the document (KINDS): `design` (the default, everything above),
`highlight` (a few pages: the brief's opening, the board's facts, top and
bottom renders, the BOM and the schematic's first page, the decisions that
changed the board, how often the run went back, gates and cost) or `full`
(the design doc plus a render of every routing/pre-* and post-* (or pre_*, post_*) snapshot,
every state.json decision with its why, the run's history - phase timeline,
backtracks, part choices and the kicad/parts.json changes git shows, commits
by hour, COMPARISON.md and the placement and routing notes - and a figure of
how the run went, drawn by the diagram-maker skill from reports/design_full/
flow.json; lib/dochistory.py reads all of it). Each kind writes its own
reports/<design_doc|highlight|design_full>/ and files as its own document
("<PN> <board> design doc", "... highlight doc", "... full design doc").
--render-history renders the snapshots first (render.py, top view; a PNG
newer than its board is kept); without it only PNGs already there are shown
and a warning says so. The metadata table (the highlight's facts table too)
has a design spend row, 'USD <actual> of a USD <cap> cap', from the optional
reports/design_spend.json (actual_usd, cap_usd, source, as_of) that the
boards repo writes; without it the row says 'not recorded'. The full doc also gets Board Layers - one page per
copper layer, front to back, drawn as KiCad shows it with the net names on
its pads, tracks and zones, then the top, bottom and iso 3D views - and the
highlight gets the outer two layers and the iso view. layer_views.py draws
them into reports/layers/ (layers.json lists them), which both kinds share;
--render-layers runs it first unless its layers.json is newer than the
board and says pass (a failed run is drawn again), and without it only what is already there is shown, with a warning. --history-ref names the git ref whose log is the
history (a board squash-merged into the boards repo keeps its run on its
track branch). The figure needs node and the diagram-maker skill
(HWDE_DIAGRAM_MAKER, else ~/.claude/skills/diagram-maker); without them it
is a warning and a line saying why.

Exit 0 "pass"   = requested outputs produced (--tex-only: the .tex alone).
Exit 1 "violations" = degraded: compile failed, lualatex or the house style
                  absent (auto tex-only), or core artifacts missing for the
                  run's phase.
Exit 2 "error"  = unusable workspace / internal error (a bad HWDE_LUALATEX
                  pin propagates here - loud, never degraded).

CLI:
  report_gen.py --workspace ~/dev/boards/<name> [--out report.json] [--tex-only]
                [--file [--note TEXT]] [--name NAME] [--kind design|highlight|full]
                [--render-history] [--render-layers] [--history-ref REF]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import state as statemod  # noqa: E402  (read-only: PHASES/CHECKPOINTS consts)
from lib import boardreg, dochistory, env, statelib  # noqa: E402

PHASE_INDEX = {p: i for i, p in enumerate(statemod.PHASES)}

# Section id -> (LaTeX heading, phase whose start makes the section due;
# None = always included). Order = document order = pipeline order.
SECTIONS = [
    ("title", "Board and Run Metadata", None),
    ("overview", "Overview", "P1"),
    ("requirements", "Requirements", "P1"),
    ("architecture", "Architecture", "P2"),
    ("schematic", "Schematic", "P4"),
    ("layout", "Layout", "P6"),
    ("verification", "Verification", "P8"),
    ("dfm_fab", "DFM and Fabrication", "P9"),
    ("run_record", "Run Record (Appendix)", None),
    ("artifact_index", "Artifact Index", None),
]
# The short highlight doc: what the board is, a picture, the facts, the BOM
# and the schematic's first page, the decisions that shaped it and where the
# checks stand.
HIGHLIGHT_SECTIONS = [
    ("title", "At a Glance", None),
    ("hl_board", "The Board", None),
    ("hl_layers", "Copper Layers", "P6"),
    ("hl_parts", "What Is On It", None),
    ("hl_decisions", "Key Decisions", None),
    ("hl_run", "How the Run Went", None),
    ("hl_checks", "Checks and Cost", None),
]
# The full design doc: the design doc's sections plus every render, every
# decision and why, the run's history and a figure of how it actually went.
FULL_SECTIONS = SECTIONS[:6] + [
    ("layers", "Board Layers", "P6"),
    ("renders", "Routing Renders", "P6"),
    ("decisions", "Design Decisions", None),
    ("history", "Design History", None),
    ("flow", "How the Design Process Went", None),
] + SECTIONS[6:]
# kind -> output subdir of reports/, file stem suffix, title words, the
# cc-docs title suffix, and its sections. Each kind files as its own document.
KINDS = {
    "design": ("design_doc", "design-doc", "Design Document", "design doc", SECTIONS),
    "highlight": ("highlight", "highlight", "Highlights", "highlight doc",
                  HIGHLIGHT_SECTIONS),
    "full": ("design_full", "design-full", "Full Design Document",
             "full design doc", FULL_SECTIONS),
}
# Core artifacts: (payload label, owning section, phase that must have PASSED
# for absence to be a violation). Renders are special-cased (ladder).
CORE_SCHEMATIC = ("reports/schematic.pdf", "schematic", "P4")
CORE_RENDER = ("board render (reports render ladder)", "layout", "P6")
CORE_BOM = ("reports/bom_cpl.json bom_rows", "dfm_fab", "P9")
CORE_ORDER = ("fab/order.json", "dfm_fab", "P10")

PDF_TIMEOUT = 300  # seconds per lualatex pass
RENDER_TIMEOUT = 300  # seconds per routing-snapshot render
LAYERS_REL = "reports/layers"   # layer_views.py output, shared by highlight and full
LAYERS_TIMEOUT = 1200  # seconds for every layer plus three 3D views
DIAGRAM_TIMEOUT = 120  # seconds for the flow figure's export


class ReportError(RuntimeError):
    """Unusable workspace or internal invariant failure (exit 2)."""


# ---------------------------------------------------------------- escaping

# Per-character total map: every input char maps independently, so ordering
# hazards (escaping the backslashes of \textbackslash{}) cannot occur.
_CHAR_MAP = {
    "\\": r"\textbackslash{}",
    "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_",
    "{": r"\{", "}": r"\}",
    "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
    # OT1 text mode renders these ASCII chars as inverted punctuation / an
    # em-dash (host-verified: ">3A" printed as an upside-down question mark).
    "<": r"\textless{}", ">": r"\textgreater{}", "|": r"\textbar{}",
    # Context-sensitive after the \\ that joins tt_block/longtable lines and
    # inside \item: a line-initial "[" parses as the optional argument of \\
    # (fatal "Missing number") or as the item label, and "*" as the \\* form.
    # Brace-wrapping renders the literal char in every context.
    "[": "{[}", "]": "{]}", "*": "{*}",
    # Non-ASCII transliteration (the corpus really contains all of these).
    "\u00b1": r"\(\pm\)",          # plus-minus
    "\u03a9": r"\(\Omega\)",       # greek capital omega
    "\u2126": r"\(\Omega\)",       # ohm sign (normalizes to omega)
    "\u00b5": r"\(\mu\)",          # micro sign U+00B5
    "\u03bc": r"\(\mu\)",          # greek small mu U+03BC
    "\u2103": r"\(^{\circ}\)C",    # degree celsius single glyph U+2103
    "\u00b0": r"\(^{\circ}\)",     # degree sign
    "\u2022": r"\textbullet{}",    # bullet
    "\u0394": r"\(\Delta\)",       # greek capital delta
    "\u00d8": "dia. ",             # diameter-ish O-slash
}


# A line claiming castellation, and the words that make it a denial instead
# ("not castellated", "plain PTH instead of castellated").
_CASTELLATED_CLAIM = re.compile(r"castellat", re.I)
_CASTELLATED_DENIED = re.compile(
    r"\b(not|no|without|instead|plain|deviation|non)\b", re.I)


def latex_escape(text) -> str:
    """Total function: any value -> LaTeX-safe pure-ASCII text.

    Backslash maps first by construction (single per-char pass over the
    ORIGINAL string). Unknown non-ASCII (CJK vendor names etc.) -> '?'.
    """
    if text is None:
        return ""
    out = []
    for ch in str(text):
        if ch in _CHAR_MAP:
            out.append(_CHAR_MAP[ch])
        elif ch == "\r":
            continue
        elif ch in ("\n", "\t") or 32 <= ord(ch) < 127:
            out.append(ch)
        elif ord(ch) < 32 or ord(ch) == 127:
            out.append(" ")
        else:
            out.append("?")
    return "".join(out)


# ---------------------------------------------------------------- markdown-lite

_INLINE_RE = re.compile(
    r"`([^`]+)`"                                   # `code`
    r"|\*\*([^*]+)\*\*"                            # **bold**
    r"|\*([^*\s][^*]*)\*"                          # *emph*
    r"|(?<!\w)_([^_\s](?:[^_]*[^_\s])?)_(?!\w)"    # _emph_ (word-bounded)
    r"|\[([^\]]+)\]\(([^)]+)\)"                    # [text](url)
)


def _inline(raw: str) -> str:
    """Inline md-lite -> LaTeX: parse markers on RAW text, escape every
    payload fragment."""
    out, pos = [], 0
    for m in _INLINE_RE.finditer(raw):
        out.append(latex_escape(raw[pos:m.start()]))
        code, bold, star, under, ltext, lurl = m.groups()
        if code is not None:
            out.append(r"\texttt{" + latex_escape(code) + "}")
        elif bold is not None:
            out.append(r"\textbf{" + latex_escape(bold) + "}")
        elif star is not None:
            out.append(r"\emph{" + latex_escape(star) + "}")
        elif under is not None:
            out.append(r"\emph{" + latex_escape(under) + "}")
        else:
            out.append(latex_escape(ltext) + r" (\texttt{" + latex_escape(lurl) + "})")
        pos = m.end()
    out.append(latex_escape(raw[pos:]))
    return "".join(out)


def tt_block(text: str) -> str:
    """Verbatim-ish block: escaped lines in a ragged \\ttfamily quote.
    Used for fenced code, md tables (never parsed) and run digests."""
    lines = [latex_escape(ln.rstrip()) for ln in text.splitlines()]
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return ""
    body = "\\\\\n".join(ln if ln.strip() else "~" for ln in lines)
    return ("\\begin{quote}\\small\\ttfamily\\raggedright\n"
            + body + "\n\\end{quote}")


_HEAD_CMD = {1: r"\subsection*", 2: r"\subsubsection*", 3: r"\paragraph*"}


def md_to_latex(text: str) -> str:
    """Markdown-lite -> LaTeX (a ceiling, not an engine): #/##/### headings,
    -/* bullets (one nesting level), inline bold/emph/code/link; md table
    lines and fenced blocks pass through as escaped tt blocks; everything
    else becomes escaped paragraphs."""
    out: list[str] = []
    lines = text.splitlines()
    i, n = 0, len(lines)
    depth = 0          # current itemize nesting (0..2)
    para: list[str] = []

    def close_lists(to: int = 0) -> None:
        nonlocal depth
        while depth > to:
            out.append(r"\end{itemize}")
            depth -= 1

    def flush_para() -> None:
        if para:
            out.append(" ".join(para))
            out.append("")
            para.clear()

    while i < n:
        raw = lines[i]
        stripped = raw.strip()
        if stripped.startswith("```"):                      # fenced block
            flush_para()
            close_lists()
            j = i + 1
            block = []
            while j < n and not lines[j].strip().startswith("```"):
                block.append(lines[j])
                j += 1
            out.append(tt_block("\n".join(block)))
            i = j + 1
            continue
        if stripped.startswith("|"):                        # md table run
            flush_para()
            close_lists()
            j = i
            block = []
            while j < n and lines[j].strip().startswith("|"):
                block.append(lines[j])
                j += 1
            out.append(tt_block("\n".join(block)))
            i = j
            continue
        mh = re.match(r"^(#{1,3})\s+(.*)$", stripped)
        if mh and not raw.startswith(" "):                  # heading
            flush_para()
            close_lists()
            out.append(_HEAD_CMD[len(mh.group(1))] + "{" + _inline(mh.group(2)) + "}")
            i += 1
            continue
        mb = re.match(r"^(\s*)[-*]\s+(.*)$", raw)
        if mb:                                              # bullet
            flush_para()
            want = 2 if len(mb.group(1)) >= 2 else 1
            want = min(want, depth + 1)                     # never skip a level
            if want > depth:
                out.append(r"\begin{itemize}")
                depth += 1
            else:
                close_lists(want)
            out.append(r"\item " + _inline(mb.group(2)))
            i += 1
            continue
        if not stripped:                                    # blank
            flush_para()
            i += 1
            continue
        if depth and raw[:1].isspace():                     # bullet continuation
            out.append(_inline(stripped))
            i += 1
            continue
        close_lists()
        para.append(_inline(stripped))
        i += 1
    flush_para()
    close_lists()
    return "\n".join(out)


# ---------------------------------------------------------------- tex helpers

def longtable(colspec: str, header: list[str], rows: list[list[str]]) -> str:
    """Cells must already be LaTeX-ready (escaped by the caller)."""
    if not rows:
        return r"\emph{(no entries)}"
    # House-style data table: an ink rule over small-caps heads, grey rules
    # below (\hstoprule / \hshead from housestyle.sty).
    head = [h.replace(r"\textbf{", r"\hshead{") for h in header]
    out = ["{\\small", r"\begin{longtable}{" + colspec + "}", r"\hstoprule",
           " & ".join(head) + r" \\", r"\hline", r"\endhead"]
    for r in rows:
        out.append(" & ".join(r) + r" \\")
    out += [r"\hline", r"\end{longtable}", "}"]
    return "\n".join(out)


def image_block(rel_posix: str, width: str) -> str:
    """Centered non-floating image + its path as a caption line.
    The \\includegraphics argument is the RAW forward-slash path (modern
    LaTeX kernels handle underscores in file names); the caption is escaped."""
    return ("\\begin{center}\n"
            f"\\includegraphics[width={width}\\textwidth]{{{rel_posix}}}\\\\\n"
            "{\\small\\texttt{" + latex_escape(rel_posix) + "}}\n"
            "\\end{center}")


def _chosen_stackup(stack_md: str) -> str | None:
    """Pull the chosen stackup out of architecture/stackup.md.

    Two shapes are accepted, because architects legitimately write both:
    `## Chosen stackup: NAME` (value on the heading) and a bare
    `## Chosen stackup` heading whose value is the first content line under
    it. The bare form is what order_submit.derive_copper_oz's scan window
    encourages, so it must not crash the report (it did: bare heading ->
    split(":", 1)[1] -> IndexError, buck-5v3a P2).
    """
    lines = stack_md.splitlines()
    for i, ln in enumerate(lines):
        if not ln.startswith("## Chosen"):
            continue
        _, sep, tail = ln.partition(":")
        if sep and tail.strip():
            return tail.strip()
        for nxt in lines[i + 1:]:
            if nxt.startswith("#"):
                break          # ran into the next heading: no value
            if nxt.strip():
                return nxt.strip()
        return None
    return None


# ---------------------------------------------------------------- data access

def read_text(ws: Path, rel: str) -> str | None:
    p = ws / rel
    if not p.is_file():
        return None
    return p.read_text(encoding="utf-8", errors="replace")


def read_json(ws: Path, rel: str) -> dict | None:
    t = read_text(ws, rel)
    if t is None:
        return None
    try:
        d = json.loads(t)
        return d if isinstance(d, dict) else None
    except json.JSONDecodeError:
        return None


# Each stage's report, by the name the recipes save it under (tasks.yaml,
# recipes/review.md) first, then the names runs have also used: verify_all
# itself always writes reports/checks/summary.json, and a run that picks its
# own --out tends to hyphenate. The first one there is read.
STAGE_REPORTS = {
    "verify": ("reports/verify_all.json", "reports/checks/summary.json",
               "reports/verify-summary.json"),
    "fab_export": ("reports/fab_export.json", "reports/fab-export.json"),
    "bom_cpl": ("reports/bom_cpl.json", "reports/bom-cpl.json"),
    "gate_dfm": ("reports/gate-dfm.json", "reports/dfm-gate.json"),
}


def read_stage(ws: Path, stage: str) -> tuple[dict | None, str]:
    """A stage's report (STAGE_REPORTS) and the name it was read from; None
    and the recipes' name when no name holds a JSON object."""
    for rel in STAGE_REPORTS[stage]:
        d = read_json(ws, rel)
        if d is not None:
            return d, rel
    return None, STAGE_REPORTS[stage][0]


VERIFY_WAIVERS = "verify-waivers.json"
HL_REASON_CAP = 260   # the highlight cuts a waiver's reason here


def verify_waivers(ws: Path) -> tuple[list[dict], str | None, str | None]:
    """The board's verify waivers as the verify gate reads them: the file
    releaselib.waivers_for_input resolves for this workspace (the first
    parent of state.json holding state.json is the workspace itself), loaded
    and checked by gate.load_waivers. Returns (waivers, the file's path
    relative to the workspace, None); ([], path, error) for a file the gate
    would refuse or with an entry releaselib.waiver_shape_problem refuses;
    ([], None, None) for a board with none."""
    from lib import releaselib
    path = releaselib.waivers_for_input(ws / "state.json", VERIFY_WAIVERS)
    if path is None:
        return [], None, None
    try:
        rel = path.resolve().relative_to(ws.resolve()).as_posix()
    except ValueError:
        rel = str(path)
    import gate  # sibling script, loaded only for a board with waivers
    try:
        waivers = gate.load_waivers(path)
    except (OSError, ValueError, RuntimeError) as exc:
        return [], rel, f"{type(exc).__name__}: {exc}"
    for i, w in enumerate(waivers):
        problem = releaselib.waiver_shape_problem(w)
        if problem:
            return [], rel, f"waiver {i}: {problem}"
    return waivers, rel, None


def waiver_item(w: dict, reason_cap: int | None = None) -> str:
    """One verify waiver as a LaTeX \\item: what fires (check, kind), where
    (net, refs, pos), the reason, who accepted it and when (`approved`) and
    `expires`. A field the entry lacks is left out, never filled in."""
    fires = " / ".join(str(w[k]) for k in ("check", "kind") if w.get(k))
    where = []
    if w.get("net"):
        where.append(f"net {w['net']}")
    refs = w.get("refs")
    if refs:
        where.append("refs " + (", ".join(map(str, refs))
                                if isinstance(refs, list) else str(refs)))
    pos = w.get("pos")
    if pos is not None:
        try:
            where.append(f"at ({float(pos[0]):g}, {float(pos[1]):g}) mm")
        except (TypeError, ValueError, IndexError, KeyError):
            where.append(f"at {pos}")
    head = r"\textbf{" + latex_escape(fires) + "}"
    if where:
        head += " on " + latex_escape(", ".join(where))
    lines = [head + "."]
    reason = " ".join(str(w.get("reason") or "").split())
    if reason:
        if reason_cap and len(reason) > reason_cap:
            reason = reason[:reason_cap - 3].rstrip() + "..."
        lines.append("Reason: " + latex_escape(reason))
    for key, label in (("approved", "Accepted"), ("expires", "Expires")):
        if str(w.get(key) or "").strip():
            lines.append(f"{label}: " + latex_escape(" ".join(str(w[key]).split())))
    return r"\item " + " \\par\n".join(lines)


SPEND_REL = "reports/design_spend.json"


def design_spend(ws: Path) -> str:
    """The 'Design spend' row: 'USD <actual> of a USD <cap> cap' from the
    optional reports/design_spend.json (actual_usd, cap_usd, source, as_of),
    which something outside hwde writes - hwde never reads the box's own run
    records. Absent or unreadable it is 'not recorded'."""
    d = read_json(ws, SPEND_REL) or {}
    try:
        actual = float(d["actual_usd"])
    except (KeyError, TypeError, ValueError):
        return "not recorded"
    text = f"USD {actual:.2f}"
    try:
        text += f" of a USD {float(d['cap_usd']):.2f} cap"
    except (KeyError, TypeError, ValueError):
        text += " (no cap recorded)"
    if d.get("as_of"):
        text += f", as of {d['as_of']}"
    return text


def phase_idx(phase: str) -> int:
    return PHASE_INDEX.get(phase, 0)


def find_renders(ws: Path, board: str) -> tuple[list[str], list[str]]:
    """Main render ladder (first convention that hits wins) + extras.
    Returns workspace-relative forward-slash paths."""
    ladders = [
        ["reports/render_final/top.png", "reports/render_final/bottom.png"],
        [f"reports/renders/{board}_top.png", f"reports/renders/{board}_bottom.png",
         f"reports/renders/{board}_iso.png"],
        [f"reports/{board}_top.png", f"reports/{board}_bottom.png"],
    ]
    main: list[str] = []
    for rung in ladders:
        main = [r for r in rung if (ws / r).is_file()]
        if main:
            break
    extras = [r for r in (f"reports/render_labeled/{board}_top.png",
                          f"reports/layers/{board}_layers.png")
              if (ws / r).is_file()]
    return main, extras


# ---------------------------------------------------------------- builder

class DocBuilder:
    def __init__(self, ws: Path, st: dict, name: str, kind: str = "design",
                 render_history: bool = False, history_ref: str = "HEAD",
                 render_layers: bool = False):
        self.ws = ws
        self.render_layers = render_layers
        self.history_ref = history_ref
        self.kind = kind
        self.render_history = render_history
        self.out_rel = "reports/" + KINDS[kind][0]
        self.st = st
        self.board = st.get("board") or boardreg.split_dir(ws.name)[1]
        self.name = name
        self.pn, _ = boardreg.part_number(ws)
        self.stem = statelib.project_stem(ws, self.board)
        self.cur = phase_idx(str(st.get("phase", "P0")))
        self.sections: list[dict] = []
        self.missing: list[str] = []
        self.warnings: list[str] = []
        self.filed: str | None = None   # cc-docs' line when it filed
        self.unchanged = False   # a matching FILED_STAMP skipped the filing
        self.attached: list[str] = []   # fab files put on the revision
        self.head: list[str] = []   # title block (before \tableofcontents)
        self.body: list[str] = []

    @property
    def commits(self) -> list[dict]:
        """The workspace's git history, read once (only the full doc asks)."""
        if not hasattr(self, "_commits"):
            self._commits = dochistory.git_commits(self.ws, self.history_ref)
        return self._commits

    # -- bookkeeping ------------------------------------------------------
    def due(self, phase: str | None) -> bool:
        return phase is None or self.cur >= phase_idx(phase)

    def passed(self, phase: str) -> bool:
        return self.cur > phase_idx(phase)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def core(self, core: tuple[str, str, str], present: bool) -> bool:
        """Record a core artifact. Returns True if it is HARD-missing
        (due because its phase has passed, and absent)."""
        label, _section, phase = core
        if present or not self.passed(phase):
            if not present and self.due(phase):
                self.warn(f"{label} not present yet (phase {self.st.get('phase')} in progress)")
            return False
        self.missing.append(label)
        return True

    def gate_line(self, gate: str) -> str:
        g = (self.st.get("gates") or {}).get(gate)
        if not g:
            if self.due_gate(gate):
                self.warn(f"gate {gate} has no record in state.json")
            return (r"Gate \texttt{" + latex_escape(gate)
                    + r"}: \emph{not recorded}.")
        last = g.get("last") or {}
        return (r"Gate \texttt{" + latex_escape(gate) + "} (phase "
                + latex_escape(g.get("phase", "?")) + "): \\textbf{"
                + latex_escape(g.get("status", "?")) + "} after "
                + latex_escape(g.get("attempts", "?")) + " attempt(s); last "
                + latex_escape(last.get("ts", "?")) + ", failing "
                + latex_escape(last.get("failing_count", "?")) + " of "
                + latex_escape(last.get("total", "?")) + ".")

    def due_gate(self, gate: str) -> bool:
        for ph, g in statemod.GATE_ORDER:
            if g == gate:
                return self.passed(ph)
        return False

    # -- section plumbing -------------------------------------------------
    def start(self, heading: str) -> None:
        self.body.append("\\section{" + heading + "}")

    def record(self, sid: str, status: str, source: str) -> None:
        self.sections.append({"name": sid, "status": status, "source": source})

    def pending(self, sid: str, heading: str, phase: str) -> None:
        self.start(heading)
        self.body.append(r"\emph{Pending \textemdash{} produced at " + phase + ".}")
        self.record(sid, "pending", f"due at {phase}")

    # -- sections ---------------------------------------------------------
    def pn_label(self) -> str:
        """The part number line of the title block and every running head."""
        return (self.pn["pn"] if self.pn
                else "no part number (not in the boards register)")

    def sec_title(self) -> None:
        st = self.st
        gates = st.get("gates") or {}
        n_pass = sum(1 for g in gates.values() if g.get("status") == "pass")
        if not gates:
            overall = "no gates recorded yet"
        elif n_pass == len(gates):
            overall = f"all {len(gates)} recorded gates pass"
        else:
            bad = sorted(k for k, g in gates.items() if g.get("status") != "pass")
            overall = f"{n_pass}/{len(gates)} gates pass (not passing: {', '.join(bad)})"
        # House-style title block: eyebrow (part number, pipeline, date),
        # the title, and a one-line lead with the gate status.
        self.head.append(
            "\\hstitleblock{" + latex_escape(self.pn_label())
            + " \\textperiodcentered\\ hwde v1 pipeline \\textperiodcentered\\ generated "
            + latex_escape(time.strftime("%Y-%m-%d %H:%M")) + "}%\n{"
            + latex_escape(self.name) + r" \textemdash{} " + KINDS[self.kind][2] + "}%\n{"
            + latex_escape(f"Phase {st.get('phase', '?')}; {overall}.") + "}")
        if self.kind == "highlight":
            self.sec_glance()
            return
        self.start("Board and Run Metadata")
        rows = [
            ["board", latex_escape(self.board)],
            ["part number", latex_escape(self.pn["pn"] if self.pn else
                                         "none (not in the boards register)")],
            ["workspace", r"\texttt{" + latex_escape(st.get("workspace", "")) + "}"],
            ["phase", latex_escape(st.get("phase", "?"))],
            ["gate status", latex_escape(overall)],
            ["state created", latex_escape(st.get("created", "?"))],
            ["state updated", latex_escape(st.get("updated", "?"))],
            ["design spend", latex_escape(design_spend(self.ws))],
        ]
        self.body.append(longtable("lp{11cm}", [r"\textbf{Field}", r"\textbf{Value}"], rows))
        self.record("title", "included", "state.json")

    def sec_overview(self) -> None:
        used = []
        self.start("Overview")
        brief = read_text(self.ws, "brief/brief.md")
        if brief is not None:
            self.body.append(md_to_latex(brief))
            used.append("brief/brief.md")
        else:
            self.warn("brief/brief.md not found")
        order = read_json(self.ws, "fab/order.json") or {}
        snap = order.get("spec_snapshot")
        if isinstance(snap, dict):
            keys = ["layers", "width_mm", "height_mm", "qty", "surface_finish",
                    "solder_mask_color", "assembly", "castellated_pads"]
            rows = [[latex_escape(k), latex_escape(snap.get(k))] for k in keys
                    if k in snap]
            self.body.append(r"\subsection*{Fabrication spec snapshot}")
            self.body.append(longtable("ll", [r"\textbf{Key}", r"\textbf{Value}"], rows))
            used.append("fab/order.json")
        stack = read_text(self.ws, "architecture/stackup.md")
        if stack is not None:
            chosen = _chosen_stackup(stack)
            if chosen:
                self.body.append(r"\subsection*{Stackup}")
                self.body.append("Chosen stackup: " + _inline(chosen))
                used.append("architecture/stackup.md")
        if used:
            self.record("overview", "included", ", ".join(used))
        else:
            self.record("overview", "missing", "brief/brief.md")

    def sec_requirements(self) -> None:
        """Resolution ladder (T6 reportgen-fallback): the state.json artifacts
        registry entry, then the workspace root, then the one live stray
        location (lumina-carrier shipped a 'not found' stub while the file
        sat at architecture/requirements.md). Off-root hits stay a warning so
        the misplacement remains visible - check_requirements.py prevents new
        ones at P0 exit."""
        self.start("Requirements")
        reg = (self.st.get("artifacts") or {}).get("requirements")
        if isinstance(reg, dict):           # state v2 registry entry (T7)
            reg = reg.get("path")
        candidates = ([str(reg).replace("\\", "/")] if reg else []) + \
            ["requirements.md", "architecture/requirements.md"]
        req = rel = None
        for cand in candidates:
            req = read_text(self.ws, cand)
            if req is not None:
                rel = cand
                break
        if req is None:
            self.warn("requirements.md not found")
            self.body.append(r"\emph{requirements.md not found.}")
            self.record("requirements", "missing", "requirements.md")
            return
        if rel != "requirements.md":
            self.warn(f"requirements.md found at {rel}, not workspace root")
        self.body.append(md_to_latex(req))
        self.record("requirements", "included", rel)

    def sec_architecture(self) -> None:
        used = []
        self.start("Architecture")
        decisions = self.st.get("decisions") or []
        if decisions:
            self.body.append(r"\subsection*{Decisions of record (state.json)}")
            rows = [[latex_escape(d.get("phase", "?")),
                     latex_escape(d.get("what", "")),
                     latex_escape(d.get("why", ""))] for d in decisions]
            self.body.append(longtable(
                "lp{5.2cm}p{7.2cm}",
                [r"\textbf{Phase}", r"\textbf{What}", r"\textbf{Why}"], rows))
            used.append("state.json decisions")
        sheets = read_text(self.ws, "architecture/sheets.md")
        if sheets is not None:
            self.body.append(r"\subsection*{Sheet plan}")
            self.body.append(md_to_latex(sheets))
            used.append("architecture/sheets.md")
        else:
            self.warn("architecture/sheets.md not found")
        others = [f"architecture/{n}" for n in
                  ("blocks.md", "decisions.md", "power_tree.md", "stackup.md")
                  if (self.ws / "architecture" / n).is_file()]
        if others:
            self.body.append("Full architecture narrative (not inlined; see the "
                             "artifact index): "
                             + ", ".join(r"\texttt{" + latex_escape(o) + "}"
                                         for o in others) + ".")
        if used:
            self.record("architecture", "included", ", ".join(used))
        else:
            self.record("architecture", "missing", "architecture/sheets.md")

    def sec_schematic(self) -> None:
        used = []
        self.start("Schematic")
        self.body.append(self.gate_line("erc"))
        waivers = read_text(self.ws, "reports/erc-waivers.md")
        if waivers is not None:
            self.body.append(r"\subsection*{Review waivers}")
            self.body.append(md_to_latex(waivers))
            used.append("reports/erc-waivers.md")
        pdf_present = (self.ws / "reports" / "schematic.pdf").is_file()
        hard = self.core(CORE_SCHEMATIC, pdf_present)
        if pdf_present:
            self.body.append("The full schematic PDF follows.")
            self.body.append(r"\includepdf[pages=-]{reports/schematic.pdf}")
            used.append("reports/schematic.pdf")
        else:
            self.body.append(r"\emph{reports/schematic.pdf not available.}")
        self.record("schematic", "missing" if hard else "included",
                    ", ".join(used) or "reports/schematic.pdf")

    def sec_layout(self) -> None:
        used = []
        self.start("Layout")
        self.body.append(self.gate_line("place"))
        self.body.append("")
        self.body.append(self.gate_line("drc_routed"))
        main, extras = find_renders(self.ws, self.stem)
        if not (main or extras) and self.stem != self.board:
            main, extras = find_renders(self.ws, self.board)
        hard = self.core(CORE_RENDER, bool(main or extras))
        for rel in main:
            self.body.append(image_block(rel, "0.72"))
            used.append(rel)
        for rel in extras:
            width = "0.95" if rel.endswith("_layers.png") else "0.85"
            self.body.append(image_block(rel, width))
            used.append(rel)
        if not (main or extras):
            self.body.append(r"\emph{No board renders found.}")
        self.record("layout", "missing" if hard else "included",
                    ", ".join(used) or "state.json gates")

    def sec_verification(self) -> None:
        used = []
        self.start("Verification")
        self.body.append(self.gate_line("verify"))
        va, va_rel = read_stage(self.ws, "verify")
        if va and isinstance(va.get("checks"), dict):
            rows = []
            for cname, c in va["checks"].items():
                status = str(c.get("status", "?"))
                note = ""
                if status == "skipped" and c.get("reason"):
                    note = "skipped - " + str(c["reason"])
                elif c.get("reason"):
                    note = str(c["reason"])
                total = (c.get("counts") or {}).get("total")
                rows.append([r"\texttt{" + latex_escape(cname) + "}",
                             latex_escape(status),
                             latex_escape(total if total is not None else ""),
                             latex_escape(note)])
            self.body.append(r"\subsection*{Verification checks}")
            self.body.append(longtable(
                "llcp{6cm}",
                [r"\textbf{Check}", r"\textbf{Status}", r"\textbf{Findings}",
                 r"\textbf{Note}"], rows))
            used.append(va_rel)
        else:
            self.warn(f"{va_rel} not found or unparseable")
            self.body.append(r"\emph{reports/verify\_all.json not available.}")
        self.verify_waiver_block(used)
        review = read_text(self.ws, "reports/review-board.md")
        if review is not None:
            self.body.append(r"\subsection*{Design review of record}")
            self.body.append(md_to_latex(review))
            used.append("reports/review-board.md")
        else:
            self.warn("reports/review-board.md not found")
        self.record("verification", "included" if used else "missing",
                    ", ".join(used) or "reports/verify_all.json")

    def verify_waiver_block(self, used: list[str],
                            reason_cap: int | None = None) -> None:
        """The findings a person accepted instead of fixing, one
        item per entry of its verify-waivers.json (verify_waivers). Nothing
        at all for a board without one, or with an empty list; a file the
        gate would refuse is a warning and a line saying so."""
        waivers, rel, err = verify_waivers(self.ws)
        if rel is None or not (waivers or err):
            return
        used.append(rel)
        self.body.append(r"\subsection*{Accepted verify waivers}")
        if err:
            self.warn(f"{rel} could not be read: {err}")
            self.body.append(r"\emph{" + latex_escape(
                f"{rel} could not be read: {err}") + "}")
            return
        n = len(waivers)
        self.body.append(latex_escape(
            f"{n} verify finding{'s were' if n != 1 else ' was'} accepted rather "
            "than fixed. The verify gate still reports each one as waived but "
            f"does not fail on it ({rel}).") + "\n")
        self.body.append("\\begin{itemize}")
        self.body.extend(waiver_item(w, reason_cap) for w in waivers)
        self.body.append("\\end{itemize}")
        if reason_cap and any(len(" ".join(str(w.get("reason") or "").split()))
                              > reason_cap for w in waivers):
            self.body.append(latex_escape(
                "The design doc gives each reason in full.") + "\n")

    def castellation_line(self, bom: dict | None, order: dict | None) -> None:
        """Say 'castellated' only when the board has castellated pads: the
        count comes from the board (bom_cpl.json, else order.json's spec
        snapshot), and a brief or architecture note that claims castellation
        the board does not have is a warning."""
        n = (bom or {}).get("castellated_pads")
        refs = (bom or {}).get("castellated_refs") or []
        if n is None:
            n = ((order or {}).get("spec_snapshot") or {}).get("castellated_pads")
        if n is None:
            return
        if n:
            on = f" on {', '.join(map(str, refs))}" if refs else ""
            self.body.append(latex_escape(
                f"Castellated edges: {n} castellated pad(s){on}; the order "
                "sets Castellated Holes: Yes."))
            return
        self.body.append("Castellated edges: none (the board has no "
                         "castellated pads).")
        texts = [("brief/brief.md", read_text(self.ws, "brief/brief.md"))]
        arch = self.ws / "architecture"
        if arch.is_dir():
            texts += [(f"architecture/{f.name}",
                       f.read_text(encoding="utf-8", errors="replace"))
                      for f in sorted(arch.glob("*.md"))]
        for rel, text in texts:
            if text and any(_CASTELLATED_CLAIM.search(ln)
                            and not _CASTELLATED_DENIED.search(ln)
                            for ln in text.splitlines()):
                self.warn(f"{rel} says 'castellated' but the board has no "
                          "castellated pads - correct the text or add them "
                          "(castellated_fp.py)")

    def sec_dfm_fab(self) -> None:
        used = []
        self.start("DFM and Fabrication")
        self.body.append(self.gate_line("dfm"))
        gd, gd_rel = read_stage(self.ws, "gate_dfm")
        by_sev = ((gd or {}).get("counts") or {}).get("by_severity")
        if isinstance(by_sev, dict):
            line = ", ".join(f"{k}: {v}" for k, v in sorted(by_sev.items())) or "none"
            self.body.append("DFM findings by severity: " + latex_escape(line) + ".")
            used.append(gd_rel)
        fx, fx_rel = read_stage(self.ws, "fab_export")
        if fx and fx.get("layers_exported"):
            self.body.append(r"\subsection*{Fabrication outputs}")
            self.body.append(
                "Exported layers: "
                + latex_escape(", ".join(map(str, fx["layers_exported"]))) + ".")
            used.append(fx_rel)
        else:
            self.warn(f"{fx_rel} not found or has no layers_exported")

        bom, bom_rel = read_stage(self.ws, "bom_cpl")
        self.castellation_line(bom, read_json(self.ws, "fab/order.json"))
        rows_ok = bool(bom and isinstance(bom.get("bom_rows"), list)
                       and bom["bom_rows"])
        hard = self.core(CORE_BOM, rows_ok)
        if rows_ok:
            self.body.append(r"\subsection*{Bill of materials}")
            rows = [[latex_escape(r.get("Comment", "")),
                     latex_escape(r.get("Designator", "")),
                     latex_escape(r.get("Footprint", "")),
                     latex_escape(r.get("LCSC", ""))] for r in bom["bom_rows"]]
            self.body.append(longtable(
                "p{4.4cm}p{2.8cm}p{4.6cm}l",
                [r"\textbf{Comment}", r"\textbf{Designator}",
                 r"\textbf{Footprint}", r"\textbf{LCSC}"], rows))
            extras = []
            if bom.get("n_rotation_corrections") is not None:
                extras.append(f"{bom['n_rotation_corrections']} CPL rotation "
                              "correction(s) applied")
            ml = bom.get("missing_lcsc")
            if ml:
                extras.append("missing LCSC for: " + ", ".join(map(str, ml)))
            elif ml == []:
                extras.append("no missing LCSC numbers")
            if extras:
                self.body.append(latex_escape("; ".join(extras) + "."))
            used.append(bom_rel)

        order = read_json(self.ws, "fab/order.json")
        hard = self.core(CORE_ORDER, order is not None) or hard
        if order:
            q = (order.get("quote") or {}).get("selected") or {}
            if q:
                self.body.append(r"\subsection*{Quote}")
                line = (f"Estimated total \\${latex_escape(q.get('total', '?'))} "
                        f"for qty {latex_escape(q.get('qty', '?'))}"
                        f" (unit \\${latex_escape(q.get('unit_cost', '?'))}).")
                if (order.get("quote") or {}).get("estimated"):
                    line += (" This is an estimate from published pricing; the "
                             "JLCPCB cart quote page is authoritative.")
                self.body.append(line)
            steps = order.get("human_steps") or []
            if steps:
                self.body.append(r"\subsection*{Human steps before ordering}")
                self.body.append(r"\begin{enumerate}")
                for s in steps:
                    self.body.append(r"\item " + latex_escape(s))
                self.body.append(r"\end{enumerate}")
            used.append("fab/order.json")
        else:
            self.body.append(r"\emph{fab/order.json not available.}")
        self.record("dfm_fab", "missing" if hard else "included",
                    ", ".join(used) or "reports/bom_cpl.json")

    def sec_run_record(self) -> None:
        used = []
        self.start("Run Record (Appendix)")
        self.body.append(r"\subsection*{Phase digests}")
        top = min(self.cur, phase_idx("P10"))
        for pi in range(0, top + 1):
            phase = statemod.PHASES[pi]
            rel = f"log/{phase}-digest.md"
            txt = read_text(self.ws, rel)
            self.body.append(r"\paragraph*{" + phase + "}")
            if txt is None:
                self.body.append(r"\emph{(no digest recorded)}")
                if pi < self.cur:
                    self.warn(f"{rel} missing for passed phase {phase}")
            else:
                self.body.append(tt_block(txt))
                used.append(rel)

        self.body.append(r"\subsection*{Gate attempt history}")
        hist = []
        for gname, g in sorted((self.st.get("gates") or {}).items()):
            entries = g.get("history") or ([g["last"]] if g.get("last") else [])
            for h in entries:
                hist.append((str(h.get("ts", "")), gname, str(h.get("status", "?")),
                             f"{h.get('failing_count', '?')}/{h.get('total', '?')}"))
        hist.sort(key=lambda t: t[0])
        self.body.append(longtable(
            "llll", [r"\textbf{Timestamp}", r"\textbf{Gate}", r"\textbf{Status}",
                     r"\textbf{Failing/Total}"],
            [[latex_escape(c) for c in row] for row in hist]))

        issues = self.st.get("open_issues") or []
        self.body.append(r"\subsection*{Issues}")
        self.body.append(longtable(
            "llp{5cm}ll",
            [r"\textbf{Id}", r"\textbf{Phase}", r"\textbf{Kinds}",
             r"\textbf{Severity}", r"\textbf{Status}"],
            [[latex_escape(i.get("id")), latex_escape(i.get("phase")),
              latex_escape(", ".join(map(str, i.get("kinds") or []))),
              latex_escape(i.get("severity")), latex_escape(i.get("status"))]
             for i in issues]))

        self.body.append(r"\subsection*{Human checkpoints}")
        human = self.st.get("human") or {}
        rows = []
        for cid in sorted(statemod.CHECKPOINTS):
            h = human.get(cid) or {}
            rows.append([latex_escape(cid),
                         latex_escape(statemod.CHECKPOINTS[cid]),
                         latex_escape(h.get("status", "not reached")),
                         latex_escape(h.get("ts", "")),
                         latex_escape(h.get("note", ""))])
        self.body.append(longtable(
            "lllp{2.6cm}p{6.4cm}",
            [r"\textbf{Id}", r"\textbf{Phase}", r"\textbf{Status}",
             r"\textbf{When}", r"\textbf{Note}"], rows))
        if self.sec_cost():
            used.append("reports/cost.json")
        self.record("run_record", "included",
                    ", ".join(used + ["state.json"]))

    def sec_cost(self) -> bool:
        """Generation cost from reports/cost.json (gen_cost.py): the total, the
        per-step split, or the reason there is none. Absent -> a warning and a
        one-line note; never an estimate."""
        self.body.append(r"\subsection*{Generation cost}")
        cost = read_json(self.ws, "reports/cost.json")
        if not cost:
            self.warn("reports/cost.json missing - run gen_cost.py")
            self.body.append(r"\emph{Not recorded (no reports/cost.json).}")
            return False

        def usd(v) -> str:
            return latex_escape(f"${v:,.2f}") if isinstance(
                v, (int, float)) else "not recorded"

        total = cost.get("total_usd")
        self.body.append("Model cost of generating this board: \\textbf{"
                         + usd(total) + "}.\n")
        steps = cost.get("by_step") or []
        if steps:
            self.body.append(longtable(
                "p{10cm}r", [r"\textbf{Step}", r"\textbf{Cost}"],
                [[latex_escape(s.get("label") or s.get("step")),
                  usd(s.get("usd"))] for s in steps]))
        if cost.get("breakdown_reason"):
            self.body.append(r"\emph{No full split by step: "
                             + latex_escape(cost["breakdown_reason"]) + "}\n")
        for sh in cost.get("shared") or []:
            self.body.append(latex_escape(
                f"Not in the total: {sh.get('label')}, shared with "
                f"{sh.get('shared_with')}, cost ") + usd(sh.get("usd")) + ".\n")
        lines = list(cost.get("notes") or [])
        if isinstance(cost.get("loop_logged_usd"), (int, float)):
            line = f"The worker loop logged ${cost['loop_logged_usd']:,.2f}."
            unlogged = sum((r.get("loop") or {}).get("unlogged_iterations", 0)
                           for r in cost.get("rounds") or []
                           if not r.get("shared_with"))
            if unlogged:
                line += (f" {unlogged} of its iterations timed out, and a"
                         " timed-out iteration logs no cost; the session"
                         " transcripts hold the whole total.")
            lines.append(line)
        for n in lines:
            self.body.append(latex_escape(n) + "\n")
        return True

    def sec_artifact_index(self) -> None:
        self.start("Artifact Index")
        order = read_json(self.ws, "fab/order.json") or {}
        zip_sha = (((order.get("artifacts") or {}).get("gerber_zip") or {})
                   .get("sha256") or "")
        rows: list[list[str]] = []

        def add(rel: str, note: str = "") -> None:
            p = self.ws / rel
            if p.is_file():
                rows.append([r"\texttt{" + latex_escape(rel) + "}",
                             latex_escape(f"{p.stat().st_size:,} B"),
                             latex_escape(note)])

        add("state.json")
        add(f"kicad/{self.stem}.kicad_pcb")
        add(f"kicad/{self.stem}.kicad_sch")
        add(f"fab/{self.stem}_gerbers.zip",
            f"sha256 {zip_sha[:16]}..." if zip_sha else "")
        for rel in ("fab/order.json", "fab/BOM.csv", "fab/CPL.csv",
                    "brief/brief.md", "requirements.md"):
            add(rel)
        arch = self.ws / "architecture"
        if arch.is_dir():
            for p in sorted(arch.iterdir()):
                if p.is_file():
                    add(f"architecture/{p.name}")
        reports = self.ws / "reports"
        if reports.is_dir():
            for p in sorted(reports.iterdir()):
                if p.is_file():
                    add(f"reports/{p.name}")
        self.body.append(longtable(
            "p{9cm}rp{4cm}",
            [r"\textbf{File}", r"\textbf{Size}", r"\textbf{Note}"], rows))
        self.record("artifact_index", "included", "workspace scan")

    # -- highlight + full-doc sections ------------------------------------
    def board_size(self) -> str | None:
        """The outline as last edited (state.json edits), else board_init's."""
        for e in reversed(self.st.get("edits") or []):
            m = re.search(r"outline\s*->\s*([\d.]+)\s*x\s*([\d.]+)\s*mm",
                          str(e.get("note", "")))
            if m:
                return f"{float(m.group(1)):.1f} x {float(m.group(2)):.1f} mm"
        bb = (read_json(self.ws, "reports/board_init.json") or {}).get("outline_bbox")
        if isinstance(bb, list) and len(bb) == 4:
            return f"{bb[2] - bb[0]:.1f} x {bb[3] - bb[1]:.1f} mm"
        return None

    def sec_glance(self) -> None:
        """Highlight: what the board is (the brief's opening) and its facts."""
        self.start("At a Glance")
        used = ["state.json"]
        brief = read_text(self.ws, "brief/brief.md")
        if brief is not None:
            paras = [b for b in re.split(r"\n\s*\n", brief)
                     if b.strip() and not b.lstrip().startswith("#")]
            self.body.append(md_to_latex("\n\n".join(paras[:2])))
            used.append("brief/brief.md")
        init = read_json(self.ws, "reports/board_init.json") or {}
        bom = read_stage(self.ws, "bom_cpl")[0] or {}
        gates = self.st.get("gates") or {}
        n_pass = sum(1 for g in gates.values() if g.get("status") == "pass")
        rows = [["part number", self.pn_label()], ["board", self.board],
                ["phase", f"{self.st.get('phase', '?')} "
                          f"({dochistory.PHASE_NAMES.get(self.st.get('phase'), '')})"]]
        if self.board_size():
            rows.append(["size", self.board_size()])
        if init.get("layers"):
            rows.append(["layers", f"{init['layers']}, {init.get('copper_oz', '?')} oz"
                         f" copper ({init.get('stackup', '?')})"])
        if bom.get("n_parts"):
            rows.append(["parts", f"{bom['n_parts']} placed parts, "
                         f"{len(bom.get('bom_rows') or [])} BOM lines"])
        if gates:
            rows.append(["gates", f"{n_pass} of {len(gates)} recorded gates pass"])
        rows.append(["design spend", design_spend(self.ws)])
        self.body.append(longtable("lp{11cm}", [r"\textbf{Field}", r"\textbf{Value}"],
                                   [[latex_escape(a), latex_escape(b)] for a, b in rows]))
        self.record("title", "included", ", ".join(used))

    def renders(self) -> list[str]:
        main, extras = find_renders(self.ws, self.stem)
        if not (main or extras) and self.stem != self.board:
            main, extras = find_renders(self.ws, self.board)
        return main + extras

    def sec_hl_board(self) -> None:
        self.start("The Board")
        shots = [r for r in self.renders() if r.endswith(("_top.png", "_bottom.png",
                                                          "/top.png", "/bottom.png"))]
        if not shots:
            self.body.append(r"\emph{No board render yet.}")
            self.record("hl_board", "pending", "board renders")
            return
        self.body.append("\\begin{center}")
        for rel in shots[:2]:
            self.body.append(f"\\includegraphics[width=0.48\\textwidth]{{{rel}}}\\hfill")
        self.body.append("\\\\{\\small top and bottom, as rendered from the board file}"
                         "\n\\end{center}")
        self.record("hl_board", "included", ", ".join(shots[:2]))

    def sec_hl_parts(self) -> None:
        """Highlight: the BOM lines and the schematic's first page."""
        self.start("What Is On It")
        used = []
        bom, bom_rel = read_stage(self.ws, "bom_cpl")
        rows = (bom or {}).get("bom_rows") or []
        if rows:
            self.body.append(longtable(
                "p{3.6cm}p{5.4cm}p{3cm}l",
                [r"\textbf{Refs}", r"\textbf{Part}", r"\textbf{Footprint}",
                 r"\textbf{LCSC}"],
                [[latex_escape(r.get("Designator", "")), latex_escape(r.get("Comment", "")),
                  latex_escape(r.get("Footprint", "")), latex_escape(r.get("LCSC", ""))]
                 for r in rows]))
            used.append(bom_rel)
        if (self.ws / "reports" / "schematic.pdf").is_file():
            self.body.append(r"\includepdf[pages=1]{reports/schematic.pdf}")
            used.append("reports/schematic.pdf")
        if not used:
            self.body.append(r"\emph{No BOM or schematic yet.}")
        self.record("hl_parts", "included" if used else "pending",
                    ", ".join(used) or "reports/bom_cpl.json")

    def decision_rows(self, ds: list[dict], why_cap: int | None = None) -> list[list[str]]:
        rows = []
        for d in ds:
            why = str(d.get("why", ""))
            if why_cap and len(why) > why_cap:
                why = why[:why_cap - 3].rstrip() + "..."
            rows.append([latex_escape(d.get("phase", "")),
                         latex_escape(str(d.get("ts", ""))[5:16].replace("T", " ")),
                         latex_escape(d.get("what", "")), latex_escape(why)])
        return rows

    def sec_hl_decisions(self) -> None:
        """Highlight: deviations, part picks and the decisions of each
        backtrack, oldest first, at most ten."""
        self.start("Key Decisions")
        picked = {id(d) for d in dochistory.component_decisions(self.st)}
        for bt in dochistory.backtracks(self.st):
            picked.update(id(d) for d in bt["decisions"])
        ds = [d for d in dochistory.decisions(self.st) if id(d) in picked
              or re.search(r"\b(deviation|accept)", str(d.get("what", "")), re.I)]
        if len(ds) > 10:
            self.body.append(latex_escape(f"The ten that changed the board most, of "
                                          f"{len(ds)}; the full design doc has every one."))
            ds = ds[:10]
        self.body.append(longtable(
            "lp{1.6cm}p{6.2cm}p{6.2cm}",
            [r"\textbf{Ph}", r"\textbf{When}", r"\textbf{Decision}", r"\textbf{Why}"],
            self.decision_rows(ds, why_cap=260)))
        self.record("hl_decisions", "included" if ds else "missing", "state.json decisions")

    def sec_hl_run(self) -> None:
        self.start("How the Run Went")
        spans = dochistory.phase_spans(self.st)
        bts = dochistory.backtracks(self.st)
        n = sum(s["count"] for s in spans)
        if spans:
            self.body.append(latex_escape(
                f"{n} recorded decisions from {spans[0]['phase']} to {spans[-1]['phase']}"
                f", {str(spans[0]['first'])[:16]} to "
                f"{max(s['last'] for s in spans)[:16]}. The run went back to an earlier"
                f" phase {len(bts)} time{'s' if len(bts) != 1 else ''}."))
        if bts:
            self.body.append("\\begin{itemize}")
            for bt in bts:
                self.body.append("\\item " + latex_escape(
                    f"{bt['from']} back to {bt['to']} ({str(bt['ts'])[11:16]}): "
                    f"{dochistory.clean(bt['decisions'][0].get('what', ''))}"))
            self.body.append("\\end{itemize}")
        self.record("hl_run", "included" if spans else "missing", "state.json decisions")

    def sec_hl_checks(self) -> None:
        self.start("Checks and Cost")
        rows = []
        for _ph, gname in statemod.GATE_ORDER:
            g = (self.st.get("gates") or {}).get(gname)
            if g:
                rows.append([latex_escape(gname), latex_escape(g.get("status", "?")),
                             latex_escape(g.get("attempts", "?")),
                             latex_escape((g.get("last") or {}).get("failing_count", "?"))])
        self.body.append(longtable("llrr", [r"\textbf{Gate}", r"\textbf{Status}",
                                            r"\textbf{Attempts}", r"\textbf{Failing}"], rows))
        used = ["state.json"]
        self.verify_waiver_block(used, HL_REASON_CAP)
        cheap = (read_json(self.ws, "fab/quote.json") or {}).get("cheapest") or {}
        if isinstance(cheap.get("total"), (int, float)):
            self.body.append(latex_escape(
                f"Estimated JLCPCB cost for {cheap.get('qty')} assembled boards: "
                f"${cheap['total']:,.2f} (${cheap.get('unit_cost', 0):,.2f} each). "
                "It is an estimate from the price table, not a quote.") + "\n")
            used.append("fab/quote.json")
        total = (read_json(self.ws, "reports/cost.json") or {}).get("total_usd")
        if isinstance(total, (int, float)):
            self.body.append(latex_escape(
                f"Model cost of generating this board: ${total:,.2f}.") + "\n")
            used.append("reports/cost.json")
        self.body.append(latex_escape(
            f"The full design doc ({self.name}-design-full) has every decision, the "
            "renders and the run's history.") + "\n")
        self.record("hl_checks", "included", ", ".join(used))

    def sec_renders(self) -> None:
        """Full doc: one top render per routing snapshot the run left (routing/pre-*, post-*; pre_*, post_* too), in the order it made them."""
        self.start("Routing Renders")
        used = []
        self.body.append(latex_escape(
            "The finished board's renders are under Layout; this section shows how "
            "it got there."))
        snaps = dochistory.snapshots(self.ws, self.commits)
        rdir = self.ws / self.out_rel / "renders"
        if snaps and self.render_history:
            self.render_snapshots(snaps, rdir)
        shots = [(s, f"{self.out_rel}/renders/{Path(s).stem}_top.png") for s in snaps]
        shots = [(s, png) for s, png in shots if (self.ws / png).is_file()]
        if snaps:
            self.body.append(r"\subsection*{Snapshots the run kept}")
            self.body.append(latex_escape(
                f"The run saved {len(snaps)} board snapshots before (pre-) or after "
                "(post-) a step that changed the board; they are shown in the order "
                "the run made them."))
            if not shots:
                self.body.append(latex_escape(
                    " None is rendered yet: report_gen.py --kind full --render-history "
                    "renders them."))
                self.warn("routing snapshots not rendered - pass --render-history")
            for s, png in shots:
                self.body.append(r"\paragraph*{" + latex_escape(Path(s).stem) + "}")
                self.body.append(image_block(png, "0.62"))
                used.append(png)
        if not snaps:
            self.body.append(latex_escape(" The run kept no routing snapshots."))
            if self.passed("P7"):
                self.warn("no routing snapshots found (routing/pre-*.kicad_pcb, "
                          "post-*.kicad_pcb) for a routed board - the full doc has "
                          "no routing renders")
        self.record("renders", "included" if used else "missing",
                    ", ".join(used[:4]) or "routing/ snapshots")

    def render_snapshots(self, snaps: list[str], rdir: Path) -> None:
        """Render each snapshot's top view through render.py, skipping one
        whose PNG is newer than its board file."""
        rdir.mkdir(parents=True, exist_ok=True)
        for s in snaps:
            pcb = self.ws / s
            png = rdir / f"{pcb.stem}_top.png"
            if png.is_file() and png.stat().st_mtime >= pcb.stat().st_mtime:
                continue
            try:
                cp = subprocess.run(
                    [sys.executable, str(SCRIPTS / "render.py"), str(pcb), "--views",
                     "top", "--w", "1400", "--height", "900", "--quality", "basic",
                     "--out-dir", str(rdir)],
                    capture_output=True, text=True, timeout=RENDER_TIMEOUT)
                ok = cp.returncode == 0 and png.is_file()
            except (OSError, subprocess.TimeoutExpired):
                ok = False
            if not ok:
                self.warn(f"render of {s} failed")

    def layer_views(self) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        """([(layer, pdf)], [(view, png)]) from reports/layers/layers.json,
        workspace-relative and only those on disk; --render-layers runs
        layer_views.py first when that file is missing, older than the board
        or not a pass."""
        pcb = self.ws / "kicad" / f"{self.stem}.kicad_pcb"
        ldir = self.ws / LAYERS_REL
        index = ldir / "layers.json"
        # Reuse layers.json only when it is newer than the board AND passed:
        # a failed run (exit 2 without layers, or a 3D view that failed) is
        # drawn again rather than kept as fresh.
        fresh = (index.is_file() and pcb.is_file()
                 and index.stat().st_mtime >= pcb.stat().st_mtime
                 and (read_json(self.ws, f"{LAYERS_REL}/layers.json") or {}).get("status") == "pass")
        if self.render_layers and pcb.is_file() and not fresh:
            ldir.mkdir(parents=True, exist_ok=True)
            try:
                cp = subprocess.run(
                    [sys.executable, str(SCRIPTS / "layer_views.py"), str(pcb),
                     "--out-dir", str(ldir), "--out", str(index)],
                    capture_output=True, text=True, timeout=LAYERS_TIMEOUT)
                if cp.returncode != 0:
                    self.warn(f"layer_views.py exited {cp.returncode}: "
                              + (cp.stderr or "").strip()[-200:])
            except (OSError, subprocess.TimeoutExpired) as exc:
                self.warn(f"layer_views.py failed: {exc}")
        rep = read_json(self.ws, f"{LAYERS_REL}/layers.json") or {}
        for w in rep.get("warnings", []):
            self.warn(w)

        def here(items: list[dict], key: str) -> list[tuple[str, str]]:
            rels = [(i[key], f"{LAYERS_REL}/{Path(i['path']).name}") for i in items]
            return [(k, rel) for k, rel in rels if (self.ws / rel).is_file()]
        return here(rep.get("layers", []), "layer"), here(rep.get("views", []), "view")

    def no_layers(self, sid: str) -> None:
        self.body.append(latex_escape(
            "The layer views are not drawn yet: report_gen.py --render-layers draws them."))
        self.warn("layer views not drawn - pass --render-layers")
        self.record(sid, "missing", f"{LAYERS_REL}/layers.json")

    def layer_page(self, layer: str, rel: str) -> None:
        """One copper layer on a page of its own, as large as the page allows."""
        self.body.append(r"\clearpage")
        self.body.append(r"\subsection*{" + latex_escape(layer) + "}")
        self.body.append("\\begin{center}\n\\includegraphics[width=\\textwidth,"
                         "height=0.82\\textheight,keepaspectratio]{" + rel + "}\\\\\n"
                         "{\\small\\texttt{" + latex_escape(rel) + "}}\n\\end{center}")

    def sec_layers(self) -> None:
        """Full doc: every copper layer with its net names, then the 3D views."""
        self.start("Board Layers")
        layers, views = self.layer_views()
        if not layers:
            self.no_layers("layers")
            return
        self.body.append(latex_escape(
            f"Each of the board's {len(layers)} copper layers as KiCad's editor shows "
            "it, seen from the top: the copper with its side's silkscreen, fabrication "
            "and courtyard outlines, and each pad, track and zone labelled with its "
            "net. The pages are vector drawings, so zooming in keeps the names sharp."))
        for layer, rel in layers:
            self.layer_page(layer, rel)
        if views:
            self.body.append(r"\clearpage")
            self.body.append(r"\subsection*{3D views}")
            for view, rel in views:
                self.body.append(r"\paragraph*{" + latex_escape(view) + "}")
                self.body.append(image_block(rel, "0.8"))
        self.record("layers", "included",
                    ", ".join(rel for _, rel in layers + views))

    def sec_hl_layers(self) -> None:
        """Highlight: the outer copper layers with their net names, and the iso view."""
        self.start("Copper Layers")
        layers, views = self.layer_views()
        if not layers:
            self.no_layers("hl_layers")
            return
        outer = [layers[0]] + ([layers[-1]] if len(layers) > 1 else [])
        self.body.append(latex_escape(
            "The outer copper layers as KiCad shows them, with every pad, track and "
            "zone labelled with its net (zoom in to read the small ones)"
            + (f"; the full design doc has all {len(layers)}." if len(layers) > 2 else ".")))
        for layer, rel in outer:
            self.layer_page(layer, rel)
        iso = [rel for view, rel in views if view == "iso"]
        if iso:
            self.body.append(r"\subsection*{3D view}")
            self.body.append(image_block(iso[0], "0.8"))
        self.record("hl_layers", "included",
                    ", ".join([rel for _, rel in outer] + iso[:1]))

    def sec_decisions(self) -> None:
        self.start("Design Decisions")
        ds = dochistory.decisions(self.st)
        self.body.append(latex_escape(
            f"Every decision the run recorded in state.json ({len(ds)}), oldest "
            "first, with the reason given at the time."))
        self.body.append(longtable(
            "lp{1.6cm}p{6.2cm}p{6.2cm}",
            [r"\textbf{Ph}", r"\textbf{When}", r"\textbf{Decision}", r"\textbf{Why}"],
            self.decision_rows(ds)))
        self.record("decisions", "included" if ds else "missing", "state.json decisions")

    def sec_history(self) -> None:
        """Full doc: phase timeline, backtracks, part changes, git activity,
        then the run's own notes (COMPARISON, placement and route notes)."""
        self.start("Design History")
        used = ["state.json"]
        spans = dochistory.phase_spans(self.st)
        self.body.append(r"\subsection*{Phase timeline}")
        self.body.append(longtable(
            "llllr", [r"\textbf{Phase}", r"\textbf{Name}", r"\textbf{First decision}",
                      r"\textbf{Last decision}", r"\textbf{Decisions}"],
            [[latex_escape(s["phase"]),
              latex_escape(dochistory.PHASE_NAMES.get(s["phase"], "")),
              latex_escape(str(s["first"])[:16]), latex_escape(str(s["last"])[:16]),
              latex_escape(s["count"])] for s in spans]))
        human = self.st.get("human") or {}
        hrows = [[latex_escape(cid), latex_escape(h.get("status", "")),
                  latex_escape(str(h.get("ts", ""))[:16]), latex_escape(h.get("note", ""))]
                 for cid, h in sorted(human.items()) if isinstance(h, dict)]
        if hrows:
            self.body.append(r"\subsection*{Human checkpoints}")
            self.body.append(longtable("lllp{9cm}", [
                r"\textbf{H}", r"\textbf{Status}", r"\textbf{When}", r"\textbf{Note}"], hrows))
        self.body.append(r"\subsection*{Where the run went back}")
        bts = dochistory.backtracks(self.st)
        if not bts:
            self.body.append(latex_escape("The run never returned to an earlier phase."))
        for bt in bts:
            self.body.append(r"\paragraph*{" + latex_escape(
                f"{bt['from']} back to {bt['to']}, {str(bt['ts'])[:16]}") + "}")
            self.body.append("\\begin{itemize}")
            for d in bt["decisions"]:
                self.body.append("\\item " + latex_escape(d.get("what", ""))
                                 + " \\emph{Why:} " + latex_escape(d.get("why", "")))
            self.body.append("\\end{itemize}")
        self.body.append(r"\subsection*{Component choices and changes}")
        comp = dochistory.component_decisions(self.st)
        self.body.append(longtable(
            "lp{1.6cm}p{6.2cm}p{6.2cm}",
            [r"\textbf{Ph}", r"\textbf{When}", r"\textbf{Decision}", r"\textbf{Why}"],
            self.decision_rows(comp)))
        pch = dochistory.parts_changes(self.ws, self.commits)
        if pch:
            self.body.append(latex_escape(
                "What kicad/parts.json actually changed between commits:"))
            rows = []
            for c in pch:
                for ref, part in c["added"]:
                    rows.append([c["ts"][:16], ref, "added", part])
                for ref, part in c["removed"]:
                    rows.append([c["ts"][:16], ref, "removed", part])
                for ref, old, new in c["changed"]:
                    rows.append([c["ts"][:16], ref, "changed", f"{old} -> {new}"])
            self.body.append(longtable(
                "lllp{8.4cm}", [r"\textbf{When}", r"\textbf{Ref}", r"\textbf{Change}",
                                r"\textbf{Part (MPN LCSC)}"],
                [[latex_escape(x) for x in r] for r in rows]))
            used.append("git: kicad/parts.json")
        acts = dochistory.activity(self.commits)
        self.body.append(r"\subsection*{Git history}")
        if acts:
            self.body.append(latex_escape(
                f"{len(self.commits)} commits touched the workspace, "
                f"{self.commits[0]['ts'][:16]} to {self.commits[-1]['ts'][:16]}. "
                "By hour, with the files each area had changed:"))
            self.body.append(longtable("lrp{10cm}", [
                r"\textbf{Hour}", r"\textbf{Commits}", r"\textbf{Areas (files)}"],
                [[latex_escape(a["hour"].replace("T", " ") + ":00"),
                  latex_escape(a["commits"]),
                  latex_escape(", ".join(f"{k} {v}" for k, v in sorted(
                      a["areas"].items(), key=lambda kv: -kv[1])))] for a in acts]))
            used.append("git log")
        else:
            self.body.append(latex_escape("No git history found for the workspace."))
        for rel, head in (("COMPARISON.md", "Comparison"),
                          ("reports/placement-notes.md", "Placement notes"),
                          ("routing/route-notes.md", "Routing notes")):
            txt = read_text(self.ws, rel)
            if txt is not None:
                self.body.append(r"\subsection*{" + head + "}")
                self.body.append(md_to_latex(txt))
                used.append(rel)
        self.record("history", "included", ", ".join(used))

    def sec_flow(self) -> None:
        """Full doc: the run as a diagram-maker flowchart, rendered to PDF."""
        self.start("How the Design Process Went")
        bts = dochistory.backtracks(self.st)
        spec = dochistory.flow_spec(self.st, bts, f"How the {self.board} run went")
        out = self.ws / self.out_rel
        out.mkdir(parents=True, exist_ok=True)
        spec_path = out / "flow.json"
        spec_path.write_text(json.dumps(spec, indent=1), encoding="utf-8")
        why = render_diagram(spec_path)
        self.body.append(latex_escape(
            "Each phase the run passed through, top to bottom; a red box is a point "
            "where it went back to an earlier phase, with the first decision that "
            "sent it there. The current phase has the red outline."))
        if why is None:
            self.body.append(image_block(f"{self.out_rel}/flow.pdf", "0.8"))
            self.record("flow", "included", f"{self.out_rel}/flow.json")
        else:
            self.warn(f"flow diagram not rendered: {why}")
            self.body.append(r"\emph{" + latex_escape(f"Figure not rendered: {why}") + "}")
            self.record("flow", "missing", f"{self.out_rel}/flow.json")

    # -- assembly ---------------------------------------------------------
    BUILDERS = {
        "title": sec_title, "overview": sec_overview,
        "requirements": sec_requirements, "architecture": sec_architecture,
        "schematic": sec_schematic, "layout": sec_layout,
        "verification": sec_verification, "dfm_fab": sec_dfm_fab,
        "run_record": sec_run_record, "artifact_index": sec_artifact_index,
        "hl_board": sec_hl_board, "hl_parts": sec_hl_parts,
        "hl_decisions": sec_hl_decisions,
        "hl_run": sec_hl_run, "hl_checks": sec_hl_checks,
        "hl_layers": sec_hl_layers, "layers": sec_layers,
        "renders": sec_renders, "decisions": sec_decisions,
        "history": sec_history, "flow": sec_flow,
    }

    def build(self) -> str:
        for sid, heading, phase in KINDS[self.kind][4]:
            if not self.due(phase):
                self.pending(sid, heading, phase)
                continue
            self.BUILDERS[sid](self)
        preamble = "\n".join([
            "% Generated by report_gen.py (hwde v1) - do not hand-edit.",
            # pdf-material-builder's house style (housestyle.sty, found on
            # TEXINPUTS by compile_pdf; it loads its own vendored fonts).
            r"\documentclass[11pt]{article}",
            r"\newcommand\hspaper{a4paper}",
            r"\usepackage[nodiagramkit]{housestyle}",
            r"\usepackage{longtable}",
            r"\usepackage{pdfpages}",
            # Compat shim (host-verified): pdfpages >= 2026 v0.6h passes an
            # `artifact` key to \includegraphics on MULTI-page insertions;
            # graphics stacks <= 2024 lack that key and die with "keyval
            # Error: artifact undefined". Define it as a no-op iff absent.
            r"\makeatletter",
            r"\@ifundefined{KV@Gin@artifact}{\define@key{Gin}{artifact}[]{}}{}",
            r"\makeatother",
            r"\setcounter{tocdepth}{1}",
            # Running head: part number and board at the left, the current
            # section at the right; the foot keeps the page number.
            # House fonts set ASCII dashes as typed, so no "--" here.
            r"\hsslug{" + latex_escape(self.pn_label()) + r" \textperiodcentered\ "
            + latex_escape(self.board) + "}",
            r"\renewcommand{\sectionmark}[1]{\markboth{#1}{}}",
            r"\hssection{\leftmark}",
            r"\begin{document}",
            r"\sloppy",
        ])
        tex = (preamble + "\n" + "\n".join(self.head) + "\n"
               + ("" if self.kind == "highlight" else r"\tableofcontents")
               + "\n\n"
               + "\n".join(self.body)
               + "\n\\end{document}\n")
        bad = sorted({c for c in tex if ord(c) >= 128})
        if bad:
            raise ReportError(
                "internal: generated .tex is not pure ASCII: "
                + ", ".join(f"U+{ord(c):04X}" for c in bad))
        return tex


# ---------------------------------------------------------------- compile

def diagram_maker() -> tuple[Path | None, str]:
    """The diagram-maker skill dir and its fonts arg: HWDE_DIAGRAM_MAKER, else
    ~/.claude/skills/diagram-maker; fonts from pdf-material-builder beside it,
    else "-" (the machine's fonts)."""
    root = Path(os.environ.get("HWDE_DIAGRAM_MAKER")
                or Path.home() / ".claude" / "skills" / "diagram-maker")
    if not (root / "scripts" / "export.sh").is_file():
        return None, "-"
    fonts = root.parent / "pdf-material-builder" / "assets" / "fonts"
    return root, str(fonts) if fonts.is_dir() else "-"


def render_diagram(spec: Path) -> str | None:
    """spec.json -> spec.pdf through diagram-maker's export.sh (never
    hand-written TikZ). None on success, else the reason it did not render."""
    root, fonts = diagram_maker()
    if root is None:
        return "diagram-maker skill not found (set HWDE_DIAGRAM_MAKER)"
    if shutil.which("node") is None:
        return "node is not on PATH"
    try:
        cp = subprocess.run(["bash", str(root / "scripts" / "export.sh"), str(spec),
                             fonts], capture_output=True, text=True,
                            timeout=DIAGRAM_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"export.sh {type(exc).__name__}"
    if cp.returncode != 0 or not spec.with_suffix(".pdf").is_file():
        return (f"export.sh rc={cp.returncode}: "
                + ((cp.stderr or cp.stdout or "").strip().splitlines() or [""])[-1][:160])
    return None


def find_lualatex() -> Path | None:
    """lualatex for the house style (optional toolchain member).

    Ladder: HWDE_LUALATEX pin > PATH. None = not installed (degraded to
    --tex-only, exit 1); a pin naming no file raises EnvError (exit 2).
    """
    pin, var = env.skill_env("LUALATEX")
    if pin:
        p = Path(pin)
        if not p.exists():
            raise env.EnvError(f"{var} does not exist: {pin}")
        return p
    w = shutil.which("lualatex")
    return Path(w) if w else None


def house_style_dir() -> Path | None:
    """pdf-material-builder's references/house-style/ (housestyle.sty), used
    by path and never copied: HWDE_HOUSE_STYLE, else the skill under
    ~/.claude/skills. None when housestyle.sty is not there."""
    d = Path(os.environ.get("HWDE_HOUSE_STYLE")
             or Path.home() / ".claude" / "skills" / "pdf-material-builder"
             / "references" / "house-style")
    return d if (d / "housestyle.sty").is_file() else None


def compile_pdf(engine: Path, ws: Path, name: str, subdir: str = "design_doc",
                style: Path | None = None) -> tuple[dict, Path | None]:
    """Two lualatex passes (housestyle.sty put on TEXINPUTS from `style`) staged in a system temp dir; only the final PDF is
    moved into the workspace (no .aux/.log/.toc residue - boards/ is
    git-tracked and gate commits sweep the whole tree). Never raises on
    compile failure/timeout: returns a compile dict with latex_log_tail."""
    tex_rel = f"reports/{subdir}/{name}.tex"
    comp: dict = {"engine": str(engine).replace("\\", "/"), "rc": None,
                  "passes": 0, "seconds": 0.0}
    run_env = dict(os.environ)
    if style is not None:
        # Trailing separator keeps the default search path after it.
        run_env["TEXINPUTS"] = (str(style) + os.pathsep
                                + os.environ.get("TEXINPUTS", ""))
    # Guard the two files the engine can drop in cwd on exotic failures.
    guards = {n: (ws / n).exists() for n in ("missfont.log", "texput.log")}
    t0 = time.time()
    final_pdf: Path | None = None
    with tempfile.TemporaryDirectory(prefix="aiee_report_") as td:
        staging = Path(td)
        argv = [str(engine), "-interaction=nonstopmode", "-halt-on-error",
                "-output-directory", str(staging), tex_rel]
        rc = None
        tail_src = ""
        for _ in range(2):
            try:
                cp = subprocess.run(argv, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace",
                                    timeout=PDF_TIMEOUT, cwd=str(ws),
                                    env=run_env)
                rc = cp.returncode
                tail_src = cp.stdout or ""
            except subprocess.TimeoutExpired as exc:
                rc = 124
                comp["timed_out"] = True
                out = exc.stdout
                tail_src = (out.decode("utf-8", "replace")
                            if isinstance(out, bytes) else (out or ""))
                comp["passes"] += 1
                break
            comp["passes"] += 1
            if rc != 0:
                break
        comp["rc"] = rc
        comp["seconds"] = round(time.time() - t0, 1)
        staged = staging / f"{name}.pdf"
        ok = rc == 0 and staged.is_file() and staged.stat().st_size > 0
        if ok:
            final_pdf = ws / "reports" / subdir / f"{name}.pdf"
            final_pdf.unlink(missing_ok=True)
            shutil.move(str(staged), str(final_pdf))
        else:
            log = staging / f"{name}.log"
            if log.is_file():
                tail_src = log.read_text(encoding="utf-8", errors="replace")
            comp["latex_log_tail"] = tail_src[-2000:]
    for n, pre in guards.items():
        if not pre and (ws / n).exists():
            (ws / n).unlink()
    return comp, final_pdf


def count_pages(pdf: Path) -> int | None:
    try:
        from pypdf import PdfReader
        return len(PdfReader(str(pdf)).pages)
    except Exception:
        return None


# ---------------------------------------------------------------- driver

def resolve_workspace(arg: str) -> Path:
    p = Path(arg)
    # Relative: from the cwd, then the boards root (a bare <name>, or the
    # old repo-relative boards/<name> spelling), then hwde's own root.
    rel = Path(*p.parts[1:]) if p.parts[:1] == ("boards",) else p
    candidates = [p] if p.is_absolute() else [
        Path.cwd() / p, env.boards_root() / rel, env.repo_root() / p]
    # A board's old name, its part number or its <PN>_<name> directory all
    # find it through the register (boardreg.locate).
    candidates.append(boardreg.locate(
        p if p.is_absolute() else env.boards_root() / rel, env.boards_root()))
    for c in candidates:
        if (c / "state.json").is_file():
            return c.resolve()
    raise ReportError(f"workspace has no state.json: {arg}")


def load_state(ws: Path) -> dict:
    try:
        d = json.loads((ws / "state.json").read_text(encoding="utf-8",
                                                     errors="replace"))
    except (OSError, json.JSONDecodeError) as e:
        raise ReportError(f"state.json unreadable: {type(e).__name__}: {e}")
    if not isinstance(d, dict) or "board" not in d or "phase" not in d:
        raise ReportError("state.json lacks the board/phase schema fields")
    return d


BOARDS_PROJECT = "Boards"
# cc-docs' default library; CC_DOCS_ROOT overrides it, as it does for cc-docs.
DOCS_HOME = Path.home() / ".cc" / "documents"


def board_project(ws: Path | None) -> str:
    """The library project a new board document files into: DOC_PROJECT when
    set, else "Boards" (002), where every board's documents are. Not a
    per-board "Boards/<PN> <name>": cc-docs refuses a Group/Name while a
    project outside any group is named like the group, and 002 is."""
    return os.environ.get("DOC_PROJECT", "").strip() or BOARDS_PROJECT


def docs_register() -> dict:
    """cc-docs' register ($CC_DOCS_ROOT/register.json, else DOCS_HOME's),
    read only; {} when it is missing or unreadable."""
    root = os.environ.get("CC_DOCS_ROOT", "").strip()
    path = (Path(root).expanduser() if root else DOCS_HOME) / "register.json"
    try:
        reg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return reg if isinstance(reg, dict) else {}


def filed_document(ws: Path | None, kind: str = "design",
                   pn: str | None = None) -> dict | None:
    """This board's document of `kind` already in the register, as
    {number, project, title}, else None. A document is the board's when its
    source sits in reports/<kind's subdir>/ of a directory that is the
    workspace's own name, or (with the part number `pn`) is <pn> or starts
    "<pn>_", or its last revision describes `pn`; one whose last revision
    describes another board is never it. Of several, the last filed wins.
    cc-docs keys a document on the source path, else the title in the
    project, and a rebuild from another worktree has another path, so the
    filing reuses this one's project and exact title (owner's register:
    "PCB-0023-A gan-rf-inverter highlights" is 002-0045)."""
    if ws is None:
        return None
    docs = docs_register().get("documents")
    if not isinstance(docs, dict):
        return None
    subdir, name = KINDS[kind][0], Path(ws).resolve().name
    best, best_key = None, None
    for number, d in docs.items():
        if not (isinstance(d, dict) and d.get("title") and d.get("project")):
            continue
        parts = [p for p in re.split(r"[\\/]+", str(d.get("source") or "")) if p]
        at = [i for i in range(1, len(parts) - 1)
              if parts[i] == "reports" and parts[i + 1] == subdir]
        if not at:
            continue
        revs = d.get("revisions") or []
        revs = list(revs.values()) if isinstance(revs, dict) else list(revs)
        last = revs[-1] if revs and isinstance(revs[-1], dict) else {}
        about = last.get("describes")
        if pn and about and about != pn:
            continue
        home = parts[at[-1] - 1]
        if not (home == name or (pn and (home == pn or home.startswith(pn + "_")
                                         or about == pn))):
            continue
        key = (str(last.get("filed_at") or last.get("date") or ""), str(number))
        if best_key is None or key > best_key:
            best = {"number": str(d.get("number") or number),
                    "project": str(d["project"]), "title": str(d["title"])}
            best_key = key
    return best


def doc_target(ws: Path | None, board: str,
               kind: str = "design") -> tuple[str, str]:
    """(project, title) a filing of this board's `kind` document uses: the
    filed one's (filed_document), so a rebuild is its next revision and
    never a new number; else board_project(ws) and "<PN> <board> <kind>"
    (no PN: "<board> <kind>"), the title leading with the part number so a
    second revision of a board never matches the first one's documents."""
    pn = boardreg.part_number(ws)[0] if ws is not None else None
    found = filed_document(ws, kind, pn["pn"] if pn else None)
    if found:
        return found["project"], found["title"]
    title = f"{board} {KINDS[kind][3]}"
    if pn and not board.startswith(pn["pn"]):
        title = f"{pn['pn']} {title}"
    return board_project(ws), title


def cc_docs_args(ws: Path | None, board: str, pdf: Path,
                 project: str | None = None, kind: str = "design",
                 title: str | None = None, note: str | None = None) -> list[str]:
    """The `cc-docs file` arguments for this board's document of `kind`
    into `project` with `title` (both default to doc_target's): the
    revision's --note when given, the part number it describes when the
    register has one, and one --cost per step of reports/cost.json that
    carries a number (neither of those two without a ws)."""
    if project is None or title is None:
        p, t = doc_target(ws, board, kind)
        project, title = project or p, title or t
    pn = boardreg.part_number(ws)[0] if ws is not None else None
    args = ["file", str(pdf), "--project", project, "--title", title,
            "--source", str(pdf)]
    if note:
        args += ["--note", note]
    if ws is None:
        return args
    if pn:
        args += ["--describes", pn["pn"]]
    for s in (read_json(ws, "reports/cost.json") or {}).get("by_step") or []:
        usd = s.get("usd")
        if s.get("step") and isinstance(usd, (int, float)):
            args += ["--cost", f"{s['step']}={usd:.2f}"]
    return args


# (file under fab/, the name it is attached under); "{ws}" is the workspace
# directory's name, so every attached name starts with it.
FAB_ATTACH = (
    ("{ws}_gerbers.zip", "{ws}_gerbers.zip"),
    ("BOM.csv", "{ws}_BOM.csv"),
    ("CPL.csv", "{ws}_CPL.csv"),
    ("{ws}_BOM_digikey.csv", "{ws}_BOM_digikey.csv"),
    ("{ws}_BOM_mouser.csv", "{ws}_BOM_mouser.csv"),
    ("{ws}_BOM_cost.json", "{ws}_BOM_cost.json"),
)


def fab_attachments(ws: Path | None) -> list[tuple[Path, str]]:
    """The board's fab files that exist, each with the name it is attached
    under (FAB_ATTACH); empty without a ws or a fab set."""
    if ws is None:
        return []
    name = Path(ws).resolve().name
    out = []
    for src, dst in FAB_ATTACH:
        p = Path(ws) / "fab" / src.format(ws=name)
        if p.is_file():
            out.append((p, dst.format(ws=name)))
    return out


def attach_fab(exe: str, number: str, fab: list[tuple[Path, str]],
               builder) -> bool:
    """Put `fab` on revision `number` with `cc-docs attach --replace`, each
    file copied under its attach name. True when cc-docs took them all."""
    with tempfile.TemporaryDirectory(prefix="hwde-fab-") as td:
        files = []
        for src, name in fab:
            shutil.copyfile(src, Path(td) / name)
            files.append(str(Path(td) / name))
        try:
            cp = subprocess.run([exe, "attach", number, "--replace", *files],
                                capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.TimeoutExpired) as exc:
            builder.warn(f"fab files not attached: {type(exc).__name__}: {exc}")
            return False
    if cp.returncode != 0:
        builder.warn("fab files not attached (rc=%d): %s"
                     % (cp.returncode, (cp.stderr or "").strip()[:200]))
        return False
    builder.attached = [name for _, name in fab]
    return True


FILED_STAMP = ".filed.json"
# the title block's build time (sec_title: minutes, no seconds), so two
# builds a minute apart hash the same (#1255)
_GENERATED_RE = re.compile(r"generated \d{4}-\d\d-\d\d \d\d:\d\d(?::\d\d)?")
_GRAPHICS_RE = re.compile(r"\\include(?:graphics|pdf)(?:\[[^\]]*\])?\{([^}]*)\}")


def content_hash(tex_text: str, ws: Path) -> str:
    """Hash what the document says: the .tex without its build time, plus
    the bytes of every image it includes (paths are relative to ws)."""
    h = hashlib.sha256(_GENERATED_RE.sub("generated", tex_text).encode("utf-8"))
    for rel in sorted(set(_GRAPHICS_RE.findall(tex_text))):
        img = ws / rel
        h.update(rel.encode("utf-8"))
        if img.is_file():
            h.update(hashlib.sha256(img.read_bytes()).digest())
    return h.hexdigest()


def file_in_register(pdf: Path, board: str, builder, requested: bool = False,
                     digest: str | None = None, ws: Path | None = None,
                     kind: str = "design", note: str | None = None) -> None:
    """File the finished design doc with cc-docs, as doc_target(ws) names it.

    Only when asked (requested, or DOC_PROJECT in the environment) and cc-docs
    is on PATH; a failed filing warns and never fails the
    report. cc-docs stamps the number itself. Its output is captured so
    stdout stays the JSON payload. With a digest, a matching FILED_STAMP next
    to the PDF (same digest, project and CC_DOCS_ROOT library) skips the
    filing and sets builder.unchanged, and a successful filing writes it.
    The board's fab set (fab_attachments) then goes on the revision cc-docs
    named, and its hashes into the stamp, so a changed fab set alone files
    again; a failed attach leaves the stamp unwritten so the next run retries.
    `note` goes to cc-docs as the revision's --note; an unchanged rebuild
    files nothing, so its note is dropped with a warning.
    """
    project = os.environ.get("DOC_PROJECT", "").strip()
    if not (requested or project):
        return
    project, title = doc_target(ws, board, kind)
    stamp = pdf.parent / FILED_STAMP
    want = {"digest": digest, "project": project,
            "library": os.environ.get("CC_DOCS_ROOT", "")}
    fab = fab_attachments(ws)
    if fab:
        want["attach"] = {name: hashlib.sha256(p.read_bytes()).hexdigest()
                          for p, name in fab}
    if digest is not None:
        try:
            if json.loads(stamp.read_text(encoding="utf-8")) == want:
                builder.unchanged = True
                if note:
                    builder.warn("--note not filed: the document is unchanged "
                                 "since it was last filed")
                return
        except (OSError, ValueError):
            pass
    exe = shutil.which("cc-docs")
    if exe is None:
        builder.warn("cc-docs is not on PATH - design doc not filed")
        return
    try:
        cp = subprocess.run(
            [exe, *cc_docs_args(ws, board, pdf, project, kind, title, note)],
            capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        builder.warn(f"cc-docs filing failed: {type(exc).__name__}: {exc}")
        return
    if cp.returncode != 0:
        builder.warn("cc-docs filing failed (rc=%d): %s"
                     % (cp.returncode, (cp.stderr or "").strip()[:200]))
        return
    builder.filed = ((cp.stdout or "").strip().splitlines() or [""])[0]
    number = (builder.filed.split() or [""])[0]
    if fab and not (number and attach_fab(exe, number, fab, builder)):
        if not number:
            builder.warn("fab files not attached: cc-docs printed no number")
        return
    if digest is not None:
        stamp.write_text(json.dumps(want), encoding="utf-8")


def run(workspace: str, name: str | None = None, tex_only: bool = False,
        file_doc: bool = False, kind: str = "design",
        render_history: bool = False,
        history_ref: str = "HEAD", render_layers: bool = False,
        note: str | None = None) -> tuple[dict, int]:
    ws = resolve_workspace(workspace)
    st = load_state(ws)
    subdir, suffix = KINDS[kind][:2]
    doc_name = f"{name or st['board']}-{suffix}"

    builder = DocBuilder(ws, st, name or st["board"], kind, render_history,
                         history_ref, render_layers)
    tex_text = builder.build()

    out_dir = ws / "reports" / subdir
    out_dir.mkdir(parents=True, exist_ok=True)
    tex_path = out_dir / f"{doc_name}.tex"
    tex_path.write_text(tex_text, encoding="utf-8")

    engine: Path | None = None
    style = house_style_dir()
    if not tex_only:
        # Brief: "keep the degraded exit-1 path when lualatex is absent" -
        # no engine or no style leaves engine None, so pdf_path stays None.
        # Resolved after the tex write: a bad HWDE_LUALATEX pin still exits 2
        # (EnvError propagates) but must not discard the built document.
        engine = find_lualatex()
        if engine is None:
            builder.warn("lualatex not installed - degraded to --tex-only "
                         "(no PDF produced)")
        elif style is None:
            engine = None
            builder.warn("pdf-material-builder house style not found (set "
                         "HWDE_HOUSE_STYLE) - degraded to --tex-only "
                         "(no PDF produced)")

    comp = None
    pdf_path: Path | None = None
    pages = None
    if engine is not None:
        comp, pdf_path = compile_pdf(engine, ws, doc_name, subdir, style)
        if pdf_path is None:
            if comp.get("timed_out"):
                builder.warn(f"lualatex timed out after {PDF_TIMEOUT}s - "
                             "PDF not produced")
            else:
                builder.warn(f"lualatex failed (rc={comp.get('rc')}) - PDF "
                             "not produced; see compile.latex_log_tail")
        else:
            pages = count_pages(pdf_path)
            if pages is None:
                builder.warn("pypdf could not read the produced PDF")
            file_in_register(pdf_path, name or st["board"], builder, file_doc,
                             content_hash(tex_text, ws), ws, kind, note)

    degraded = (not tex_only) and pdf_path is None
    violations = bool(builder.missing) or degraded
    payload = {
        "script": "report_gen",
        "status": "violations" if violations else "pass",
        "board": name or st["board"],
        "workspace": str(ws).replace("\\", "/"),
        "tex": f"reports/{subdir}/{doc_name}.tex",
        "pdf": f"reports/{subdir}/{doc_name}.pdf" if pdf_path else None,
        "pages": pages,
        "sections": builder.sections,
        "missing": builder.missing,
        "warnings": builder.warnings,
        "compile": comp,
        "filed": builder.filed,
        "unchanged": builder.unchanged,
        "attached": builder.attached,
        "kind": kind,
    }
    return payload, (1 if violations else 0)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workspace", required=True,
                    help="board workspace dir containing state.json "
                         "(absolute or repo-relative)")
    ap.add_argument("--out", help="write the JSON payload here instead of stdout")
    ap.add_argument("--tex-only", action="store_true",
                    help="write the .tex only; skip the lualatex compile")
    ap.add_argument("--file", action="store_true", dest="file_doc",
                    help="file the finished PDF in the document register "
                         "(also: DOC_PROJECT set); default files nothing")
    ap.add_argument("--note",
                    help="with --file: the filed revision's note, passed to "
                         "`cc-docs file --note`")
    ap.add_argument("--name", help="override the board name from state.json")
    ap.add_argument("--kind", choices=sorted(KINDS), default="design",
                    help="design (default), highlight (short) or full (design "
                         "doc + renders, every decision, history, flow figure)")
    ap.add_argument("--render-history", action="store_true",
                    help="--kind full: render the routing/ snapshots first")
    ap.add_argument("--render-layers", action="store_true",
                    help="--kind full or highlight: draw the copper layers and 3D "
                         "views first (layer_views.py)")
    ap.add_argument("--history-ref", default="HEAD",
                    help="--kind full: the git ref whose log is the run's history "
                         "(the board's track branch once its PR squash-merged)")
    args = ap.parse_args(argv)
    if args.note and not (args.file_doc
                          or os.environ.get("DOC_PROJECT", "").strip()):
        ap.error("--note is the filed revision's note: it needs --file")

    try:
        payload, code = run(args.workspace, name=args.name,
                            tex_only=args.tex_only, file_doc=args.file_doc,
                            kind=args.kind, render_history=args.render_history,
                            history_ref=args.history_ref,
                            render_layers=args.render_layers, note=args.note)
    except Exception as exc:  # noqa: BLE001 (SPEC: any error -> exit 2)
        err = {"script": "report_gen", "status": "error",
               "error": f"{type(exc).__name__}: {exc}"}
        text = json.dumps(err, indent=1)
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
        else:
            print(text)
        return 2

    text = json.dumps(payload, indent=1)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        print(text)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
