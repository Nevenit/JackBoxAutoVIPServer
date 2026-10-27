import asyncio
import contextlib
import re
import time
from pathlib import Path

import pytest

import relay.app
from relay.app import Relay, build_app
from relay.state import (
    MAX_SESSIONS,
    MAX_SUBSCRIBERS_PER_SESSION,
    RateLimiter,
    RelayState,
    ROOMLESS_SECONDS,
    SESSION_IDLE_SECONDS,
)
from tests.helpers import (
    Clock,
    auth,
    first_event,
    new_session,
    next_events,
    serving,
    stream_ends,
)


def make(clock=None, **limiters):
    state = RelayState(now=clock or Clock())
    return state, build_app(state, **limiters)


def publish_url(session):
    return f"/sessions/{session['session_id']}/publish"


def subscribe_url(session):
    return f"/sessions/{session['session_id']}/subscribe"


def code_url(session):
    return f"/sessions/{session['session_id']}/code"


# --- the headline requirement -------------------------------------------

async def test_a_subscriber_never_receives_another_sessions_code():
    """Two game nights at once, each blind to the other.

    This is the entire point of the rework. Nothing in the single-tenant
    suite could have caught the absence of it.
    """
    _, app = make()
    async with serving(app) as http:
        a, b = await new_session(http), await new_session(http)
        async with http.stream(
            "GET", subscribe_url(a), headers=auth(a["subscriber_token"])
        ) as to_a, http.stream(
            "GET", subscribe_url(b), headers=auth(b["subscriber_token"])
        ) as to_b:
            assert to_a.status_code == 200
            assert to_b.status_code == 200
            published = await http.post(
                publish_url(a),
                json={"code": "WEWW", "ts": 1000.0},
                headers=auth(a["publisher_token"]),
            )
            assert published.status_code == 204
            seed, code = await next_events(to_a, 2)
            assert seed["code"] is None  # a night with no room says so on connect
            assert code == {"code": "WEWW", "ts": 1000.0}
            # b's next code is its own, so nothing of a's came in between.
            # (Asserting on that, rather than on silence, keeps this from sleeping.)
            await http.post(
                publish_url(b),
                json={"code": "BBBB", "ts": 1000.0},
                headers=auth(b["publisher_token"]),
            )
            assert [e["code"] for e in await next_events(to_b, 2)] == [None, "BBBB"]


# --- session creation ---------------------------------------------------

async def test_creating_a_session_returns_an_id_and_two_tokens():
    _, app = make()
    async with serving(app) as http:
        response = await http.post("/sessions")
    assert response.status_code == 201
    body = response.json()
    assert set(body) == {
        "session_id", "publisher_token", "subscriber_token", "expires_at"
    }
    assert body["publisher_token"] != body["subscriber_token"]
    assert body["expires_at"] == 1000.0 + ROOMLESS_SECONDS


async def test_creating_a_session_needs_no_credential():
    # Anyone may run a game night: this is a public service.
    _, app = make()
    async with serving(app) as http:
        response = await http.post("/sessions")
    assert response.status_code == 201


async def test_creation_is_refused_at_capacity_rather_than_evicting():
    # Spec §8: never end a live game night to admit a stranger.
    state, app = make()
    for _ in range(MAX_SESSIONS):
        state.create_session()
    async with serving(app) as http:
        response = await http.post("/sessions")
    assert response.status_code == 503


# --- authentication -----------------------------------------------------

async def test_another_sessions_publisher_token_cannot_publish():
    state, app = make()
    async with serving(app) as http:
        a, b = await new_session(http), await new_session(http)
        response = await http.post(
            publish_url(a),
            json={"code": "WEWW", "ts": 1000.0},
            headers=auth(b["publisher_token"]),
        )
    assert response.status_code == 401
    assert state.current_code(a["session_id"]) is None


async def test_another_sessions_subscriber_token_cannot_subscribe():
    _, app = make()
    async with serving(app) as http:
        a, b = await new_session(http), await new_session(http)
        response = await http.get(
            subscribe_url(a), headers=auth(b["subscriber_token"])
        )
    assert response.status_code == 401


