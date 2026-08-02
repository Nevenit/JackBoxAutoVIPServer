import asyncio
import contextlib
import json
import urllib.request

import httpx
import pytest
import uvicorn

import pair
from extractor import relay_publisher
from extractor.relay_publisher import RelayPublisher
from relay.app import Relay, build_app
from relay.state import RateLimiter, RelayState


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def make(tmp_path, clock=None, subscribers=("sub",), **limiters):
    state = RelayState("pub", subscribers, now=clock or Clock())
    return state, build_app(state, tmp_path / "tokens.json", **limiters)


@contextlib.asynccontextmanager
async def serving(app):
    """Run the app on a real uvicorn server bound to an ephemeral port.

    httpx's ASGITransport drains the whole app coroutine before returning a
    response, which deadlocks against the deliberately-infinite SSE generator
    (encode/httpx#2186). A real server streams properly.
    """
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            if task.done():
                await task  # surfaces a startup failure instead of spinning
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
            yield http
    finally:
        server.should_exit = True
        await task


async def first_event(response, timeout=3.0):
    async def read():
        async for line in response.aiter_lines():
            if line.startswith("data: "):
                return json.loads(line[len("data: "):])
        return None

    return await asyncio.wait_for(read(), timeout)


async def test_publish_requires_the_publisher_token(tmp_path):
    _, app = make(tmp_path)
    async with serving(app) as http:
        response = await http.post(
            "/publish",
            json={"code": "WEWW", "ts": 1000.0},
            headers={"Authorization": "Bearer wrong"},
        )
    assert response.status_code == 401


async def test_publish_accepts_a_valid_code(tmp_path):
    state, app = make(tmp_path)
    async with serving(app) as http:
        response = await http.post(
            "/publish",
            json={"code": "WEWW", "ts": 1000.0},
            headers={"Authorization": "Bearer pub"},
        )
    assert response.status_code == 204
    assert state.current_code().code == "WEWW"


async def test_a_non_ascii_bearer_token_is_rejected_not_a_server_error(tmp_path):
    # Raw bytes, not "Bearer \xff": httpx refuses to ascii-encode that, while a
    # hand-rolled client puts the byte straight on the wire. The ASGI server
    # then hands it to Starlette, which decodes headers as latin-1.
    _, app = make(tmp_path)
    async with serving(app) as http:
        response = await http.post(
            "/publish",
            json={"code": "WEWW", "ts": 1000.0},
            headers=[(b"authorization", b"Bearer \xff")],
        )
    assert response.status_code == 401


async def test_publish_rejects_a_non_finite_timestamp(tmp_path):
    # json.loads accepts a bare NaN, so this reaches the handler as a float.
    _, app = make(tmp_path)
    async with serving(app) as http:
        response = await http.post(
            "/publish",
            content='{"code": "WEWW", "ts": NaN}',
            headers={
                "Authorization": "Bearer pub",
                "Content-Type": "application/json",
            },
        )
    assert response.status_code == 400


async def test_publish_rejects_an_implausible_code(tmp_path):
    _, app = make(tmp_path)
    async with serving(app) as http:
        response = await http.post(
            "/publish",
            json={"code": "not a code", "ts": 1000.0},
            headers={"Authorization": "Bearer pub"},
        )
    assert response.status_code == 400


async def test_subscribe_requires_a_subscriber_token(tmp_path):
    _, app = make(tmp_path)
    async with serving(app) as http:
        response = await http.get(
            "/subscribe", headers={"Authorization": "Bearer pub"}
        )
    assert response.status_code == 401


async def test_subscriber_receives_the_current_code_on_connect(tmp_path):
    state, app = make(tmp_path)
    state.publish("WEWW", 1000.0)
    async with serving(app) as http:
        async with http.stream(
            "GET", "/subscribe", headers={"Authorization": "Bearer sub"}
        ) as response:
            assert response.status_code == 200
            assert await first_event(response) == {"code": "WEWW", "ts": 1000.0}


async def test_subscriber_is_not_sent_a_stale_code_on_connect(tmp_path):
    clock = Clock(1000.0)
    state, app = make(tmp_path, clock=clock)
    state.publish("WEWW", 1000.0)
    clock.t = 2000.0
    async with serving(app) as http:
        async with http.stream(
            "GET", "/subscribe", headers={"Authorization": "Bearer sub"}
        ) as response:
            with pytest.raises(asyncio.TimeoutError):
                await first_event(response, timeout=0.5)


