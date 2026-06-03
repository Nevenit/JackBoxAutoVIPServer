#!/usr/bin/env python3
"""Clear the jbvip launch option from every Jackbox game it was set on."""
import shutil
import sys
from pathlib import Path

import vdf

from extractor.steam import jackbox_app_ids, clear_launch_option
from install import WRAP, library_steamapps, localconfig_paths, steam_root


def main():
    root = steam_root()
    games = jackbox_app_ids(library_steamapps(root))
    for cfg_path in localconfig_paths(root):
        cfg = vdf.loads(cfg_path.read_text())
        for app_id in games:
            cfg = clear_launch_option(cfg, app_id, WRAP)
        cfg_path.write_text(vdf.dumps(cfg, pretty=True))
        print(f"Cleared jbvip launch options in {cfg_path}")


if __name__ == "__main__":
    main()