async def test_the_subscriber_token_cannot_publish():
    _, app = make()
    async with serving(app) as http:
        session = await new_session(http)
        response = await http.post(
            publish_url(session),
            json={"code": "WEWW", "ts": 1000.0},
            headers=auth(session["subscriber_token"]),
        )
    assert response.status_code == 401


async def test_the_publisher_token_cannot_subscribe():
    _, app = make()
    async with serving(app) as http:
        session = await new_session(http)
        response = await http.get(
            subscribe_url(session), headers=auth(session["publisher_token"])
        )
    assert response.status_code == 401


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/sessions/nope/publish"),
        ("GET", "/sessions/nope"),
        ("GET", "/sessions/nope/subscribe"),
        ("DELETE", "/sessions/nope"),
        ("DELETE", "/sessions/nope/code"),
    ],
)
async def test_an_unknown_session_is_a_404_not_a_401(method, path):
    # Spec §8. The publisher needs to tell "my session expired, stop trying
    # forever" from "my token is wrong", and a session id is not a capability
    # on its own — the QR code publishes it anyway.
    _, app = make()
    async with serving(app) as http:
        response = await http.request(method, path, headers=auth("anything"))
    assert response.status_code == 404


async def test_a_non_ascii_bearer_token_is_rejected_not_a_server_error():
    # Raw bytes, not "Bearer \xff": httpx refuses to ascii-encode that, while a
    # hand-rolled client puts the byte straight on the wire. The ASGI server
    # then hands it to Starlette, which decodes headers as latin-1.
    _, app = make()
    async with serving(app) as http:
        session = await new_session(http)
        response = await http.post(
            publish_url(session),
            json={"code": "WEWW", "ts": 1000.0},
            headers=[(b"authorization", b"Bearer \xff")],
        )
    assert response.status_code == 401


# --- publishing ---------------------------------------------------------

async def test_publishing_stores_the_code_against_that_session():
    state, app = make()
    async with serving(app) as http:
        session = await new_session(http)
        response = await http.post(
            publish_url(session),
            json={"code": "WEWW", "ts": 1000.0},
            headers=auth(session["publisher_token"]),
        )
    assert response.status_code == 204
    assert state.current_code(session["session_id"]).code == "WEWW"


async def test_an_explicit_epoch_timestamp_is_not_silently_replaced():
    # 0.0 is falsy, so a `or time.time()` default would discard it. `ts` is
    # the host's stamp for when it saw the code and reaches phones unchanged;
    # the relay replacing it with its own clock would misreport that moment.
    state, app = make()
    async with serving(app) as http:
        session = await new_session(http)
        response = await http.post(
            publish_url(session),
            json={"code": "WEWW", "ts": 0.0},
            headers=auth(session["publisher_token"]),
        )
    assert response.status_code == 204
    assert state.current_code(session["session_id"]).ts == 0.0


async def test_publishing_without_a_ts_defaults_to_now():
    # ts is optional in the request body; when absent it should fall back to
    # the relay's own wall clock, distinct from an explicit 0.0 above, which
    # must survive untouched.
    state, app = make()
    async with serving(app) as http:
        session = await new_session(http)
        before = time.time()
        response = await http.post(
            publish_url(session),
            json={"code": "WEWW"},
            headers=auth(session["publisher_token"]),
        )
        after = time.time()
    assert response.status_code == 204
    record_ts = state.current_code(session["session_id"]).ts
    assert before <= record_ts <= after


async def test_publishing_rejects_a_non_finite_timestamp():
    # json.loads accepts a bare NaN, so this reaches the handler as a float.
    _, app = make()
    async with serving(app) as http:
        session = await new_session(http)
        response = await http.post(
            publish_url(session),
            content='{"code": "WEWW", "ts": NaN}',
            headers={
                **auth(session["publisher_token"]),
                "Content-Type": "application/json",
            },
        )
    assert response.status_code == 400


async def test_publishing_rejects_an_implausible_code():
    _, app = make()
    async with serving(app) as http:
        session = await new_session(http)
        response = await http.post(
            publish_url(session),
            json={"code": "not a code", "ts": 1000.0},
            headers=auth(session["publisher_token"]),
        )
    assert response.status_code == 400