async def test_subscriber_receives_a_code_published_after_connecting(tmp_path):
    _, app = make(tmp_path)
    async with serving(app) as http:
        async with http.stream(
            "GET", "/subscribe", headers={"Authorization": "Bearer sub"}
        ) as response:
            await http.post(
                "/publish",
                json={"code": "ZZZZ", "ts": 1000.0},
                headers={"Authorization": "Bearer pub"},
            )
            assert await first_event(response) == {"code": "ZZZZ", "ts": 1000.0}


async def test_minting_a_pairing_code_requires_the_publisher_token(tmp_path):
    _, app = make(tmp_path)
    async with serving(app) as http:
        response = await http.post(
            "/pairing-codes", headers={"Authorization": "Bearer sub"}
        )
    assert response.status_code == 401


async def test_pairing_round_trip_yields_a_usable_subscriber_token(tmp_path):
    state, app = make(tmp_path, subscribers=())
    async with serving(app) as http:
        minted = await http.post(
            "/pairing-codes", headers={"Authorization": "Bearer pub"}
        )
        pairing_code = minted.json()["pairing_code"]
        paired = await http.post("/pair", json={"pairing_code": pairing_code})
        token = paired.json()["subscriber_token"]

        assert state.is_subscriber(token)
        async with http.stream(
            "GET", "/subscribe", headers={"Authorization": f"Bearer {token}"}
        ) as response:
            assert response.status_code == 200


async def test_pairing_persists_the_token_to_disk(tmp_path):
    from relay.tokens import load_tokens

    _, app = make(tmp_path, subscribers=())
    async with serving(app) as http:
        minted = await http.post(
            "/pairing-codes", headers={"Authorization": "Bearer pub"}
        )
        paired = await http.post(
            "/pair", json={"pairing_code": minted.json()["pairing_code"]}
        )
    assert load_tokens(tmp_path / "tokens.json") == [
        paired.json()["subscriber_token"]
    ]


def break_saving(monkeypatch):
    """Make persistence fail the way a root-owned /var/lib/jbvip would."""
    import relay.app

    def explode(path, tokens):
        raise OSError("read-only file system")

    monkeypatch.setattr(relay.app, "save_tokens", explode)


async def pair_once(http):
    minted = await http.post(
        "/pairing-codes", headers={"Authorization": "Bearer pub"}
    )
    return await http.post(
        "/pair", json={"pairing_code": minted.json()["pairing_code"]}
    )


async def test_pairing_still_hands_back_a_token_when_the_save_fails(
    tmp_path, monkeypatch
):
    # The pairing code is already burned by the time save_tokens runs. Failing
    # the request would leave the phone with nothing and the code unusable, so
    # a subscriber that works until the next restart is the better degradation.
    break_saving(monkeypatch)
    state, app = make(tmp_path, subscribers=())
    async with serving(app) as http:
        paired = await pair_once(http)

    assert paired.status_code == 200
    assert state.is_subscriber(paired.json()["subscriber_token"])


async def test_a_failed_token_save_is_warned_about_without_the_token(
    tmp_path, monkeypatch, caplog
):
    break_saving(monkeypatch)
    _, app = make(tmp_path, subscribers=())
    with caplog.at_level("WARNING"):
        async with serving(app) as http:
            paired = await pair_once(http)

    assert "read-only file system" in caplog.text
    assert paired.json()["subscriber_token"] not in caplog.text


async def test_a_subscriber_that_stops_draining_is_dropped(tmp_path):
    # The stream generator's `finally` is the only place a queue is
    # unregistered, and it never runs if the task is cancelled before the
    # generator body starts. Bounding the queue caps the damage to 64 payloads.
    relay = Relay(RelayState("pub", ["sub"], now=Clock()), tmp_path / "tokens.json")
    queue = asyncio.Queue(maxsize=2)
    relay._subscribers.add(queue)
    for _ in range(3):
        relay._fan_out({"code": "WEWW", "ts": 1000.0})
    assert queue not in relay._subscribers
    assert queue.qsize() == 2


async def test_an_abandoned_queue_does_not_stall_a_live_subscriber(tmp_path):
    relay = Relay(RelayState("pub", ["sub"], now=Clock()), tmp_path / "tokens.json")
    abandoned, live = asyncio.Queue(maxsize=1), asyncio.Queue(maxsize=8)
    relay._subscribers.update({abandoned, live})
    for _ in range(3):
        relay._fan_out({"code": "WEWW", "ts": 1000.0})
    assert live in relay._subscribers
    assert live.qsize() == 3


async def test_an_unknown_pairing_code_is_refused(tmp_path):
    _, app = make(tmp_path)
    async with serving(app) as http:
        response = await http.post("/pair", json={"pairing_code": "ZZZZZZ"})
    assert response.status_code == 400


