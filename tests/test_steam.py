import vdf
from pathlib import Path
from extractor.steam import (
    SAVED_KEY,
    set_launch_option,
    clear_launch_option,
    jackbox_app_ids,
)

FIX = Path(__file__).parent / "fixtures" / "localconfig_sample.vdf"
WRAP = "/opt/jbvip/jbvip-run.sh %command%"


def _load():
    return vdf.loads(FIX.read_text())


def _apps(cfg):
    return cfg["UserLocalConfigStore"]["Software"]["Valve"]["Steam"]["apps"]


def test_set_launch_option_adds_to_app_without_one():
    cfg = set_launch_option(_load(), "1148350", WRAP)
    assert _apps(cfg)["1148350"]["LaunchOptions"] == WRAP
    # no prior value existed, so nothing should be stashed
    assert SAVED_KEY not in _apps(cfg)["1148350"]


def test_set_launch_option_preserves_existing_under_sidecar():
    # 331670 has a pre-existing custom "existing %command%" value.
    cfg = set_launch_option(_load(), "331670", WRAP)
    block = _apps(cfg)["331670"]
    assert block["LaunchOptions"] == WRAP
    assert block[SAVED_KEY] == "existing %command%"


def test_set_launch_option_saves_prior_value_only_once():
    cfg = set_launch_option(_load(), "331670", WRAP)
    # a second install run must not clobber the originally-saved custom value
    cfg = set_launch_option(cfg, "331670", WRAP)
    assert _apps(cfg)["331670"][SAVED_KEY] == "existing %command%"


def test_set_launch_option_creates_app_block_if_absent():
    cfg = set_launch_option(_load(), "999999", WRAP)
    assert _apps(cfg)["999999"]["LaunchOptions"] == WRAP


def test_clear_launch_option_removes_when_no_prior_value():
    cfg = set_launch_option(_load(), "1148350", WRAP)
    cfg = clear_launch_option(cfg, "1148350", WRAP)
    assert "LaunchOptions" not in _apps(cfg)["1148350"]


def test_clear_launch_option_restores_saved_value():
    # set-then-clear on a game that had a custom value must restore it
    cfg = set_launch_option(_load(), "331670", WRAP)
    cfg = clear_launch_option(cfg, "331670", WRAP)
    block = _apps(cfg)["331670"]
    assert block["LaunchOptions"] == "existing %command%"
    assert SAVED_KEY not in block


def test_clear_launch_option_leaves_unrelated_value():
    cfg = clear_launch_option(_load(), "331670", WRAP)  # existing value != WRAP
    assert _apps(cfg)["331670"]["LaunchOptions"] == "existing %command%"


def test_clear_launch_option_matches_moved_repo_path():
    # A repo that moved since install leaves a DIFFERENT absolute path; clear
    # must still recognise it by the jbvip marker, not exact equality.
    cfg = _load()
    _apps(cfg)["1148350"]["LaunchOptions"] = "/old/location/jbvip-run.sh %command%"
    cfg = clear_launch_option(cfg, "1148350", WRAP)
    assert "LaunchOptions" not in _apps(cfg)["1148350"]


def test_lowercase_valve_block_is_reused_not_duplicated():
    # Some configs may carry a lowercase "valve" key; we must reuse it rather
    # than create a sibling "Valve" block Steam would ignore.
    cfg = {
        "UserLocalConfigStore": {
            "Software": {
                "valve": {"Steam": {"apps": {"123": {}}}}
            }
        }
    }
    cfg = set_launch_option(cfg, "123", WRAP)
    software = cfg["UserLocalConfigStore"]["Software"]
    assert list(software.keys()) == ["valve"]  # no duplicate "Valve" sibling
    assert software["valve"]["Steam"]["apps"]["123"]["LaunchOptions"] == WRAP


def test_jackbox_app_ids_filters_by_name(tmp_path):
    # build a fake steam library tree with two appmanifests
    lib = tmp_path / "steamapps"
    lib.mkdir()
    (lib / "appmanifest_1.acf").write_text(
        '"AppState"\n{\n\t"appid"\t"1"\n\t"name"\t"The Jackbox Party Pack 9"\n}\n'
    )
    (lib / "appmanifest_2.acf").write_text(
        '"AppState"\n{\n\t"appid"\t"2"\n\t"name"\t"Half-Life"\n}\n'
    )
    ids = jackbox_app_ids([lib])
    assert ids == {"1": "The Jackbox Party Pack 9"}