async def test_publishing_to_a_session_that_expired_mid_game_is_a_404():
    # The publisher latches on this and stops trying; the LAN server is
    # unaffected, so the game night carries on regardless.
    state, app = make()
    async with serving(app) as http:
        session = await new_session(http)
        state.delete_session(session["session_id"])
        response = await http.post(
            publish_url(session),
            json={"code": "WEWW", "ts": 1000.0},
            headers=auth(session["publisher_token"]),
        )
    assert response.status_code == 404


async def test_a_wrong_token_on_a_live_session_is_a_401_not_a_404():
    # The other half of the pair above: existence and authorisation are
    # different answers, and the publisher acts on the difference.
    _, app = make()
    async with serving(app) as http:
        session = await new_session(http)
        response = await http.post(
            publish_url(session),
            json={"code": "WEWW", "ts": 1000.0},
            headers=auth("not-the-token"),
        )
    assert response.status_code == 401


# --- clearing the room --------------------------------------------------

async def test_clearing_the_room_tells_listening_phones():
    state, app = make()
    async with serving(app) as http:
        session = await new_session(http)
        state.publish(session["session_id"], "WEWW", 1000.0)
        async with http.stream(
            "GET", subscribe_url(session), headers=auth(session["subscriber_token"])
        ) as response:
            cleared = await http.delete(
                code_url(session), headers=auth(session["publisher_token"])
            )
            assert cleared.status_code == 204
            first, second = await next_events(response, 2)
            assert first["code"] == "WEWW"
            assert second["code"] is None
            assert set(second) == {"code", "ts"}  # the shape the page reads


async def test_a_phone_that_joins_after_a_clear_is_told_there_is_no_room():
    # A phone that was asleep or reconnecting when the host quit still has the
    # closed room in its frame, and silence would leave it there.
    state, app = make()
    async with serving(app) as http:
        session = await new_session(http)
        state.publish(session["session_id"], "WEWW", 1000.0)
        state.clear_code(session["session_id"])
        async with http.stream(
            "GET", subscribe_url(session), headers=auth(session["subscriber_token"])
        ) as response:
            first = await first_event(response)
    assert first["code"] is None
    assert set(first) == {"code", "ts"}  # the shape the page reads


async def test_clearing_when_there_is_no_room_tells_nobody():
    # Nothing was removed, so there is nothing to announce: after the "no room"
    # every phone hears on connect, the next thing is the code that follows.
    # (Asserting on that, rather than on silence, keeps the test from having to
    # sleep.)
    _, app = make()
    async with serving(app) as http:
        session = await new_session(http)
        async with http.stream(
            "GET", subscribe_url(session), headers=auth(session["subscriber_token"])
        ) as response:
            cleared = await http.delete(
                code_url(session), headers=auth(session["publisher_token"])
            )
            assert cleared.status_code == 204
            await http.post(
                publish_url(session),
                json={"code": "WEWW", "ts": 1000.0},
                headers=auth(session["publisher_token"]),
            )
            seed, code = await next_events(response, 2)
            assert seed["code"] is None
            assert code == {"code": "WEWW", "ts": 1000.0}


async def test_clearing_needs_the_publisher_token():
    _, app = make()
    async with serving(app) as http:
        session = await new_session(http)
        response = await http.delete(
            code_url(session), headers=auth(session["subscriber_token"])
        )
    assert response.status_code == 401


async def test_clearing_is_charged_to_the_sessions_publish_budget():
    # Publishing and clearing share one budget. A clear with its own (or no)
    # limiter would be a way round the per-session limit.
    _, app = make(
        publish_limiter_factory=lambda: RateLimiter(limit=1, window_seconds=60.0)
    )
    async with serving(app) as http:
        session = await new_session(http)
        headers = auth(session["publisher_token"])
        published = await http.post(
            publish_url(session), json={"code": "WEWW", "ts": 1000.0}, headers=headers
        )
        cleared = await http.delete(code_url(session), headers=headers)
    assert published.status_code == 204
    assert cleared.status_code == 429


