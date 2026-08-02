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


@pytest.mark.parametrize("variable", ["JBVIP_RELAY_URL", "JBVIP_RELAY_TOKEN"])
def test_the_readme_does_not_tell_the_user_to_export_the_relay_variables(variable):
    # The documented procedure was the bug: an export in a terminal cannot reach
    # a Steam launch option. The README must document the file instead.
    readme = README.read_text()
    assert f"export {variable}" not in readme
    assert "~/.config/jbvip/relay.env" in readme
