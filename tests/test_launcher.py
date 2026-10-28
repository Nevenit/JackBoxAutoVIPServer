"""The launcher's relay configuration must not depend on a shell `export`.

Steam runs the launch option with the environment the Steam *client* inherited
from the desktop session, via `sh -c`, which sources no profile. A variable
exported in a terminal therefore never reaches jbvip-run.sh, which is why the
relay is configured through a file instead.

These tests run the real config block out of the real script, so the file path
and the sourcing behaviour are pinned to what jbvip-run.sh actually does.
"""
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from extractor.relay_publisher import LOG_MARKER

REPO = Path(__file__).resolve().parent.parent
LAUNCHER = REPO / "jbvip-run.sh"
README = REPO / "README.md"

_BLOCK_START = "RELAY_ENV="
_BLOCK_END = 'JBVIP_RELAY_URL="${JBVIP_RELAY_URL:-}"'


def relay_config_block() -> str:
    """The relay-config lines of jbvip-run.sh, so the test runs the real code."""
    lines = LAUNCHER.read_text().splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith(_BLOCK_START)]
    ends = [i for i, line in enumerate(lines) if line.startswith(_BLOCK_END)]
    assert starts and ends, "jbvip-run.sh no longer has a relay-config block"
    return "\n".join(lines[starts[0]: ends[0] + 1])


def run_block(home: Path, env=None, show: str = "JBVIP_RELAY_URL") -> str:
    """Run the block under the launcher's own shell options and report a variable."""
    script = (
        "set -euo pipefail\n"
        + relay_config_block()
        + f'\nprintf "%s\\n" "${{{show}-UNSET}}"\n'
    )
    child_env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
    child_env.update(env or {})
    done = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True,
        env=child_env, cwd=str(REPO),
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.splitlines()[0]


def test_the_launcher_reads_the_relay_url_from_the_config_file(tmp_path):
    config = tmp_path / ".config" / "jbvip"
    config.mkdir(parents=True)
    (config / "relay.env").write_text("JBVIP_RELAY_URL=https://sourced.example\n")
    assert run_block(tmp_path) == "https://sourced.example"


def test_the_config_file_honours_xdg_config_home(tmp_path):
    config = tmp_path / "xdg" / "jbvip"
    config.mkdir(parents=True)
    (config / "relay.env").write_text("JBVIP_RELAY_URL=https://xdg.example\n")
    assert run_block(
        tmp_path, env={"XDG_CONFIG_HOME": str(tmp_path / "xdg")}
    ) == "https://xdg.example"


def test_a_missing_config_file_leaves_the_url_empty(tmp_path):
    # Empty rather than unset: jbvip-run.sh runs under `set -u`.
    assert run_block(tmp_path) == ""


def test_a_real_environment_variable_still_works_without_a_config_file(tmp_path):
    # A directly-invoked run (not via Steam) can still be configured by export.
    assert run_block(
        tmp_path, env={"JBVIP_RELAY_URL": "https://env.example"}
    ) == "https://env.example"


def test_the_proxy_port_comes_from_the_config_file_or_defaults_to_8081(tmp_path):
    # A Steam launch option has no shell to export from, so a port other than the
    # default has to be settable in relay.env like the URL is. That only works if
    # PORT is read after the file has been sourced.
    home = tmp_path / "with-config"
    config = home / ".config" / "jbvip"
    config.mkdir(parents=True)
    (config / "relay.env").write_text("JBVIP_PROXY_PORT=9123\n")
    assert run_block(home, show="PORT") == "9123"
    assert run_block(tmp_path, show="PORT") == "8081"


@pytest.mark.parametrize("contents", [
    "JBVIP_RELAY_URL = https://x\n",  # a command, not an assignment
    'JBVIP_RELAY_URL="https://x\n',   # an unclosed quote: a syntax error
], ids=["bad-line", "syntax-error"])
def test_a_typo_in_the_config_file_does_not_stop_the_launch(tmp_path, contents):
    # The file is sourced under set -e, where a failing line used to end the
    # launch before the game started. A hand-edited file will have typos.
    config = tmp_path / ".config" / "jbvip"
    config.mkdir(parents=True)
    (config / "relay.env").write_text(contents)
    assert run_block(tmp_path) == ""  # as if there were no file
    assert run_block(tmp_path, show="PORT") == "8081"