async def test_a_clear_that_loses_a_race_with_expiry_is_a_404_not_a_500(monkeypatch):
    # The clock can carry the session past its deadline between the auth check
    # and the clear itself; the state then raises KeyError.
    state, app = make()

    def expired_meanwhile(session_id):
        raise KeyError(session_id)

    monkeypatch.setattr(state, "clear_code", expired_meanwhile)
    async with serving(app) as http:
        session = await new_session(http)
        response = await http.delete(
            code_url(session), headers=auth(session["publisher_token"])
        )
    assert response.status_code == 404
    assert response.json() == {"error": "no such session"}


# --- subscribing --------------------------------------------------------

async def test_a_subscriber_receives_the_current_code_on_connect():
    state, app = make()
    async with serving(app) as http:
        session = await new_session(http)
        state.publish(session["session_id"], "WEWW", 1000.0)
        async with http.stream(
            "GET", subscribe_url(session), headers=auth(session["subscriber_token"])
        ) as response:
            assert response.status_code == 200
            assert await first_event(response) == {"code": "WEWW", "ts": 1000.0}


async def test_a_subscriber_receives_a_code_published_after_connecting():
    _, app = make()
    async with serving(app) as http:
        session = await new_session(http)
        async with http.stream(
            "GET", subscribe_url(session), headers=auth(session["subscriber_token"])
        ) as response:
            await http.post(
                publish_url(session),
                json={"code": "ZZZZ", "ts": 1000.0},
                headers=auth(session["publisher_token"]),
            )
            seed, code = await next_events(response, 2)
            assert seed["code"] is None  # nothing published yet: no room
            assert code == {"code": "ZZZZ", "ts": 1000.0}


async def test_a_late_subscriber_gets_the_current_code_however_old():
    clock = Clock(1000.0)
    state, app = make(clock)
    async with serving(app) as http:
        session = await new_session(http)
        state.publish(session["session_id"], "WEWW", 1000.0)
        clock.t = 1000.0 + 3 * 3600
        async with http.stream(
            "GET", subscribe_url(session), headers=auth(session["subscriber_token"])
        ) as response:
            assert (await first_event(response))["code"] == "WEWW"


async def test_a_full_session_drops_its_oldest_stream_for_a_new_phone():
    _, app = make()
    async with serving(app) as http:
        session = await new_session(http)
        url, headers = subscribe_url(session), auth(session["subscriber_token"])
        async with contextlib.AsyncExitStack() as stack:
            streams = [
                await stack.enter_async_context(
                    http.stream("GET", url, headers=headers)
                )
                for _ in range(MAX_SUBSCRIBERS_PER_SESSION)
            ]
            newcomer = await stack.enter_async_context(
                http.stream("GET", url, headers=headers)
            )
            assert newcomer.status_code == 200
            assert await stream_ends(streams[0])


# --- session info -------------------------------------------------------

async def test_the_publisher_can_see_that_a_phone_arrived():
    # This is how the QR window knows to close itself.
    _, app = make()
    async with serving(app) as http:
        session = await new_session(http)
        info_url = f"/sessions/{session['session_id']}"
        headers = auth(session["publisher_token"])
        before = await http.get(info_url, headers=headers)
        assert before.json()["subscribers"] == 0
        async with http.stream(
            "GET", subscribe_url(session), headers=auth(session["subscriber_token"])
        ) as response:
            assert response.status_code == 200
            after = await http.get(info_url, headers=headers)
        assert after.json()["subscribers"] == 1


async def test_polling_the_session_does_not_keep_it_alive():
    # Only publishing and subscribing count as activity. The QR window polls
    # this once a second and must not be able to extend a session by doing so.
    # Poll repeatedly *within* the idle window (each poll must succeed, since
    # the session is genuinely still alive), then cross the boundary the
    # original deadline set and confirm the session expired on schedule
    # anyway — proving the polling never pushed the deadline out. No other
    # request in this test ever reaps or otherwise touches the session, so
    # the final 404 comes from the read path alone.
    clock = Clock(1000.0)
    state, app = make(clock)
    async with serving(app) as http:
        session = await new_session(http)
        # A room, so the idle rule, not the roomless one, is what expires it.
        state.publish(session["session_id"], "WEWW", 1000.0)
        info_url = f"/sessions/{session['session_id']}"
        headers = auth(session["publisher_token"])

        clock.t += SESSION_IDLE_SECONDS - 10
        for _ in range(3):
            polled = await http.get(info_url, headers=headers)
            assert polled.status_code == 200

        clock.t += 20  # now past the original SESSION_IDLE_SECONDS deadline
        expired = await http.get(info_url, headers=headers)
    assert expired.status_code == 404
    assert state.session(session["session_id"]) is None


