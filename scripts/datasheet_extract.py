#!/usr/bin/env python
"""datasheet_extract.py - datasheet PDF -> schema-validated part JSON (SPEC.md P3, 6.1).

The actual field extraction is LLM-assisted (a datasheet-extractor sub-agent
reads the PDF pages); this script owns the deterministic half:

  --pdf FILE        pull machine-readable text per page (pypdf) and emit a
                    grounding payload {text_by_page, schema, template} for the
                    agent to fill. Refuses non-PDF bytes (exit 2): the
                    www.lcsc.com datasheet URLs serve an HTML viewer shell and
                    an HTML "pinout" must never reach the extractor. Pages with
                    no schema-relevant keywords are emitted as stubs
                    {page, first_line, chars} to keep the grounding payload
                    proportional to schema needs, not datasheet length
                    (--full-text restores them). Image-only datasheets yield
                    little/no text - the agent then reads the rendered pages
                    directly.
  --fetch WS        download each WS/parts/parts.json line's datasheet to
                    WS/parts/datasheets/<lcsc>.pdf (or only --lcsc's) and
                    index them in WS/parts/datasheets/index.json. Every hop
                    must be on the research allowlist; LCSC's HTML redirect
                    stubs (wmsc.lcsc.com answers one) are followed, and
                    anything that isn't a PDF in the end is refused, never
                    written. index.json records pages, text chars per page
                    and status ok|suspect (+ reasons): suspect = the file is
                    cut off, has no text layer, or nothing ties it to the MPN
                    (#706, #755: see assess_pdf); a suspect PDF is kept but
                    check_ratings and the ledger never count what was read
                    from it verified.
                    exit 0 = every datasheet fetched, 1 = some suspect,
                    refused or missing, 2 = bad workspace.
  --assess WS       re-read every index.json entry's PDF on disk (or only
                    --lcsc's) and write its status, with no download. An entry
                    with no status and no PDF becomes status unassessed. Not
                    needed in the ordinary way: check_ratings and the ledger
                    judge the PDFs on disk whenever they read the index, and
                    judge again only if the PDF, the MPN or ASSESS_VERSION
                    changed (#768); this forces it. exit 0 = all ok,
                    1 = some suspect or unassessed, 2 = no index.
  --render DIR      with --pdf: render the image-only pages (under
                    IMAGE_ONLY_CHARS of text) and the mechanical-drawing
                    pages to DIR/page-NNN.png with pdftoppm, OCR the
                    image-only ones with tesseract when it is on PATH (their
                    text then fills text_by_page, marked "ocr"), and list
                    them under "rendered" for the agent to read - a
                    connector drawing's pin and post sizes and its PCB-edge
                    line go into the extraction's `mechanical` block.
  --validate FILE   validate a candidate extraction against the schema.
                    exit 0 = valid, 1 = schema violations, 2 = unreadable.
  --schema          print the JSON Schema (single source of truth).
  --app-note FILE   U4 variant: same PDF handling, but the grounding payload
                    carries the KNOWLEDGE RECORD schema + template
                    (lib/knowledgelib.py) and layout-rule keywords - the
                    extractor fills reference/knowledge/records/*.yaml (one
                    record per class-level rule, sources citing this PDF by
                    page), stores the PDF under reference/knowledge/sources/,
                    then lints with `knowledge.py --validate`.
  --screen WS       U9 P3 layout screen (lib/layoutimpl.py): per part in
                    WS/parts/parts.json, the layout implications computed from
                    its extraction - thermal-via capacity of the exposed pad,
                    wire-entry direction + the rotation per board edge,
                    courtyard area vs the board budget (--board-mm, else the
                    state's stated size), pad pitch vs the routing floor - and
                    the CONFLICTS with architecture/constraints.json
                    (thermal min_vias over capacity, an edge rot that points
                    a wire entry inward, courtyards over budget). Writes
                    WS/parts/layout_implications.json; exit 1 on a conflict.
  --implications FILE [FILE ...]
                    the same fields for candidate extractions (part-sourcer),
                    ranked by layoutimpl.score against --needs JSON
                    {min_thermal_vias, edge, max_courtyard_mm2,
                    min_stub_width_mm}; exit 0.

The extracted JSON is the ground truth the schematic agents wire against
(SPEC P4: "never wire from model memory of a pinout") and the land pattern
fp_verify.py checks footprints against.

Contract (SPEC section 6): argparse, JSON to stdout or --out, exit 0/1/2.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

_POS = {"type": "number", "exclusiveMinimum": 0}
_SRC = {"type": "string", "description": "page and figure it was read from"}
# A pin or post cross-section: round (dia_mm) or rectangular (w_mm x t_mm),
# its MAX size (with tolerance) as the drawing gives it.
MECHANICAL_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "pins": {
            "type": "array",
            "description": "THT leads: which pads they go through and their "
                           "cross-section",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "pads": {"type": "array", "items": {"type": "string"},
                             "description": "pad numbers (default: every "
                                            "numbered plated hole)"},
                    "dia_mm": _POS, "w_mm": _POS, "t_mm": _POS,
                    "hole_mm": {**_POS, "description": "the drawing's "
                                "recommended finished hole"},
                    "source": _SRC,
                },
            },
        },
        "entry": {
            "type": "object",
            "description": "a horizontal connector's mating end, read by "
                           "check_mating for its mouth direction (#952)",
            "additionalProperties": False,
            "required": ["contacts_at"],
            "properties": {
                "contacts_at": {
                    "enum": ["back", "mouth"],
                    "description": "which end of the housing the contact "
                                   "tails (the signal pads) are at, from "
                                   "the drawing: back (most connectors) or "
                                   "mouth (Molex 104031 inner-lead microSD, "
                                   "many push-push card sockets)"},
                "source": _SRC,
            },
        },
        "posts": {
            "type": "array",
            "description": "locating posts / bosses / snap pegs that need "
                           "their own (usually unplated) holes",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["count"],
                "properties": {
                    "count": {"type": "integer", "minimum": 1},
                    "dia_mm": _POS, "w_mm": _POS, "t_mm": _POS,
                    "hole_mm": _POS,
                    "source": _SRC,
                },
            },
        },
        "edge": {
            "type": "object",
            "additionalProperties": False,
            "required": ["overhang_mm"],
            "description": "where the drawing puts the PCB edge",
            "properties": {
                "overhang_mm": {"type": "number", "description":
                                "how far the body's front stands past the "
                                "PCB edge line (negative: set back behind "
                                "it)"},
                "tol_mm": {"type": "number", "minimum": 0},
                "source": _SRC,
            },
        },
        "source": _SRC,
    },
}

# JSON Schema (Draft 2020-12) for a datasheet-extract JSON document.
PIN_TYPES = ["power_in", "power_out", "ground", "input", "output",
             "bidirectional", "passive", "analog", "clock", "nc",
             "open_collector", "tristate", "unspecified"]

DATASHEET_SCHEMA: dict = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "hwde datasheet extraction",
    "type": "object",
    "required": ["mpn", "pinout"],
    "additionalProperties": False,
    "properties": {
        "mpn": {"type": "string", "minLength": 1},
        "lcsc": {"type": "string"},
        "package": {"type": "string"},
        "description": {"type": "string"},
        # check_ratings holds [lo, hi] (C) against the board's ambient when
        # parts.json gives no Operating Temperature; the source cites the
        # datasheet page it was read from (rating_fill writes both)
        "operating_temp_c": {"type": "array", "items": {"type": "number"},
                             "minItems": 2, "maxItems": 2},
        "operating_temp_source": {"type": "string"},
        # a fixed regulator's output, in volts: check_ratings judges its rail
        # against it. Leave it out for an adjustable part.
        "output_voltage": {"type": "number", "exclusiveMinimum": 0},
        "pinout": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["pin", "name", "type"],
                "additionalProperties": False,
                "properties": {
                    "pin": {"type": "string", "minLength": 1},
                    "name": {"type": "string", "minLength": 1},
                    "type": {"type": "string", "enum": PIN_TYPES},
                    "notes": {"type": "string"},
                    # a pin the part drives itself that isn't typed output
                    # (a reference or charge-pump node): check_ratings
                    # covers it by the part's supply rating (#835)
                    "self_driven": {"type": "boolean"},
                },
            },
        },
        "decoupling": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["pins", "value"],
                "additionalProperties": False,
                "properties": {
                    "pins": {"type": "array", "items": {"type": "string"},
                             "minItems": 1},
                    "value": {"type": "string", "minLength": 1},
                    "placement": {"type": "string"},
                    "notes": {"type": "string"},
                },
            },
        },
        "land_pattern": {
            "type": "object",
            "additionalProperties": False,
            "required": ["pad_count"],
            "properties": {
                "package": {"type": "string"},
                "pad_count": {"type": "integer", "minimum": 1},
                "pitch_mm": {"type": "number", "exclusiveMinimum": 0},
                "pad_size_mm": {
                    "type": "array", "items": {"type": "number", "minimum": 0},
                    "minItems": 2, "maxItems": 2,
                },
                "row_spacing_mm": {"type": "number", "minimum": 0},
                "drill_mm": {"type": "number", "exclusiveMinimum": 0},
                "annulus_mm": {"type": "number", "exclusiveMinimum": 0},
                "courtyard_excess_mm": {"type": "number", "minimum": 0},
                # U9 layout-implication inputs
                "courtyard_mm": {
                    "type": "array", "items": {"type": "number",
                                               "exclusiveMinimum": 0},
                    "minItems": 2, "maxItems": 2,
                },
                "wire_entry_local": {"type": "string",
                                     "enum": ["+X", "-X", "+Y", "-Y"],
                                     "description": "connector wire/cable "
                                     "entry direction in footprint-local "
                                     "coords (KiCad y down)"},
                "pin1": {"type": "string",
                         # a pad or ball name, never prose: fp_verify looks
                         # for a pad of this name (#1127, an MGM240P record's
                         # "top-left in land pattern view ...")
                         "pattern": "^[A-Za-z0-9][A-Za-z0-9_.+-]{0,7}$",
                         "description": "pad or ball name that is pin 1 "
                         "(default '1'; 'A1' on a ball grid, even when "
                         "that ball is depopulated). A name, not a "
                         "description: put where pin 1 is in notes"},
                "notes": {"type": "string"},
            },
        },
        "exposed_pad": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "present": {"type": "boolean"},
                "connect_to": {"type": "string"},
                "thermal_vias": {"type": "integer", "minimum": 0},
                # U9: the recommended EP LAND (w x h) - via capacity needs it
                "size_mm": {
                    "type": "array", "items": {"type": "number",
                                               "exclusiveMinimum": 0},
                    "minItems": 2, "maxItems": 2,
                },
            },
            "required": ["present"],
        },
        # check_connector_fit: the part's mechanical drawing, read off the
        # vendor's dimensioned drawing / PCB layout (the land pattern above
        # is the pads; this is what goes THROUGH them and past the edge).
        "mechanical": MECHANICAL_SCHEMA,
        "layout_notes": {"type": "array", "items": {"type": "string"}},
        "abs_max": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["param", "value"],
                "additionalProperties": False,
                "properties": {
                    "param": {"type": "string"},
                    "value": {"type": ["number", "string"]},
                    "unit": {"type": "string"},
                },
            },
        },
        # check_ratings: per-pin ratings, read before the free-text abs_max
        # rows (which it parses heuristically). min/max: volts or amps, or
        # "<PIN>+0.3" relative to that pin's net; ref: rated against that
        # pin's net instead of ground (VGS: pins [G], ref S).
        "pin_ratings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["pins"],
                "additionalProperties": False,
                "properties": {
                    "pins": {"type": "array", "items": {"type": "string"},
                             "minItems": 1},
                    # power: watts at the datasheet's stated ambient
                    # (rating_fill writes it for a resistor; not judged yet)
                    "kind": {"type": "string",
                             "enum": ["voltage", "current", "power"]},
                    "level": {"type": "string",
                              "enum": ["abs_max", "recommended"]},
                    "min": {"type": ["number", "string"]},
                    "max": {"type": ["number", "string"]},
                    "ref": {"type": "string"},
                    "source": {"type": "string"},
                },
            },
        },
        # netlist_audit pin_part_missing: a part the datasheet requires
        # between two of the IC's pins (its external-components table:
        # CBK from FB_BK to GND_BK). Copy the rows into
        # reference/pin_requirements.yaml, whose header gives the fields.
        "pin_requirements": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "pin", "to", "kinds", "source"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string", "minLength": 1},
                    "pin": {"type": "string", "minLength": 1},
                    "to": {"type": "string", "minLength": 1},
                    "kinds": {"type": "array", "minItems": 1, "items": {
                        "type": "string", "enum": ["C", "L", "R"]}},
                    "value": {"type": "string"},
                    "min": {"type": "number"},
                    "max": {"type": "number"},
                    "when": {"type": "string", "enum": ["connected"]},
                    "source": {"type": "string", "minLength": 1},
                },
            },
        },
        "source_pdf": {"type": "string"},
        # U9: computed by --screen/--implications, never hand-filled
        "layout_implications": {"type": "object"},
    },
}


def blank_template() -> dict:
    """A minimal, schema-shaped skeleton for the extractor agent to fill."""
    return {
        "mpn": "",
        "lcsc": "",
        "package": "",
        "pinout": [{"pin": "1", "name": "", "type": "unspecified"}],
        "decoupling": [],
        "pin_requirements": [],
        "land_pattern": {"pad_count": 0},
        "layout_notes": [],
        "abs_max": [],
    }


class MissingModule(RuntimeError):
    """A locked dependency is absent from this venv (exit 2, named remedy)."""


def extract_pdf_text(pdf_path: Path) -> list[dict]:
    """Per-page text via pypdf. Never raises on a page - records the error instead."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise MissingModule(
            f"python module '{exc.name or 'pypdf'}' is not installed in "
            f"{sys.executable}; run check_env.py (it compares the venv with "
            f"requirements.lock) and install with: {sys.executable} -m pip "
            "install -r requirements.lock") from exc
    reader = PdfReader(str(pdf_path))
    pages = []
    for i, page in enumerate(reader.pages):
        try:
            txt = page.extract_text() or ""
        except Exception as exc:  # noqa: BLE001 - malformed pages shouldn't abort
            txt = ""
            pages.append({"page": i, "text": "", "error": f"{type(exc).__name__}: {exc}"})
            continue
        pages.append({"page": i, "text": txt})
    return pages


