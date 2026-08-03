# Multi-Tenant Relay — Design Spec

**Date:** 2026-08-03
**Status:** Approved
**Supersedes:** the relay portions of `2026-08-01-jackbox-auto-join-design.md`
(§5.1, §5.5, §5.6 and the pairing flow). The extraction side, the injection
script and the freshness model carry over unchanged.

---

## 1. Goal & context

The relay built on the `auto-join-client` branch serves **one host**. It holds a
single current-code slot and a single publisher token, so a second host
publishing overwrites the first and every phone receives both hosts' codes
interleaved with no way to tell them apart. A phone is bound to *the relay*, not
to *the PC that paired it*.

That is not what the relay is for. It should be a **public service** on which
many independent game nights run at once, where each phone receives only the
codes from the host that paired it, and pairing happens by scanning a QR code on
the host's screen.

This spec reworks the relay to be multi-tenant and ephemeral. It makes the system
**smaller**, not larger: several components are deleted outright.

## 2. What changes, in one paragraph

A **session** becomes the unit of tenancy. Anyone may create one; it is issued
its own publisher and subscriber tokens and expires on its own. The host displays
a QR carrying the relay URL, the session id and the subscriber token. The phone
scans it and subscribes to that session alone. Nothing persists to disk, there is
no global admin credential, and there is no revocation path — a session dies a
few hours after it stops being used, and with it every credential it issued.

## 3. Non-goals

- **Durable pairings.** A phone is paired for one session. Scanning again next
  time is the accepted cost, and it is what removes the token store, the atomic
  write machinery, the revocation path and the pairing-code exchange.
- **Per-IP rate limiting in the relay.** That requires trusting
  `X-Forwarded-For`, which is spoofable and a configuration footgun. The reverse
  proxy already knows the true client IP and does this properly. The relay caps
  total resource use; the proxy does per-client. Documented, not built.
- **Accounts, admin UI, session listing, or any notion of a user.**
- **Migration from the single-tenant relay.** It was never deployed, so the HTTP
  surface can change completely at no cost.
- The Android app, which remains a separate future plan. Its contract with this
  spec is the QR payload format (§7) and `GET /sessions/<id>/subscribe`.

## 4. Architecture

```
  Jackbox game (host PC)
        │ TLS, transparently redirected (unchanged)
        ▼
  mitmdump + extractor/addon.py         ← extraction unchanged
        │ fans out to sinks
        ├──────────────► RoomCodeServer          (unchanged, LAN TCP :38469)
        └──────────────► RelayPublisher          (now session-scoped)
                              │ POST /sessions/<id>/publish
                              ▼
                         Relay on VPS            (multi-tenant, in-memory)
                              │ SSE GET /sessions/<id>/subscribe
                              ▼
                         Phone, paired by QR to THAT session
```

The launcher creates the session and shows the QR *before* the game starts, then
hands the session's credentials to mitmdump.

## 5. Components

### 5.1 `relay/state.py` — pure state (rewritten)

Keeps the shape that worked: no HTTP, no disk, an injected clock, so the whole
decision surface tests as plain function calls.

- `Session` — a mutable record holding `session_id`, `publisher_token`,
  `subscriber_token`, `created_at`, `last_activity`, and
  `record: CodeRecord | None`. Mutable because `last_activity` and `record`
  change over its life; `session_id` is carried on the record so callers never
  have to pair it back up with its key.