def test_the_config_file_no_longer_carries_a_pre_shared_secret():
    # The relay mints every credential; tonight's are kept in night.json.
    text = LAUNCHER.read_text()
    assert "JBVIP_RELAY_TOKEN" not in text
    assert "JBVIP_PUBLISHER_TOKEN" not in text


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
    f"[19:26:21.486] {LOG_MARKER} relay publishing is disabled and the room "
    "code stays LAN-only: JBVIP_RELAY_URL is set but JBVIP_SESSION_ID, "
    "JBVIP_SESSION_TOKEN is not.\n"
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


def mitmdump_line() -> str:
    """The launcher's real mitmdump invocation, line continuations joined."""
    text = LAUNCHER.read_text().replace("\\\n", " ")
    return next(
        one for one in text.splitlines() if "mitmdump --mode transparent" in one
    )


def mitmdump_flow_detail_args() -> list[str]:
    """The flow-detail arguments jbvip-run.sh actually passes to mitmdump."""
    tokens = shlex.split(mitmdump_line().split(">", 1)[0])  # drop the redirection
    args: list[str] = []
    for index, token in enumerate(tokens):
        if token == "--flow-detail" or (
            token == "--set" and tokens[index + 1].startswith("flow_detail=")
        ):
            args += [token, tokens[index + 1]]
    assert args, "jbvip-run.sh no longer pins mitmdump's flow_detail"
    return args