# Pages whose text carries none of these are stubbed out of the grounding
# payload: extractor context must scale with SCHEMA needs, not datasheet
# length (measured: 85-97 KB groundings dominated by electrical-characteristics
# and application pages the schema never reads).
GROUNDING_KEYWORDS = ("pin", "function", "land pattern", "recommended",
                      "absolute maximum", "layout", "decoupl", "bypass",
                      "thermal pad", "exposed pad", "package outline", "solder")

# --app-note: pages carrying none of these are stubbed - app notes are
# layout-rule sources, so the payload scales with RULE content, not page count.
APP_NOTE_KEYWORDS = ("layout", "loop", "ground", "plane", "via", "place",
                     "rout", "emi", "noise", "thermal", "copper", "trace",
                     "inductor", "capacitor", "switch", "snubber", "ring",
                     "feedback", "shield", "current path")


def _trim_pages(pages: list[dict],
                keywords: tuple = GROUNDING_KEYWORDS) -> tuple[list[dict], int]:
    """Full text only for schema-relevant pages; stubs for the rest."""
    out, trimmed = [], 0
    for p in pages:
        text = p.get("text", "")
        low = text.lower()
        if p.get("error") or any(k in low for k in keywords):
            out.append(p)
            continue
        first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        out.append({"page": p["page"], "first_line": first[:120],
                    "chars": len(text)})
        trimmed += 1
    return out, trimmed


