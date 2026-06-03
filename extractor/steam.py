"""Pure helpers for detecting installed Jackbox games and editing Steam's
per-game LaunchOptions in localconfig.vdf. No process side effects here; the
install.py / uninstall.py CLIs do the file I/O and Steam-running checks.
"""
from pathlib import Path
import vdf

# Sidecar key under an app block holding the user's pre-existing LaunchOptions
# so uninstall can restore it. We only ever create this key when there was a
# genuine prior (non-jbvip) value, so the key's mere presence means "restore me".
SAVED_KEY = "LaunchOptions_jbvip_saved"

# Stable marker identifying a LaunchOptions value that we set. Used instead of
# exact-path equality so a moved/renamed repo still uninstalls cleanly.
JBVIP_MARKER = "jbvip-run.sh"


def _child(parent: dict, name: str) -> dict:
    """Return parent[name], reusing any case-insensitive match instead of
    creating a duplicate sibling; create the canonical name only if none exists.

    Steam's VDF keys (Software/Valve/Steam/apps) are normally canonical, but
    vdf.loads is case-sensitive, so an existing lowercase ``valve`` block must be
    reused rather than shadowed by a fresh ``Valve`` block Steam would ignore.
    """
    for k in parent:
        if k.casefold() == name.casefold():
            child = parent[k]
            if not isinstance(child, dict):
                child = parent[k] = {}
            return child
    new = parent[name] = {}
    return new


def _apps(cfg: dict) -> dict:
    store = _child(cfg, "UserLocalConfigStore")
    soft = _child(store, "Software")
    valve = _child(soft, "Valve")
    steam = _child(valve, "Steam")
    return _child(steam, "apps")


def _is_ours(value: str) -> bool:
    """True if a LaunchOptions value is the jbvip wrapper we set.

    Our wrapper always contains the script name and ends with %command%; we match
    on that marker (not the absolute path) so a moved repo still uninstalls.
    """
    v = (value or "").strip()
    return JBVIP_MARKER in v and v.endswith("%command%")


def set_launch_option(cfg: dict, app_id: str, value: str) -> dict:
    """Set apps.<app_id>.LaunchOptions to value, preserving any custom prior value.

    If the app already had a non-jbvip LaunchOptions, it is stashed once under
    SAVED_KEY so clear_launch_option() can restore it on uninstall.
    """
    apps = _apps(cfg)
    block = apps.setdefault(app_id, {})
    prev = block.get("LaunchOptions")
    # Save a genuine user value exactly once. We never save our own wrapper, and
    # the sidecar's presence alone signals "there was a prior value to restore".
    if prev is not None and not _is_ours(prev) and SAVED_KEY not in block:
        block[SAVED_KEY] = prev
    block["LaunchOptions"] = value
    return cfg


def clear_launch_option(cfg: dict, app_id: str, value: str | None = None) -> dict:
    """Remove apps.<app_id>.LaunchOptions when it is our jbvip wrapper, restoring
    the user's saved prior value if one was stashed.

    ``value`` is accepted for backward compatibility but ignored in favor of
    marker matching, so a moved/renamed repo still uninstalls cleanly. Unrelated
    user options (which do not look like our wrapper) are left untouched.
    """
    apps = _apps(cfg)
    block = apps.get(app_id)
    if block and _is_ours(block.get("LaunchOptions", "")):
        if SAVED_KEY in block:
            block["LaunchOptions"] = block.pop(SAVED_KEY)
        else:
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
