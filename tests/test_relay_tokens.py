import os

from relay import tokens as tokens_module
from relay.tokens import load_tokens, save_tokens


def test_loading_a_missing_file_yields_no_tokens(tmp_path):
    assert load_tokens(tmp_path / "absent.json") == []


def test_saved_tokens_round_trip(tmp_path):
    path = tmp_path / "tokens.json"
    save_tokens(path, ["b-token", "a-token"])
    assert load_tokens(path) == ["a-token", "b-token"]


def test_saving_creates_missing_parent_directories(tmp_path):
    path = tmp_path / "nested" / "deeper" / "tokens.json"
    save_tokens(path, ["token"])
    assert load_tokens(path) == ["token"]


def test_saved_file_is_not_world_readable(tmp_path):
    path = tmp_path / "tokens.json"
    save_tokens(path, ["token"])
    assert os.stat(path).st_mode & 0o077 == 0


def test_corrupt_file_yields_no_tokens_rather_than_raising(tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text("{not json at all")
    assert load_tokens(path) == []


def test_a_corrupt_file_is_warned_about_rather_than_looking_empty(tmp_path, caplog):
    # Otherwise it is indistinguishable from a missing file: every phone stops
    # working and the next pairing overwrites the file with just its own token.
    path = tmp_path / "tokens.json"
    path.write_text('{"subscriber_tokens": ["a-real-token"')
    with caplog.at_level("WARNING"):
        assert load_tokens(path) == []
    assert "tokens.json" in caplog.text
    assert "a-real-token" not in caplog.text  # never log the contents


def test_a_file_of_the_wrong_shape_is_warned_about(tmp_path, caplog):
    path = tmp_path / "tokens.json"
    path.write_text('["a-real-token"]')
    with caplog.at_level("WARNING"):
        assert load_tokens(path) == []
    assert "subscriber_tokens" in caplog.text
    assert "a-real-token" not in caplog.text


def test_a_missing_file_is_not_warned_about(tmp_path, caplog):
    with caplog.at_level("WARNING"):
        load_tokens(tmp_path / "absent.json")
    assert caplog.text == ""


def test_saving_fsyncs_before_replacing(tmp_path, monkeypatch):
    # os.replace is only atomic against interruption; without the fsync a power
    # loss can land the rename before the data.
    order = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(
        tokens_module.os, "fsync", lambda fd: (order.append("fsync"), real_fsync(fd))
    )
    monkeypatch.setattr(
        tokens_module.os,
        "replace",
        lambda src, dst: (order.append("replace"), real_replace(src, dst)),
    )
    save_tokens(tmp_path / "tokens.json", ["token"])
    assert order == ["fsync", "replace"]


def test_non_string_entries_are_discarded(tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text('{"subscriber_tokens": ["good", 42, null]}')
    assert load_tokens(path) == ["good"]


def test_saving_replaces_previous_contents(tmp_path):
    path = tmp_path / "tokens.json"
    save_tokens(path, ["old"])
    save_tokens(path, ["new"])
    assert load_tokens(path) == ["new"]


def test_no_temporary_files_are_left_behind(tmp_path):
    path = tmp_path / "tokens.json"
    save_tokens(path, ["token"])
    assert [p.name for p in tmp_path.iterdir()] == ["tokens.json"]