def _checked_pdf(path: str) -> tuple[Path | None, dict | None]:
    """(path, None) for a real PDF; (None, error payload) otherwise."""
    pdf = Path(path)
    if not pdf.exists():
        return None, {"script": "datasheet_extract", "status": "error",
                      "error": f"PDF not found: {pdf}"}
    with pdf.open("rb") as fh:
        head = fh.read(16)
    if not head.startswith(b"%PDF"):
        # An HTML shell would otherwise be text-extracted into a "pinout" -
        # and the extraction is the ONLY pinout source P4 may wire from.
        return None, {"script": "datasheet_extract", "status": "error",
                      "error": (f"{pdf} is not a PDF (starts {head!r}); "
                                "www.lcsc.com/datasheet URLs serve an HTML "
                                "viewer shell and wmsc.lcsc.com a redirect "
                                "stub - download it with "
                                "`datasheet_extract.py --fetch <ws>`, which "
                                "follows the stub")}
    why = stub_notice(pdf.read_bytes())
    if why:
        return None, {"script": "datasheet_extract", "status": "error",
                      "error": f"{pdf} is a host placeholder, not a "
                               f"datasheet ({why}); no facts can be read "
                               "from it - delete it and find another source"}
    return pdf, None


# A page with less text than this is a scan or a drawing: its numbers are in
# the image, so it is rendered (and OCR'd when tesseract is on PATH).
IMAGE_ONLY_CHARS = 200
# Pages that hold the mechanical drawing (mechanical: pins, posts, edge) are
# rendered too: a dimension read off a drawing needs the picture, not the
# text layer's loose numbers.
DRAWING_KEYWORDS = ("land pattern", "recommended pcb", "pcb layout",
                    "pcb edge", "board edge", "panel", "hole", "dimension",
                    "mounting", "footprint", "package outline", "drawing")


