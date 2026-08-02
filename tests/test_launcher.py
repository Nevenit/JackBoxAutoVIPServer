"""The launcher's relay configuration must not depend on a shell `export`.

Steam runs the launch option with the environment the Steam *client* inherited
from the desktop session, via `sh -c`, which sources no profile. A variable
exported in a terminal therefore never reaches jbvip-run.sh, which is why the
relay is configured through a file instead.

These tests run the real config block out of the real script, so the file path
and the export behaviour are pinned to what jbvip-run.sh actually does.
"""
import subprocess
from pathlib import Path

import pytest

from extractor.relay_publisher import LOG_MARKER

REPO = Path(__file__).resolve().parent.parent
LAUNCHER = REPO / "jbvip-run.sh"
README = REPO / "README.md"

_BLOCK_START = "RELAY_ENV="
_BLOCK_END = "export JBVIP_RELAY_TOKEN="


def relay_config_block() -> str:
    """The relay-config lines of jbvip-run.sh, so the test runs the real code."""
    lines = LAUNCHER.read_text().splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith(_BLOCK_START)]
    ends = [i for i, line in enumerate(lines) if line.startswith(_BLOCK_END)]
    assert starts and ends, "jbvip-run.sh no longer has a relay-config block"
    return "\n".join(lines[starts[0]: ends[0] + 1])


def run_block(home: Path, env=None) -> dict:
    """Run the block under the launcher's own shell options, in a child env."""
    script = (
        "set -euo pipefail\n"
        + relay_config_block()
        + "\n"
        # A child process, so this proves the values were *exported*, not just
        # assigned in the launcher's own shell.
        + 'bash -c \'printf "%s\\n%s\\n" "${JBVIP_RELAY_URL-UNSET}" '
        '"${JBVIP_RELAY_TOKEN-UNSET}"\'\n'
    )
    child_env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
    child_env.update(env or {})
    done = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=child_env,
        cwd=str(REPO),
    )
    assert done.returncode == 0, done.stderr
    url, token = done.stdout.splitlines()[:2]
    return {"url": url, "token": token}


def test_the_launcher_reads_relay_config_from_the_config_file(tmp_path):
    config = tmp_path / ".config" / "jbvip"
    config.mkdir(parents=True)
    (config / "relay.env").write_text(
        "JBVIP_RELAY_URL=https://sourced.example\n"
        "JBVIP_RELAY_TOKEN=sourced-token\n"
    )
    assert run_block(tmp_path) == {
        "url": "https://sourced.example",
        "token": "sourced-token",
    }


def test_the_config_file_honours_xdg_config_home(tmp_path):
    config = tmp_path / "xdg" / "jbvip"
    config.mkdir(parents=True)
    (config / "relay.env").write_text("JBVIP_RELAY_URL=https://xdg.example\n")
    result = run_block(tmp_path, env={"XDG_CONFIG_HOME": str(tmp_path / "xdg")})
    assert result["url"] == "https://xdg.example"


def test_a_missing_config_file_leaves_both_variables_empty(tmp_path):
    # Empty rather than unset: jbvip-run.sh runs under `set -u`.
    assert run_block(tmp_path) == {"url": "", "token": ""}


def test_a_real_environment_variable_still_works_without_a_config_file(tmp_path):
    # A directly-invoked run (not via Steam) can still be configured by export.
    result = run_block(tmp_path, env={"JBVIP_RELAY_URL": "https://env.example"})
    assert result["url"] == "https://env.example"


_CLEANUP_START = "LAST_LOG="
_CLEANUP_END = "trap cleanup EXIT INT TERM"


def cleanup_block() -> str:
    """The launcher's cleanup trap, so the test runs the real code."""
    lines = LAUNCHER.read_text().splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith(_CLEANUP_START)]
    ends = [i for i, line in enumerate(lines) if line.strip() == _CLEANUP_END]
    assert starts and ends, "jbvip-run.sh no longer has a cleanup trap"
    return "\n".join(lines[starts[0]: ends[0] + 1])