def test_the_launcher_pins_flow_detail_against_a_user_config_file():
    """The pin must survive a ~/.mitmproxy/config.yaml that sets flow_detail.

    Asserting the flag *string* cannot catch this: `--set flow_detail=1` looks
    right and is inert, because mitmproxy applies --set before loading
    config.yaml and the config load overwrites it. So this runs the real
    mitmdump with the launcher's own arguments against a config file asking for
    4, and asserts what the dumper will actually do. At 4 it prints response
    bodies and websocket contents — the ecast create response's `code` and
    Blobcast's `roomId` — neither of which the /rooms/-anchored redaction touches.
    """
    mitmdump = Path(sys.executable).parent / "mitmdump"
    if not mitmdump.exists():
        found = shutil.which("mitmdump")
        if found is None:  # pragma: no cover - mitmproxy is a hard requirement
            pytest.skip("mitmdump is not installed")
        mitmdump = Path(found)

    with tempfile.TemporaryDirectory() as workspace:
        work = Path(workspace)
        confdir = work / "confdir"
        confdir.mkdir()
        (confdir / "config.yaml").write_text("flow_detail: 4\n")
        probe = work / "probe.py"
        probe.write_text(
            "import logging\n"
            "from mitmproxy import ctx\n"
            "def running():\n"
            "    logging.getLogger('probe').warning(\n"
            "        'OBSERVED flow_detail=%s', ctx.options.flow_detail)\n"
            "    ctx.master.shutdown()\n"
        )
        done = subprocess.run(
            [
                str(mitmdump),
                "-p", "0",
                "--set", f"confdir={confdir}",
                *mitmdump_flow_detail_args(),
                "-s", str(probe),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )

    assert "OBSERVED flow_detail=1" in done.stdout, done.stdout + done.stderr


def test_the_session_credentials_reach_mitmdump_but_not_the_game():
    # bwrap runs proprietary third-party game code. It has no business
    # holding a token that can publish to the relay session.
    line = mitmdump_line()
    for name in ("JBVIP_RELAY_URL", "JBVIP_SESSION_ID", "JBVIP_SESSION_TOKEN"):
        assert f"{name}=" in line
    text = LAUNCHER.read_text()
    assert "export JBVIP_SESSION_TOKEN" not in text
    assert "export JBVIP_SESSION_ID" not in text


def test_a_relay_that_cannot_be_reached_does_not_abort_the_launch():
    # set -e plus a bare `python3 -m host.qr_window` would end the script on a
    # dead VPS and the game would never start. It has to be an if-condition.
    text = LAUNCHER.read_text().replace("\\\n", " ")
    launch = next(
        one for one in text.splitlines() if "host.qr_window" in one
    )
    assert launch.strip().startswith("if "), launch


def test_the_launcher_clears_the_room_on_the_way_out():
    text = LAUNCHER.read_text()
    assert "host.session --clear" in text
    # Before $STATE is removed, and never allowed to fail the trap.
    cleanup = text[text.index("cleanup() {"):text.index("trap cleanup")]
    assert cleanup.index("host.session --clear") < cleanup.index('rm -rf "$STATE"')
    assert "|| true" in cleanup


def test_cleanup_frees_the_proxy_port_before_the_slow_clear():
    # The next pack's launcher needs the port, and the clear can take seconds:
    # python3 starting up, then an HTTPS round trip.
    text = LAUNCHER.read_text()
    cleanup = text[text.index("cleanup() {"):text.index("trap cleanup")]
    assert cleanup.index('kill "$MITM"') < cleanup.index("host.session --clear")


def ca_target_line() -> str:
    """The launcher's line that picks the file the trust bundle is mounted over."""
    line = next(
        (one for one in LAUNCHER.read_text().splitlines() if one.startswith("CA_TARGET=")),
        None,
    )
    assert line, "jbvip-run.sh no longer picks a CA_TARGET for the trust bundle"
    return line


def bwrap_line() -> str:
    """The launcher's real bwrap invocation of the game, line continuations joined."""
    text = LAUNCHER.read_text().replace("\\\n", " ")
    line = next(
        one.strip() for one in text.splitlines()
        if one.strip().startswith(("bwrap ", "exec bwrap ")) and '"$BUNDLE"' in one
    )
    return line.removeprefix("exec ")


# Before pairing, the launcher makes the game's mount with a stand-in for the
# bundle, which does not exist yet: bwrap failing at the game launch itself
# would end the launch with no game.
BWRAP_PREFLIGHT = 'bwrap --dev-bind / / --bind "$SYS_CA" "$CA_TARGET" -- true'


def bwrap_preflight_line() -> str:
    """The launcher's check that bwrap can start, which falls back if not."""
    line = next(
        (one for one in LAUNCHER.read_text().splitlines()
         if one.startswith(BWRAP_PREFLIGHT)),
        None,
    )
    assert line, "jbvip-run.sh no longer checks that bwrap can start before pairing"
    return line


def symlinked_ca(tmp_path):
    """A CA path laid out like Arch's: a symlink to the real bundle file."""
    real = tmp_path / "tls-ca-bundle.pem"
    real.write_text("system CAs\n")
    link = tmp_path / "ca-certificates.crt"
    link.symlink_to(real.name)
    return link, real


def test_the_ca_target_is_the_file_a_symlinked_ca_path_points_to(tmp_path):
    # Arch ships /etc/ssl/certs/ca-certificates.crt as a symlink, and bubblewrap
    # 0.13 refuses to mount onto one, so the bind has to land on its target.
    link, real = symlinked_ca(tmp_path)
    script = (
        f'set -euo pipefail\nSYS_CA="{link}"\n{ca_target_line()}\n'
        'printf "%s\\n" "$CA_TARGET"\n'
    )
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == str(real)


def test_the_game_reads_the_trust_bundle_through_a_symlinked_ca_path(tmp_path):
    """Run the launcher's real bwrap line and read the CA path as the game does.

    Before the fix every launch died here with "bwrap: Can't mount on symlink
    destination" and the game never started.
    """
    if shutil.which("bwrap") is None:  # pragma: no cover - bwrap is a hard requirement
        pytest.skip("bubblewrap is not installed")
    link, real = symlinked_ca(tmp_path)
    bundle = tmp_path / "cabundle.pem"
    bundle.write_text("system CAs\nmitmproxy CA\n")
    script = (
        f'set -euo pipefail\nSYS_CA="{link}"\nBUNDLE="{bundle}"\n'
        f"{ca_target_line()}\n"
        # The pre-flight makes the same mount, so it must pass here too, or
        # every launch would lose its room codes.
        'run_plain() { echo "fell back: $1" >&2; exit 3; }\n'
        f"{bwrap_preflight_line()}\n"
        'set -- cat "$SYS_CA"\n'  # stands in for the game
        f"{bwrap_line()}\n"
    )
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert done.stdout == "system CAs\nmitmproxy CA\n"
    assert real.read_text() == "system CAs\n"  # only the game's view changed


_GAME_START = 'mv -f "$tmpbundle" "$BUNDLE"'


def game_launch_block() -> str:
    """Everything the launcher runs once the trust bundle is ready: the game."""
    lines = LAUNCHER.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == _GAME_START)
    return "\n".join(lines[start + 1:])


