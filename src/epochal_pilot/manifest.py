"""Run manifest: everything needed to trust and repeat a run (never any secret)."""

from __future__ import annotations

import hashlib
import platform
import subprocess
import sys
from pathlib import Path

from epochal_pilot import __version__


def sha256_file(path: Path | None) -> str | None:
    if path is None or not Path(path).is_file():
        return None
    return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def git_info(start: Path | None = None) -> dict:
    root = Path(start or Path(__file__).resolve().parents[2])
    info: dict = {"sha": None, "dirty": None}
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, timeout=10, check=False
        )
        if sha.returncode == 0:
            info["sha"] = sha.stdout.strip()
            st = subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=no"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            info["dirty"] = bool(st.stdout.strip()) if st.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        pass
    return info


def environment() -> dict:
    return {
        "package": "epochal-pilot",
        "version": __version__,
        "python": sys.version.split()[0],
        "platform": f"{platform.system()} {platform.machine()}",
    }
