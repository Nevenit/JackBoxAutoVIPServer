import math
import time

import pytest

from relay.state import (
    MAX_SESSIONS,
    MAX_SESSION_LIFETIME_SECONDS,
    RateLimiter,
    RelayState,
    Session,
    SessionCapacityError,
    SESSION_IDLE_SECONDS,
)


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def make(clock=None):
    return RelayState(now=clock or Clock())


# --- session creation ---------------------------------------------------

def test_a_new_session_gets_an_id_and_two_distinct_tokens():
    session = make().create_session()
    assert isinstance(session, Session)
    assert session.session_id
    assert session.publisher_token != session.subscriber_token
    assert session.record is None


def test_two_sessions_share_no_identifier_or_token():
    state = make()
    a, b = state.create_session(), state.create_session()
    assert len({a.session_id, b.session_id}) == 2
    assert len({a.publisher_token, b.publisher_token,
                a.subscriber_token, b.subscriber_token}) == 4


def test_a_session_can_be_looked_up_by_id():
    state = make()
    session = state.create_session()
    assert state.session(session.session_id) is session


def test_an_unknown_session_id_is_none_not_an_error():
    state = make()
    assert state.session("nope") is None
    assert state.session(None) is None
    assert state.session(17) is None


# --- authentication -----------------------------------------------------

def test_the_publisher_token_authenticates_its_own_session():
    state = make()
    session = state.create_session()
    assert state.is_publisher(session.session_id, session.publisher_token)
    assert state.is_subscriber(session.session_id, session.subscriber_token)


def test_a_session_id_is_not_a_capability_on_its_own():
    # The whole point of two credentials: knowing an id must buy nothing.
    state = make()
    session = state.create_session()
    assert not state.is_publisher(session.session_id, session.subscriber_token)
    assert not state.is_subscriber(session.session_id, session.publisher_token)
    assert not state.is_publisher(session.session_id, "")


def test_another_sessions_token_is_rejected():
    state = make()
    a, b = state.create_session(), state.create_session()
    assert not state.is_publisher(a.session_id, b.publisher_token)
    assert not state.is_subscriber(a.session_id, b.subscriber_token)


def test_a_token_for_an_unknown_session_is_rejected():
    state = make()
    session = state.create_session()
    assert not state.is_publisher("nope", session.publisher_token)


@pytest.mark.parametrize("token", ["Bearer \xff", "ÿ", 17, None, ""])
def test_a_junk_token_is_false_not_an_exception(token):
    # Starlette decodes headers as latin-1, so a byte >= 0x80 arrives as a
    # non-ASCII str, and secrets.compare_digest raises TypeError on those.
    # An unauthenticated caller must get a False, never a 500.
    state = make()
    session = state.create_session()
    assert state.is_publisher(session.session_id, token) is False


# --- publishing ---------------------------------------------------------

def test_publishing_stores_the_code_and_reports_the_change():
    state = make()
    session = state.create_session()
    assert state.publish(session.session_id, "WEWW", 1000.0) is True
    assert state.current_code(session.session_id).code == "WEWW"


def test_republishing_the_same_code_reports_no_change():
    state = make()
    session = state.create_session()
    state.publish(session.session_id, "WEWW", 1000.0)
    assert state.publish(session.session_id, "WEWW", 1001.0) is False
    assert state.publish(session.session_id, "ZZZZ", 1002.0) is True


def test_publishing_to_one_session_leaves_the_other_alone():
    # The headline requirement, at the state layer.
    state = make()
    a, b = state.create_session(), state.create_session()
    state.publish(a.session_id, "WEWW", 1000.0)
    assert state.current_code(a.session_id).code == "WEWW"
    assert state.current_code(b.session_id) is None


def test_publishing_to_an_unknown_session_raises_key_error():
    with pytest.raises(KeyError):
        make().publish("nope", "WEWW", 1000.0)


@pytest.mark.parametrize("code", ["", "x", "toolongcode", "not a code", 17, None])
def test_an_implausible_code_is_refused(code):
    state = make()
    session = state.create_session()
    with pytest.raises(ValueError):
        state.publish(session.session_id, code, 1000.0)


