"""researchlib - the research verb's deterministic spine (U15, v3 design
decision 5a: coverage-gated design).

A coverage GAP (`knowledge.py --coverage` -> `gaps[]`, one per slot) is a
research task spec. Research is agent work (agents/researcher.md reads,
synthesizes and writes records; agents/research-second-reader.md re-reads the
cited pages and refutes or verifies) - this module owns everything that must
be MECHANICAL around it, laid out under one research root
(`<workspace>/research/`; a bench fixture freezes the same layout):

    research/tasks/<task-id>.json    the task: gap spec, caps snapshot, the
                                     SOURCE LEDGER (every acquisition, sha-
                                     pinned), verdicts, status
    research/sources/<file>          QUARANTINE - downloads land here and
                                     nowhere else; only `fetch` writes it
    research/records/<id>.yaml       draft records (schema v2, status draft
                                     until the second reader verifies)
    research/checklists/<id>.yaml    a draft coverage checklist when the gap
                                     was "no checklist for this topology"

Enforced here, not in prose:
  - Domain allowlist (reference/knowledge/domains.yaml): https only, host must
    be covered, every redirect hop re-checked, refused BEFORE any bytes are
    kept. Vendor community subdomains (`forum_hosts`) force tier `forum`.
  - Source-tier policy: vendor-layout > vendor-appnote > cross-vendor > forum;
    a forum source may corroborate but never be a record's SOLE source.
  - Caps: `depth_per_gap` = sources acquired per task (state.json
    budgets.research; snapshotted into the task at open); failed/refused
    attempts are bounded too (ATTEMPT_FACTOR x depth). A cap hit is a
    VISIBLE checkpoint payload (status "checkpoint"), never a silent stop.
    The per-run cap (`per_run`) is consumed by research.py open through
    state.py's budget ledger.
  - Provenance: a research record may cite ONLY files in its task's ledger
    (`research/sources/<file>`), each citation with a page AND a note saying
    what was READ on that page (layout figures are the highest-value content
    and text extraction cannot see them - the note is the visual read's
    trace; the second reader re-reads the same page).
  - Envelope justification: a record that carries an envelope carries an
    `envelope_note` (what the rule scales with, U14's ruling standard).
  - Maturity governance: research records are draft (unverified) or verified
    (second reader signed `verification`); approved/proven can only be set
    by the owner / bring-up evidence after promotion.
  - Close = a promotion-queue entry (U6): the workspace LEARNINGS.md gets one
    dated, stage-tagged entry per closed task and `learnings.py compile`
    turns it into a pending queue row; `promote` copies a verified record
    (+ its sources, paths rewritten) into the library for the owner's
    approval ruling.

Everything is ASCII, JSON-serializable, and testable without network:
`fetch_source(transport=...)` takes an injectable transport.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import date as _date
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import yaml

import knowledgelib

LIB = Path(__file__).resolve().parent
SCRIPTS = LIB.parent
SKILL = SCRIPTS.parent
REPO = SKILL
DOMAINS_PATH = SKILL / "reference" / "knowledge" / "domains.yaml"

TIERS = ("vendor-layout", "vendor-appnote", "cross-vendor", "forum")
TIER_RANK = {t: i for i, t in enumerate(TIERS)}
DOMAIN_KINDS = ("vendor", "distributor", "standards", "forum")
EXPECTS = ("pdf", "html", "any")
VERDICTS = ("verified", "refuted")
TASK_STATUSES = ("open", "closed")
OUTCOMES = ("verified", "abandoned", "blocked")
# A host that REFUSES the fetch (auth/forbidden/rate-limit, or a bot-manager
# challenge page in place of the document) is a distinct outcome from "the
# document has nothing": PCB-0028 closed its ldo + tvs slots empty after every
# Torex/Littelfuse fetch got an Akamai 403. Never evaded - recorded, then the
# distributor's hosted copy is tried (distributor_datasheets).
BLOCK_STATUSES = (401, 403, 429)
_CHALLENGE_MARKERS = (            # lower-cased, searched in a non-PDF body head
    (b"_sec/cp_challenge", "akamai"),     # Akamai Bot Manager interstitial
    (b"/cdn-cgi/challenge-platform", "cloudflare"),
    (b"cf-chl", "cloudflare"),
    (b"<title>just a moment...</title>", "cloudflare"),
    (b"attention required! | cloudflare", "cloudflare"),
)
# Weak markers also turn up in real pages served through Akamai (asset links
# to edgesuite.net), so they count only on an error status or a short body.
_WEAK_CHALLENGE_MARKERS = (
    (b"edgesuite", "akamai"),             # Akamai "Access Denied" reference link
    (b"bm-verify", "akamai"),
)
WEAK_MARKER_MAX_BODY = 4096       # bytes: an interstitial, not a real page
STUB_MAX_BODY = 4096              # bytes: an HTML redirect stub, not a page
STUB_MAX_HOPS = 3                 # stub redirects followed per fetch
_LCSC_PN = re.compile(r"^C\d{2,}$", re.I)
FALLBACK_TRIES = 3          # distributor copies tried per block (attempts cap)
# Mirror of state.DEFAULT_BUDGETS["research"] (a lib must not import the
# state script); tests pin the two equal.
DEFAULT_CAPS = {"per_run": 6, "depth_per_gap": 4}
ATTEMPT_FACTOR = 3          # refused/failed attempts allowed = factor x depth
SOURCE_NOTE_MIN = 12        # chars: "fig 3 hot loop" is a note, "p7" is not
ENVELOPE_NOTE_MIN = 12
MAX_BYTES = 60 * 1024 * 1024
USER_AGENT = "Mozilla/5.0 (hwde research fetch)"
CHECKLIST_CLASS = "coverage-checklist"   # the gap's `missing` class when a
                                          # topology/interface has no checklist
SECOND_READER = "second-reader"

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_TASK_ID = re.compile(r"^(block|interface|part)-[a-z0-9][a-z0-9_.+-]*-\d+$")
_RECORD_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")

DOMAINS_SCHEMA: dict = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "hwde research fetch allowlist",
    "type": "object",
    "required": ["version", "domains"],
    "additionalProperties": False,
    "properties": {
        "version": {"const": 1},
        "domains": {
            "type": "array", "minItems": 1,
            "items": {
                "type": "object",
                "required": ["domain", "kind"],
                "additionalProperties": False,   # a comma in an unquoted flow
                "properties": {                  # value shows up HERE (LEARNINGS
                    "domain": {"type": "string",  # 2026-08-14 [yaml][knowledge])
                               "pattern": r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$"},
                    "kind": {"type": "string", "enum": list(DOMAIN_KINDS)},
                    "note": {"type": "string"},
                },
            },
        },
        "forum_hosts": {"type": "array", "items": {"type": "string"}},
    },
}


def today() -> str:
    return _date.today().isoformat()


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# layout
# ---------------------------------------------------------------------------
def root_of(ws: Path | str) -> Path:
    """The research root of a workspace."""
    return Path(ws) / knowledgelib.WS_RESEARCH


def tasks_dir(root: Path) -> Path:
    return Path(root) / "tasks"


def sources_dir(root: Path) -> Path:
    return Path(root) / "sources"


def records_dir(root: Path) -> Path:
    return Path(root) / "records"


def checklists_dir(root: Path) -> Path:
    return Path(root) / "checklists"


def source_ref(name: str) -> str:
    """How a record cites a quarantined file: workspace-relative."""
    return f"{knowledgelib.WS_RESEARCH}/sources/{name}"


def ensure_layout(root: Path) -> None:
    for d in (tasks_dir(root), sources_dir(root), records_dir(root),
              checklists_dir(root)):
        d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# the domain allowlist
# ---------------------------------------------------------------------------
def load_domains(path: Path | str | None = None) -> dict:
    p = Path(path) if path else DOMAINS_PATH
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{p}: not a mapping")
    return data


def domain_problems(data: dict) -> list[str]:
    """Lint the allowlist: schema (additionalProperties catches the comma-in-
    a-flow-value split), ASCII, duplicates, forum_hosts under an allowed
    domain."""
    import jsonschema
    problems: list[str] = []
    validator = jsonschema.Draft202012Validator(DOMAINS_SCHEMA)
    for err in sorted(validator.iter_errors(data), key=lambda e: list(e.path)):
        path = "/".join(str(x) for x in err.path) or "(top)"
        problems.append(f"domains.yaml: schema {path}: {err.message}")
    if problems:
        return problems
    try:
        json.dumps(data, ensure_ascii=False).encode("ascii")
    except UnicodeEncodeError:
        problems.append("domains.yaml: not ASCII-safe")
    seen: set[str] = set()
    for e in data["domains"]:
        d = e["domain"]
        if d in seen:
            problems.append(f"domains.yaml: duplicate domain {d}")
        seen.add(d)
    for h in data.get("forum_hosts") or []:
        if not any(_host_matches(h, d) for d in seen):
            problems.append(f"domains.yaml: forum_host {h} is not under an "
                            "allowed domain (add its parent to domains)")
    return problems


def _host_matches(host: str, domain: str) -> bool:
    host = host.lower().rstrip(".")
    domain = domain.lower()
    return host == domain or host.endswith("." + domain)


def check_url(url: str, domains: dict | None = None) -> dict:
    """{ok, host, entry, forced_tier, reason}. ok=False carries the reason.
    https only; the host must equal or sit under a listed domain; a listed
    forum domain or a `forum_hosts` entry forces tier forum. Userinfo
    (`user@host`) is refused outright - it is the classic host-spoofing
    shape and no vendor URL needs it."""
    domains = domains or load_domains()
    out = {"url": url, "ok": False, "host": None, "entry": None,
           "forced_tier": None, "reason": None}
    try:
        parts = urlsplit(url.strip())
    except ValueError as exc:
        out["reason"] = f"unparseable URL ({exc})"
        return out
    if parts.scheme.lower() != "https":
        out["reason"] = f"scheme {parts.scheme or '(none)'!r} refused - https only"
        return out
    if parts.username is not None or parts.password is not None:
        out["reason"] = "userinfo in URL refused"
        return out
    host = (parts.hostname or "").lower()
    out["host"] = host
    if not host:
        out["reason"] = "no host"
        return out
    entry = None
    for e in domains.get("domains") or []:
        if _host_matches(host, e["domain"]):
            # the most specific match wins (product.tdk.com over tdk.com)
            if entry is None or len(e["domain"]) > len(entry["domain"]):
                entry = e
    if entry is None:
        out["reason"] = (f"host {host!r} is not in the vendor/distributor "
                         "allowlist (reference/knowledge/domains.yaml) - "
                         "off-list fetch refused")
        return out
    out["ok"] = True
    out["entry"] = {"domain": entry["domain"], "kind": entry["kind"]}
    if entry["kind"] == "forum" or any(
            _host_matches(host, h) for h in domains.get("forum_hosts") or []):
        out["forced_tier"] = "forum"
    return out


def effective_tier(declared: str, forced: str | None) -> str:
    if declared not in TIER_RANK:
        raise ValueError(f"unknown tier {declared!r} (one of {', '.join(TIERS)})")
    if forced and TIER_RANK[forced] > TIER_RANK[declared]:
        return forced
    return declared


# ---------------------------------------------------------------------------
# tasks
# ---------------------------------------------------------------------------
def slot_token(gap: dict) -> str:
    kind = gap.get("kind")
    if kind == "block":
        return str(gap.get("topology") or "")
    if kind == "interface":
        return str(gap.get("interface") or "")
    return str(gap.get("mpn") or gap.get("lcsc") or "")


def sanitize_token(tok: str) -> str:
    t = re.sub(r"[^a-z0-9_.+-]+", "-", str(tok).strip().lower()).strip("-")
    return t or "slot"


def is_checklist_gap(gap: dict) -> bool:
    return any(m.get("class") == CHECKLIST_CLASS
               for m in gap.get("missing") or [])


def load_task(root: Path, tid: str) -> dict:
    p = tasks_dir(root) / f"{tid}.json"
    if not p.is_file():
        raise FileNotFoundError(f"no research task {tid!r} under "
                                f"{tasks_dir(root).as_posix()}")
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("id") != tid:
        raise ValueError(f"{p}: not a research task file")
    return data


def write_task(root: Path, task: dict) -> Path:
    tasks_dir(root).mkdir(parents=True, exist_ok=True)
    p = tasks_dir(root) / f"{task['id']}.json"
    task["updated"] = now()
    text = json.dumps(task, indent=1, ensure_ascii=True) + "\n"
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(p)
    return p


def list_tasks(root: Path) -> list[dict]:
    d = tasks_dir(root)
    out = []
    for p in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("id"):
            out.append(data)
    return out


def make_task_id(gap: dict, existing: list[dict]) -> str:
    base = f"{gap.get('kind')}-{sanitize_token(slot_token(gap))}"
    n = 1 + sum(1 for t in existing if str(t.get("id", "")).startswith(base + "-")
                and re.fullmatch(r"\d+", str(t.get("id", ""))[len(base) + 1:]))
    return f"{base}-{n}"


def open_task_for_slot(root: Path, gap: dict, phase: str,
                       caps: dict | None = None,
                       coverage_report: str | None = None) -> dict:
    """The task dict for one gap (not yet written). Refuses a second OPEN
    task on the same slot - one writer per slot at a time."""
    existing = list_tasks(root)
    for t in existing:
        if t.get("slot") == gap.get("slot") and t.get("status") == "open":
            raise ValueError(f"slot {gap.get('slot')!r} already has open "
                             f"research task {t['id']} - close it first")
    caps = dict(DEFAULT_CAPS, **(caps or {}))
    tid = make_task_id(gap, existing)
    if not _TASK_ID.match(tid):
        raise ValueError(f"cannot form a task id from slot {gap.get('slot')!r}")
    return {
        "id": tid, "slot": gap.get("slot"), "kind": gap.get("kind"),
        "token": slot_token(gap), "phase": phase, "status": "open",
        "outcome": None, "opened": now(), "closed": None,
        "coverage_report": coverage_report,
        "gap": gap,
        "checklist_needed": is_checklist_gap(gap),
        "caps": {"depth_per_gap": int(caps["depth_per_gap"]),
                 "attempt_factor": ATTEMPT_FACTOR},
        "sources": [], "attempts": [], "verdicts": {},
        "queue_entry": None,
    }


# ---------------------------------------------------------------------------
# the researcher's brief
# ---------------------------------------------------------------------------
TIER_POLICY = (
    "Source tiers, best first: vendor-layout (the subject part's OWN "
    "datasheet layout section / eval board / reference layout) > "
    "vendor-appnote (same vendor, the topology) > cross-vendor (another "
    "vendor's note on the same topology/interface) > forum (corroboration "
    "only - NEVER a record's sole source; the close step refuses it). "
    "Declare the tier per fetch (--tier); vendor community subdomains are "
    "forced to forum. Prefer the highest tier that actually covers the "
    "operating point; two independent tiers beat one when they disagree - "
    "record the disagreement in prose.")

VISUAL_RULE = (
    "Cited pages must be READ VISUALLY (Read the quarantined PDF page as an "
    "image): layout figures are the highest-value content and text "
    "extraction cannot see them. Every sources[] entry carries `page` and a "
    "`note` saying WHAT WAS READ there (figure/table/section and its claim); "
    "figure descriptions land in the record prose. The second reader "
    "re-reads exactly those pages to refute you.")


def record_template(task: dict) -> dict:
    """The schema-v2 skeleton a researcher fills for THIS task: origin bound
    to the task, applies keyed to the slot, sources pointing at the
    quarantine, status/maturity draft, envelope + envelope_note stubs."""
    rec = knowledgelib.blank_record()
    gap = task["gap"]
    kind = task["kind"]
    tok = task["token"]
    applies = {"topologies": [], "packages": [], "interfaces": [], "parts": []}
    if kind == "block":
        applies["topologies"] = [tok]
    elif kind == "interface":
        applies["interfaces"] = [tok]
    else:
        applies["parts"] = [p for p in (gap.get("mpn"), gap.get("lcsc")) if p]
        if gap.get("package"):
            applies["packages"] = [gap["package"]]
    missing = [m["class"] for m in gap.get("missing") or []
               if m.get("class") != CHECKLIST_CLASS]
    rec.update({
        "id": f"<{sanitize_token(tok)}-<what-the-rule-is>>",
        "classes": missing[:1] or ["<class from the controlled list>"],
        "applies": applies,
        "prose": ("<the rule + why, incl. the figure descriptions you read; "
                  "<= 1500 chars; ASCII>"),
        "sources": [{"file": source_ref("<file from research.py fetch>"),
                     "page": 1,
                     "note": "<what was READ on that page: fig/table + claim>"}],
        "status": "draft",
        "origin": f"research:{task['id']}",
        "level": "topology" if kind != "part" else "part",
        "envelope": {"<dim>_<unit>": {"min": 0, "max": 0},
                     "<name>_kind": {"in": ["<value>"]}},
        "envelope_note": "<what the rule scales with, and why these bounds>",
        "maturity": "draft",
        "generalizes": [],
    })
    return rec


def checklist_template(task: dict) -> dict:
    tok = task["token"]
    kind = task["kind"]
    return {
        "id": sanitize_token(tok),
        "kind": "coverage-checklist",
        "applies": ({"topologies": [tok]} if kind == "block"
                    else {"interfaces": [tok]}),
        "requires": [{"class": "<class>", "min_level": "topology",
                      "note": "<why this class must be populated first>"}],
        "maturity": "draft",
        "sources": [{"file": source_ref("<file>"), "page": 1,
                     "note": "<what was READ>"}],
        "prose": "<what a designer must know before designing this>",
        "origin": f"research:{task['id']}",
    }


def brief(task: dict, ws: Path, library_records: list[dict] | None = None,
          checklists: list[dict] | None = None,
          domains: dict | None = None) -> dict:
    """The researcher's assignment payload (what the orchestrator pastes into
    the spawn): the gap, what the library already holds (principle parents +
    related records, so research targets the APPLICATION delta), the
    policies, the templates, the exact commands and the caps."""
    library_records = (knowledgelib.load_records() if library_records is None
                       else library_records)
    checklists = knowledgelib.load_checklists() if checklists is None \
        else checklists
    domains = domains or load_domains()
    gap = task["gap"]
    by_id = {r.get("id"): r for r in library_records}
    known = []
    for rid in list(gap.get("principle_parents") or []) + \
            list(gap.get("related_records") or []):
        r = by_id.get(rid)
        if r is None:
            continue
        known.append({"id": rid, "level": knowledgelib.record_level(r),
                      "maturity": knowledgelib.record_maturity(r),
                      "classes": r.get("classes"),
                      "applies": r.get("applies"),
                      "prose": " ".join((r.get("prose") or "").split())})
    cl = knowledgelib.find_checklist(
        checklists, task["kind"], task["token"]) if task["kind"] != "part" \
        else None
    depth = task["caps"]["depth_per_gap"]
    ws_posix = Path(ws).as_posix()
    # earlier passes on the same slot: their verdicts (a refuted record's
    # note says what NOT to repeat) are the second pass's starting point
    prior = [{"id": t["id"], "status": t.get("status"),
              "outcome": t.get("outcome"),
              "sources": [x.get("file") for x in t.get("sources") or []],
              "verdicts": t.get("verdicts") or {}}
             for t in list_tasks(root_of(ws))
             if t.get("slot") == task["slot"] and t["id"] != task["id"]]
    commands = {
        "fetch": (f"scripts/research.py fetch --workspace {ws_posix} --task "
                  f"{task['id']} --url <https://vendor/...pdf> --tier "
                  "<vendor-layout|vendor-appnote|cross-vendor|forum> "
                  "[--about <mpn>] [--expect pdf|html] [--part <LCSC|MPN>]"
                  " (a host that refuses - 401/403/429 or a bot challenge - "
                  "is recorded blocked_by_host, never evaded; --part then "
                  "tries the distributor's hosted datasheet)"),
        "validate": (f"scripts/research.py validate --workspace {ws_posix} "
                     f"--task {task['id']}"),
        "status": (f"scripts/research.py status --workspace {ws_posix} "
                   f"--task {task['id']}"),
    }
    payload = {
        "task": task["id"], "slot": task["slot"], "kind": task["kind"],
        "token": task["token"], "phase": task["phase"],
        "assignment": [
            gap.get("task") or f"research {task['slot']}",
            "Populate the missing classes at this operating point with "
            "class-level records the coverage query can retrieve "
            "(applies keyed to the slot; level + envelope declarable at P2).",
            ("The slot has NO coverage checklist yet: write research/"
             "checklists/<token>.yaml first (what a designer must know before "
             "designing this), then the records it demands."
             if task["checklist_needed"] else
             "The slot's checklist exists - fill only the classes it lists "
             "as missing; do not widen it."),
            ((f"Principle-level records exist for some of the missing "
              f"CLASSES ({', '.join(gap['principle_parents'])}) - matched by "
              "class, not by this slot (a buck principle can show up for an "
              "Ethernet gap). Where one genuinely covers the mechanism, "
              "research the APPLICATION delta and link it in `generalizes`; "
              "where it does not, state the principle for this slot "
              "yourself (level principle, no envelope) plus the "
              "topology/family record.")
             if gap.get("principle_parents") else
             "No principle parent exists: a record that states the "
             "principle (level principle, no envelope) plus one at "
             "topology/family level is the usual pair."),
        ],
        "operating_point": gap.get("operating_point") or {},
        "missing": gap.get("missing") or [],
        "reasons": gap.get("reasons") or [],
        "existing_knowledge": known,
        "prior_tasks": prior,
        "checklist": ({"id": cl.get("id"), "requires": cl.get("requires"),
                       "maturity": knowledgelib.record_maturity(cl)}
                      if cl else None),
        "policy": {"tiers": TIER_POLICY, "visual_reads": VISUAL_RULE,
                   "quarantine": ("Every source enters the workspace through "
                                  "research.py fetch ONLY (allowlisted https "
                                  "download or a local file registered "
                                  "against an allowlisted origin URL); "
                                  "records may cite only ledger files "
                                  "(research/sources/<file>) - validate "
                                  "refuses anything else."),
                   "maturity": ("Write status draft + maturity draft. The "
                                "second reader flips a record to verified; "
                                "the owner approves after promotion. Never "
                                "self-declare higher.")},
        "allowlist": [{"domain": e["domain"], "kind": e["kind"]}
                      for e in domains.get("domains") or []],
        "forum_hosts": list(domains.get("forum_hosts") or []),
        "caps": {"depth_per_gap": depth,
                 "sources_acquired": len(task.get("sources") or []),
                 "sources_remaining": max(depth - len(task.get("sources") or []), 0),
                 "attempts_used": len(task.get("attempts") or []),
                 "attempts_cap": ATTEMPT_FACTOR * depth},
        "record_schema": knowledgelib.RECORD_SCHEMA,
        "record_template": record_template(task),
        "checklist_schema": knowledgelib.CHECKLIST_SCHEMA,
        "checklist_template": (checklist_template(task)
                               if task["checklist_needed"] else None),
        "classes": sorted(knowledgelib.CLASSES),
        "levels": list(knowledgelib.LEVELS),
        "envelope_units": list(knowledgelib.ENVELOPE_UNITS),
        "paths": {"records": f"{knowledgelib.WS_RECORDS}/<id>.yaml",
                  "checklists": f"{knowledgelib.WS_CHECKLISTS}/<id>.yaml",
                  "sources": f"{knowledgelib.WS_RESEARCH}/sources/",
                  "task": f"{knowledgelib.WS_RESEARCH}/tasks/{task['id']}.json"},
        "commands": commands,
    }
    return payload


# ---------------------------------------------------------------------------
# fetch: allowlisted acquisition into quarantine
# ---------------------------------------------------------------------------
def http_transport(url: str, timeout: float = 60.0, _mock=None) -> dict:
    """Default transport: GET with redirects, browser-ish UA. Returns
    {status, final_url, hops[], content_type, server, cf_mitigated,
    retry_after, body}. Raises on transport
    failure (DNS/TLS/timeout) - the caller maps that to exit 2. `_mock` is
    an httpx transport for tests."""
    import httpx
    with httpx.Client(follow_redirects=True, timeout=timeout,
                      headers={"User-Agent": USER_AGENT},
                      transport=_mock) as client:
        r = client.get(url)
        hops = [str(h.url) for h in r.history] + [str(r.url)]
        # a short HTML page whose only job is to send the browser on (LCSC's
        # wmsc mirror answers 200 with one) is followed like an HTTP redirect;
        # its target is a hop, so the caller's allowlist check covers it
        for _ in range(STUB_MAX_HOPS):
            nxt = stub_redirect(r.content)
            if not nxt:
                break
            r = client.get(urljoin(str(r.url), nxt))
            hops += [str(h.url) for h in r.history] + [str(r.url)]
    return {"status": r.status_code, "final_url": str(r.url), "hops": hops,
            "content_type": r.headers.get("content-type", ""),
            "server": r.headers.get("server", ""),
            "cf_mitigated": r.headers.get("cf-mitigated", ""),
            "retry_after": r.headers.get("retry-after", ""),
            "body": r.content}


_STUB_TARGET = re.compile(
    rb"""(?:location(?:\.href)?\s*=\s*|location\.(?:replace|assign)\(\s*"""
    rb"""|http-equiv\s*=\s*["']?refresh["']?[^>]*?url\s*=\s*)"""
    rb"""["']?([^"'\s<>)]+)""", re.I)


def stub_redirect(body: bytes) -> str | None:
    """The URL a short HTML redirect stub sends the browser to (a script
    setting `location`, or a meta refresh), else None. A PDF or a body over
    STUB_MAX_BODY is never a stub. wmsc.lcsc.com/wmsc/upload/file/pdf/v2/...
    answers 200 text/html with `<script>window.top.location.href='https://
    datasheet.lcsc.com/datasheet/pdf/<hash>.pdf?productCode=C...'</script>`
    (140 bytes), and that target is the PDF."""
    body = body or b""
    if body.startswith(b"%PDF") or len(body) > STUB_MAX_BODY:
        return None
    m = _STUB_TARGET.search(body)
    if not m:
        return None
    target = m.group(1).decode("ascii", "replace")
    return target if target.lower().startswith(("https://", "http://", "/")) \
        else None


def host_block(resp: dict, url: str) -> dict | None:
    """{host, http_status, challenge} when the host REFUSED the fetch: HTTP
    401/403/429, a `cf-mitigated` header, or an Akamai/Cloudflare challenge
    page served in place of the document (any status; the weak markers only
    on an error status or a short body). None otherwise - a
    404 or a 500 is an ordinary failure, not a block."""
    status = int(resp.get("status") or 0)
    body = resp.get("body") or b""
    head = b"" if body.startswith(b"%PDF") else body[:8192].lower()
    challenge = next((v for m, v in _CHALLENGE_MARKERS if m in head), None)
    if challenge is None and (status >= 400 or len(body) < WEAK_MARKER_MAX_BODY):
        challenge = next((v for m, v in _WEAK_CHALLENGE_MARKERS if m in head),
                         None)
    server = str(resp.get("server") or "").lower()
    if resp.get("cf_mitigated"):
        challenge = challenge or "cloudflare"
    if status not in BLOCK_STATUSES and not challenge:
        return None
    if challenge is None:
        challenge = ("akamai" if "akamai" in server else
                     "cloudflare" if "cloudflare" in server else None)
    return {"host": urlsplit(resp.get("final_url") or url).hostname or "",
            "http_status": status, "challenge": challenge}


def fallback_parts(task: dict, part: str | None = None) -> list[str]:
    """Part numbers the distributor fallback may look up: an explicit --part
    first, then a part slot's own LCSC number and MPN."""
    out: list[str] = []
    gap = task.get("gap") or {}
    for k in (part, gap.get("lcsc"), gap.get("mpn")):
        k = str(k or "").strip()
        if k and k not in out:
            out.append(k)
    return out


def _lcsc_search(part: str) -> list[dict]:
    import partslib
    hits, _total = partslib.live_search(part, page_size=10)
    return [partslib.normalize(h) for h in hits]


def vendor_token(url: str, domains: dict | None = None) -> str | None:
    """The vendor a URL belongs to, as a lower-case brand token: the first
    word of its allowlist note ("Torex", "Littelfuse (TVS, fuses)"), else
    the domain's first label. None for a distributor/forum/standards host."""
    domains = domains or load_domains()
    ck = check_url(url, domains)
    e = ck.get("entry") or {}
    if not ck["ok"] or e.get("kind") != "vendor":
        return None
    note = next((d.get("note") for d in domains.get("domains") or []
                 if d.get("domain") == e["domain"]), None)
    words = re.findall(r"[a-z0-9]+", str(note or "").lower())
    return words[0] if words else e["domain"].split(".")[0]


def distributor_datasheets(parts: list[str], search=None, lookup=None,
                           vendor: str | None = None
                           ) -> tuple[list[dict], list[str]]:
    """Distributor-hosted datasheet copies for `parts` (LCSC numbers or MPNs):
    ([{source, part, mpn, brand, url}], notes). LCSC/JLC first (anonymous
    search, the wmsc PDF mirror), then DigiKey/Mouser by MPN when their
    credentials are present - a provider without credentials is skipped and
    named in `notes`, never raised. Only exact LCSC-number / MPN matches
    count (an MPN may carry a `-suffix`), and an MPN match must carry `vendor`'s brand when the vendor is
    known: an MPN search also returns second-source clones (UMW XC6206,
    Brightking SMBJ5.0A), whose datasheet is not the subject part's."""
    import distributors
    import partslib
    search = search or _lcsc_search
    lookup = lookup or distributors.lookup
    cands: list[dict] = []
    notes: list[str] = []
    mpns: list[str] = []

    def _add(c: dict) -> None:
        if c["url"] and c["url"] not in {x["url"] for x in cands}:
            cands.append(c)

    for p in parts:
        if not _LCSC_PN.match(p):
            mpns.append(p)
        try:
            hits = search(p)
        except Exception as exc:  # noqa: BLE001 - a lookup failure is a note
            notes.append(f"lcsc: lookup of {p} failed ({type(exc).__name__}: "
                         f"{exc})")
            continue
        want = p.upper()
        # an MPN also matches its packaging/RoHS suffix (LCSC lists Torex's
        # XC6206P332MR as XC6206P332MR-G)
        match = [h for h in hits or []
                 if want == str(h.get("lcsc") or "").upper()
                 or str(h.get("mpn") or "").upper() == want
                 or str(h.get("mpn") or "").upper().startswith(want + "-")]
        if not match:
            notes.append(f"lcsc: no exact match for {p} (or the JLC search "
                         "is unreachable)")
        if vendor and not _LCSC_PN.match(p):
            clones = [h for h in match
                      if vendor not in str(h.get("brand") or "").lower()]
            if clones:
                notes.append(f"lcsc: skipped {len(clones)} other-brand "
                             f"cop{'y' if len(clones) == 1 else 'ies'} of {p} "
                             "(" + ", ".join(sorted({str(h.get('brand'))
                                                     for h in clones}))
                             + f") - not {vendor}'s datasheet")
            match = [h for h in match if h not in clones]
        for h in match:
            if h.get("mpn") and h["mpn"] not in mpns:
                mpns.append(h["mpn"])
            url = partslib.fix_datasheet_url(h.get("datasheet") or "")
            if not url:
                notes.append(f"lcsc: {h.get('lcsc') or p} lists no datasheet")
            _add({"source": "lcsc", "part": p, "lcsc": h.get("lcsc"),
                  "mpn": h.get("mpn"), "brand": h.get("brand"), "url": url})
    if not mpns:
        notes.append("digikey/mouser: not tried - no MPN known")
    for m in mpns:
        try:
            res = lookup(m)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"distributors: lookup of {m} failed "
                         f"({type(exc).__name__}: {exc})")
            continue
        for prov, msg in sorted((res.get("missing") or {}).items()):
            note = f"{prov}: skipped - {msg}"
            if note not in notes:
                notes.append(note)
        for prov, r in sorted((res.get("results") or {}).items()):
            for h in r.get("hits") or []:
                if str(h.get("mpn") or "").upper() != m.upper():
                    continue
                if vendor and vendor not in str(
                        h.get("manufacturer") or "").lower():
                    continue
                url = str(h.get("datasheet_url") or "")
                if url.startswith("//"):
                    url = "https:" + url
                _add({"source": prov, "part": m, "mpn": h.get("mpn"),
                      "brand": h.get("manufacturer"), "url": url})
    return cands, notes


def blocked_hosts(task: dict) -> list[dict]:
    """The task's blocked_by_host attempts grouped by host:
    [{host, http_status[], challenge, urls}]."""
    by: dict[str, dict] = {}
    for a in task.get("attempts") or []:
        if a.get("kind") != "blocked_by_host":
            continue
        e = by.setdefault(a.get("host") or "", {
            "host": a.get("host") or "", "http_status": [],
            "challenge": None, "urls": 0})
        if a.get("http_status") not in e["http_status"]:
            e["http_status"].append(a.get("http_status"))
        e["challenge"] = e["challenge"] or a.get("challenge")
        e["urls"] += 1
    return [by[h] for h in sorted(by)]


def _blocked_text(blocked: list[dict]) -> str:
    return ", ".join(
        f"{b['host']} (HTTP {'/'.join(str(s) for s in b['http_status'])}"
        + (f", {b['challenge']} challenge" if b.get("challenge") else "")
        + ")" for b in blocked)


def _pdf_pages(data: bytes) -> int | None:
    try:
        from io import BytesIO
        from pypdf import PdfReader
        return len(PdfReader(BytesIO(data)).pages)
    except Exception:  # noqa: BLE001 - page count is best-effort metadata
        return None


def _source_name(final_url: str, data: bytes, expect: str) -> str:
    """Quarantine file name: the URL's basename (sanitized) with an extension
    that says what the bytes ARE; `source-<sha12>.<ext>` when the URL has no
    usable basename (a thread id, a query-only URL)."""
    base = Path(urlsplit(final_url).path).name
    base = _SAFE_NAME.sub("-", base).strip("-.")
    sha8 = sha256_bytes(data)[:12]
    head = data[:512].lower().lstrip()
    looks_html = head.startswith(b"<!doctype html") or head.startswith(b"<html")
    ext = "pdf" if data.startswith(b"%PDF") else (
        "html" if expect == "html" or looks_html else "bin")
    if not base or len(base) > 120:
        base = f"source-{sha8}.{ext}"
    elif "." not in base:
        base = f"{base}.{ext}"
    if data.startswith(b"%PDF") and not base.lower().endswith(".pdf"):
        base += ".pdf"
    return base


def depth_state(task: dict) -> dict:
    cap = int(task["caps"]["depth_per_gap"])
    used = len(task.get("sources") or [])
    attempts = len(task.get("attempts") or [])
    return {"cap": cap, "used": used, "remaining": max(cap - used, 0),
            "attempts": attempts,
            "attempts_cap": int(task["caps"].get("attempt_factor",
                                                 ATTEMPT_FACTOR)) * cap}


def _checkpoint(task: dict, which: str, detail: str) -> dict:
    """The visible cap-hit payload. The caller records it in state.json."""
    return {"status": "checkpoint", "checkpoint": which, "task": task["id"],
            "slot": task["slot"], "detail": detail, "depth": depth_state(task),
            "action": ("your call: if the slot still needs sources, raise "
                       "budgets.research in state.json (record why with "
                       "state.py decision), or close the task "
                       "with what was acquired (research.py close), or "
                       "abandon it (research.py close --abandon)")}


def fetch_source(root: Path, task: dict, url: str, tier: str,
                 about: str | None = None, expect: str = "pdf",
                 local_file: Path | str | None = None,
                 note: str | None = None, transport=None,
                 domains: dict | None = None, part: str | None = None,
                 resolver=None,
                 fallback_of: dict | None = None) -> tuple[dict, int]:
    """Acquire ONE source into quarantine under the task's ledger.

    Returns (payload, exit): 0 acquired (or de-duplicated against an
    identical file already in quarantine), 1 checkpoint (depth/attempt cap)
    or a content refusal (not a PDF when one was expected), 2 allowlist
    refusal / blocked by host / transport failure / bad arguments. Refusals
    and failures are recorded as attempts; only acquisitions consume depth.

    A host that refuses the fetch (host_block) is ledgered as
    `blocked_by_host`, then the distributor's hosted datasheet copy for
    `part` (or the part slot's LCSC/MPN) is tried through this same function
    (`resolver` = distributor_datasheets, injectable); the acquired entry
    names the source used under `fallback`."""
    if task.get("status") != "open":
        return {"status": "error", "error": f"task {task['id']} is "
                f"{task.get('status')} - fetch needs an open task"}, 2
    if expect not in EXPECTS:
        return {"status": "error",
                "error": f"--expect {expect!r} not in {EXPECTS}"}, 2
    domains = domains or load_domains()

    def _attempt(kind: str, detail: str, **extra) -> None:
        """Every refusal/failure is ledgered (audit + the attempts cap)."""
        task.setdefault("attempts", []).append(
            {"ts": now(), "url": url, "kind": kind, "detail": detail, **extra})
        write_task(root, task)

    ds = depth_state(task)
    if ds["remaining"] <= 0:
        return _checkpoint(task, "research_depth",
                           f"depth cap {ds['cap']} reached: {ds['used']} "
                           "source(s) acquired for this gap"), 1
    if ds["attempts"] >= ds["attempts_cap"]:
        return _checkpoint(task, "research_attempts",
                           f"{ds['attempts']} failed/refused fetch attempts "
                           f"(cap {ds['attempts_cap']}) - the search is not "
                           "converging"), 1
    ck = check_url(url, domains)
    if not ck["ok"]:
        _attempt("allowlist", ck["reason"] or "refused")
        return {"status": "error", "refused": "allowlist", "url": url,
                "host": ck["host"], "error": ck["reason"],
                "depth": depth_state(task)}, 2
    try:
        tier_eff = effective_tier(tier, ck["forced_tier"])
    except ValueError as exc:
        return {"status": "error", "error": str(exc)}, 2

    if local_file is not None:
        lp = Path(local_file)
        if not lp.is_file():
            return {"status": "error",
                    "error": f"--file {lp} does not exist"}, 2
        data = lp.read_bytes()
        resp = {"status": 200, "final_url": url, "hops": [url],
                "content_type": "", "body": data}
        via = "local"
    else:
        transport = transport or http_transport
        try:
            resp = transport(url)
        except Exception as exc:  # noqa: BLE001 - transport failure = exit 2
            _attempt("transport", f"{type(exc).__name__}: {exc}")
            return {"status": "error", "url": url,
                    "error": f"transport failure: {type(exc).__name__}: "
                             f"{exc}"}, 2
        via = "http"
        # every redirect hop must stay on the allowlist
        for hop in resp.get("hops") or [resp.get("final_url") or url]:
            hk = check_url(hop, domains)
            if not hk["ok"]:
                _attempt("redirect-off-list", hop)
                return {"status": "error", "refused": "allowlist-redirect",
                        "url": url, "hop": hop, "error":
                        f"redirect to {hop} refused: {hk['reason']}"}, 2
            if hk["forced_tier"]:
                tier_eff = effective_tier(tier_eff, hk["forced_tier"])
        blk = host_block(resp, url)
        if blk:
            _attempt("blocked_by_host",
                     f"HTTP {blk['http_status']} from {blk['host']}"
                     + (f" ({blk['challenge']} challenge)"
                        if blk["challenge"] else ""), **blk)
            fb_payload, fb = _distributor_fallback(
                root, task, url, blk, tier, about, expect, note, transport,
                domains, part, resolver, fallback_of)
            if fb_payload is not None:
                return fb_payload
            return {"status": "error", "refused": "blocked_by_host",
                    "url": url, **blk, "fallback": fb,
                    "error": (f"blocked by host: {blk['host']} answered HTTP "
                              f"{blk['http_status']}"
                              + (f" ({blk['challenge']} challenge page)"
                                 if blk["challenge"] else "")
                              + " - the host refused, the document was not "
                              "read; this is not 'no records found'"),
                    "depth": depth_state(task),
                    "next": ("try another allowlisted source; pass --part "
                             "<LCSC or MPN> to try the distributor's copy; "
                             "or register an owner-downloaded copy with "
                             "--file. A task whose every fetch was blocked "
                             "closes as `blocked` (research.py close).")}, 2
        if int(resp.get("status") or 0) >= 400:
            _attempt("http", f"HTTP {resp.get('status')}")
            return {"status": "error", "url": url,
                    "error": f"HTTP {resp.get('status')} for {url}"}, 2
    data: bytes = resp["body"]
    if len(data) > MAX_BYTES:
        _attempt("too-large", f"{len(data)} bytes")
        return {"status": "error", "error": f"{len(data)} bytes exceeds "
                f"MAX_BYTES {MAX_BYTES}"}, 2
    is_pdf = data.startswith(b"%PDF")
    if expect == "pdf" and not is_pdf:
        head = data[:16]
        hint = ""
        if "lcsc.com/datasheet/" in url:
            hint = (" - www.lcsc.com/datasheet URLs serve an HTML viewer "
                    "shell; use the wmsc.lcsc.com/wmsc/upload/file/pdf/v2/"
                    "lcsc/<stem>.pdf form (partslib.fix_datasheet_url)")
        elif data[:1] == b"<" and len(data) <= STUB_MAX_BODY:
            hint = (" - a short HTML page with no redirect this fetch "
                    "recognises (stub_redirect); read its body and add the "
                    "form it uses")
        _attempt("not-pdf", f"starts {head!r}")
        return {"status": "violations", "refused": "not_pdf", "url": url,
                "error": f"expected a PDF, got bytes starting {head!r}{hint}",
                "depth": depth_state(task)}, 1

    sha = sha256_bytes(data)
    sdir = sources_dir(root)
    sdir.mkdir(parents=True, exist_ok=True)
    name = _source_name(resp.get("final_url") or url, data, expect)
    dest = sdir / name
    if dest.exists() and sha256_bytes(dest.read_bytes()) != sha:
        stem, dot, ext = name.rpartition(".")
        name = f"{stem or ext}-{sha[:8]}{dot}{ext if stem else ''}"
        dest = sdir / name
    dedup = dest.exists()
    if not dedup:
        dest.write_bytes(data)
    entry = {
        "n": len(task.get("sources") or []) + 1,
        "url": url, "final_url": resp.get("final_url") or url,
        "host": ck["host"], "domain": ck["entry"]["domain"],
        "domain_kind": ck["entry"]["kind"],
        "tier": tier_eff, "declared_tier": tier, "about": about,
        "file": source_ref(name), "sha256": sha, "bytes": len(data),
        "content_type": (resp.get("content_type") or
                         ("application/pdf" if is_pdf else "")),
        "pages": _pdf_pages(data) if is_pdf else None,
        "fetched": now(), "via": via, "note": note,
        "deduplicated": dedup, "fallback": fallback_of,
    }
    task.setdefault("sources", []).append(entry)
    write_task(root, task)
    return {"status": "pass", "task": task["id"], "source": entry,
            "depth": depth_state(task),
            "next": ("Read the cited pages VISUALLY, then write research/"
                     "records/<id>.yaml citing " + entry["file"] +
                     " by page + note; research.py validate")}, 0


def _distributor_fallback(root, task, url, blk, tier, about, expect, note,
                          transport, domains, part, resolver, fallback_of):
    """After a host block: fetch the first distributor-hosted copy that
    lands. Returns ((payload, exit) | None, info{parts, tried[], notes[]})."""
    info: dict = {"parts": [], "tried": [], "notes": []}
    if fallback_of is not None:          # already a fallback - no recursion
        info["notes"].append("this fetch was itself the fallback")
        return None, info
    info["parts"] = fallback_parts(task, part)
    if not info["parts"]:
        info["notes"].append("no part number to look up: pass --part <LCSC "
                             "number or MPN> to try the distributor copy")
        return None, info
    cands, notes = (resolver or distributor_datasheets)(
        info["parts"], vendor=vendor_token(url, domains))
    info["notes"] += notes
    for c in cands[:FALLBACK_TRIES]:
        if c["url"] == url:
            continue
        pl, code = fetch_source(
            root, task, c["url"], tier, about=about, expect=expect, note=note,
            transport=transport, domains=domains,
            fallback_of={"source": c["source"], "part": c["part"],
                         "mpn": c.get("mpn"), "brand": c.get("brand"),
                         "for_url": url,
                         "blocked": blk})
        info["tried"].append({"source": c["source"], "url": c["url"],
                              "status": pl.get("status"),
                              "error": pl.get("error")})
        if code == 0:
            pl.update({"blocked": blk, "fallback": info,
                       "source_used": c["source"]})
            return (pl, code), info
        if pl.get("status") == "checkpoint":
            pl.update({"blocked": blk, "fallback": info})
            return (pl, code), info
    if not cands:
        info["notes"].append("no distributor-hosted datasheet found")
    return None, info


# ---------------------------------------------------------------------------
# records: load / targeted edits / the second reader's verdict
# ---------------------------------------------------------------------------
def task_records(root: Path, task: dict) -> list[tuple[Path, dict | None]]:
    """(path, parsed-or-None) for every record file whose origin binds it to
    this task. Unparseable YAML in the records dir is reported as
    (path, None) for EVERY task (nobody can claim it)."""
    out = []
    d = records_dir(root)
    for p in sorted(d.glob("*.yaml")) if d.is_dir() else []:
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            out.append((p, None))
            continue
        if isinstance(data, dict) and data.get("origin") == f"research:{task['id']}":
            out.append((p, data))
    return out


def record_is_draft(data: dict) -> bool:
    """A research record that is NOT usable knowledge: the second reader has
    not verified it (never ruled, or ruled refuted - both leave it draft).
    Checklists are excluded by the caller: a DRAFT checklist is a normal,
    promotable research output, an unverified record is not.

    Keyed on MATURITY alone, the same bar `close_task` and
    `knowledgelib.record_draft_unverified` use - `verify` sets maturity and
    status together and `validate` refuses them apart, so a record that
    carries one without the other is hand-edited damage and the sweep is
    exactly where it should surface, not slip through."""
    return knowledgelib.record_maturity(data) != "verified"


def draft_sweep(ws: Path | str) -> dict:
    """Every research RECORD in the workspace that is still draft, with the
    task that owns it and that task's status (U21).

    The per-task close barrier is a snapshot of one task's own records; it
    cannot see a record that lands after that task closed, or one whose
    `origin` names a task that closed already (bb-amp's miss: five records
    bound to `interface-in-1`, which had closed 20:23:47 holding a single
    verdict). This sweep is workspace-wide and status-blind, so a draft can
    never be hidden behind a closed task again.

    Rows carry `state`:
      unruled   the owning task holds no verdict for it
      refuted   the second reader read it and refuted it
      orphaned  no task file with that id exists at all
    and `stalled` = True when the owning task is closed/missing (nobody is
    coming back for it) - that is the silent-failure set."""
    ws = Path(ws)
    root = root_of(ws)
    tasks = {t["id"]: t for t in list_tasks(root)}
    rows = []
    d = records_dir(root)
    for p in sorted(d.glob("*.yaml")) if d.is_dir() else []:
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            continue
        if not isinstance(data, dict):
            continue
        origin = str(data.get("origin") or "")
        if not origin.startswith("research:"):
            continue
        if not record_is_draft(data):
            continue
        tid = origin.split(":", 1)[1]
        task = tasks.get(tid)
        verdict = ((task or {}).get("verdicts") or {}).get(
            str(data.get("id") or p.stem), {}).get("verdict")
        if task is None:
            state, tstatus = "orphaned", None
        elif verdict == "refuted":
            state, tstatus = "refuted", task.get("status")
        else:
            state, tstatus = "unruled", task.get("status")
        rows.append({
            "id": str(data.get("id") or p.stem),
            "file": f"{knowledgelib.WS_RECORDS}/{p.name}",
            "task": tid, "task_status": tstatus, "state": state,
            "classes": sorted({knowledgelib.norm_token(c)
                               for c in data.get("classes") or []}),
            "applies": data.get("applies") or {},
            "stalled": task is None or task.get("status") != "open",
        })
    stalled = [r for r in rows if r["stalled"]]
    return {"workspace": ws.as_posix(), "drafts": rows, "stalled": stalled,
            "counts": {"drafts": len(rows), "stalled": len(stalled),
                       "unruled": sum(1 for r in rows if r["state"] == "unruled"),
                       "refuted": sum(1 for r in rows if r["state"] == "refuted"),
                       "orphaned": sum(1 for r in rows if r["state"] == "orphaned")}}


def task_record_counts(root: Path, task: dict) -> dict:
    """verified / refuted / unruled / draft for ONE task's own records."""
    verdicts = task.get("verdicts") or {}
    out = {"records": 0, "verified": 0, "refuted": 0, "unruled": 0, "draft": 0}
    for p, data in task_records(root, task):
        if data is None:
            continue
        out["records"] += 1
        rid = str(data.get("id") or p.stem)
        v = verdicts.get(rid, {}).get("verdict")
        if v == "verified":
            out["verified"] += 1
        elif v == "refuted":
            out["refuted"] += 1
        else:
            out["unruled"] += 1
        if record_is_draft(data):
            out["draft"] += 1
    return out


def task_checklists(root: Path, task: dict) -> list[tuple[Path, dict | None]]:
    out = []
    d = checklists_dir(root)
    for p in sorted(d.glob("*.yaml")) if d.is_dir() else []:
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            out.append((p, None))
            continue
        if isinstance(data, dict) and data.get("origin") == f"research:{task['id']}":
            out.append((p, data))
    return out


def _drop_top_key(text: str, key: str) -> str:
    """Remove a top-level key line and its indented continuation lines
    (targeted edit; blank lines end the block)."""
    lines = text.splitlines()
    out = []
    i = 0
    while i < len(lines):
        if re.match(rf"^{re.escape(key)}:", lines[i]):
            i += 1
            while i < len(lines) and lines[i][:1] in (" ", "\t"):
                i += 1
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out) + "\n"