def test_only_the_game_joins_the_redirected_cgroup(tmp_path):
    # Whatever joins the cgroup has its :443 redirected into mitmproxy. When the
    # launcher joined it itself, the relay DELETE in cleanup() went into the proxy
    # too, failed its certificate check, and every session outlived its game.
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    (cgroup / "cgroup.procs").write_text("")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_bwrap = fake_bin / "bwrap"
    fake_bwrap.write_text('#!/bin/sh\necho "game=$$"\n')
    fake_bwrap.chmod(0o755)
    script = (
        f'set -euo pipefail\nCGROUP="{cgroup}"\nBUNDLE=/dev/null\nCA_TARGET=/dev/null\n'
        "set -- true\n"
        + game_launch_block()
        + '\necho "launcher=$$"\n'
    )
    done = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True,
        env={"PATH": f"{fake_bin}:/usr/bin:/bin"},
    )
    assert done.returncode == 0, done.stderr
    pids = dict(line.split("=", 1) for line in done.stdout.split())
    joined = (cgroup / "cgroup.procs").read_text().split()
    assert joined == [pids["game"]]
    assert pids["launcher"] not in joined


def launcher_function(name: str) -> str:
    """A function of jbvip-run.sh: from `name() {` to its first `}` line."""
    lines = LAUNCHER.read_text().splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith(f"{name}() {{")]
    assert starts, f"jbvip-run.sh no longer defines {name}()"
    return "\n".join(lines[starts[0]: lines.index("}", starts[0]) + 1])


def launcher_if_block(first_line: str) -> str:
    """One `if` of jbvip-run.sh: that exact line down to the `fi` at its indent."""
    lines = LAUNCHER.read_text().splitlines()
    assert first_line in lines, f"jbvip-run.sh no longer has: {first_line}"
    start = lines.index(first_line)
    indent = first_line[: len(first_line) - len(first_line.lstrip())]
    return "\n".join(lines[start: lines.index(indent + "fi", start) + 1])


