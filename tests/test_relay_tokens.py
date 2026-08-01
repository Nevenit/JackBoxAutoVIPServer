import os

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
