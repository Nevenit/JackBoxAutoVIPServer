#!/usr/bin/env python3
"""Set the jbvip launch option on every installed Jackbox game.

Usage: python3 install.py        (Steam MUST be closed — it rewrites
localconfig.vdf on exit, clobbering edits made while it runs.)
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import vdf

from extractor.steam import jackbox_app_ids, set_launch_option

REPO = Path(__file__).resolve().parent
WRAP = f"{REPO / 'jbvip-run.sh'} %command%"


def steam_root() -> Path:
    for c in ("~/.steam/steam", "~/.local/share/Steam", "~/.steam/root"):
        p = Path(os.path.expanduser(c))
        if (p / "steamapps").exists():
            return p
    sys.exit("Could not find a Steam install under ~/.steam or ~/.local/share/Steam")


def library_steamapps(root: Path) -> list:
    dirs = [root / "steamapps"]
    lf = root / "steamapps" / "libraryfolders.vdf"
    if lf.exists():
        data = vdf.loads(lf.read_text()).get("libraryfolders", {})
        for entry in data.values():
            if isinstance(entry, dict) and "path" in entry:
                dirs.append(Path(entry["path"]) / "steamapps")
    return [d for d in dirs if d.exists()]


def preflight():
    if not shutil.which("bwrap"):
        sys.exit("bubblewrap (bwrap) is not installed — install it and retry.")
    if not shutil.which("mitmdump"):
        sys.exit("mitmproxy (mitmdump) not on PATH — `pipx install mitmproxy` and retry.")
    try:
        if int(Path("/proc/sys/user/max_user_namespaces").read_text()) <= 0:
            raise ValueError
    except (FileNotFoundError, ValueError):
        sys.exit("Unprivileged user namespaces appear disabled — required for bwrap.")
    if subprocess.run(["pgrep", "-x", "steam"], capture_output=True).returncode == 0:
        sys.exit("Steam is running. Quit Steam fully, then re-run install.py.")


def localconfig_paths(root: Path) -> list:
    return list((root / "userdata").glob("*/config/localconfig.vdf"))


def main():
    preflight()
    root = steam_root()
    games = jackbox_app_ids(library_steamapps(root))
    if not games:
        sys.exit("No installed Jackbox games found.")
    print("Found Jackbox games:", ", ".join(f"{n} ({i})" for i, n in games.items()))
    configs = localconfig_paths(root)
    if not configs:
        sys.exit("No Steam userdata/localconfig.vdf found — log into Steam once first.")
    for cfg_path in configs:
        cfg = vdf.loads(cfg_path.read_text())
        for app_id in games:
            cfg = set_launch_option(cfg, app_id, WRAP)
        shutil.copy2(cfg_path, str(cfg_path) + ".jbvip.bak")
        cfg_path.write_text(vdf.dumps(cfg, pretty=True))
        print(f"Updated {cfg_path} (backup: {cfg_path}.jbvip.bak)")
    print("Done. Launch any Jackbox game from Steam — the room code serves on TCP :38469.")


if __name__ == "__main__":
    main()