def _flow(d: dict) -> str:
    return "{" + ", ".join(f"{k}: {json.dumps(str(v))}" for k, v in d.items()
                           if v is not None) + "}"


def verify_record(root: Path, task: dict, record_id: str, verdict: str,
                  by: str = SECOND_READER, note: str | None = None,
                  date: str | None = None) -> tuple[dict, int]:
    """Record the second reader's verdict on ONE record. verified: maturity
    verified + status active + a `verification` block; refuted: back to
    status draft / maturity draft, verification dropped. The task ledger
    keeps every verdict (a refuted record may be rewritten and re-read).
    Targeted line edits - the researcher's prose is left untouched."""
    if verdict not in VERDICTS:
        return {"status": "error",
                "error": f"verdict {verdict!r} not in {VERDICTS}"}, 2
    if not note or len(note.strip()) < SOURCE_NOTE_MIN:
        return {"status": "error",
                "error": "a verdict needs a --note that says what was re-read "
                         f"and found (>= {SOURCE_NOTE_MIN} chars)"}, 2
    if not (note.isascii() and by.isascii()):
        return {"status": "error", "error": "--by/--note must be ASCII"}, 2
    date = date or today()
    p = records_dir(root) / f"{record_id}.yaml"
    if not p.is_file():
        return {"status": "error",
                "error": f"no record {record_id!r} under "
                         f"{records_dir(root).as_posix()}"}, 2
    text = p.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return {"status": "error", "error": f"{p.name}: invalid YAML ({exc})"}, 2
    if not isinstance(data, dict) or data.get("origin") != f"research:{task['id']}":
        return {"status": "error",
                "error": f"{p.name} does not belong to task {task['id']} "
                         f"(origin {data.get('origin') if isinstance(data, dict) else None!r})"}, 2
    new = text
    if verdict == "verified":
        new = knowledgelib._set_top_key(new, "maturity", "verified")
        new = knowledgelib._set_top_key(new, "status", "active")
        new = _drop_top_key(new, "verification")
        new = knowledgelib._set_top_key(
            new, "verification", _flow({"by": by, "date": date, "note": note}))
    else:
        new = knowledgelib._set_top_key(new, "maturity", "draft")
        new = knowledgelib._set_top_key(new, "status", "draft")
        new = _drop_top_key(new, "verification")
    p.write_text(new, encoding="utf-8", newline="\n")
    try:
        yaml.safe_load(new)
    except yaml.YAMLError as exc:      # restore - never leave a broken record
        p.write_text(text, encoding="utf-8", newline="\n")
        return {"status": "error",
                "error": f"edit would break {p.name} ({exc}); restored"}, 2
    task.setdefault("verdicts", {})[record_id] = {
        "verdict": verdict, "by": by, "date": date, "note": note, "ts": now()}
    write_task(root, task)
    return {"status": "pass", "task": task["id"], "record": record_id,
            "verdict": verdict, "file": p.as_posix()}, 0