RELAY_WARNING = (
    f"[19:26:21.486] {LOG_MARKER} JBVIP_RELAY_URL is set but "
    "JBVIP_RELAY_TOKEN is not; relay publishing is disabled.\n"
)
# Real mitmdump flow_detail=1 output. The play URL carries the room code, and
# websocket_message echoes request.path once per frame.
FLOW_LINES = (
    "127.0.0.1:52088: POST https://ecast.jackboxgames.com/api/v2/rooms\n"
    "127.0.0.1:52090: GET "
    "https://ecast-prod-use2.jackboxgames.com/api/v2/rooms/WXYZ/play\n"
    "127.0.0.1:52090 <- WebSocket text message <- 1.2.3.4:443"
    "/api/v2/rooms/WXYZ/play?role=host\n"
    # flow_detail=1 truncates a long URL mid-code, so a redaction that needs a
    # trailing delimiter would leak the first characters.
    "127.0.0.1:52090: GET https://ecast-prod-use2.jackboxgames.com/api/v2/rooms/W…\n"
)


def run_cleanup(tmp_path, log_text: str, exit_status: int = 0):
    """Exercise the trap exactly as the launcher does, then report the aftermath."""
    state = tmp_path / "run-state"
    state.mkdir(mode=0o700)
    log = state / "mitm.log"
    log.write_text(log_text)
    log.chmod(0o600)  # as the launcher's `umask 077` makes it
    script = (
        "set -euo pipefail\n"
        "umask 077\n"  # the launcher sets this before the block below
        f'STATE="{state}"\n'
        + cleanup_block()
        + f"\nexit {exit_status}\n"
    )
    done = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={
            "HOME": str(tmp_path),
            "PATH": "/usr/bin:/bin",
            "XDG_STATE_HOME": str(tmp_path / "state"),
        },
    )
    return done, tmp_path / "state" / "jbvip" / "last-run.log", state


def test_a_run_that_reported_a_relay_problem_keeps_the_log(tmp_path):
    # The proxy log is the only place a relay problem is ever reported, and
    # $STATE is deleted on every exit path.
    done, last_log, state = run_cleanup(tmp_path, RELAY_WARNING)
    assert done.returncode == 0
    assert LOG_MARKER in last_log.read_text()
    assert "the relay reported a problem" in done.stderr
    assert not state.exists()


def test_the_kept_log_has_room_codes_redacted(tmp_path):
    _, last_log, _ = run_cleanup(tmp_path, RELAY_WARNING + FLOW_LINES)
    kept = last_log.read_text()
    assert "WXYZ" not in kept
    assert "/rooms/W…" not in kept  # the truncated-URL case
    assert kept.count("/rooms/<redacted>") == 3
    # The create POST carries no code and is the line that proves the redirect
    # is working, so it must survive intact.
    assert "POST https://ecast.jackboxgames.com/api/v2/rooms\n" in kept
    assert LOG_MARKER in kept


def test_the_kept_log_is_not_readable_by_other_users(tmp_path):
    _, last_log, _ = run_cleanup(tmp_path, RELAY_WARNING)
    assert last_log.stat().st_mode & 0o077 == 0
    assert last_log.parent.stat().st_mode & 0o077 == 0


def test_a_run_with_nothing_to_say_about_the_relay_keeps_nothing(tmp_path):
    # Jackbox games exit non-zero routinely; keeping a log every time would be
    # noise, and would put a room code on disk for no reason.
    done, last_log, state = run_cleanup(tmp_path, FLOW_LINES, exit_status=7)
    assert done.returncode == 7
    assert not last_log.exists()
    assert not state.exists()


def test_the_shell_and_the_addon_agree_on_the_log_marker():
    # The launcher greps the proxy log for the marker the addon writes. If they
    # drift, a relay problem is silently discarded again.
    assert f'LOG_MARKER="{LOG_MARKER}"' in LAUNCHER.read_text()


def test_the_launcher_pins_mitmdump_flow_detail():
    # At flow_detail >= 3 mitmdump prints websocket message contents and
    # response bodies, which is where Blobcast's roomId and the ecast create
    # response live — past the URL-only redaction.
    assert "--set flow_detail=1" in LAUNCHER.read_text()


@pytest.mark.parametrize("variable", ["JBVIP_RELAY_URL", "JBVIP_RELAY_TOKEN"])
def test_the_readme_does_not_tell_the_user_to_export_the_relay_variables(variable):
    # The documented procedure was the bug: an export in a terminal cannot reach
    # a Steam launch option. The README must document the file instead.
    readme = README.read_text()
    assert f"export {variable}" not in readme
    assert "~/.config/jbvip/relay.env" in readme
