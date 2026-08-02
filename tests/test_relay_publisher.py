import json
import urllib.error

import pytest

from extractor import relay_publisher
from extractor.relay_publisher import RelayPublisher


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
        "pub-token",
        post=post,
        sleep=no_sleep,
        now=lambda: 1000.0,
        **kwargs,
    )


def test_a_plaintext_base_url_is_refused():
    with pytest.raises(ValueError):
        RelayPublisher("http://relay.example", "pub-token")


def test_an_empty_token_is_refused():
    with pytest.raises(ValueError):
        RelayPublisher("https://relay.example", "")


def test_the_publisher_is_not_a_critical_sink():
    assert RelayPublisher.critical is False


async def test_set_code_posts_the_code_to_the_publish_endpoint():
    post = RecordingPost()
    await make(post).set_code("WEWW")
    url, token, payload = post.calls[0]
    assert url == "https://relay.example/publish"
    assert token == "pub-token"
    assert payload == {"code": "WEWW", "ts": 1000.0}


async def test_a_trailing_slash_does_not_double_up():
    post = RecordingPost()
    await make(post).set_code("WEWW")
    assert post.calls[0][0] == "https://relay.example/publish"


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
        "pub-token",
        sleep=no_sleep,
        now=lambda: 1000.0,
        attempts=2,
    )
    await publisher.set_code("WEWW")  # must not raise
