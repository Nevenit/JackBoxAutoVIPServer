#!/usr/bin/env python3
"""Clear the jbvip launch option from every Jackbox game it was set on,
restoring any custom LaunchOptions the user had before install."""
import vdf

from extractor.steam import _apps, clear_launch_option
from install import (
    assert_steam_not_running,
    localconfig_paths,
    steam_root,
    write_config_atomic,
)


def main():
    # Steam rewrites localconfig.vdf on exit, so editing it while Steam runs
    # would be clobbered — refuse, exactly like install.py.
    assert_steam_not_running("uninstall.py")
    root = steam_root()
    for cfg_path in localconfig_paths(root):
        cfg = vdf.loads(cfg_path.read_text())
        # Scan every app block (not just currently-installed Jackbox games) so
        # orphaned options survive a game being uninstalled from Steam, and a
        # moved repo still matches via the jbvip marker rather than exact path.
        for app_id in list(_apps(cfg).keys()):
            cfg = clear_launch_option(cfg, app_id)
        write_config_atomic(cfg_path, vdf.dumps(cfg, pretty=True))
        print(f"Cleared jbvip launch options in {cfg_path}")


if __name__ == "__main__":
    main()