@pytest.mark.parametrize("ts", [math.nan, math.inf, -math.inf])
def test_a_non_finite_timestamp_is_refused(ts):
    # json.loads accepts a bare NaN, `now - nan > window` is False, so a NaN
    # stamp would be permanently fresh and json.dumps would re-emit it as
    # JSON no client can parse.
    state = make()
    session = state.create_session()
    with pytest.raises(ValueError):
        state.publish(session.session_id, "WEWW", ts)


# --- freshness ----------------------------------------------------------

def test_a_code_older_than_the_window_is_withheld():
    clock = Clock(1000.0)
    state = make(clock)
    session = state.create_session()
    state.publish(session.session_id, "WEWW", 1000.0)
    clock.t = 1000.0 + 121.0
    assert state.current_code(session.session_id) is None


def test_freshness_uses_the_relays_clock_not_the_publishers_stamp():
    # A dual-boot host writing localtime to the RTC is hours out. Its stamp
    # must not be able to make a code look permanently fresh or permanently
    # stale — only the relay's own receipt time counts.
    clock = Clock(1000.0)
    state = make(clock)
    session = state.create_session()
    state.publish(session.session_id, "WEWW", 500_000.0)
    record = state.current_code(session.session_id)
    assert record.ts == 500_000.0       # the host's stamp travels to clients
    assert record.received_at == 1000.0  # the relay's clock gates freshness
    clock.t = 1121.0
    assert state.current_code(session.session_id) is None


def test_the_current_code_of_an_unknown_session_is_none():
    assert make().current_code("nope") is None


# --- activity and expiry ------------------------------------------------

def test_publishing_extends_the_session():
    clock = Clock(1000.0)
    state = make(clock)
    session = state.create_session()
    clock.t = 2000.0
    state.publish(session.session_id, "WEWW", 2000.0)
    assert session.last_activity == 2000.0
    assert state.expires_at(session.session_id) == 2000.0 + SESSION_IDLE_SECONDS


def test_touching_extends_the_session():
    # A phone listening all evening to a host that has not started a new room
    # must not have its session collected out from under it.
    clock = Clock(1000.0)
    state = make(clock)
    session = state.create_session()
    clock.t = 2000.0
    state.touch(session.session_id)
    assert session.last_activity == 2000.0


def test_touching_an_unknown_session_is_a_no_op():
    make().touch("nope")  # must not raise


def test_expires_at_is_none_for_an_unknown_session():
    assert make().expires_at("nope") is None


def test_an_idle_session_is_evicted_and_its_id_returned():
    clock = Clock(1000.0)
    state = make(clock)
    session = state.create_session()
    clock.t = 1000.0 + SESSION_IDLE_SECONDS + 1
    assert state.evict_expired() == [session.session_id]
    assert state.session(session.session_id) is None


def test_a_session_kept_alive_by_a_subscriber_is_not_evicted():
    # Well within MAX_SESSION_LIFETIME_SECONDS: this proves touching resets
    # the *idle* clock. What happens once the absolute ceiling is reached
    # despite continuous touching is a separate test, below.
    clock = Clock(1000.0)
    state = make(clock)
    session = state.create_session()
    for _ in range(2):
        clock.t += SESSION_IDLE_SECONDS - 1
        state.touch(session.session_id)
        assert state.evict_expired() == []
    assert state.session(session.session_id) is not None


def test_eviction_takes_only_the_idle_session():
    clock = Clock(1000.0)
    state = make(clock)
    stale, live = state.create_session(), state.create_session()
    # Touch `live` before it crosses its own idle deadline — touching after
    # the fact must not be able to resurrect it; that is Finding 1's whole
    # point, and is covered on its own above.
    clock.t += SESSION_IDLE_SECONDS - 1
    state.touch(live.session_id)
    clock.t += 2
    assert state.evict_expired() == [stale.session_id]
    assert state.session(live.session_id) is not None


# --- Finding 1: expiry must be enforced on the read path, not just by reaping ---

def test_an_idle_sessions_credentials_stop_authenticating_with_no_reaping():
    # No call to evict_expired() anywhere in this test: the read path alone
    # must refuse an idle session, or a quiet relay never enforces its own
    # advertised lifetime.
    clock = Clock(1000.0)
    state = make(clock)
    session = state.create_session()
    clock.t = 1000.0 + SESSION_IDLE_SECONDS + 1
    assert state.is_publisher(session.session_id, session.publisher_token) is False
    assert state.is_subscriber(session.session_id, session.subscriber_token) is False
    assert state.session(session.session_id) is None