def render_pages(pdf: Path, pages: list[dict], out_dir: Path,
                 dpi: int = 200) -> dict:
    """Render the image-only and drawing pages of `pdf` to PNGs in `out_dir`
    with poppler's pdftoppm, OCR the image-only ones with tesseract when it
    is installed, and put that text into their `pages` entry (`ocr`: true).
    Neither tool is a hard dependency: without pdftoppm nothing is rendered
    and `renderer` is null; the agent then reads the PDF pages itself."""
    import shutil
    import subprocess
    renderer, ocr = shutil.which("pdftoppm"), shutil.which("tesseract")
    out: dict = {"renderer": renderer, "ocr": ocr, "dir": str(out_dir),
                 "rendered": []}
    want = []
    for p in pages:
        text = p.get("text", "")
        if len(text.strip()) < IMAGE_ONLY_CHARS:
            want.append((p, "image_only"))
        elif any(k in text.lower() for k in DRAWING_KEYWORDS):
            want.append((p, "drawing"))
    out["pages"] = [{"page": p["page"], "why": why} for p, why in want]
    if renderer is None or not want:
        return out
    out_dir.mkdir(parents=True, exist_ok=True)
    for p, why in want:
        n = p["page"]
        stem = out_dir / f"page-{n + 1:03d}"
        r = subprocess.run([renderer, "-f", str(n + 1), "-l", str(n + 1),
                            "-r", str(dpi), "-png", "-singlefile", str(pdf),
                            str(stem)], capture_output=True, text=True,
                           timeout=120)
        png = stem.with_suffix(".png")
        row = {"page": n, "why": why, "png": str(png)}
        if r.returncode or not png.is_file():
            row = {"page": n, "why": why,
                   "error": (r.stderr or "pdftoppm wrote nothing").strip()}
        elif why == "image_only" and ocr:
            t = subprocess.run([ocr, str(png), "-", "--psm", "3"],
                               capture_output=True, text=True, timeout=300)
            if t.returncode == 0 and t.stdout.strip():
                p["text"], p["ocr"] = t.stdout, True
                row["ocr_chars"] = len(t.stdout)
        out["rendered"].append(row)
    return out


def do_pdf(args) -> tuple[dict, int]:
    pdf, err = _checked_pdf(args.pdf)
    if err:
        return err, 2
    pages = extract_pdf_text(pdf)
    total_chars = sum(len(p.get("text", "")) for p in pages)
    rendered = render_pages(pdf, pages, Path(args.render)) \
        if args.render else None
    trimmed = 0
    if not args.full_text:
        pages, trimmed = _trim_pages(pages)
    payload = {
        "script": "datasheet_extract",
        "status": "extracted",
        "source_pdf": str(pdf),
        "lcsc": args.lcsc or "",
        "n_pages": len(pages),
        "text_chars": total_chars,
        "pages_trimmed": trimmed,
        "text_by_page": pages,
        "schema": DATASHEET_SCHEMA,
        "template": blank_template(),
        "note": ("Fill the template from text_by_page and the PDF pages, then "
                 "re-run with --validate. Pages without schema-relevant "
                 "keywords are stubs {page, first_line, chars} - read those "
                 "PDF pages directly if needed, or re-run with --full-text. "
                 "If text_chars is ~0 the PDF is image-only; read the "
                 "rendered pages directly (--render DIR writes them as PNGs, "
                 "OCR'd when tesseract is installed). Read the mechanical "
                 "drawing into `mechanical` (pin and post cross-sections, "
                 "where the PCB edge falls) for check_connector_fit, and "
                 "for a side-entry connector which end the contact tails "
                 "are at (mechanical.entry.contacts_at) for check_mating."),
    }
    if rendered is not None:
        payload["rendered"] = rendered
    return payload, 0