def run_fallback(tmp_path, code: str, *, notify_send: bool = True, env=None,
                 falls_back: bool = True):
    """Run launcher `code` that should end in run_plain, as the launcher would.

    Returns (process, game_marker, notify_log). The game is a shell function
    that leaves the marker, so the marker says whether the game started. PATH is
    never the real one: this machine's own notify-send would raise a real
    desktop notification. The stub logs its arguments, one per line; with
    notify_send=False there is none at all. `env` adds to the environment.
    With falls_back=False the code must carry on instead.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    notify_log = tmp_path / "notify.log"
    if notify_send:
        stub = bin_dir / "notify-send"
        stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" >> "{notify_log}"\n')
        stub.chmod(0o755)
    marker = tmp_path / "game-started"
    script = (
        "set -euo pipefail\n"
        f'game() {{ : > "{marker}"; }}\nGAME=(game)\n'
        + launcher_function("run_plain")
        + f"\n{code}\n"
        'echo "the launcher carried on"\n'
    )
    done = subprocess.run(
        [shutil.which("bash"), "-c", script], capture_output=True, text=True,
        env={
            "HOME": str(tmp_path),
            "PATH": f"{bin_dir}:/usr/bin:/bin" if notify_send else str(bin_dir),
            **(env or {}),
        },
    )
    # run_plain ends the launcher. Returning would carry on into mitmdump and
    # then start the game a second time.
    assert ("carried on" not in done.stdout) == falls_back, done.stderr
    return done, marker, notify_log


REDIRECT_CHECK = 'if [ ! -w "$CGROUP/cgroup.procs" ]; then'
# What a notification about a mitmdump that would not start has to tell the user:
# how to check it, and how to install it if that shows it is missing.
MITMDUMP_HINTS = ("mitmdump --version", "pipx install mitmproxy")


def test_a_missing_redirect_still_starts_the_game_and_says_why(tmp_path):
    done, marker, notified = run_fallback(
        tmp_path,
        "PORT=8081; REPO=/r; CGROUP=/nonexistent\n" + launcher_if_block(REDIRECT_CHECK),
    )
    assert done.returncode == 0, done.stderr
    assert marker.exists()
    assert "starting the game without room-code capture" in done.stderr
    flag, app, summary, body = notified.read_text().splitlines()
    assert (flag, app, summary) == ("-a", "jbvip", "Jackbox: no room codes this time")
    assert "jbvip-redirect-up.sh" in body


def busy_port_block() -> str:
    """What the launcher does about a busy proxy port: everything after
    port_up()'s definition, through the fallback's `fi`."""
    lines = LAUNCHER.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("port_up() {")) + 1
    return "\n".join(lines[start: lines.index("fi", start) + 1])


def test_a_busy_proxy_port_still_starts_the_game(tmp_path):
    done, marker, notified = run_fallback(
        tmp_path,
        "PORT=8081; REPO=/r; port_up() { return 0; }; sleep() { :; }\n" + busy_port_block(),
    )
    assert done.returncode == 0, done.stderr
    assert marker.exists()
    body = notified.read_text()
    assert "8081" in body
    # The fix as well as the problem. Usually the last launch's proxy is still
    # closing; otherwise another port, which the redirect and this launcher
    # have to be given alike, and relay.env is where a Steam user can set it.
    hints = ("wait a few seconds", "jbvip-redirect-down.sh", "jbvip-redirect-up.sh",
             "JBVIP_PROXY_PORT", "relay.env")
    for hint in hints:
        assert hint in body
    # In that order: up only appends rules (the first match wins), and down
    # also removes the cgroup, so down after up would undo the new redirect.
    assert [body.index(hint) for hint in hints] == sorted(body.index(hint) for hint in hints)


def test_a_port_that_frees_within_3_seconds_is_waited_for(tmp_path):
    # Usually it is the last launch's mitmdump, still closing after its pack
    # quit (a quick pack switch). Worth a moment, not a night without codes.
    done, marker, notified = run_fallback(
        tmp_path,
        "PORT=8081; REPO=/r; looks=0\n"
        'port_up() { looks=$((looks + 1)); [ "$looks" -le 3 ]; }\n'  # busy 3 times
        + busy_port_block(),
        falls_back=False,
    )
    assert done.returncode == 0, done.stderr
    assert not marker.exists()  # the real launch starts the game, further on
    assert not notified.exists()


def test_the_game_still_starts_without_notify_send(tmp_path):
    done, marker, notified = run_fallback(
        tmp_path,
        "PORT=8081; REPO=/r; CGROUP=/nonexistent\n" + launcher_if_block(REDIRECT_CHECK),
        notify_send=False,
    )
    assert done.returncode == 0, done.stderr
    assert marker.exists()
    assert "jbvip-redirect-up.sh" in done.stderr  # the reason still reaches stderr
    assert not notified.exists()


def test_a_missing_system_ca_bundle_still_starts_the_game(tmp_path):
    check = next(
        line for line in LAUNCHER.read_text().splitlines()
        if line.startswith('[ -n "$SYS_CA" ]')
    )
    done, marker, notified = run_fallback(tmp_path, f'SYS_CA=""\n{check}')
    assert done.returncode == 0, done.stderr
    assert marker.exists()
    body = notified.read_text()
    assert "no system CA bundle found" in body
    assert "ca-certificates" in body  # and what to install


def test_a_bwrap_that_cannot_start_still_starts_the_game(tmp_path):
    # Without user namespaces, say, or with bwrap not installed at all.
    fake = tmp_path / "fake-bwrap"
    fake.mkdir()
    (fake / "bwrap").write_text(
        "#!/bin/sh\necho 'bwrap: No permissions to create new namespace' >&2\nexit 1\n"
    )
    (fake / "bwrap").chmod(0o755)
    done, marker, notified = run_fallback(
        tmp_path,
        f'PATH="{fake}:$PATH"; SYS_CA=/etc/ssl/cert.pem; CA_TARGET=/etc/ssl/cert.pem\n'
        + bwrap_preflight_line(),
    )
    assert done.returncode == 0, done.stderr
    assert marker.exists()
    assert "bwrap --dev-bind / / -- true" in notified.read_text()  # how to see why


def test_a_mitmdump_that_dies_on_startup_still_starts_the_game(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "mitm.log").write_text("ImportError: the addon would not load\n")
    done, marker, notified = run_fallback(
        tmp_path,
        f'STATE="{state}"\ntrue & MITM=$!\nwait "$MITM"\n'  # a pid that is gone
        + launcher_if_block('  if ! kill -0 "$MITM" 2>/dev/null; then'),
    )
    assert done.returncode == 0, done.stderr
    assert marker.exists()
    assert "the addon would not load" in done.stderr  # the log tail, as before
    body = notified.read_text()
    assert "mitmdump" in body
    for hint in MITMDUMP_HINTS:
        assert hint in body


def test_a_proxy_that_never_comes_up_still_starts_the_game(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "mitm.log").write_text("listening... eventually\n")
    done, marker, notified = run_fallback(
        tmp_path,
        f'STATE="{state}"; PORT=8081; ready=0\n'
        + launcher_if_block('if [ "$ready" != 1 ]; then'),
    )
    assert done.returncode == 0, done.stderr
    assert marker.exists()
    assert "listening... eventually" in done.stderr  # the log tail, as before
    body = notified.read_text()
    assert "8081" in body
    for hint in MITMDUMP_HINTS:
        assert hint in body


def test_the_fallback_returns_the_games_exit_status_and_still_cleans_up(tmp_path):
    # Jackbox games exit non-zero routinely and that status is what Steam sees.
    # It must come out of run_plain and the EXIT trap unchanged, and the trap
    # must still run: it stops mitmdump and removes $STATE.
    state = tmp_path / "run-state"
    state.mkdir(mode=0o700)
    done, _, _ = run_fallback(
        tmp_path,
        f'umask 077\nSTATE="{state}"\nGAME=(sh -c "exit 7")\n'
        + cleanup_block()
        + '\nrun_plain "for the test"',
    )
    assert done.returncode == 7, done.stderr
    assert not state.exists()


@pytest.mark.parametrize("inherited", [
    {"SESSION_ID": "inherited-id"},
    {"SESSION_TOKEN": "inherited-token"},
    {"SESSION_ID": "inherited-id", "SESSION_TOKEN": "inherited-token"},
], ids=["id", "token", "both"])
def test_inherited_session_variables_neither_reach_the_game_nor_break_cleanup(
    tmp_path, inherited
):
    # A run_plain before pairing fires the trap, which reads both. An exported
    # SESSION_TOKEN would also carry tonight's publisher token into the game.
    state = tmp_path / "run-state"
    state.mkdir(mode=0o700)
    done, _, _ = run_fallback(
        tmp_path,
        f'umask 077\nSTATE="{state}"\nJBVIP_RELAY_URL=""\n'
        'GAME=(sh -c "env; exit 7")\n'  # a game that shows what it was given
        + cleanup_block()
        + '\nrun_plain "for the test"',
        env=inherited,
    )
    assert done.returncode == 7, done.stderr
    given = {line.split("=", 1)[0] for line in done.stdout.splitlines()}
    assert given.isdisjoint({"SESSION_ID", "SESSION_TOKEN"})
    assert not state.exists()


def test_interception_checks_run_before_the_pairing_window():
    # Asking someone to scan a QR code is pointless on a night that cannot
    # capture a room code.
    text = LAUNCHER.read_text()
    pairing = text.index("host.qr_window")
    for check in (REDIRECT_CHECK, "CA_TARGET=", BWRAP_PREFLIGHT, "if port_up; then"):
        assert text.index(check) < pairing


def test_the_qr_window_is_handed_tonights_night_file():
    text = LAUNCHER.read_text().replace("\\\n", " ")
    assert 'NIGHT_FILE="${XDG_STATE_HOME:-$HOME/.local/state}/jbvip/night.json"' in text
    assert '--night-file "$NIGHT_FILE"' in next(
        line for line in text.splitlines() if "host.qr_window" in line
    )


def test_the_qr_card_starts_once_interception_is_up():
    # After every fallback, so a launch without room codes never shows it, and
    # before the game, which it sits over.
    text = LAUNCHER.read_text()
    call = text.index("\nstart_qr_card\n")  # the call: a line of its own
    assert text.index('if [ "$ready" != 1 ]; then') < call
    assert call < text.index('tmpbundle="$(mktemp')
    assert text.rindex('run_plain "') < call


def fake_python3(tmp_path):
    """A `python3` that records its arguments, one per line, then keeps running
    for 30 s like the card does. Returns its directory (for PATH) and the file
    the arguments land in."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    args = tmp_path / "python3-args"
    fake = bin_dir / "python3"
    fake.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{args}"\nexec sleep 30\n')
    fake.chmod(0o755)
    return bin_dir, args