# ---------------------------------------------------------------------------
# validate: schema v2 + the research contract
# ---------------------------------------------------------------------------
def _slot_keyed(task: dict, applies: dict) -> bool:
    kind, tok = task["kind"], task["token"]
    ap = applies if isinstance(applies, dict) else {}
    if kind == "block":
        return knowledgelib.norm_token(tok) in {
            knowledgelib.norm_token(t) for t in ap.get("topologies") or []}
    if kind == "interface":
        return knowledgelib.norm_token(tok) in {
            knowledgelib.norm_token(t) for t in ap.get("interfaces") or []}
    gap = task["gap"]
    keys = {knowledgelib.norm_pkg(k) for k in (gap.get("mpn"), gap.get("lcsc"))
            if k}
    pk = {knowledgelib.norm_pkg(gap["package"])} if gap.get("package") else set()
    return bool(keys & {knowledgelib.norm_pkg(x) for x in ap.get("parts") or []}
                or pk & {knowledgelib.norm_pkg(x) for x in ap.get("packages") or []})


def validate_task(root: Path, task: dict,
                  library_ids: set[str] | None = None,
                  library_records: list[dict] | None = None) -> dict:
    """{problems, warnings, records, checklists}. problems empty = the task's
    outputs are schema-v2 strict-clean AND honour the research contract
    (provenance, page+note citations, tier policy, envelope_note, maturity
    governance, slot keying, no id clash with the library)."""
    root = Path(root)
    ws = root.parent
    problems: list[str] = []
    warnings: list[str] = []
    if library_records is None and library_ids is None:
        library_records = knowledgelib.load_records()
    if library_ids is None:
        library_ids = {r.get("id") for r in library_records or []} | \
                      {c.get("id") for c in knowledgelib.load_checklists()}
    recs = task_records(root, task)
    cls = task_checklists(root, task)
    tag = f"research:{task['id']}"
    ledger = {s["file"]: s for s in task.get("sources") or []}
    missing_classes = {knowledgelib.norm_token(m["class"])
                       for m in task["gap"].get("missing") or []
                       if m.get("class") != CHECKLIST_CLASS}

    # schema lint over the whole workspace dirs; keep the lines that concern
    # this task's files (or nobody's - unparseable files block everyone)
    mine = {p.name for p, _ in recs} | {f"checklists/{p.name}" for p, _ in cls}
    others = set()
    for p in (records_dir(root).glob("*.yaml") if records_dir(root).is_dir() else []):
        if p.name in mine:
            continue
        try:
            d = yaml.safe_load(p.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            continue          # unparseable = nobody's = reported
        if isinstance(d, dict) and str(d.get("origin", "")).startswith("research:"):
            others.add(p.name)
    for p in (checklists_dir(root).glob("*.yaml") if checklists_dir(root).is_dir() else []):
        if f"checklists/{p.name}" in mine:
            continue
        try:
            d = yaml.safe_load(p.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            continue
        if isinstance(d, dict) and str(d.get("origin", "")).startswith("research:"):
            others.add(f"checklists/{p.name}")
    if records_dir(root).is_dir() or checklists_dir(root).is_dir():
        # the library's records count as generalizes targets (a research
        # record points at its principle parent there)
        lint = knowledgelib.validate(
            records_dir(root), checklists_dir(root), strict=True,
            source_roots=[ws],
            extra_records=library_records if library_records is not None
            else knowledgelib.load_records())
        for ln in lint:
            head = ln.split(":", 1)[0]
            if head in others:
                continue
            problems.append(f"lint: {ln}")

    rec_rows = []
    for p, data in recs:
        if data is None:
            problems.append(f"{p.name}: invalid YAML")
            continue
        rid = str(data.get("id") or p.stem)
        where = p.name
        row = {"id": rid, "file": p.as_posix(),
               "status": data.get("status"),
               "maturity": knowledgelib.record_maturity(data),
               "level": knowledgelib.record_level(data),
               "classes": data.get("classes") or [],
               "sources": len(data.get("sources") or []),
               "verdict": (task.get("verdicts") or {}).get(rid, {}).get("verdict")}
        rec_rows.append(row)
        if rid in library_ids:
            problems.append(f"{where}: id {rid!r} already exists in the "
                            "library - pick a new id (promotion would clash)")
        if not _slot_keyed(task, data.get("applies") or {}):
            problems.append(f"{where}: applies does not key slot "
                            f"{task['slot']!r} - coverage could never "
                            "retrieve it for this gap")
        rc = {knowledgelib.norm_token(c) for c in data.get("classes") or []}
        if missing_classes and not (rc & missing_classes):
            problems.append(f"{where}: classes {sorted(rc)} cover none of the "
                            f"gap's missing classes {sorted(missing_classes)}")
        mat = knowledgelib.record_maturity(data)
        st = data.get("status")
        if mat in ("approved", "proven"):
            problems.append(f"{where}: maturity {mat} cannot be self-declared "
                            "by research (owner approval / bring-up evidence "
                            "only, after promotion)")
        elif mat == "verified":
            if st != "active":
                problems.append(f"{where}: verified records are status active "
                                f"(is {st!r}) - research.py verify sets both")
            if not isinstance(data.get("verification"), dict):
                problems.append(f"{where}: verified without a verification "
                                "block (only research.py verify may set it)")
        else:   # draft
            if st != "draft":
                problems.append(f"{where}: unverified records are status draft "
                                f"(is {st!r}) - only the second reader's "
                                "verdict activates a record")
            if data.get("verification"):
                problems.append(f"{where}: draft record carries a verification "
                                "block")
        if data.get("approval") or data.get("evidence"):
            problems.append(f"{where}: approval/evidence blocks are not "
                            "research outputs")
        env = data.get("envelope")
        if env and len(str(data.get("envelope_note") or "").strip()) < ENVELOPE_NOTE_MIN:
            problems.append(f"{where}: envelope without an envelope_note "
                            "(what does this rule scale with, and why these "
                            "bounds?)")
        srcs = data.get("sources") or []
        tiers = []
        for i, s in enumerate(srcs):
            if not isinstance(s, dict):
                continue
            f = s.get("file", "")
            led = ledger.get(f)
            if led is None:
                problems.append(f"{where}: sources[{i}].file {f!r} is not in "
                                f"task {task['id']}'s ledger - only research.py "
                                "fetch acquisitions may be cited")
                continue
            tiers.append(led.get("tier"))
            if not isinstance(s.get("page"), int):
                problems.append(f"{where}: sources[{i}] has no page - cite the "
                                "page you READ")
            elif led.get("pages") and s["page"] > int(led["pages"]):
                problems.append(f"{where}: sources[{i}].page {s['page']} > "
                                f"{led['pages']} pages in {f}")
            if len(str(s.get("note") or "").strip()) < SOURCE_NOTE_MIN:
                problems.append(f"{where}: sources[{i}] needs a note saying "
                                "what was READ on that page (figure/table + "
                                "claim)")
        if srcs and tiers and all(t == "forum" for t in tiers):
            problems.append(f"{where}: forum is the SOLE source tier - a forum "
                            "may corroborate, never carry a record alone")
        row["tiers"] = tiers

    cl_rows = []
    for p, data in cls:
        if data is None:
            problems.append(f"checklists/{p.name}: invalid YAML")
            continue
        cid = str(data.get("id") or p.stem)
        where = f"checklists/{p.name}"
        cl_rows.append({"id": cid, "file": p.as_posix(),
                        "maturity": knowledgelib.record_maturity(data),
                        "requires": len(data.get("requires") or [])})
        if cid in library_ids:
            problems.append(f"{where}: id {cid!r} already exists in the "
                            "library")
        cmat = knowledgelib.record_maturity(data)
        if cmat == "verified":
            why = knowledgelib.checklist_verification_problem(data)
            if why:
                problems.append(f"{where}: maturity verified but {why}")
        elif cmat != "draft":
            problems.append(f"{where}: a research checklist is maturity draft "
                            "until an independent verifier rules it "
                            "(checklist_verify.py)")
        ap = data.get("applies") or {}
        field = "topologies" if task["kind"] == "block" else "interfaces"
        if knowledgelib.norm_token(task["token"]) not in {
                knowledgelib.norm_token(t) for t in ap.get(field) or []}:
            problems.append(f"{where}: applies.{field} does not name "
                            f"{task['token']!r}")
        for i, s in enumerate(data.get("sources") or []):
            if isinstance(s, dict) and s.get("file") not in ledger:
                problems.append(f"{where}: sources[{i}].file "
                                f"{s.get('file')!r} is not in the task ledger")
    if task.get("checklist_needed") and not cls:
        problems.append(f"task {task['id']}: the gap has no coverage checklist "
                        f"- write {knowledgelib.WS_CHECKLISTS}/"
                        f"{sanitize_token(task['token'])}.yaml (origin "
                        f"{tag}) before or with the records")
    if cls and not task.get("checklist_needed"):
        problems.append(f"task {task['id']}: the slot already has a coverage "
                        "checklist - a research pass fills its classes, it "
                        "does not write a second checklist (widen the "
                        "library's through the owner)")
    if not recs and not cls:
        warnings.append(f"task {task['id']}: no records yet (origin {tag})")
    if task.get("sources") and not recs:
        warnings.append(f"task {task['id']}: {len(task['sources'])} source(s) "
                        "acquired, no records written")
    return {"problems": problems, "warnings": warnings, "records": rec_rows,
            "checklists": cl_rows, "depth": depth_state(task)}


# ---------------------------------------------------------------------------
# close: the promotion-queue entry (U6)
# ---------------------------------------------------------------------------
def _ws_rel(ws: Path) -> str | None:
    try:
        return Path(ws).resolve().relative_to(REPO).as_posix()
    except ValueError:
        return None


LEARNINGS_PREAMBLE = """# LEARNINGS - {board} (workspace learnings)

Workspace-local. Every entry gets a date, tags (stage tag first: P0-P10), and a
one-line claim as its title. `learnings.py compile --workspace {ws}` turns new
entries into `learnings/queue.yaml`; the `promote` verb rules on them.
Research tasks (research.py close) append their entries here automatically.
"""


def learnings_entry(task: dict, ws: Path, report: dict) -> str:
    """The workspace LEARNINGS.md entry for a closed task - the shape learnlib
    parses (`## date [stage][tags] claim`), body naming the record files
    (repo-relative when the workspace is in-repo, so compile picks them up
    as targets) and the promotion commands."""
    ws_rel = _ws_rel(ws)
    prefix = f"{ws_rel}/" if ws_rel else ""
    stage = task.get("phase") or "P1"
    if not re.fullmatch(r"P(?:10|[0-9])", stage):
        stage = "P1"
    verified = [r for r in report["records"] if r.get("verdict") == "verified"]
    refuted = [r for r in report["records"] if r.get("verdict") == "refuted"]
    n = len(verified)
    title = (f"research task {task['id']}: {n} verified record(s) for "
             f"{task['slot']}")
    lines = [f"## {today()} [{stage}][research][knowledge][{task['slot']}] {title}",
             f"Gap: {task['gap'].get('task') or task['slot']}",
             f"Operating point: {json.dumps(task['gap'].get('operating_point') or {}, sort_keys=True)}",
             f"Missing classes: {', '.join(m['class'] for m in task['gap'].get('missing') or [])}",
             ""]
    if verified:
        lines.append("Verified records (second reader signed). Promotion = the "
                     "research verb's promote step (copies record + sources "
                     "into the library), then the queue ruling with kind "
                     "knowledge_record targeting reference/knowledge/records/"
                     "<id>.yaml, then the owner's approval block:")
        for r in verified:
            lines.append(f"- {r['id']} [{', '.join(r['classes'])}] "
                         f"{prefix}{knowledgelib.WS_RECORDS}/{r['id']}.yaml")
    if refuted:
        lines.append("Refuted (left draft, not promotable):")
        for r in refuted:
            v = (task.get("verdicts") or {}).get(r["id"], {})
            lines.append(f"- {r['id']}: {v.get('note', '')}")
    if report["checklists"]:
        lines.append("Coverage checklist(s) for an independent verifier "
                     "(checklist_verify.py packet / rule):")
        for c in report["checklists"]:
            lines.append(f"- {c['id']} {prefix}{knowledgelib.WS_CHECKLISTS}/"
                         f"{c['id']}.yaml ({c['requires']} classes)")
    lines.append("Sources (quarantined, sha-pinned in the task ledger):")
    for s in task.get("sources") or []:
        lines.append(f"- {s['file']} tier {s['tier']} sha256 {s['sha256'][:12]} "
                     f"<{s['url']}>")
    lines.append(f"Task file: {prefix}{knowledgelib.WS_RESEARCH}/tasks/{task['id']}.json")
    return "\n".join(lines) + "\n"


def close_task(root: Path, task: dict, abandon: bool = False,
               reason: str | None = None,
               library_ids: set[str] | None = None,
               accept_drafts: bool = False) -> tuple[dict, int]:
    """Close a task. Normal close requires validate clean + every record
    VERIFIED (U21 - a verdict is not enough: refuted leaves the record draft
    and draft never injects); it appends the LEARNINGS entry and compiles the
    queue. `accept_drafts` closes over the drafts anyway and names them in the
    payload so the caller can record the decision.
    --abandon closes with a reason and no queue entry (a cap-hit task the
    owner chose not to extend, a slot that turned out not to need
    research).
    A task that acquired NOTHING and was refused by a host at least once
    closes as `blocked` (on --abandon, or on a plain close with no records
    and no checklist): the slot's gap is named with the host and status
    instead of reading as an empty research pass."""
    import learnlib
    root = Path(root)
    ws = root.parent
    if task.get("status") != "open":
        return {"status": "error",
                "error": f"task {task['id']} is already {task.get('status')}"}, 2
    blocked = [] if task.get("sources") else blocked_hosts(task)
    if blocked and (abandon or not (task_records(root, task)
                                    or task_checklists(root, task))):
        text = _blocked_text(blocked)
        reason = (reason or "").strip() or (
            f"every fetch was refused by the host: {text}")
        task.update({"status": "closed", "outcome": "blocked",
                     "closed": now(), "reason": reason,
                     "blocked_by": blocked})
        write_task(root, task)
        return {"status": "pass", "task": task["id"], "slot": task["slot"],
                "outcome": "blocked", "blocked_by": blocked,
                "reason": reason,
                "next": (f"the slot stays a coverage gap, named as blocked "
                         f"by {text} in knowledge.py --coverage "
                         "(research_blocked). Get the document another way "
                         "(an owner download registered with research.py "
                         "fetch --file) or accept the gap as a state "
                         "decision.")}, 0
    if abandon:
        if not reason or len(reason.strip()) < SOURCE_NOTE_MIN:
            return {"status": "error",
                    "error": "--abandon needs a --reason (>= 12 chars)"}, 2
        task.update({"status": "closed", "outcome": "abandoned",
                     "closed": now(), "reason": reason})
        write_task(root, task)
        return {"status": "pass", "task": task["id"], "outcome": "abandoned",
                "reason": reason}, 0
    report = validate_task(root, task, library_ids)
    if report["problems"]:
        return {"status": "violations", "task": task["id"],
                "error": "validate is not clean - fix before closing",
                **report}, 1
    # U21: the barrier is the record's STATE, not the presence of a verdict.
    # A refuted record stays draft and never injects, so closing over one
    # ships a board designed without that knowledge and says nothing. Drafts
    # in the CHECKLISTS dir are fine - a draft checklist is promotable.
    unruled = [r["id"] for r in report["records"] if not r.get("verdict")]
    refuted = [r["id"] for r in report["records"]
               if r.get("verdict") == "refuted"]
    drafts = [r["id"] for r in report["records"] if r.get("maturity") != "verified"]
    if drafts and not accept_drafts:
        return {"status": "violations", "task": task["id"],
                "error": (f"{len(drafts)} record(s) are still draft - "
                          "unverified research never injects, so closing here "
                          "designs the board without them: "
                          + ", ".join(drafts)),
                "drafts": drafts, "unruled": unruled, "refuted": refuted,
                "remedy": ("re-read and rule each with research.py verify, "
                           "rewrite and re-read a refuted one, or accept the "
                           "loss explicitly with --accept-drafts (recorded as "
                           "a state decision)"),
                **report}, 1
    if not report["records"] and not report["checklists"]:
        return {"status": "violations", "task": task["id"],
                "error": "nothing to close - no records and no checklist "
                         "(research.py close --abandon --reason ... if the "
                         "gap does not need research)", **report}, 1
    lp = learnlib.learnings_path(ws)
    if not lp.is_file():
        lp.parent.mkdir(parents=True, exist_ok=True)
        lp.write_text(LEARNINGS_PREAMBLE.format(board=ws.name,
                                                ws=ws.as_posix()),
                      encoding="utf-8", newline="\n")
    entry = learnings_entry(task, ws, report)
    text = lp.read_text(encoding="utf-8")
    if not text.endswith("\n"):
        text += "\n"
    lp.write_text(text + "\n" + entry, encoding="utf-8", newline="\n")
    queue, qrep = learnlib.compile_queue(ws)
    learnlib.save_queue(ws, queue)
    heading = entry.splitlines()[0]
    m = learnlib.ENTRY_RE.match(heading)
    slug = f"{m.group(1)}-{learnlib.slug(m.group(3))}" if m else None
    qid = next((e["entry"] for e in reversed(queue["entries"])
                if slug and e["entry"].startswith(slug)), slug)
    task.update({"status": "closed",
                 "outcome": "verified_with_drafts" if drafts else "verified",
                 "closed": now(), "queue_entry": qid,
                 "accepted_drafts": drafts or None,
                 "summary": {"verified": sum(1 for r in report["records"]
                                             if r.get("verdict") == "verified"),
                             "refuted": sum(1 for r in report["records"]
                                            if r.get("verdict") == "refuted"),
                             "checklists": len(report["checklists"]),
                             "sources": len(task.get("sources") or [])}})
    write_task(root, task)
    return {"status": "pass", "task": task["id"],
            "outcome": task["outcome"], "accepted_drafts": drafts,
            "queue_entry": qid, "learnings": lp.as_posix(),
            "queue": learnlib.queue_path(ws).as_posix(),
            "compile": qrep, "summary": task["summary"],
            "next": ("re-run knowledge.py --coverage: the slot now reads "
                     "provisional (researched, unapproved); promote verified "
                     "records with research.py promote when the owner rules")
            }, 0


# ---------------------------------------------------------------------------
# promote: verified research record -> the library (owner approves there)
# ---------------------------------------------------------------------------
def promote_record(root: Path, record_id: str,
                   lib_records_dir: Path | str | None = None,
                   lib_sources_dir: Path | str | None = None,
                   lib_checklists_dir: Path | str | None = None,
                   dry_run: bool = False) -> tuple[dict, int]:
    """Copy ONE verified record (or a draft checklist) into the library:
    quarantined sources are copied to the library sources dir, the record's
    `sources[].file` paths rewritten to the library form, the record text
    otherwise untouched; the library is re-linted and the copy removed on
    any problem. Maturity stays verified - `approved` is the owner's edit."""
    import shutil
    root = Path(root)
    ws = root.parent
    lrd = Path(lib_records_dir) if lib_records_dir else knowledgelib.RECORDS_DIR
    lsd = Path(lib_sources_dir) if lib_sources_dir else knowledgelib.SOURCES_DIR
    lcd = Path(lib_checklists_dir) if lib_checklists_dir else knowledgelib.CHECKLISTS_DIR
    src = records_dir(root) / f"{record_id}.yaml"
    is_checklist = False
    if not src.is_file():
        src = checklists_dir(root) / f"{record_id}.yaml"
        is_checklist = True
    if not src.is_file():
        return {"status": "error",
                "error": f"no research record or checklist {record_id!r} "
                         f"under {root.as_posix()}"}, 2
    text = src.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return {"status": "error", "error": f"{src.name}: invalid YAML ({exc})"}, 2
    if not isinstance(data, dict):
        return {"status": "error", "error": f"{src.name}: not a mapping"}, 2
    if not is_checklist and knowledgelib.record_maturity(data) != "verified":
        return {"status": "violations", "record": record_id,
                "error": f"maturity {knowledgelib.record_maturity(data)!r} - "
                         "only VERIFIED records promote (the second reader "
                         "must sign first)"}, 1
    dest = (lcd if is_checklist else lrd) / f"{record_id}.yaml"
    if dest.exists():
        return {"status": "error",
                "error": f"{dest.as_posix()} already exists"}, 2
    # citation form after promotion: skill-relative for the real library
    # (reference/knowledge/sources/<name>); an override dir (tests) is cited
    # by its absolute posix path, which _source_exists resolves as-is
    try:
        lib_prefix = lsd.resolve().relative_to(SKILL.resolve()).as_posix()
    except ValueError:
        lib_prefix = lsd.resolve().as_posix()
    copies = []
    new_text = text
    for s in data.get("sources") or []:
        if not isinstance(s, dict):
            continue
        f = str(s.get("file") or "")
        if not f.startswith(f"{knowledgelib.WS_RESEARCH}/sources/"):
            continue      # already a library/repo path - leave it
        name = Path(f).name
        sp = ws / f
        side = ""
        if not sp.is_file():
            # A source that may not be redistributed lives as its sidecar
            # (url + sha256, knowledgelib.NOT_REDISTRIBUTED_SUFFIX): the
            # sidecar travels to the library in place of the bytes and the
            # citation keeps naming the source itself.
            sc = sp.with_name(name + knowledgelib.NOT_REDISTRIBUTED_SUFFIX)
            if not sc.is_file():
                return {"status": "error",
                        "error": f"cited source {f} missing from the workspace"}, 2
            sp, side = sc, knowledgelib.NOT_REDISTRIBUTED_SUFFIX
        target = lsd / (name + side)
        if target.exists() and knowledgelib.sha256_file(target) != \
                knowledgelib.sha256_file(sp):
            stem, dot, ext = name.rpartition(".")
            name = f"{stem or ext}-{knowledgelib.sha256_file(sp)[:8]}{dot}{ext if stem else ''}"
            target = lsd / (name + side)
        copies.append((sp, target))
        new_text = new_text.replace(f, f"{lib_prefix}/{name}")
    if dry_run:
        return {"status": "pass", "dry_run": True, "record": record_id,
                "dest": dest.as_posix(),
                "sources": [{"from": a.as_posix(), "to": b.as_posix()}
                            for a, b in copies]}, 0
    lsd.mkdir(parents=True, exist_ok=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    written = []
    for a, b in copies:
        if not b.exists():
            shutil.copy2(a, b)
            written.append(b)
    dest.write_text(new_text, encoding="utf-8", newline="\n")
    probs = knowledgelib.validate(lrd, lcd, strict=True)
    if probs:
        dest.unlink()
        for b in written:
            b.unlink()
        return {"status": "violations", "record": record_id,
                "error": "library lint failed after the copy - copy removed",
                "problems": probs}, 1
    return {"status": "pass", "record": record_id, "kind":
            "checklist" if is_checklist else "record",
            "dest": dest.as_posix(),
            "sources": [{"from": a.as_posix(), "to": b.as_posix()}
                        for a, b in copies],
            "next": (f"learnings.py resolve --workspace {ws.as_posix()} "
                     f"--entry <queue id> --status promoted --kind "
                     f"knowledge_record --level L0 --targets "
                     f"{dest.relative_to(SKILL).as_posix() if dest.is_relative_to(SKILL) else dest.as_posix()} "
                     "--reason <why>; then the owner rules maturity "
                     "(approval block) and any topology view is re-rendered")
            }, 0


# ---------------------------------------------------------------------------
# assess: the research-quality scorer (bench stage P1)
# ---------------------------------------------------------------------------
def assess(root: Path, task_id: str, library_ids: set[str] | None = None) -> dict:
    """Mechanical research-quality metrics for one task under a research
    root (a workspace's research/ dir or a frozen bench copy of it):
    records, verdicts, lint problems, citation discipline, ledger provenance,
    tier policy. Penalties are the bench P1 terms (benchlib.WEIGHTS)."""
    root = Path(root)
    task = load_task(root, task_id)
    # no library collision check by default: a promoted record must not make
    # a frozen fixture drift; generalizes targets still resolve in the library
    rep = validate_task(root, task,
                        library_ids if library_ids is not None else set(),
                        library_records=knowledgelib.load_records())
    recs = rep["records"]
    n = len(recs)
    verified = sum(1 for r in recs if r.get("verdict") == "verified")
    refuted = sum(1 for r in recs if r.get("verdict") == "refuted")
    unruled = n - verified - refuted
    probs = rep["problems"]
    off_ledger = sum(1 for p in probs if "not in task" in p and "ledger" in p)
    forum_sole = sum(1 for p in probs if "SOLE source" in p)
    uncited = sum(1 for p in probs if "has no page" in p or "needs a note" in p
                  or "envelope_note" in p)
    other = len(probs) - off_ledger - forum_sole - uncited
    srcs = task.get("sources") or []
    tiers = {}
    for s in srcs:
        tiers[s.get("tier")] = tiers.get(s.get("tier"), 0) + 1
    metrics = {
        "task": task_id, "slot": task.get("slot"), "records": n,
        "verified": verified, "refuted": refuted, "unruled": unruled,
        "checklists": len(rep["checklists"]),
        "lint_problems": other, "citation_problems": uncited,
        "off_ledger_citations": off_ledger, "forum_sole": forum_sole,
        "sources": len(srcs), "source_tiers": dict(sorted(tiers.items())),
        "pages_cited": sum(r.get("sources", 0) for r in recs),
        "depth": rep["depth"],
    }
    penalties = {
        "no_records": 0 if (n or rep["checklists"]) else 1,
        "lint_problems": other, "citation_problems": uncited,
        "off_ledger": off_ledger, "forum_sole": forum_sole,
        "unruled": unruled, "refuted": refuted,
    }
    return {"metrics": metrics, "penalties": penalties, "problems": probs}