async def test_expires_at_moves_forward_when_the_host_publishes():
    clock = Clock(1000.0)
    _, app = make(clock)
    async with serving(app) as http:
        session = await new_session(http)
        clock.t += 60.0
        await http.post(
            publish_url(session),
            json={"code": "WEWW", "ts": 1060.0},
            headers=auth(session["publisher_token"]),
        )
        info = await http.get(
            f"/sessions/{session['session_id']}",
            headers=auth(session["publisher_token"]),
        )
    # Publishing ends the roomless deadline, so the idle one, measured from
    # the publish, is what remains.
    assert info.json()["expires_at"] == 1060.0 + SESSION_IDLE_SECONDS


# --- expiry and deletion ------------------------------------------------

async def test_a_listening_phone_keeps_its_session_alive(monkeypatch):
    """A whole game night is normally a single publish.

    The publisher de-duplicates and a Jackbox pack keeps one room code across
    many games, so without touching on the keepalive a session with a phone
    listening all evening is evicted out from under it at the four-hour mark.
    """
    monkeypatch.setattr("relay.app.KEEPALIVE_SECONDS", 0.05)
    clock = Clock(1000.0)
    state, app = make(clock)
    async with serving(app) as http:
        session = await new_session(http)
        async with http.stream(
            "GET", subscribe_url(session), headers=auth(session["subscriber_token"])
        ) as response:
            assert response.status_code == 200
            clock.t = 5000.0
            await asyncio.sleep(0.3)  # several keepalives at 50ms
            assert state.session(session["session_id"]).last_activity == 5000.0


async def test_evicting_an_idle_session_ends_its_stream():
    # Dropping the session record without closing the queue would leak the
    # stream and the phone would sit there forever on a dead session.
    # The real keepalive is 20s, so none fires during this test and nothing
    # touches the session behind the test's back.
    clock = Clock(1000.0)
    state, app = make(clock)
    async with serving(app) as http:
        session = await new_session(http)
        # A room, so the idle rule, not the roomless one, is what evicts it.
        state.publish(session["session_id"], "WEWW", 1000.0)
        async with http.stream(
            "GET", subscribe_url(session), headers=auth(session["subscriber_token"])
        ) as response:
            assert response.status_code == 200
            clock.t += SESSION_IDLE_SECONDS + 1
            await http.post("/sessions")  # any request that reaps
            assert await stream_ends(response) is True


async def test_a_stream_ends_once_its_session_has_expired(monkeypatch):
    # A session with no room ends on the clock alone, so no request arrives to
    # reap it, and the keepalive's touch() quietly does nothing for a dead
    # session. The stream has to notice for itself, or the phone waits forever.
    monkeypatch.setattr("relay.app.KEEPALIVE_SECONDS", 0.05)
    clock = Clock(1000.0)
    _, app = make(clock)
    async with serving(app) as http:
        session = await new_session(http)  # never gets a room
        async with http.stream(
            "GET", subscribe_url(session), headers=auth(session["subscriber_token"])
        ) as response:
            assert response.status_code == 200
            clock.t += ROOMLESS_SECONDS + 1
            assert await stream_ends(response) is True
        # Reaped, not just abandoned: the session's bookkeeping is released.
        assert session["session_id"] not in app.state.relay._queues


async def test_deleting_a_session_ends_its_stream_and_frees_capacity():
    state, app = make()
    async with serving(app) as http:
        session = await new_session(http)
        async with http.stream(
            "GET", subscribe_url(session), headers=auth(session["subscriber_token"])
        ) as response:
            assert response.status_code == 200
            deleted = await http.delete(
                f"/sessions/{session['session_id']}",
                headers=auth(session["publisher_token"]),
            )
            assert deleted.status_code == 204
            assert await stream_ends(response) is True
    assert state.session(session["session_id"]) is None


