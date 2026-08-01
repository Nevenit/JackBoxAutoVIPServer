import asyncio
import contextlib
import json

import httpx
import pytest
import uvicorn

from relay.app import build_app
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
    _, app = make(tmp_path, pair_limiter=RateLimiter(limit=1, window_seconds=60.0))
    headers = {"Authorization": "Bearer pub"}
    async with serving(app) as http:
        first = await http.post("/pairing-codes", headers=headers)
        second = await http.post("/pairing-codes", headers=headers)
    assert first.status_code == 200
    assert second.status_code == 429