def run_start_qr_card(tmp_path, session_id: str):
    """Run the launcher's real start_qr_card, then stop the fake card it started.

    Returns (process, the file the card's arguments were recorded in, seconds
    taken). The real python3 never runs, so no real card can open a window.
    """
    bin_dir, args = fake_python3(tmp_path)
    script = (
        "set -euo pipefail\n"
        f'SESSION_ID="{session_id}"\nNIGHT_FILE=/x/night.json\nCARD=""\n'
        + launcher_function("start_qr_card")
        + '\nstart_qr_card\necho "card=$CARD"\n'
        # The card records its arguments from its own process: wait for that
        # before stopping it, or the kill can land first.
        'if [ -n "$CARD" ]; then\n'
        f'  for _ in $(seq 1 50); do [ -s "{args}" ] && break; sleep 0.1; done\n'
        '  kill "$CARD"\n'
        "fi\n"
    )
    started = time.monotonic()
    done = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
    )
    return done, args, time.monotonic() - started


def test_the_launch_does_not_wait_for_the_qr_card(tmp_path):
    # The card asks the relay whether to appear, and a dead one can hold that up
    # for ~10 s. The fake card here runs for 30, so a start_qr_card that waited
    # for it could not finish in 2.
    done, args, took = run_start_qr_card(tmp_path, "s")
    assert done.returncode == 0, done.stderr
    assert took < 2
    assert done.stdout.strip().removeprefix("card=").isdigit(), done.stdout
    # The night file's path and nothing else: a command line is world-readable.
    assert args.read_text().splitlines() == [
        "-m", "host.qr_window", "--card", "--night-file", "/x/night.json",
    ]


