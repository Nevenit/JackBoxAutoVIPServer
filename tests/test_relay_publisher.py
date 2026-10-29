import json
import urllib.error

import pytest

from extractor import relay_publisher
from extractor.relay_publisher import LOG_MARKER, RelayPublisher


class RecordingPost:
    """Stands in for the urllib POST so no test touches the network."""

    def __init__(self, failures=0, exception=OSError("relay down")):
        self.calls = []
        self.failures = failures
        self.exception = exception

    def __call__(self, url, token, payload):
        self.calls.append((url, token, payload))
        if len(self.calls) <= self.failures:
            raise self.exception


async def no_sleep(_seconds):
    return None


def make(post, **kwargs):
    return RelayPublisher(
        "https://relay.example/",
        "sess-1",
        "pub-token",
        post=post,
        sleep=no_sleep,
        now=lambda: 1000.0,
        **kwargs,
    )


def http_error(code):
    return urllib.error.HTTPError(
        "https://relay.example/sessions/sess-1/publish", code, "nope", {}, None
    )


class RejectingPost:
    """Raises `exception` for the listed codes, accepts every other one."""

    def __init__(self, reject, exception):
        self.calls = []
        self.reject = set(reject)
        self.exception = exception

    def __call__(self, url, token, payload):
        self.calls.append((url, token, payload))
        if payload["code"] in self.reject:
            raise self.exception


def test_a_plaintext_base_url_is_refused():
    with pytest.raises(ValueError):
        RelayPublisher("http://relay.example", "sess-1", "pub-token")


def test_an_empty_token_is_refused():
    with pytest.raises(ValueError):
        RelayPublisher("https://relay.example", "sess-1", "")


def test_an_empty_session_id_is_refused():
    with pytest.raises(ValueError):
        RelayPublisher("https://relay.example", "", "pub-token")


def test_the_publisher_is_not_a_critical_sink():
    assert RelayPublisher.critical is False


async def test_the_publish_url_is_scoped_to_the_session():
    post = RecordingPost()
    await make(post).set_code("WEWW")
    assert post.calls[0][0] == "https://relay.example/sessions/sess-1/publish"


async def test_a_trailing_slash_does_not_double_up():
    post = RecordingPost()
    await make(post).set_code("WEWW")
    assert post.calls[0][0] == "https://relay.example/sessions/sess-1/publish"


async def test_a_transient_failure_is_retried_and_then_succeeds():
    post = RecordingPost(failures=2)
    await make(post).set_code("WEWW")
    assert len(post.calls) == 3


async def test_exhausting_every_attempt_never_raises():
    post = RecordingPost(failures=99)
    await make(post).set_code("WEWW")
    assert len(post.calls) == 3


async def test_the_attempt_count_is_configurable():
    post = RecordingPost(failures=99)
    await make(post, attempts=5).set_code("WEWW")
    assert len(post.calls) == 5


async def test_a_repeated_code_is_not_republished():
    # A normal PP9 session yields the same code at least twice: once from the
    # POST /api/v2/rooms response, once from websocket_start on the play URL.
    post = RecordingPost()
    publisher = make(post)
    await publisher.set_code("WEWW")
    await publisher.set_code("WEWW")
    await publisher.set_code("ZZZZ")
    assert [call[2]["code"] for call in post.calls] == ["WEWW", "ZZZZ"]


async def test_a_repeated_code_is_republished_after_the_code_changes_back():
    post = RecordingPost()
    publisher = make(post)
    await publisher.set_code("WEWW")
    await publisher.set_code("ZZZZ")
    await publisher.set_code("WEWW")
    assert [call[2]["code"] for call in post.calls] == ["WEWW", "ZZZZ", "WEWW"]


async def test_a_failed_publish_does_not_suppress_a_later_retry_of_the_same_code():
    # Otherwise one transient outage suppresses that code for the whole session.
    post = RecordingPost(failures=99)
    publisher = make(post)
    await publisher.set_code("WEWW")
    attempts_after_first = len(post.calls)
    await publisher.set_code("WEWW")
    assert len(post.calls) > attempts_after_first


async def test_start_is_a_no_op():
    assert await make(RecordingPost()).start() is None


async def test_the_room_code_is_never_logged(caplog):
    post = RecordingPost(failures=99)
    with caplog.at_level("DEBUG"):
        await make(post).set_code("SECRET1")
    assert "SECRET1" not in caplog.text


async def test_a_publish_failure_is_marked_for_the_launcher_to_find(caplog):
    # jbvip-run.sh greps the proxy log for this marker to decide whether the
    # run said anything about the relay worth keeping.
    post = RecordingPost(failures=99)
    with caplog.at_level("WARNING"):
        await make(post).set_code("WEWW")
    assert relay_publisher.LOG_MARKER in caplog.text


