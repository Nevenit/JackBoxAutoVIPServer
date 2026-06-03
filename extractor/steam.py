"""Pure helpers for detecting installed Jackbox games and editing Steam's
per-game LaunchOptions in localconfig.vdf. No process side effects here; the
install.py / uninstall.py CLIs do the file I/O and Steam-running checks.
"""
from pathlib import Path
import vdf


def _apps(cfg: dict) -> dict:
    store = cfg.setdefault("UserLocalConfigStore", {})
    soft = store.setdefault("Software", {})
    # Steam writes the key as "Valve" but reads case-insensitively; match what's there.
    valve = soft.get("Valve") or soft.setdefault("Valve", {})
    steam = valve.get("Steam") or valve.setdefault("Steam", {})
    return steam.setdefault("apps", {})


def set_launch_option(cfg: dict, app_id: str, value: str) -> dict:
    """Return cfg with apps.<app_id>.LaunchOptions set to value."""
    apps = _apps(cfg)
    apps.setdefault(app_id, {})["LaunchOptions"] = value
    return cfg


def clear_launch_option(cfg: dict, app_id: str, value: str) -> dict:
    """Remove apps.<app_id>.LaunchOptions only if it equals value (our wrapper)."""
    apps = _apps(cfg)
    block = apps.get(app_id)
    if block and block.get("LaunchOptions") == value:
        block.pop("LaunchOptions", None)
    return cfg


def jackbox_app_ids(steamapps_dirs) -> dict:
    """Map app_id -> name for installed apps whose name contains 'jackbox'."""
    found: dict[str, str] = {}
    for d in steamapps_dirs:
        for acf in Path(d).glob("appmanifest_*.acf"):
            try:
                state = vdf.loads(acf.read_text()).get("AppState", {})
            except Exception:
                continue
            name = state.get("name", "")
            app_id = state.get("appid", "")
            if app_id and "jackbox" in name.lower():
                found[app_id] = name
    return found