def test_no_qr_card_without_a_relay_session(tmp_path):
    # No session: no relay is configured, or it could not be reached.
    done, args, _ = run_start_qr_card(tmp_path, "")
    assert done.returncode == 0, done.stderr
    assert done.stdout == "card=\n"
    assert not args.exists()


def test_cleanup_stops_the_qr_card(tmp_path):
    # The card is on the TV, so it goes the moment the game does.
    state = tmp_path / "run-state"
    state.mkdir(mode=0o700)
    script = (
        "set -euo pipefail\n"
        "umask 077\n"
        f'STATE="{state}"\n'
        # Forked before the block sets its TERM trap: a child forked after it
        # swallows a TERM that lands before its exec, and cleanup comes at once.
        "sleep 30 & stand_in=$!\n"
        + cleanup_block()
        # CARD is set after the block, which starts it off empty.
        + "\nCARD=$stand_in\n"
        "cleanup\n"
        # The kill is a signal: give it a moment to land.
        'for _ in $(seq 1 20); do kill -0 "$CARD" 2>/dev/null || break; sleep 0.1; done\n'
        'if kill -0 "$CARD" 2>/dev/null; then kill -9 "$CARD"; echo running; '
        "else echo stopped; fi\n"
    )
    done = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "stopped"


def test_cleanup_stops_the_qr_card_before_the_slow_clear():
    # The clear can take seconds (python3 starting up, then an HTTPS round
    # trip), and the TV should not keep showing the card meanwhile.
    text = LAUNCHER.read_text()
    cleanup = text[text.index("cleanup() {"):text.index("trap cleanup")]
    assert cleanup.index('kill "$CARD"') < cleanup.index("host.session --clear")


@pytest.mark.parametrize("variable", ["JBVIP_RELAY_URL"])
def test_the_readme_does_not_tell_the_user_to_export_the_relay_variables(variable):
    # The documented procedure was the bug: an export in a terminal cannot reach
    # a Steam launch option. The README must document the file instead.
    readme = README.read_text()
    assert f"export {variable}" not in readme
    assert "~/.config/jbvip/relay.env" in readme