def test_current_code_refuses_an_expired_session():
    clock = Clock(1000.0)
    state = make(clock)
    session = state.create_session()
    state.publish(session.session_id, "WEWW", 1000.0)
    clock.t = 1000.0 + SESSION_IDLE_SECONDS + 1
    assert state.current_code(session.session_id) is None


def test_publish_refuses_an_expired_session():
    clock = Clock(1000.0)
    state = make(clock)
    session = state.create_session()
    clock.t = 1000.0 + SESSION_IDLE_SECONDS + 1
    with pytest.raises(KeyError):
        state.publish(session.session_id, "WEWW", clock.t)


# --- Finding 2: an absolute ceiling bounds a session no matter how often it
# is touched, so a held stream cannot pin a slot forever ------------------

def test_a_continuously_touched_session_still_expires_at_the_absolute_ceiling():
    clock = Clock(1000.0)
    state = make(clock)
    session = state.create_session()
    step = SESSION_IDLE_SECONDS - 1  # shorter than the idle window every time
    elapsed = 0.0
    while elapsed <= MAX_SESSION_LIFETIME_SECONDS:
        clock.t += step
        elapsed += step
        state.touch(session.session_id)
    # Continuous touching kept it alive against idle eviction the whole way,
    # yet the absolute ceiling still ends it.
    assert state.session(session.session_id) is None
    assert state.is_publisher(session.session_id, session.publisher_token) is False


# --- expires_at reports whichever deadline actually binds -----------------

def test_expires_at_reflects_the_absolute_ceiling_once_it_binds():
    # Touch often enough (each gap shorter than the idle window, so none of
    # them is ever refused) that the idle deadline keeps moving out ahead of
    # the absolute ceiling. Near the end of the session's life, expires_at
    # must report the ceiling — that is the deadline that actually binds.
    clock = Clock(1000.0)
    state = make(clock)
    session = state.create_session()
    step = SESSION_IDLE_SECONDS - 1
    ceiling = 1000.0 + MAX_SESSION_LIFETIME_SECONDS
    while clock.t + step < ceiling:
        clock.t += step
        state.touch(session.session_id)
    assert state.expires_at(session.session_id) == ceiling


# --- capacity -----------------------------------------------------------

def test_creation_is_refused_at_capacity_rather_than_evicting_a_live_session():
    # Spec §8: never evict a live session to admit a stranger.
    state = make()
    sessions = [state.create_session() for _ in range(MAX_SESSIONS)]
    with pytest.raises(SessionCapacityError):
        state.create_session()
    assert state.session(sessions[0].session_id) is not None


def test_deleting_a_session_frees_capacity():
    state = make()
    sessions = [state.create_session() for _ in range(MAX_SESSIONS)]
    assert state.delete_session(sessions[0].session_id) is True
    assert state.create_session() is not None


def test_deleting_an_unknown_session_reports_false():
    assert make().delete_session("nope") is False


def test_evicting_frees_capacity():
    clock = Clock(1000.0)
    state = make(clock)
    for _ in range(MAX_SESSIONS):
        state.create_session()
    clock.t += SESSION_IDLE_SECONDS + 1
    assert len(state.evict_expired()) == MAX_SESSIONS
    assert state.create_session() is not None


# --- rate limiter (carried over unchanged) ------------------------------

def test_the_rate_limiter_allows_up_to_the_limit_then_refuses():
    clock = Clock(0.0)
    limiter = RateLimiter(limit=2, window_seconds=60.0, now=clock)
    assert [limiter.allow() for _ in range(3)] == [True, True, False]


def test_the_rate_limiter_forgets_hits_outside_the_window():
    clock = Clock(0.0)
    limiter = RateLimiter(limit=1, window_seconds=60.0, now=clock)
    assert limiter.allow() is True
    clock.t = 61.0
    assert limiter.allow() is True


def test_the_rate_limiter_defaults_to_the_monotonic_clock():
    # Never the wall clock: an NTP step backwards would widen the window.
    limiter = RateLimiter(limit=1, window_seconds=60.0)
    assert limiter._now is time.monotonic
