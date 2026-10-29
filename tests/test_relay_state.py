import math
import time

import pytest

from relay.state import (
    MAX_SESSIONS,
    MAX_SESSION_LIFETIME_SECONDS,
    RateLimiter,
    RelayState,
    ROOMLESS_SECONDS,
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
    # json.loads accepts a bare NaN, and json.dumps would re-emit it as JSON
    # no client can parse.
    state = make()
    session = state.create_session()
    with pytest.raises(ValueError):
        state.publish(session.session_id, "WEWW", ts)


# --- the current code ---------------------------------------------------

def test_a_code_is_served_however_old_it_is():
    clock = Clock(1000.0)
    state = make(clock)
    session = state.create_session()
    state.publish(session.session_id, "WEWW", 1000.0)
    clock.t = 1000.0 + 3 * 3600
    assert state.current_code(session.session_id).code == "WEWW"


def test_the_hosts_stamp_travels_to_clients_unchanged():
    state = make()
    session = state.create_session()
    state.publish(session.session_id, "WEWW", 500_000.0)
    assert state.current_code(session.session_id).ts == 500_000.0


def test_clearing_removes_the_current_code_and_reports_it():
    state = make()
    session = state.create_session()
    state.publish(session.session_id, "WEWW", 1000.0)
    assert state.clear_code(session.session_id) is True
    assert state.current_code(session.session_id) is None


def test_clearing_with_no_code_reports_no_change():
    state = make()
    assert state.clear_code(state.create_session().session_id) is False


def test_clearing_an_unknown_session_raises_key_error():
    with pytest.raises(KeyError):
        make().clear_code("nope")


def test_the_same_code_after_a_clear_counts_as_a_change():
    state = make()
    session = state.create_session()
    state.publish(session.session_id, "WEWW", 1000.0)
    state.clear_code(session.session_id)
    assert state.publish(session.session_id, "WEWW", 1001.0) is True


def test_the_current_code_of_an_unknown_session_is_none():
    assert make().current_code("nope") is None


# --- the QR flag --------------------------------------------------------

def test_a_new_session_shows_its_qr():
    assert RelayState(now=Clock()).create_session().qr_hidden is False


def test_hiding_the_qr_is_a_change_only_once():
    state = RelayState(now=Clock())
    sid = state.create_session().session_id
    assert state.set_qr_hidden(sid, True) is True
    assert state.set_qr_hidden(sid, True) is False
    assert state.session(sid).qr_hidden is True
    assert state.set_qr_hidden(sid, False) is True


def test_the_qr_flag_does_not_move_the_sessions_expiry():
    clock = Clock()
    state = RelayState(now=clock)
    sid = state.create_session().session_id
    before = state.expires_at(sid)
    clock.t += 60
    state.set_qr_hidden(sid, True)
    assert state.expires_at(sid) == before


def test_the_qr_flag_does_not_extend_a_session_that_has_a_room():
    # The test above has no room, so the roomless deadline binds and a bump to
    # last_activity would not show. With a room, the idle deadline is the one.
    clock = Clock()
    state = RelayState(now=clock)
    sid = state.create_session().session_id
    state.publish(sid, "WEWW", 1000.0)
    before = state.expires_at(sid)
    clock.t += 60
    state.set_qr_hidden(sid, True)
    assert state.expires_at(sid) == before


def test_the_qr_flag_of_an_unknown_session_is_a_key_error():
    with pytest.raises(KeyError):
        RelayState(now=Clock()).set_qr_hidden("nope", True)


# --- activity and expiry ------------------------------------------------
# A session with no room ends at ROOMLESS_SECONDS, so the tests below that are
# about the idle and lifetime rules publish a code first. Without one, the
# roomless rule would end their sessions before those rules could.

def test_publishing_extends_the_session():
    clock = Clock(1000.0)
    state = make(clock)
    session = state.create_session()
    clock.t = 2000.0
    state.publish(session.session_id, "WEWW", 2000.0)
    assert session.last_activity == 2000.0
    assert state.expires_at(session.session_id) == 2000.0 + SESSION_IDLE_SECONDS


def test_clearing_extends_the_session():
    clock = Clock(1000.0)
    state = make(clock)
    session = state.create_session()
    state.publish(session.session_id, "WEWW", 1000.0)
    clock.t = 2000.0
    state.clear_code(session.session_id)
    assert session.last_activity == 2000.0


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
    state.publish(session.session_id, "WEWW", 1000.0)
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
    state.publish(session.session_id, "WEWW", 1000.0)
    for _ in range(2):
        clock.t += SESSION_IDLE_SECONDS - 1
        state.touch(session.session_id)
        assert state.evict_expired() == []
    assert state.session(session.session_id) is not None


def test_eviction_takes_only_the_idle_session():
    clock = Clock(1000.0)
    state = make(clock)
    stale, live = state.create_session(), state.create_session()
    state.publish(stale.session_id, "WEWW", 1000.0)
    state.publish(live.session_id, "WEWW", 1000.0)
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
    state.publish(session.session_id, "WEWW", 1000.0)
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
    state.publish(session.session_id, "WEWW", 1000.0)
    clock.t = 1000.0 + SESSION_IDLE_SECONDS + 1
    with pytest.raises(KeyError):
        state.publish(session.session_id, "WEWW", clock.t)


# --- Finding 2: an absolute ceiling bounds a session no matter how often it
# is touched, so a held stream cannot pin a slot forever ------------------

def test_a_continuously_touched_session_still_expires_at_the_absolute_ceiling():
    clock = Clock(1000.0)
    state = make(clock)
    session = state.create_session()
    state.publish(session.session_id, "WEWW", 1000.0)
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
    state.publish(session.session_id, "WEWW", 1000.0)
    step = SESSION_IDLE_SECONDS - 1
    ceiling = 1000.0 + MAX_SESSION_LIFETIME_SECONDS
    while clock.t + step < ceiling:
        clock.t += step
        state.touch(session.session_id)
    assert state.expires_at(session.session_id) == ceiling


def test_expires_at_reports_the_roomless_deadline_when_it_binds_first():
    clock = Clock(1000.0)
    state = make(clock)
    session = state.create_session()
    assert state.expires_at(session.session_id) == 1000.0 + ROOMLESS_SECONDS


# --- a night with no room ends, however many phones are listening ---------

def test_a_roomless_session_ends_after_the_window_even_while_touched():
    clock = Clock(1000.0)
    state = make(clock)
    session = state.create_session()
    state.publish(session.session_id, "WEWW", 1000.0)
    clock.t = 2000.0
    state.clear_code(session.session_id)
    clock.t = 2000.0 + ROOMLESS_SECONDS - 1
    state.touch(session.session_id)
    assert state.session(session.session_id) is not None
    clock.t = 2000.0 + ROOMLESS_SECONDS
    assert state.session(session.session_id) is None


def test_a_session_that_never_got_a_room_ends_after_the_window():
    clock = Clock(1000.0)
    state = make(clock)
    session = state.create_session()
    clock.t = 1000.0 + ROOMLESS_SECONDS
    assert state.session(session.session_id) is None


def test_a_session_with_a_room_outlives_the_roomless_window():
    clock = Clock(1000.0)
    state = make(clock)
    session = state.create_session()
    state.publish(session.session_id, "WEWW", 1000.0)
    clock.t = 1000.0 + ROOMLESS_SECONDS + 60
    assert state.session(session.session_id) is not None


def test_a_clear_that_removed_nothing_changes_nothing():
    # A launcher that clears on every exit must not be able to keep a session
    # that never got a room alive by doing so.
    clock = Clock(1000.0)
    state = make(clock)
    session = state.create_session()
    clock.t = 2000.0
    assert state.clear_code(session.session_id) is False
    assert session.last_activity == 1000.0
    assert state.expires_at(session.session_id) == 1000.0 + ROOMLESS_SECONDS


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


# --- the admin key ------------------------------------------------------

KEY = "d2f597d1c562c90fdcb4f02255abdf38aee33a342ed215663554309fb17091c6"
OTHER_KEY = "0" * 64


def test_admin_status_is_admin_wrong_off_or_guest():
    state = make()
    s = state.create_session()
    assert state.admin_status(s.session_id, KEY) == "off"
    assert state.admin_status(s.session_id, None) == "guest"
    state.register_admin(s.session_id, KEY, 1.0)
    assert state.admin_status(s.session_id, KEY) == "admin"
    assert state.admin_status(s.session_id, OTHER_KEY) == "wrong"
    assert state.admin_status(s.session_id, None) == "guest"


def test_register_admin_clamps_head_start_and_replaces():
    state = make()
    s = state.create_session()
    state.register_admin(s.session_id, KEY, -1)
    assert s.head_start == 0
    state.register_admin(s.session_id, OTHER_KEY, 99)
    assert s.head_start == 30
    assert state.admin_status(s.session_id, KEY) == "wrong"


@pytest.mark.parametrize("key", ["abc", "A" * 64, "g" * 64, None, 5])
def test_register_admin_rejects_a_bad_key(key):
    state = make()
    s = state.create_session()
    with pytest.raises(ValueError):
        state.register_admin(s.session_id, key, 1.0)


@pytest.mark.parametrize("head", [math.nan, math.inf, "x", None])
def test_register_admin_rejects_a_bad_head_start(head):
    state = make()
    s = state.create_session()
    with pytest.raises(ValueError):
        state.register_admin(s.session_id, KEY, head)


def test_register_admin_unknown_session_is_a_key_error():
    with pytest.raises(KeyError):
        make().register_admin("nope", KEY, 1.0)


def test_exhausted_charges_nothing():
    limiter = RateLimiter(limit=1, window_seconds=60.0)
    assert not limiter.exhausted() and not limiter.exhausted()
    assert limiter.allow()
    assert limiter.exhausted()
    assert not limiter.allow()