async def test_deleting_requires_the_publisher_token():
    state, app = make()
    async with serving(app) as http:
        session = await new_session(http)
        response = await http.delete(
            f"/sessions/{session['session_id']}",
            headers=auth(session["subscriber_token"]),
        )
    assert response.status_code == 401
    assert state.session(session["session_id"]) is not None


# --- rate limiting ------------------------------------------------------

async def test_session_creation_is_rate_limited():
    _, app = make(create_limiter=RateLimiter(limit=1, window_seconds=60.0))
    async with serving(app) as http:
        first = await http.post("/sessions")
        second = await http.post("/sessions")
    assert first.status_code == 201
    assert second.status_code == 429


async def test_one_session_exhausting_its_publish_budget_spares_the_other():
    # A shared publish budget would let one noisy host lock out every other
    # game night on the relay.
    _, app = make(
        publish_limiter_factory=lambda: RateLimiter(limit=1, window_seconds=60.0)
    )
    async with serving(app) as http:
        a, b = await new_session(http), await new_session(http)
        await http.post(
            publish_url(a),
            json={"code": "WEWW", "ts": 1000.0},
            headers=auth(a["publisher_token"]),
        )
        exhausted = await http.post(
            publish_url(a),
            json={"code": "ZZZZ", "ts": 1000.0},
            headers=auth(a["publisher_token"]),
        )
        other = await http.post(
            publish_url(b),
            json={"code": "WEWW", "ts": 1000.0},
            headers=auth(b["publisher_token"]),
        )
    assert exhausted.status_code == 429
    assert other.status_code == 204


async def test_a_bad_token_is_refused_before_the_rate_limiter_is_consulted():
    # Otherwise junk credentials exhaust the budget and lock out the holder.
    _, app = make(
        publish_limiter_factory=lambda: RateLimiter(limit=0, window_seconds=60.0)
    )
    async with serving(app) as http:
        session = await new_session(http)
        body = {"code": "WEWW", "ts": 1000.0}
        wrong = await http.post(publish_url(session), json=body, headers=auth("wrong"))
        right = await http.post(
            publish_url(session), json=body, headers=auth(session["publisher_token"])
        )
    assert wrong.status_code == 401
    assert right.status_code == 429


# --- queue hygiene (unit level: these need to poke at _queues) ----------

async def test_a_subscriber_that_stops_draining_is_dropped_and_closed():
    # Bounding the queue caps the damage, but unregistering alone is not
    # enough: an unregistered queue can no longer be reached by _release, so
    # its generator would keepalive forever, outlive both eviction and DELETE,
    # and have freed a subscriber slot for a replacement to do it again.
    relay = Relay(RelayState(now=Clock()))
    session = relay.state.create_session()
    queue = asyncio.Queue(maxsize=2)
    relay._queues[session.session_id] = dict.fromkeys([queue])
    for _ in range(3):
        relay._fan_out(session.session_id, {"code": "WEWW", "ts": 1000.0})
    assert queue not in relay._queues[session.session_id]
    drained = [queue.get_nowait() for _ in range(queue.qsize())]
    assert drained[-1] is None  # the close sentinel actually landed


async def test_an_abandoned_queue_does_not_stall_a_live_subscriber():
    relay = Relay(RelayState(now=Clock()))
    session = relay.state.create_session()
    abandoned, live = asyncio.Queue(maxsize=1), asyncio.Queue(maxsize=8)
    relay._queues[session.session_id] = dict.fromkeys([abandoned, live])
    for _ in range(3):
        relay._fan_out(session.session_id, {"code": "WEWW", "ts": 1000.0})
    assert live in relay._queues[session.session_id]
    assert live.qsize() == 3


async def test_releasing_a_session_closes_even_a_queue_that_is_full():
    # The close sentinel has to displace a payload, or the generator would run
    # until the client's TCP connection died.
    relay = Relay(RelayState(now=Clock()))
    session = relay.state.create_session()
    queue = asyncio.Queue(maxsize=1)
    relay._queues[session.session_id] = dict.fromkeys([queue])
    queue.put_nowait({"code": "WEWW", "ts": 1000.0})
    relay._release(session.session_id)
    assert queue.get_nowait() is None