def do_app_note(args) -> tuple[dict, int]:
    """--app-note: grounding payload shaped for KNOWLEDGE RECORDS (U4)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
    import knowledgelib
    pdf, err = _checked_pdf(args.app_note)
    if err:
        return err, 2
    pages = extract_pdf_text(pdf)
    total_chars = sum(len(p.get("text", "")) for p in pages)
    trimmed = 0
    if not args.full_text:
        pages, trimmed = _trim_pages(pages, APP_NOTE_KEYWORDS)
    payload = {
        "script": "datasheet_extract",
        "status": "extracted",
        "mode": "app_note",
        "source_pdf": str(pdf),
        "n_pages": len(pages),
        "text_chars": total_chars,
        "pages_trimmed": trimmed,
        "text_by_page": pages,
        "record_schema": knowledgelib.RECORD_SCHEMA,
        "record_template": knowledgelib.blank_record(),
        "classes": sorted(knowledgelib.CLASSES),
        "note": ("Write ONE reference/knowledge/records/<id>.yaml per "
                 "class-level layout rule found (record_template shape; "
                 "classes from the controlled list; page numbers here are "
                 "0-based - cite sources 1-based). Store the PDF under "
                 "reference/knowledge/sources/ and cite it by page. Then "
                 "lint: knowledge.py --validate. Stubbed pages "
                 "{page, first_line, chars} carried no rule keywords - read "
                 "those PDF pages directly if needed, or --full-text."),
    }
    return payload, 0


def _lib(name: str):
    sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
    return __import__(name)


# LCSC answers a datasheet it no longer serves with a ~7 KB one-page PDF
# ("LCSC Datasheet Notice ... Datasheet temporarily unavailable"). It is a
# valid PDF, so it must be caught by content, not by the %PDF magic.
_STUB_TEXT = ("datasheet temporarily unavailable", "lcsc datasheet notice")
_STUB_MAX_BYTES = 12_000


def stub_notice(data: bytes) -> str | None:
    """Why `data` is a host's placeholder instead of a datasheet, else None.
    Notice text first (page text via pypdf, raw bytes when pypdf is absent or
    the text is in a compressed stream it cannot read); size plus page count
    (one page, under 12 KB) is only the backstop for a reworded notice."""
    from io import BytesIO
    npages, text = None, ""
    try:
        from pypdf import PdfReader
        pages = PdfReader(BytesIO(data)).pages
        npages = len(pages)
        text = " ".join((pg.extract_text() or "") for pg in pages[:3])
    except Exception:  # noqa: BLE001 - unreadable PDF: fall back to raw bytes
        pass
    low = (text + " " + data[:_STUB_MAX_BYTES * 4].decode("latin-1")).lower()
    low = " ".join(low.split())
    for needle in _STUB_TEXT:
        if needle in low:
            return f"host notice text {needle!r}"
    if len(data) <= _STUB_MAX_BYTES and npages == 1 and len(text.strip()) < 400 \
            and "unavailable" in low:
        return f"{len(data)}-byte single-page PDF saying 'unavailable'"
    return None


def _norm(s: str) -> str:
    return re.sub(r"[^0-9A-Z]", "", str(s or "").upper())


# Between two characters of an MPN in running text: what a document may put
# there without leaving the part's name. A space only where the MPN has a
# separator of its own; colons, commas, brackets and line breaks are real breaks.
_IN_NAME = "\\-\u00ad\u2010-\u2014_."
_AT_SEPARATOR = _IN_NAME + "/ \t\u00a0"


def _family_in_text(mpn: str, k: int, text: str) -> bool:
    """Do the first `k` letters and digits of `mpn` stand in `text` as one name?
    They start a word and cross no real break. If they cross a space they also
    end a word, so TYPE-C is not found in "Output Type CMOS"; with no space they
    need not (NE555 stays in NE555D, TPS25953 in TPS25953x)."""
    pattern, sep, found = "", False, 0
    for ch in mpn.upper():
        if not (ch.isascii() and ch.isalnum()):
            sep = True
            continue
        if found == k:
            break
        if found:
            pattern += f"[{_AT_SEPARATOR if sep else _IN_NAME}]?"
        pattern += ch
        sep, found = False, found + 1
    for hit in re.finditer(r"(?<![^\W_])" + pattern, text, re.I):
        rest = text[hit.end():hit.end() + 1]
        if not (any(c.isspace() for c in hit.group()) and rest.isalnum()):
            return True
    return False


def mpn_in_text(mpn: str, text: str) -> str | None:
    """The form of `mpn` found in `text` (normalised: upper case, letters and
    digits only), else None: the whole MPN anywhere in the text, then its base
    family - the MPN with trailing characters (package, grade and reel suffixes)
    cut while at least half of it, rounded up, and 5 characters remain, standing
    in the text as one name (_family_in_text)."""
    m, t = _norm(mpn), _norm(text)
    if not m:
        return None
    if m in t:
        return m
    n = len(m)
    floor = min(n, max(5, -(-n // 2)))
    for k in range(n - 1, floor - 1, -1):
        if m[:k] in t and _family_in_text(mpn, k, text):
            return m[:k]
    return None


# Fewer text characters per page than this on average = a scan, not a datasheet.
MIN_CHARS_PER_PAGE = 20


# Words on a page that make an MPN found there an entry in a list of parts, not
# one part named in passing by another part's datasheet.
_PART_LIST = re.compile(r"part\s*(?:number|no\b)|ordering|model\s*no|type\s*no"
                        r"|product\s*(?:name|number)", re.I)
MIN_DOCUMENT_MPN = 6


def _ordering_schemes() -> dict:
    import yaml
    path = Path(__file__).resolve().parents[1] / "reference" / "ordering_codes.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))["schemes"]


def series_match(mpn: str, text: str) -> tuple[dict | None, str | None]:
    """(match, None) when `text` names the series of `mpn` and its ordering
    table decodes every field (reference/ordering_codes.yaml); (None, why) when
    the series is here but a field is not; (None, None) when no scheme applies."""
    m_norm, flat = _norm(mpn), re.sub(r"\s+", " ", text)
    for name, sch in _ordering_schemes().items():
        m = re.search(sch["mpn"], m_norm)
        if not m or not all(re.search(r, flat) for r in sch["series"]):
            continue
        fields = m.groupdict()
        lost = [f"{f}={fields[f]}" for f, r in sch["fields"].items()
                if not re.search(r.replace("{v}", re.escape(fields[f])), flat)]
        if lost:
            return None, f"its {name} ordering table has no {', '.join(lost)}"
        return {"how": "series", "scheme": name, "fields": fields}, None
    return None, None


def match_mpn(mpn: str, texts: list[str]) -> tuple[dict | None, str | None]:
    """How `texts` (one per page) tie to `mpn`, strongest first: the whole MPN
    or its base family on pages 1-2; the whole MPN on a later page that lists
    parts; the series' ordering table decoding it (#755). A base family deeper
    in the document never counts: other parts' datasheets cite families.
    Failing all three, an MPN ending in bracketed packaging codes ("(LF)(SN)")
    is tried again without them; the match then says what was cut.
    Returns (match, why not)."""
    form = mpn_in_text(mpn, " ".join(texts[:2]))
    if form:
        return {"how": "whole" if form == _norm(mpn) else "family",
                "form": form}, None
    whole = _norm(mpn)
    if len(whole) >= MIN_DOCUMENT_MPN:
        for n, t in enumerate(texts, 1):
            if whole in _norm(t) and _PART_LIST.search(t):
                return {"how": "document", "form": whole, "page": n}, None
    got = series_match(mpn, " ".join(texts))
    # a packaging or finish code in brackets at the end (JST's "(LF)(SN)")
    # is no part of the name the datasheet lists the part under (#1263)
    base = PACKAGING_SUFFIX.sub("", mpn)
    if got[0] is None and base and base != mpn:
        match, _ = match_mpn(base, texts)
        if match:
            return {**match, "cut": mpn[len(base):].strip()}, None
    return got


PACKAGING_SUFFIX = re.compile(r"(?:\s*\([A-Za-z0-9]{1,4}\))+\s*$")


def assess_pdf(data: bytes, mpn: str | None = None) -> dict:
    """Is `data` a usable datasheet for `mpn` (#706)? {status: ok|suspect,
    reasons, pages, chars_per_page, text_chars[, match]}. Suspect when the file
    is cut off (no %%EOF) or does not parse, has no text layer (a scan: nothing
    to read facts from), or nothing ties it to the MPN (match_mpn: probably
    another part's datasheet). `match` says what did. With no `mpn` the name
    check is skipped. A suspect file is kept for a person to look at; no fact
    read from it counts as verified."""
    import logging
    from io import BytesIO
    logging.getLogger("pypdf").setLevel(logging.ERROR)   # repair chatter
    reasons: list[str] = []
    out = {"pages": None, "chars_per_page": [], "text_chars": 0}
    if b"%%EOF" not in data[-2048:]:
        reasons.append("truncated: no %%EOF marker at the end of the file")
    texts: list[str] = []
    try:
        from pypdf import PdfReader
        pages = PdfReader(BytesIO(data)).pages
        out["pages"] = len(pages)
        for pg in pages:
            try:
                texts.append(pg.extract_text() or "")
            except Exception:  # noqa: BLE001 - one bad page, not a bad file
                texts.append("")
    except ImportError:
        raise
    except Exception as exc:  # noqa: BLE001 - unparseable is a verdict
        reasons.append(f"does not parse: {type(exc).__name__}: {exc}"[:160])
    out["chars_per_page"] = [len(t.strip()) for t in texts]
    out["text_chars"] = sum(out["chars_per_page"])
    if out["pages"] and out["text_chars"] < MIN_CHARS_PER_PAGE * out["pages"]:
        reasons.append(f"no text layer: {out['text_chars']} characters on "
                       f"{out['pages']} page(s), a scan or an image PDF")
    elif mpn and texts:
        match, why = match_mpn(mpn, texts)
        if match:
            out["match"] = match
        else:
            reasons.append(f"MPN {mpn} (or its base family) is not in the text "
                           "of pages 1-2 and no ordering table here decodes it"
                           f"{f' ({why})' if why else ''}: probably another "
                           "part's datasheet")
    return {"status": "suspect" if reasons else "ok", "reasons": reasons,
            **out}


def fetch_datasheet(url: str, dest: Path, transport=None,
                    domains: dict | None = None, mpn: str | None = None) -> dict:
    """GET one datasheet into dest: {status: fetched|suspect|missing|refused|error, ...}.
    transport is researchlib.http_transport unless injected (tests)."""
    researchlib = _lib("researchlib")
    partslib = _lib("partslib")
    url = partslib.fix_datasheet_url(url)
    out = {"url": url, "file": None}
    ck = researchlib.check_url(url, domains)
    if not ck["ok"]:
        return {**out, "status": "refused", "error": ck["reason"]}
    try:
        resp = (transport or researchlib.http_transport)(url)
    except Exception as exc:  # noqa: BLE001 - transport failure is a result
        return {**out, "status": "error",
                "error": f"transport failure: {type(exc).__name__}: {exc}"}
    out["hops"] = resp.get("hops") or [resp.get("final_url") or url]
    for hop in out["hops"]:
        hk = researchlib.check_url(hop, domains)
        if not hk["ok"]:
            return {**out, "status": "refused",
                    "error": f"redirect to {hop} refused: {hk['reason']}"}
    blk = researchlib.host_block(resp, url)
    if blk:
        return {**out, "status": "refused", "blocked_by_host": blk,
                "error": f"blocked by host: {blk['host']} answered HTTP "
                         f"{blk['http_status']}"}
    if int(resp.get("status") or 0) >= 400:
        return {**out, "status": "error",
                "error": f"HTTP {resp.get('status')}"}
    data = resp.get("body") or b""
    if not data.startswith(b"%PDF"):
        return {**out, "status": "refused",
                "error": (f"not a PDF after {len(out['hops'])} hop(s): "
                          f"{len(data)} bytes starting {data[:60]!r}")}
    why = stub_notice(data)
    if why:
        return {**out, "status": "missing", "stub": True, "bytes": len(data),
                "error": f"host served a placeholder, not a datasheet: {why}"}
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    import hashlib
    ass = assess_pdf(data, mpn)
    return {**out, "status": "fetched" if ass["status"] == "ok" else "suspect",
            "assessment": ass, "file": str(dest),
            "final_url": resp.get("final_url") or url, "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest()}


_ASSESSED = ("status", "reasons", "pages", "chars_per_page", "text_chars",
             "match", "assessed")

# Bump when a change to assess_pdf or match_mpn can change a verdict: the next
# read of each index judges its PDFs again. 2: the family match keeps to words.
# 3: a bracketed packaging code at the end of the MPN is cut and tried again.
ASSESS_VERSION = 3
_VERDICTS: dict = {}    # this process's verdicts, for a tree it cannot write


def _index(ddir: Path) -> dict:
    try:
        return json.loads((ddir / "index.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_index(ddir: Path, idx: dict) -> None:
    # whole or not at all: verify runs checks side by side
    tmp = ddir / f".index.json.{os.getpid()}.tmp"
    try:
        tmp.write_text(json.dumps(idx, indent=1, ensure_ascii=True,
                                  sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, ddir / "index.json")
    finally:
        tmp.unlink(missing_ok=True)


def _parts_mpns(parts_dir: Path) -> dict:
    try:
        lines = json.loads((parts_dir / "parts.json").read_text(
            encoding="utf-8")).get("parts") or []
    except (OSError, ValueError, AttributeError):
        return {}
    return {str(p.get("lcsc")): p.get("mpn") for p in lines
            if isinstance(p, dict)}


def _stamp(sha: str, mpn: str | None) -> dict:
    """What a verdict judged: it holds until one of these changes."""
    return {"sha256": sha, "mpn": mpn or None, "version": ASSESS_VERSION}


def _judge(data: bytes, sha: str, mpn: str | None) -> dict:
    key = (sha, mpn or None, ASSESS_VERSION)
    if key not in _VERDICTS:
        a = assess_pdf(data, mpn)
        why = stub_notice(data)
        if why:
            a.update(status="suspect", reasons=[
                f"host placeholder, not a datasheet ({why})"])
        elif a["status"] == "ok" and not mpn:
            a.update(status="unassessed", reasons=[
                "no MPN in index.json or parts.json to check this PDF against"])
        _VERDICTS[key] = {**a, "assessed": _stamp(sha, mpn)}
    return _VERDICTS[key]


def _refresh(ddir: Path, idx: dict, mpns: dict, only: str | None = None,
             force: bool = False) -> bool:
    """Judge into `idx` each PDF on disk its stamp doesn't cover. True if any
    entry changed. An entry with no PDF is left alone: its verdict, if any, was
    made on a file since gone, and there is nothing here to make a new one on."""
    changed = False
    for pdf in sorted(ddir.glob("*.pdf")):
        lcsc = pdf.stem
        ent = idx.get(lcsc, {})
        if (only and lcsc != only) or not isinstance(ent, dict):
            continue
        data = pdf.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        mpn = ent.get("mpn") or mpns.get(lcsc)
        if not force and ent.get("assessed") == _stamp(sha, mpn):
            continue
        new = {k: v for k, v in ent.items() if k not in _ASSESSED}
        if not ent:
            new.update(file=f"parts/datasheets/{pdf.name}", sha256=sha,
                       bytes=len(data), **({"mpn": mpn} if mpn else {}))
        idx[lcsc] = {**new, **_judge(data, sha, mpn)}
        changed = True
    return changed


def ensure_assessed(parts_dir: Path) -> dict:
    """The index with a current verdict for every PDF on disk (#768): read it,
    judge what its stamps don't cover, record that. Parsing 43 PDFs takes 42 s
    once; after that a read only hashes them. A tree it cannot write still gets
    the verdicts, and an index it cannot parse is left for a person."""
    ddir = parts_dir / "datasheets"
    if not ddir.is_dir():
        return {}
    idx: dict = {}
    if (ddir / "index.json").exists():
        try:
            idx = json.loads((ddir / "index.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(idx, dict):
            return {}
    try:
        changed = _refresh(ddir, idx, _parts_mpns(parts_dir))
    except ImportError:     # no pypdf: nothing judged, so unassessed says so
        return idx
    if changed:
        try:
            _write_index(ddir, idx)
        except OSError:     # a read-only copy of a board
            pass
    return idx


def suspect_datasheets(parts_dir: Path, lcscs) -> dict[str, str]:
    """{lcsc: why} for each part whose datasheet PDF is a placeholder or whose
    verdict is suspect (truncated, no text layer, nothing ties it to the MPN;
    #706)."""
    ddir = parts_dir / "datasheets"
    idx = ensure_assessed(parts_dir)
    out = {}
    for lcsc in lcscs:
        ent = idx.get(lcsc)
        if isinstance(ent, dict) and ent.get("status") == "suspect":
            out[lcsc] = "; ".join(ent.get("reasons") or ["marked suspect"])
            continue
        pdf = ddir / f"{lcsc}.pdf"
        if pdf.is_file():
            why = stub_notice(pdf.read_bytes())
            if why:
                out[lcsc] = f"host placeholder, not a datasheet ({why})"
    return out


def unassessed_datasheets(parts_dir: Path, lcscs) -> list[str]:
    """Parts with a datasheet that has no verdict and cannot get one here
    (#755, #768): an index.json entry with no PDF on disk, which takes --fetch,
    or a PDF with no MPN to check it against."""
    idx = ensure_assessed(parts_dir)
    ddir = parts_dir / "datasheets"
    out = []
    for lcsc in sorted(lcscs):
        ent = idx.get(lcsc)
        if ent is None:
            if (ddir / f"{lcsc}.pdf").is_file():
                out.append(lcsc)
        elif not isinstance(ent, dict) \
                or ent.get("status") not in ("ok", "suspect"):
            out.append(lcsc)
    return out


def do_assess(args) -> tuple[dict, int]:
    ws = Path(args.assess)
    ddir = ws / "parts" / "datasheets"
    idx = _index(ddir)
    if not idx and not any(ddir.glob("*.pdf")):
        return {"script": "datasheet_extract", "status": "error",
                "error": f"{ddir / 'index.json'}: no entries to assess"}, 2
    mpns = _parts_mpns(ws / "parts")
    _refresh(ddir, idx, mpns, only=args.lcsc, force=True)
    rows = []
    for lcsc, ent in sorted(idx.items()):
        if (args.lcsc and lcsc != args.lcsc) or not isinstance(ent, dict):
            continue
        if not (ddir / f"{lcsc}.pdf").is_file() \
                and ent.get("status") not in ("ok", "suspect"):
            # the verdict of a PDF since gone still stands; none was ever made
            idx[lcsc] = {**ent, "status": "unassessed", "reasons": [
                "no PDF on disk: run --fetch to read it"]}
        e = idx[lcsc]
        rows.append({"lcsc": lcsc, "mpn": e.get("mpn") or mpns.get(lcsc),
                     "status": e["status"], "reasons": e.get("reasons") or [],
                     "match": e.get("match")})
    _write_index(ddir, idx)
    counts = {st: sum(r["status"] == st for r in rows)
              for st in ("ok", "suspect", "unassessed")}
    clean = counts["ok"] == len(rows)
    return {"script": "datasheet_extract",
            "status": "pass" if clean else "violations", **counts,
            "of": len(rows), "datasheets": rows}, 0 if clean else 1


def do_fetch(args, transport=None) -> tuple[dict, int]:
    ws = Path(args.fetch)
    pj = ws / "parts" / "parts.json"
    if not pj.is_file():
        return {"script": "datasheet_extract", "status": "error",
                "error": f"{pj} not found"}, 2
    lines = json.loads(pj.read_text(encoding="utf-8")).get("parts") or []
    ddir = ws / "parts" / "datasheets"
    results = []
    for line in lines:
        lcsc = str(line.get("lcsc") or "").strip()
        if args.lcsc and lcsc != args.lcsc:
            continue
        if not lcsc or not line.get("datasheet"):
            results.append({"lcsc": lcsc, "mpn": line.get("mpn"),
                            "status": "missing",
                            "error": "no lcsc or no datasheet URL"})
            continue
        r = fetch_datasheet(line["datasheet"], ddir / f"{lcsc}.pdf",
                            transport=transport, mpn=line.get("mpn"))
        if r.get("file"):
            r["file"] = Path(r["file"]).relative_to(ws).as_posix()
        results.append({"lcsc": lcsc, "mpn": line.get("mpn"), **r})
    if args.lcsc and not results:
        return {"script": "datasheet_extract", "status": "error",
                "error": f"{args.lcsc} is not in {pj}"}, 2
    ok = all(r["status"] == "fetched" for r in results)
    if any(r.get("file") for r in results):
        idx = ddir / "index.json"
        old = (json.loads(idx.read_text(encoding="utf-8"))
               if idx.is_file() else {})
        for r in results:
            if r.get("file"):
                a = r["assessment"]
                old[r["lcsc"]] = {
                    **{k: r.get(k) for k in ("mpn", "file", "url",
                                             "final_url", "bytes", "sha256")},
                    **{k: a[k] for k in _ASSESSED if k in a},
                    **({"assessed": _stamp(r["sha256"], r["mpn"])}
                       if r.get("mpn") else {})}
        _write_index(ddir, old)
    return {"script": "datasheet_extract",
            "status": "pass" if ok else "violations",
            "fetched": sum(r["status"] == "fetched" for r in results),
            "suspect": sum(r["status"] == "suspect" for r in results),
            "of": len(results), "datasheets": results,
            "next": ("datasheet_extract.py --pdf parts/datasheets/<lcsc>.pdf "
                     "--lcsc <lcsc> --out parts/<lcsc>.grounding.json")}, \
        (0 if ok else 1)


def _layoutimpl():
    sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
    import layoutimpl
    return layoutimpl


def _board_mm(args, ws: Path) -> tuple[float, float] | None:
    if args.board_mm:
        w, h = args.board_mm.lower().split("x")
        return float(w), float(h)
    st = ws / "state.json"
    if st.is_file():
        size = (json.loads(st.read_text(encoding="utf-8")).get("mode")
                or {}).get("stated_size")
        if size:
            return float(size[0]), float(size[1])
    return None


def do_screen(args) -> tuple[dict, int]:
    li = _layoutimpl()
    ws = Path(args.screen)
    res = li.screen(ws, _board_mm(args, ws), args.cap_class)
    out = (ws / "parts" if (ws / "parts").is_dir() else ws / "kicad") \
        / "layout_implications.json"
    out.write_text(json.dumps(res, indent=1, ensure_ascii=True),
                   encoding="utf-8")
    payload = {"script": "datasheet_extract", "mode": "screen",
               "status": "violations" if res["conflicts"] else "pass",
               "written": out.as_posix(), **res}
    return payload, 1 if res["conflicts"] else 0


def do_implications(args) -> tuple[dict, int]:
    li = _layoutimpl()
    needs = json.loads(args.needs) if args.needs else {}
    cap = li.fab_row(args.cap_class)
    ranked = []
    for f in args.implications:
        ext = json.loads(Path(f).read_text(encoding="utf-8"))
        imp = li.implications(ext, None, cap)
        ranked.append({"file": f, **imp, **li.score(imp, needs)})
    ranked.sort(key=lambda r: -r["score"])
    return {"script": "datasheet_extract", "mode": "implications",
            "status": "pass", "needs": needs, "candidates": ranked}, 0


def do_validate(args) -> tuple[dict, int]:
    import jsonschema
    path = Path(args.validate)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return {"script": "datasheet_extract", "status": "error",
                "error": f"cannot read {path}: {exc}"}, 2
    except json.JSONDecodeError as exc:
        return {"script": "datasheet_extract", "status": "error",
                "error": f"{path} is not valid JSON: {exc}"}, 2
    validator = jsonschema.Draft202012Validator(DATASHEET_SCHEMA)
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
    if not errors:
        return {"script": "datasheet_extract", "status": "pass",
                "file": str(path), "mpn": data.get("mpn"),
                "pins": len(data.get("pinout", [])),
                "has_land_pattern": "land_pattern" in data}, 0
    return {
        "script": "datasheet_extract",
        "status": "invalid",
        "file": str(path),
        "error_count": len(errors),
        "errors": [{"path": "/".join(str(p) for p in e.path), "msg": e.message}
                   for e in errors],
    }, 1


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--pdf", help="extract text + emit grounding payload")
    mode.add_argument("--fetch", metavar="WS",
                      help="download parts.json datasheets into "
                           "WS/parts/datasheets/")
    mode.add_argument("--assess", metavar="WS",
                      help="write a status into WS/parts/datasheets/index.json "
                           "from the PDFs on disk")
    mode.add_argument("--validate", help="validate a candidate extraction JSON")
    mode.add_argument("--schema", action="store_true", help="print the JSON schema")
    mode.add_argument("--app-note", help="extract text + emit a KNOWLEDGE "
                                         "RECORD grounding payload (U4)")
    mode.add_argument("--screen", metavar="WS",
                      help="U9 P3 layout screen over a workspace")
    mode.add_argument("--implications", nargs="+", metavar="FILE",
                      help="U9 layout implications of candidate "
                           "extractions, ranked against --needs")
    ap.add_argument("--board-mm", help="--screen: board WxH in mm, e.g. "
                                       "50x40 (default: state mode size)")
    ap.add_argument("--cap-class", default="2layer_1oz",
                    help="jlc_capabilities design_rules row")
    ap.add_argument("--needs", help="--implications: JSON needs object")
    ap.add_argument("--lcsc", help="tag the output with this LCSC id (--pdf); "
                                   "fetch or assess only this part (--fetch, --assess)")
    ap.add_argument("--render", metavar="DIR",
                    help="--pdf: render image-only and drawing pages to "
                         "PNGs in DIR (pdftoppm), OCR the image-only ones "
                         "(tesseract, when installed)")
    ap.add_argument("--full-text", action="store_true",
                    help="--pdf: emit full text for EVERY page (no stubbing)")
    ap.add_argument("--out", help="write JSON here instead of stdout")
    args = ap.parse_args(argv)

    try:
        if args.schema:
            payload, code = {"script": "datasheet_extract", "status": "pass",
                             "schema": DATASHEET_SCHEMA}, 0
        elif args.pdf:
            payload, code = do_pdf(args)
        elif args.fetch:
            payload, code = do_fetch(args)
        elif args.assess:
            payload, code = do_assess(args)
        elif args.app_note:
            payload, code = do_app_note(args)
        elif args.screen:
            payload, code = do_screen(args)
        elif args.implications:
            payload, code = do_implications(args)
        else:
            payload, code = do_validate(args)
    except MissingModule as exc:
        payload, code = {"script": "datasheet_extract", "status": "error",
                         "error": "missing-module: " + str(exc)}, 2
    except Exception as exc:  # noqa: BLE001 - contract: any error -> exit 2
        payload, code = {"script": "datasheet_extract", "status": "error",
                         "error": f"{type(exc).__name__}: {exc}"}, 2

    text = json.dumps(payload, indent=1, ensure_ascii=True)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        print(text)
    return code


if __name__ == "__main__":
    sys.exit(main())
