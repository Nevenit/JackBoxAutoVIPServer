import pytest

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


async def test_start_is_a_no_op():
    assert await make(RecordingPost()).start() is None


async def test_the_room_code_is_never_logged(caplog):
    post = RecordingPost(failures=99)
    with caplog.at_level("DEBUG"):
        await make(post).set_code("SECRET1")
    assert "SECRET1" not in caplog.text