async def test_publishing_is_rate_limited(tmp_path):
    _, app = make(
        tmp_path, publish_limiter=RateLimiter(limit=1, window_seconds=60.0)
    )
    body = {"code": "WEWW", "ts": 1000.0}
    headers = {"Authorization": "Bearer pub"}
    async with serving(app) as http:
        first = await http.post("/publish", json=body, headers=headers)
        second = await http.post("/publish", json=body, headers=headers)
    assert first.status_code == 204
    assert second.status_code == 429


async def test_pairing_attempts_are_rate_limited(tmp_path):
    _, app = make(tmp_path, pair_limiter=RateLimiter(limit=1, window_seconds=60.0))
    async with serving(app) as http:
        first = await http.post("/pair", json={"pairing_code": "ZZZZZZ"})
        second = await http.post("/pair", json={"pairing_code": "ZZZZZZ"})
    assert first.status_code == 400
    assert second.status_code == 429


async def test_minting_pairing_codes_is_rate_limited(tmp_path):
    _, app = make(tmp_path, mint_limiter=RateLimiter(limit=1, window_seconds=60.0))
    headers = {"Authorization": "Bearer pub"}
    async with serving(app) as http:
        first = await http.post("/pairing-codes", headers=headers)
        second = await http.post("/pairing-codes", headers=headers)
    assert first.status_code == 200
    assert second.status_code == 429


async def test_anonymous_pair_spam_cannot_lock_out_minting(tmp_path):
    _, app = make(tmp_path, pair_limiter=RateLimiter(limit=1, window_seconds=60.0))
    async with serving(app) as http:
        await http.post("/pair", json={"pairing_code": "ZZZZZZ"})
        exhausted = await http.post("/pair", json={"pairing_code": "ZZZZZZ"})
        minted = await http.post(
            "/pairing-codes", headers={"Authorization": "Bearer pub"}
        )
    assert exhausted.status_code == 429
    assert minted.status_code == 200


# --- the client/server seam ---------------------------------------------
# Everywhere else the publisher tests inject a fake `post` and the relay tests
# post hand-built JSON, so the agreement between the two sides is asserted
# twice on the same side of the wire, by tests that cannot disagree with each
# other. A payload-key rename, a header change or a route change would pass the
# whole suite. These run the real clients against the real app.
#
# Both clients refuse a non-https base URL — TLS is terminated in front of the
# relay, so that check is the only place it can be enforced. The injection
# points exist for tests; they downgrade the scheme and change nothing else, so
# the real request building, the real headers and the real routes are exercised.
# asyncio.to_thread keeps the blocking request off the loop, so the live server
# can serve it.


def downgrading_post(url, token, payload):
    relay_publisher._urllib_post(url.replace("https://", "http://"), token, payload)


def downgrading_opener(request, timeout=None):
    downgraded = urllib.request.Request(
        request.full_url.replace("https://", "http://"),
        data=request.data,
        headers=dict(request.header_items()),
        method=request.get_method(),
    )
    return urllib.request.urlopen(downgraded, timeout=timeout)


async def test_the_real_publisher_reaches_the_real_relay(tmp_path):
    state, app = make(tmp_path)
    async with serving(app) as http:
        publisher = RelayPublisher(
            f"https://127.0.0.1:{http.base_url.port}",
            "pub",
            post=downgrading_post,
            now=lambda: 900.0,
        )
        await publisher.set_code("WEWW")

    record = state.current_code()
    assert record.code == "WEWW"
    assert record.ts == 900.0  # the publisher's stamp survived the wire


async def test_the_real_publisher_is_rejected_by_the_real_relay_without_the_token(
    tmp_path,
):
    state, app = make(tmp_path)
    async with serving(app) as http:
        publisher = RelayPublisher(
            f"https://127.0.0.1:{http.base_url.port}",
            "not-the-publisher-token",
            post=downgrading_post,
            attempts=1,
        )
        await publisher.set_code("WEWW")

    assert state.current_code() is None


async def test_the_real_pair_cli_mints_a_code_the_relay_will_redeem(tmp_path):
    state, app = make(tmp_path, subscribers=())
    async with serving(app) as http:
        pairing_code = await asyncio.to_thread(
            pair.mint,
            f"https://127.0.0.1:{http.base_url.port}",
            "pub",
            opener=downgrading_opener,
        )
        paired = await http.post("/pair", json={"pairing_code": pairing_code})

    assert paired.status_code == 200
    assert state.is_subscriber(paired.json()["subscriber_token"])
