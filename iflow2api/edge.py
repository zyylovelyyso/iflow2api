"""Microsoft Edge helpers (Windows-only).

Used for OAuth login where we want to open Edge with a specific profile so
multiple iFlow accounts can stay logged in simultaneously.
"""

from __future__ import annotations

import json
import os
import re
import sys
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class EdgeProfile:
    directory: str
    name: str


def find_edge_exe() -> Optional[str]:
    """Return msedge.exe path if available."""
    env = (os.getenv("IFLOW2API_EDGE_PATH") or "").strip()
    if env and Path(env).exists():
        return env

    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for p in candidates:
        if Path(p).exists():
            return p
    return None


def _edge_local_state_path() -> Optional[Path]:
    if sys.platform != "win32":
        return None
    local_appdata = os.getenv("LOCALAPPDATA")
    if not local_appdata:
        return None
    return Path(local_appdata) / "Microsoft" / "Edge" / "User Data" / "Local State"


def list_edge_profiles() -> list[EdgeProfile]:
    """List Edge profiles from 'Local State' (best-effort)."""
    path = _edge_local_state_path()
    if path is None or (not path.exists()):
        return []

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []

    info_cache = (
        (data.get("profile") or {}).get("info_cache")
        if isinstance(data, dict)
        else None
    )
    if not isinstance(info_cache, dict):
        return []

    profiles: list[EdgeProfile] = []
    for directory, info in info_cache.items():
        if not isinstance(directory, str) or not directory.strip():
            continue
        name = directory
        if isinstance(info, dict):
            n = info.get("name")
            if isinstance(n, str) and n.strip():
                name = n.strip()
        profiles.append(EdgeProfile(directory=directory.strip(), name=name))

    def sort_key(p: EdgeProfile):
        if p.directory == "Default":
            return (0, 0, p.name.lower())
        m = re.match(r"^Profile (\\d+)$", p.directory)
        if m:
            return (1, int(m.group(1)), p.name.lower())
        return (2, 0, p.name.lower())

    return sorted(profiles, key=sort_key)


def launch_edge(
    url: str,
    *,
    profile_directory: Optional[str] = None,
    inprivate: bool = False,
    new_window: bool = True,
) -> bool:
    """Launch Edge with optional profile selection.

    Returns True when Edge was launched, False when Edge is not available.
    """
    edge = find_edge_exe()
    if not edge:
        return False

    args: list[str] = [edge]
    if new_window:
        args.append("--new-window")
    if inprivate:
        args.append("--inprivate")
    if profile_directory:
        args.append(f"--profile-directory={profile_directory}")
    args.append(url)

    try:
        subprocess.Popen(args, close_fds=True)  # noqa: S603
        return True
    except Exception:
        return False

