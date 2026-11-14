"""hwde tools: fetch the board tools a Hive without a shared tools folder lacks.

    tools.py [DIR]   install into DIR (default $HWDE_HOME/tools or ~/.cache/hwde/tools):
                       kicad-10.0.6/            KiCad 10.0.6 (official AppImage, unpacked: no FUSE, no root)
                       freerouting/             freerouting 2.2.4 jar
                       kicad-routing-tools/     KiCadRoutingTools 0.19.0 with its router library
                       jre/                     Temurin 21 JRE, only when no Java 17+ is on PATH
                     then print what is still missing and needs root (xvfb).
    tools.py --check [DIR]   print what is there and what is missing; exit 1 if any is missing.

Each download is checked against a sha256 pinned here (KiCad's: the tar whose
minisign signature was verified against KiCad's published key on 2026-11-06;
freerouting's and KiCadRoutingTools' from the GitHub release digests). Standard
library only: it runs before any venv exists. Linux x86_64 only; elsewhere it
says so and installs nothing (use the platform's own KiCad install and set
HWDE_KICAD_CLI).
"""
from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

KICAD = "10.0.6"
KICAD_URL = f"https://kicad-downloads.s3.cern.ch/appimage/stable/kicad-{KICAD}-x86_64.AppImage.tar"
KICAD_SHA = "c05fa6760be64aaf55ff476911ce81272b4efb6241f4d7e2b9fdcdc7efbf5808"
FR_VER = "2.2.4"
FR_URL = f"https://github.com/freerouting/freerouting/releases/download/v{FR_VER}/freerouting-{FR_VER}.jar"
FR_SHA = "f5ed374182900ccc78e473518bbb9f6b869f4a07159495f663a76f52bb10523b"
KRT_VER = "0.19.0"
KRT_BASE = f"https://github.com/drandyhaas/KiCadRoutingTools/releases/download/v{KRT_VER}/"
KRT_ZIP = ("KiCadRoutingTools-%s.zip" % KRT_VER, "721479ff5f879593a8cdf328f712066a61e8e1c19ca65c99183052624f4491ed")
KRT_SO = ("grid_router-linux-x86_64.so", "77b640516d38402b6d6e4a63c32c7806650120d045643ba7fb8441b5b9279c14")
JRE_URL = "https://api.adoptium.net/v3/binary/latest/21/ga/linux/x64/jre/hotspot/normal/eclipse"


def default_dir() -> Path:
    home = os.environ.get("HWDE_HOME") or str(Path.home() / ".cache" / "hwde")
    return Path(home) / "tools"


def java_ok() -> bool:
    j = shutil.which("java")
    if not j:
        return False
    try:
        out = subprocess.run([j, "-version"], capture_output=True, text=True, timeout=30).stderr
        major = int(out.split('"')[1].split(".")[0])
        return major >= 17
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return False


def have(d: Path) -> dict[str, bool]:
    return {
        "kicad": (d / f"kicad-{KICAD}" / "bin" / "kicad-cli").exists(),
        "freerouting": (d / "freerouting" / f"freerouting-{FR_VER}.jar").exists(),
        "krt": (d / "kicad-routing-tools" / f"KiCadRoutingTools-{KRT_VER}" / "plugins" / "rust_router" / "grid_router.so").exists(),
        "java": java_ok() or any(d.glob("jre/*/bin/java")),
        "xvfb": bool(shutil.which("Xvfb") and shutil.which("xauth")) or bool(os.environ.get("DISPLAY")),
    }


def fetch(url: str, dest: Path, sha: str | None) -> None:
    print(f"hwde tools: downloading {url}", flush=True)
    h = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as f:
        while chunk := r.read(1 << 20):
            h.update(chunk)
            f.write(chunk)
    if sha and h.hexdigest() != sha:
        dest.unlink(missing_ok=True)
        raise SystemExit(f"hwde tools: {url}: sha256 {h.hexdigest()} is not the pinned {sha}; not installing it")


def install(d: Path) -> None:
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "AMD64"):
        raise SystemExit("hwde tools: only Linux x86_64 is fetched; install KiCad 10 yourself and set HWDE_KICAD_CLI")
    d.mkdir(parents=True, exist_ok=True)
    h = have(d)
    with tempfile.TemporaryDirectory(dir=d) as tmp:
        t = Path(tmp)
        if not h["kicad"]:
            tarp = t / "kicad.tar"
            fetch(KICAD_URL, tarp, KICAD_SHA)
            with tarfile.open(tarp) as tf:
                names = [m.name for m in tf.getmembers() if m.name.endswith(".AppImage")]
                if len(names) != 1:
                    raise SystemExit("hwde tools: the KiCad tar does not hold one AppImage")
                tf.extract(names[0], t, filter="data")
            tarp.unlink()
            app = t / names[0]
            app.chmod(0o755)
            # --appimage-extract runs the file's own extractor: no FUSE needed.
            subprocess.run([str(app), "--appimage-extract"], cwd=t, check=True, stdout=subprocess.DEVNULL)
            os.replace(t / "AppDir", d / f"kicad-{KICAD}")  # squashfs-root is only a link to it
            app.unlink()
        if not h["freerouting"]:
            (d / "freerouting").mkdir(exist_ok=True)
            fetch(FR_URL, t / "fr.jar", FR_SHA)
            os.replace(t / "fr.jar", d / "freerouting" / f"freerouting-{FR_VER}.jar")
        if not h["krt"]:
            fetch(KRT_BASE + KRT_ZIP[0], t / "krt.zip", KRT_ZIP[1])
            fetch(KRT_BASE + KRT_SO[0], t / "gr.so", KRT_SO[1])
            dest = d / "kicad-routing-tools" / f"KiCadRoutingTools-{KRT_VER}"
            shutil.rmtree(dest, ignore_errors=True)
            dest.mkdir(parents=True)
            with zipfile.ZipFile(t / "krt.zip") as z:
                z.extractall(t / "krt")
            src = next(p for p in (t / "krt").rglob("plugins") if p.is_dir())
            root = src.parent
            for p in root.iterdir():
                shutil.move(str(p), dest / p.name)
            rr = dest / "plugins" / "rust_router"
            rr.mkdir(parents=True, exist_ok=True)
            shutil.copy(t / "gr.so", rr / "grid_router-linux-x86_64.so")
            shutil.copy(t / "gr.so", rr / "grid_router.so")
        if not h["java"]:
            fetch(JRE_URL, t / "jre.tgz", None)
            with tarfile.open(t / "jre.tgz") as tf:
                top = tf.getnames()[0].split("/")[0]
                tf.extractall(t / "jre", filter="data")
            (d / "jre").mkdir(exist_ok=True)
            os.replace(t / "jre" / top, d / "jre" / top)


def report(d: Path) -> int:
    h = have(d)
    for k, v in h.items():
        print(f"  {k:12} {'ok' if v else 'MISSING'}")
    if not h["xvfb"]:
        print("hwde tools: no X display and no Xvfb. Needs root, once: sudo apt install xvfb"
              " (on WSL 2 with Windows 11 a display usually exists already)")
    return 0 if all(h.values()) else 1


def main(argv: list[str]) -> int:
    check = "--check" in argv
    rest = [a for a in argv if a != "--check"]
    d = Path(rest[0]).expanduser() if rest else default_dir()
    if not check:
        install(d)
    return report(d)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
