#!/usr/bin/env python3
"""Set the jbvip launch option on every installed Jackbox game.

Usage: python3 install.py        (Steam MUST be closed — it rewrites
localconfig.vdf on exit, clobbering edits made while it runs.)
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import vdf

from extractor.steam import _apps, jackbox_app_ids, set_launch_option

REPO = Path(__file__).resolve().parent
WRAP = f"{REPO / 'jbvip-run.sh'} %command%"


def assert_steam_not_running(tool: str = "this script"):
    """Abort if Steam is running, since it rewrites localconfig.vdf on exit."""
    if subprocess.run(["pgrep", "-x", "steam"], capture_output=True).returncode == 0:
        sys.exit(f"Steam is running. Quit Steam fully, then re-run {tool}.")


def write_config_atomic(cfg_path: Path, text: str) -> None:
    """Write text to cfg_path atomically, keeping one pristine backup.

    A `.jbvip.bak` is created the first time only (never overwritten, so the
    original config survives any number of re-runs). The new content is written
    to a temp file in the SAME directory, fsynced, then os.replace'd into place
    so the on-disk file is never observed truncated.
    """
    bak = Path(str(cfg_path) + ".jbvip.bak")
    if cfg_path.exists() and not bak.exists():
        shutil.copy2(cfg_path, bak)
    fd, tmp = tempfile.mkstemp(dir=str(cfg_path.parent),
                               prefix=cfg_path.name + ".", suffix=".jbvip.tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, cfg_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


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
        for key, entry in data.items():
            if isinstance(entry, dict):
                path = entry.get("path")
            elif isinstance(entry, str) and key.isdigit():
                # Legacy format: "libraryfolders" maps index -> path string.
                path = entry
            else:
                path = None
            if path:
                dirs.append(Path(path) / "steamapps")
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
    assert_steam_not_running("install.py")


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
            prev = _apps(cfg).get(app_id, {}).get("LaunchOptions")
            if prev and prev != WRAP:
                print(f"WARNING: {games[app_id]} ({app_id}) had custom LaunchOptions "
                      f"{prev!r}; saved for restore on uninstall.")
            cfg = set_launch_option(cfg, app_id, WRAP)
        write_config_atomic(cfg_path, vdf.dumps(cfg, pretty=True))
        print(f"Updated {cfg_path} (backup: {cfg_path}.jbvip.bak)")
    print("Done. Launch any Jackbox game from Steam — the room code serves on TCP :38469.")


if __name__ == "__main__":
    main()