async def test_set_code_never_raises_when_the_clock_fails():
    def broken_clock():
        raise RuntimeError("clock unavailable")

    publisher = RelayPublisher(
        "https://relay.example/",
        "sess-1",
        "pub-token",
        post=RecordingPost(),
        sleep=no_sleep,
        now=broken_clock,
    )
    await publisher.set_code("WEWW")  # must not raise


def test_urllib_post_builds_the_expected_request(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    def fake_urlopen(request, timeout=None):
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(relay_publisher.urllib.request, "urlopen", fake_urlopen)
    relay_publisher._urllib_post(
        "https://relay.example/publish", "pub-token", {"code": "WEWW", "ts": 1000.0}
    )

    request = captured["request"]
    assert request.full_url == "https://relay.example/publish"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer pub-token"
    assert json.loads(request.data) == {"code": "WEWW", "ts": 1000.0}
    assert captured["timeout"] == relay_publisher.REQUEST_TIMEOUT_SECONDS


async def test_a_real_urllib_error_is_caught_by_set_code(monkeypatch):
    def exploding_urlopen(request, timeout=None):
        raise urllib.error.URLError("name resolution failed")

    monkeypatch.setattr(relay_publisher.urllib.request, "urlopen", exploding_urlopen)
    publisher = RelayPublisher(
        "https://relay.example/",
        "sess-1",
        "pub-token",
        sleep=no_sleep,
        now=lambda: 1000.0,
        attempts=2,
    )
    await publisher.set_code("WEWW")  # must not raise


async def test_a_session_that_no_longer_exists_stops_the_publisher_for_good():
    post = RecordingPost(failures=99, exception=http_error(404))
    publisher = make(post)
    await publisher.set_code("WEWW")
    assert len(post.calls) == 1  # a 404 is not retried
    await publisher.set_code("ZZZZ")
    assert len(post.calls) == 1  # and nothing is attempted afterwards


async def test_a_dead_session_is_reported_once_not_once_per_frame(caplog):
    # Blobcast calls set_code once per websocket frame with the same code, and
    # a failed publish is deliberately not recorded as sent. Without the latch
    # this logs and retries at frame rate for the rest of the game.
    post = RecordingPost(failures=99, exception=http_error(404))
    publisher = make(post)
    with caplog.at_level("WARNING"):
        for _ in range(50):
            await publisher.set_code("WEWW")
    assert len(post.calls) == 1
    assert caplog.text.count(LOG_MARKER) == 1


@pytest.mark.parametrize("code", [401, 403, 410])
async def test_every_permanent_refusal_latches(code):
    post = RecordingPost(failures=99, exception=http_error(code))
    publisher = make(post)
    await publisher.set_code("WEWW")
    await publisher.set_code("ZZZZ")
    assert len(post.calls) == 1


async def test_a_refused_code_is_not_retried_but_does_not_kill_the_publisher():
    # 400 means "that code is implausible", not "this session is finished".
    post = RejectingPost({"WEWW"}, http_error(400))
    publisher = make(post)
    await publisher.set_code("WEWW")
    await publisher.set_code("WEWW")
    await publisher.set_code("ZZZZ")
    assert [call[2]["code"] for call in post.calls] == ["WEWW", "ZZZZ"]


@pytest.mark.parametrize("code", [429, 500, 502, 503])
async def test_a_transient_status_is_still_retried(code):
    # A reverse-proxy blip or a rate limit must recover, so these keep the
    # backoff they have always had.
    post = RecordingPost(failures=2, exception=http_error(code))
    await make(post).set_code("WEWW")
    assert len(post.calls) == 3


async def test_the_room_code_is_never_logged_when_the_session_dies(caplog):
    post = RecordingPost(failures=99, exception=http_error(404))
    with caplog.at_level("DEBUG"):
        await make(post).set_code("SECRET1")
    assert "SECRET1" not in caplog.text


async def test_release_posts_the_code_once(caplog):
    post = RecordingPost()
    publisher = make(post)
    await publisher.release("ABCD")
    await publisher.release("ABCD")
    assert post.calls == [
        ("https://relay.example/sessions/sess-1/release", "pub-token", {"code": "ABCD"})
    ]


async def test_release_failure_is_not_raised_retried_or_leaky(caplog):
    post = RecordingPost(failures=99)
    publisher = make(post)
    with caplog.at_level("WARNING"):
        await publisher.release("ABCD")
    assert len(post.calls) == 1
    assert LOG_MARKER in caplog.text
    assert "ABCD" not in caplog.text


async def test_release_does_nothing_once_the_publisher_is_dead():
    post = RecordingPost(failures=1, exception=http_error(404))
    publisher = make(post)
    await publisher.set_code("WXYZ")
    await publisher.release("ABCD")
    assert len(post.calls) == 1