async def test_releasing_a_session_forgets_its_rate_limiter():
    # Otherwise the limiter dict grows for the life of the process.
    relay = Relay(RelayState(now=Clock()))
    session = relay.state.create_session()
    relay._publish_limiters[session.session_id] = RateLimiter(1, 60.0)
    relay._release(session.session_id)
    assert session.session_id not in relay._publish_limiters


# --- the pages and logging ----------------------------------------------

async def test_the_player_page_is_served_without_credentials_and_never_cached():
    _, app = make()
    async with serving(app) as http:
        response = await http.get("/j")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    assert 'id="frame"' in response.text


async def test_the_bare_domain_says_to_scan_the_qr():
    _, app = make()
    async with serving(app) as http:
        response = await http.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    assert "Scan the QR code on the host's screen to join." in response.text


async def test_the_qr_library_is_served_by_the_relay_itself():
    _, app = make()
    async with serving(app) as http:
        response = await http.get("/static/qrcode.min.js")
    assert response.status_code == 200


def test_the_player_page_loads_no_third_party_scripts():
    html = (Path(relay.app.__file__).parent / "static" / "player.html").read_text()
    sources = re.findall(r"<script[^>]*\bsrc=\"([^\"]+)\"", html)
    assert sources and all(src.startswith("/static/") for src in sources)


async def test_the_relay_logs_neither_a_room_code_nor_a_token(caplog):
    _, app = make()
    with caplog.at_level("DEBUG", logger="relay.app"):
        async with serving(app) as http:
            session = await new_session(http)
            await http.post(
                publish_url(session),
                json={"code": "WEWW", "ts": 1000.0},
                headers=auth(session["publisher_token"]),
            )
    assert "WEWW" not in caplog.text
    assert session["publisher_token"] not in caplog.text
    assert session["subscriber_token"] not in caplog.text


# --- the client/server seam ---------------------------------------------
# Everywhere else the publisher tests inject a fake `post` and the relay tests
# post hand-built JSON, so the agreement between the two sides is asserted
# twice on the same side of the wire, by tests that cannot disagree with each
# other. A payload-key rename, a header change or a route change would pass
# the whole suite. These run the real client against the real app. The scheme
# downgrade is the only thing altered — RelayPublisher refuses a non-https
# base URL, and that check is worth keeping honest.

from extractor import relay_publisher
from extractor.relay_publisher import LOG_MARKER, RelayPublisher


def downgrading_post(url, token, payload):
    relay_publisher._urllib_post(url.replace("https://", "http://"), token, payload)


def real_publisher(http, session_id, token, **kwargs):
    return RelayPublisher(
        f"https://127.0.0.1:{http.base_url.port}",
        session_id,
        token,
        post=downgrading_post,
        **kwargs,
    )


async def test_the_real_publisher_reaches_the_real_relay():
    state, app = make()
    async with serving(app) as http:
        session = await new_session(http)
        publisher = real_publisher(
            http, session["session_id"], session["publisher_token"], now=lambda: 900.0
        )
        await publisher.set_code("WEWW")
    record = state.current_code(session["session_id"])
    assert record.code == "WEWW"
    assert record.ts == 900.0  # the publisher's stamp survived the wire


async def test_the_real_publisher_cannot_reach_another_session():
    state, app = make()
    async with serving(app) as http:
        a, b = await new_session(http), await new_session(http)
        publisher = real_publisher(
            http, a["session_id"], b["publisher_token"], attempts=1
        )
        await publisher.set_code("WEWW")
    assert state.current_code(a["session_id"]) is None
    assert state.current_code(b["session_id"]) is None


async def test_the_real_publisher_latches_when_the_real_session_is_deleted(caplog):
    # The whole 404 contract, end to end: a real HTTPError off a real socket.
    _, app = make()
    async with serving(app) as http:
        session = await new_session(http)
        publisher = real_publisher(
            http, session["session_id"], session["publisher_token"]
        )
        await publisher.set_code("WEWW")
        await http.delete(
            f"/sessions/{session['session_id']}",
            headers=auth(session["publisher_token"]),
        )
        with caplog.at_level("WARNING"):
            await publisher.set_code("ZZZZ")
            await publisher.set_code("YYYY")
    assert caplog.text.count(LOG_MARKER) == 1
