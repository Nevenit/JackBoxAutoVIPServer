import vdf
from pathlib import Path
from extractor.steam import set_launch_option, clear_launch_option, jackbox_app_ids

FIX = Path(__file__).parent / "fixtures" / "localconfig_sample.vdf"
WRAP = "/opt/jbvip/jbvip-run.sh %command%"


def _load():
    return vdf.loads(FIX.read_text())


def _apps(cfg):
    return cfg["UserLocalConfigStore"]["Software"]["Valve"]["Steam"]["apps"]


def test_set_launch_option_adds_to_app_without_one():
    cfg = set_launch_option(_load(), "1148350", WRAP)
    assert _apps(cfg)["1148350"]["LaunchOptions"] == WRAP


def test_set_launch_option_overwrites_existing():
    cfg = set_launch_option(_load(), "331670", WRAP)
    assert _apps(cfg)["331670"]["LaunchOptions"] == WRAP


def test_set_launch_option_creates_app_block_if_absent():
    cfg = set_launch_option(_load(), "999999", WRAP)
    assert _apps(cfg)["999999"]["LaunchOptions"] == WRAP


def test_clear_launch_option_removes_only_when_it_matches():
    cfg = set_launch_option(_load(), "1148350", WRAP)
    cfg = clear_launch_option(cfg, "1148350", WRAP)
    assert "LaunchOptions" not in _apps(cfg)["1148350"]


def test_clear_launch_option_leaves_unrelated_value():
    cfg = clear_launch_option(_load(), "331670", WRAP)  # existing value != WRAP
    assert _apps(cfg)["331670"]["LaunchOptions"] == "existing %command%"


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