- `CodeRecord` — unchanged: `code`, `ts` (the host's stamp), `received_at` (the
  relay's own clock).
- `RelayState` holds `dict[str, Session]` and exposes:
  - `create_session() -> Session` — mints the id and both tokens
  - `session(session_id) -> Session | None`
  - `publish(session_id, code, ts) -> bool` — stores, stamps `received_at`,
    refreshes `last_activity`, returns whether the code changed
  - `current_code(session_id) -> CodeRecord | None` — `None` when absent or
    older than `FRESH_SECONDS`
  - `touch(session_id)` — refreshes `last_activity`; called when a subscriber
    connects, so a session with a listening phone but no new rooms stays alive
  - `evict_expired() -> list[str]` — drops idle sessions and **returns their
    ids**, so the app layer can close their queues rather than leaking them
  - `delete_session(session_id)`

Both publishing and subscribing count as activity. A session dies only when
neither has happened for `SESSION_IDLE_SECONDS`.
- Token comparison stays `secrets.compare_digest`, guarded by the
  `isinstance`/non-empty/`isascii()` checks the current code already has — the
  non-ASCII guard exists because Starlette decodes headers as latin-1 and
  `compare_digest` raises on non-ASCII input.

Constants:

| Name | Value | Why |
|---|---|---|
| `FRESH_SECONDS` | `120.0` | Unchanged. Jackbox recycles four-letter codes. |
| `SESSION_IDLE_SECONDS` | `4 * 3600` | Kills abandoned sessions without cutting short a long game night. |
| `MAX_SESSIONS` | `500` | Total capacity. |
| `MAX_SUBSCRIBERS_PER_SESSION` | `16` | A Jackbox room holds ~10 players. |
| `SESSION_ID_BYTES` | `9` | 12 url-safe chars — short in a QR, unguessable enough given it is paired with a token. |
| `TOKEN_BYTES` | `32` | Both tokens, via `secrets.token_urlsafe`. |

### 5.2 `relay/app.py` — ASGI layer (rewritten)

Thin: routes, authenticates, streams. Holds `dict[str, set[asyncio.Queue]]`
keyed by session — the live SSE queues, which are the one thing that cannot live
in the pure layer.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/sessions` | none | Create; returns `{session_id, publisher_token, subscriber_token, expires_at}` |
| GET | `/sessions/<id>` | publisher | `{subscribers, expires_at}` — lets the QR window know a phone arrived |

`expires_at` is a projection of the current `last_activity`, not a fixed
deadline: it moves forward every time the session is published to or subscribed
to. Clients should treat it as "dead after this unless something happens".
| POST | `/sessions/<id>/publish` | publisher | Body `{"code", "ts"?}`; `ts` defaults to now |
| GET | `/sessions/<id>/subscribe` | subscriber | SSE stream of `{"code", "ts"}` |
| DELETE | `/sessions/<id>` | publisher | Tidy up on game exit, freeing capacity immediately |
| GET | `/j` | none | One-line page so a plain camera app scanning the QR does something sensible |

Rules that carry over from the current implementation and must not regress:

- **Authenticate before rate-limiting** on authenticated endpoints, so junk
  credentials cannot exhaust a budget and lock out the legitimate holder.
- The registration limiter on `POST /sessions` is **global**, since that endpoint
  is unauthenticated by design.
- Room codes are never logged, anywhere.
- Subscriber registration must stay atomic against a concurrent publish: from the
  auth check through `add(queue)`, seeding the current code, and returning the
  `StreamingResponse` there must be **no `await`**, so a code cannot land between
  the seed and the subscription.
- `except asyncio.TimeoutError` (not the bare builtin) on the keepalive.
- Bounded queues (`maxsize`) with `QueueFull` evicting a subscriber that has
  stopped draining.

Eviction runs opportunistically on session creation and on publish; whatever
`evict_expired()` returns has its queues closed.

### 5.3 `host/session.py` — host-side session client (new)

Stdlib `urllib` only, with an injected transport so it tests without a network.
No GUI, no Tk import — this is where the testable logic lives.

- `create_session(base_url) -> SessionCredentials` (`session_id`,
  `publisher_token`, `subscriber_token`)
- `qr_payload(base_url, session_id, subscriber_token) -> str` (§7)
- `subscriber_count(base_url, session_id, publisher_token) -> int`
- `delete_session(...)`
- `PairingWaiter` — the poll/timeout state machine, driven by an injected clock
  and an injected `subscriber_count` callable, so the whole "wait until a phone
  appears or give up" behaviour is unit-testable with no display and no sleeping.

Refuses any `base_url` not starting with `https://`, matching
`extractor/relay_publisher.py`.

### 5.4 `host/qr_window.py` — the window (new)

Thin Tk glue over §5.3. Shells out to `qrencode -t PNG`, loads the result with
`tkinter.PhotoImage` (Tk 8.6 reads PNG natively — no Pillow), shows the QR, the
relay host, a status line and a **Skip** button.

- Always-on-top, shown **before** the game launches. This is deliberate: fighting
  a fullscreen game for stacking order under Wayland is unreliable, and the
  launcher already has a natural gap while mitmproxy starts.
- Closes itself when `subscribers >= 1`, on Skip, on window close, or after a
  **120-second** timeout. Polls once a second.
- If `qrencode` is missing, falls back to showing the payload URL as selectable
  text — worse, but not broken.

`qrencode` becomes a documented host dependency.

### 5.5 `extractor/relay_publisher.py` — session-scoped (modified)

`RelayPublisher(base_url, session_id, token, ...)` posting to
`<base>/sessions/<id>/publish`. Everything else is untouched and must not
regress: `set_code` never raises, `https://`-only at construction, the
de-duplication guard that does **not** record a failed publish as sent, retries
with backoff, `critical = False`, and no code in any log line.

### 5.6 `extractor/addon.py` — three variables (modified)

`_build_sinks()` attaches the relay only when all of `JBVIP_RELAY_URL`,
`JBVIP_SESSION_ID` and `JBVIP_SESSION_TOKEN` are set, and warns naming what is
missing when some but not all are — extending the existing half-configured
diagnostic. The LAN server remains the critical sink; the relay remains
best-effort.

### 5.7 `jbvip-run.sh` — session lifecycle (modified)

1. Read `~/.config/jbvip/relay.env`, which now needs only `JBVIP_RELAY_URL`.
   **No pre-shared secret is stored on the host any more** — the only credentials
   are the per-session tokens minted at launch, which live in `$STATE` for the
   life of that one run and die with it.
2. If it is absent, behave exactly as today: LAN-only, no window, nothing new.
3. Otherwise create a session and show the QR window.
4. Write `JBVIP_SESSION_ID` and `JBVIP_SESSION_TOKEN` to `$STATE/session.env` at
   mode 600 and source it, so the token never appears on a command line or in a
   process listing. `$STATE` is already a private per-run directory.
5. Launch the game.
6. `cleanup()` deletes the session on every exit path.

**If the relay is unreachable, log it and launch the game anyway.** A dead VPS
must never cost a game night — the same rule `RelayPublisher` already follows.

## 6. Data flow

1. Launcher creates a session; relay returns an id and two tokens.
2. Launcher shows the QR; the phone scans and subscribes to that session.
3. Window closes; the game launches with the session credentials in its
   environment.
4. The game creates a room; the addon extracts the code exactly as today.
5. The code fans out to the LAN server and to `POST /sessions/<id>/publish`.
6. The relay pushes it down that session's SSE stream only.
7. The phone injects it into jackbox.tv and takes the VIP seat.
8. On exit the launcher deletes the session.

## 7. The QR payload

```
https://<relay-host>/j#<session-id>.<subscriber-token>
```

The credential sits in the **URL fragment**, which browsers never transmit, so it
cannot appear in the relay's access logs, a proxy log, or a `Referer` header.
`/j` serves a one-line page for anyone scanning with a plain camera app; the
Android app will later claim that URL with an intent filter so scanning opens it
directly.

## 8. Error handling

| Failure | Behaviour |
|---|---|
| Relay unreachable at launch | Log, skip the window, launch the game LAN-only. |
| Session creation refused (capacity) | Same — log the reason, launch anyway. |
| Phone never scans | Window times out after 120s and the game launches. |
| Publisher token for the wrong session | 401. Session ids are not a capability on their own. |
| Subscriber cap reached | 429 on subscribe; existing subscribers unaffected. |
| Session cap reached | 503 on create. **Never evict a live session to admit a stranger.** |
| Session expires mid-game | Publishes 404. The publisher logs a warning and keeps working — its de-duplication guard means one log line per distinct code, not per attempt. The LAN server is unaffected. |
| A subscriber stops draining | Its queue fills and it is dropped, rather than growing without bound. |
| `qrencode` missing | Window shows the URL as text. |

## 9. Testing

The headline test is **session isolation**: create two sessions, subscribe to
both, publish to one, and assert the other receives nothing while the first
receives the code. That single test is the entire point of this rework, and
nothing in the current suite could have caught its absence.

Around it: a wrong session's publisher token rejected; the subscriber cap; the
session cap refusing rather than evicting; idle eviction actually closing queues
(not merely dropping the session record); `DELETE` freeing capacity; and the
existing guarantees re-asserted per session — freshness on the relay's clock,
non-ASCII tokens returning 401 rather than 500, auth-before-rate-limit ordering.

Relay tests keep the live-uvicorn `serving()` helper. `httpx.ASGITransport`
cannot be used: it drains the whole app coroutine before returning a response and
therefore deadlocks against an infinite SSE generator (encode/httpx#2186). Any
test asserting on a successful `/subscribe` must use `.stream()`, never a
buffering `.get()`.

Host tests cover `host/session.py` with an injected transport and clock,
including `PairingWaiter`'s poll-and-timeout behaviour. `host/qr_window.py` is
hand-verified; it holds no logic worth testing.

The seam tests added on the previous branch stay, retargeted: the real
`RelayPublisher` against a real running relay, exercising real `urllib`, real
headers and the real route.

## 10. Deleted

- `relay/tokens.py` and `tests/test_relay_tokens.py`
- `pair.py` and `tests/test_pair_cli.py`
- The pairing-code machinery in `relay/state.py` (mint, redeem, expiry, alphabet)
- `EnvironmentFile=` and `StateDirectory=` from the systemd unit
- The revocation and token-file sections of `relay/README.md`
- `JBVIP_PUBLISHER_TOKEN` as a concept

## 11. Build order

1. `relay/state.py` — sessions, limits, eviction. Pure, heavily tested.
2. `relay/app.py` — the six routes over it, including the isolation test.
3. `host/session.py` — client and `PairingWaiter`.
4. `host/qr_window.py` — Tk glue.
5. `extractor/relay_publisher.py` and `addon.py` — session-scoping.
6. `jbvip-run.sh`, `relay/README.md`, `README.md`, and deletion of the
   superseded files.

Steps 1–3 are the substance; 4–6 are wiring.

## 12. Open risks

- **The window is the only untested piece.** Its logic is factored out
  deliberately, but "does a Tk window actually appear over a Steam launch on
  Wayland" is answerable only by running it.
- **`qrencode` as a host dependency** is new. Small and packaged everywhere, but
  it is one more thing to install.
- **Blocking the game launch** is a real UX cost — every launch now has a modal
  step. Skip and the timeout bound it, but if it proves annoying the alternative
  is showing the QR non-modally and accepting that it may be hidden behind a
  fullscreen game.
- **Session capacity is a guess.** 500 sessions and 16 subscribers each are
  plausible for a hobby relay and trivially adjustable; neither is derived from
  measurement.

## 13. Future work

- The Android app.
- Admin-priority join ordering: with sessions in place, the relay can hold a
  code back from friend subscribers for a second or two while releasing it
  immediately to the host's own device. That is a scheduling rule on existing
  machinery, which is what the single-tenant design failed to leave room for.
- Durable pairings, if re-scanning each night proves tiresome.
