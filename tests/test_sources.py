"""The knowledge records' source PDFs come from upstream on first use.

They're the manufacturers' (NOTICE), so the module carries only
docs/knowledge-sources.txt and bin/sources.py fetches them into HOME, each
checked against its git blob sha (issue #97: the hwde project's clone, where
bin/hwde used to find them, is gone). Nothing here touches the network.
"""
from __future__ import annotations

import io
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
import sources  # noqa: E402

SKILL = ROOT


def test_the_list_covers_every_pdf_a_record_cites():
    _, files = sources.manifest()
    listed = {name for _, _, name in files}
    cited = set()
    for p in (SKILL / "reference" / "knowledge").rglob("*.yaml"):
        cited |= set(re.findall(r"reference/knowledge/sources/([\w.-]+\.pdf)", p.read_text()))
    # A source that may not be copied ships as a sidecar (url + sha) instead.
    sidecar = {n for n in cited if (SKILL / "reference" / "knowledge" / "sources"
                                    / f"{n}.not-redistributed.md").is_file()}
    assert cited and cited - sidecar <= listed, cited - sidecar - listed


def test_blob_sha_is_gits(tmp_path):
    f = tmp_path / "x.pdf"
    f.write_bytes(b"%PDF-1.4 not really\n")
    git = subprocess.run(["git", "hash-object", str(f)], capture_output=True, text=True)
    assert sources.blob_sha(f.read_bytes()) == git.stdout.strip()


def _serve(monkeypatch, tmp_path, files: dict[str, bytes], served: dict[str, bytes]):
    m = tmp_path / "list.txt"
    m.write_text("commit abc\n" + "".join(
        f"{sources.blob_sha(d)} {len(d)} {n}\n" for n, d in files.items()))
    monkeypatch.setattr(sources, "MANIFEST", m)
    got = []

    def urlopen(url, timeout):
        got.append(url)
        name = url.rsplit("/", 1)[1]
        if name not in served:
            raise OSError("HTTP 404")
        return io.BytesIO(served[name])
    monkeypatch.setattr(sources.urllib.request, "urlopen", urlopen)
    return got


def test_fetch_keeps_what_matches_and_marks_it_complete(monkeypatch, tmp_path):
    files = {"a.pdf": b"aaa", "b.pdf": b"bbbb"}
    got = _serve(monkeypatch, tmp_path, files, files)
    d = tmp_path / "src"
    assert sources.fetch(d) == 0
    assert (d / "a.pdf").read_bytes() == b"aaa" and (d / "b.pdf").read_bytes() == b"bbbb"
    assert (d / ".complete").read_text() == "abc\n"
    assert "/abc/.claude/skills/hwde/reference/knowledge/sources/a.pdf" in got[0]
    assert sources.fetch(d) == 0 and len(got) == 2  # nothing fetched twice
    assert sources.main(["check", str(d)]) == 0


def test_fetch_refuses_a_file_that_isnt_the_pinned_blob(monkeypatch, tmp_path):
    files = {"a.pdf": b"aaa", "b.pdf": b"bbbb"}
    _serve(monkeypatch, tmp_path, files, {"a.pdf": b"aaa", "b.pdf": b"evil"})
    d = tmp_path / "src"
    assert sources.fetch(d) == 1
    assert (d / "a.pdf").is_file() and not (d / "b.pdf").exists()
    assert not (d / ".complete").exists()
    assert sources.main(["check", str(d)]) == 1


def _env(tmp_path, *args):
    # run through a published copy, the outer hwde exports where that copy's
    # sources are (HWDE_PUBLISHED): this hwde must find only its own
    env = {k: v for k, v in os.environ.items()
           if k not in ("HWDE_KNOWLEDGE_SOURCES", "HWDE_PUBLISHED")}
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True, exist_ok=True)
    if not (venv / "bin" / "python").exists():
        os.symlink(sys.executable, venv / "bin" / "python")
    (venv / ".hwde-lock").touch()
    env.update(HWDE_HOME=str(tmp_path / "state"), HWDE_VENV=str(venv),
               HWDE_KICAD_CLI="/nonexistent", DISPLAY=":99")
    return subprocess.run([str(ROOT / "bin" / "hwde"), *args], env=env, cwd=tmp_path,
                          capture_output=True, text=True, timeout=60)


def test_hwde_points_at_the_fetched_copy_once_it_is_complete(tmp_path):
    r = _env(tmp_path, "env")  # env reads no records: it fetches nothing
    assert r.returncode == 0 and "HWDE_KNOWLEDGE_SOURCES" not in r.stdout, r.stdout
    assert not (tmp_path / "state" / "knowledge-sources").exists()
    d = tmp_path / "state" / "knowledge-sources"
    d.mkdir(parents=True)
    (d / ".complete").write_text("x\n")
    r = _env(tmp_path, "env")
    assert f"export HWDE_KNOWLEDGE_SOURCES='{d}'" in r.stdout, r.stdout
