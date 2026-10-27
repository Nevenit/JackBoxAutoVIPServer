# Browser Player Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the never-built Android app with a no-install web page served by the relay, keep one relay session for a whole game night (across packs and the Jackbox Megapicker), and make a launch that can't capture codes still start the game.

**Architecture:** The relay (Starlette, in-memory, one worker) gains a "room cleared" state, serves codes at any age, ends a session after 2 hours without a room, evicts its oldest stream when full, and serves a static player page at `/j`. The page subscribes over `fetch` streaming and iframes `jackbox.tv/?code=&name=`, swapping rooms as codes arrive. On the PC, `host/session.py` saves tonight's session and reuses it, and `jbvip-run.sh` clears the room on exit instead of deleting the session. The launcher falls back to a plain launch with a desktop notification when it can't intercept.

**Tech Stack:** Python 3.11+ (stdlib, Starlette, uvicorn), pytest + pytest-asyncio + httpx; plain HTML/CSS/JS with no build step; node + puppeteer-core + `/usr/bin/chromium` for the browser test; bash for the launcher.

**Spec:** `docs/superpowers/specs/2026-10-27-browser-player-page-design.md` (Part 1 only; Part 2, VIP priority, gets its own plan).

## Global Constraints

- Relay: single worker, all state in memory, no database, no new runtime dependencies (`relay/requirements.txt` stays starlette + uvicorn[standard]). Must run on Python 3.11 (the VPS has 3.11.2).
- The subscriber token travels only in the URL fragment (`/j#<session_id>.<subscriber_token>`) and the `Authorization: Bearer` header. Never log it, never put it in a query string, never send it to a third party. No third-party scripts in the page.
- Frame URL: `https://jackbox.tv/?code=CODE&name=NAME`. iframe `allow="screen-wake-lock; autoplay; fullscreen; clipboard-write"`.
- Player name: uppercase, at most 12 characters, stored on the relay origin under localStorage key `jbvip-name`.
- Never reload a running game for a code it already has. Never pull a player out of a running game because of a relay error.
- `ROOMLESS_SECONDS = 2 * 3600`. The 12-hour `MAX_SESSION_LIFETIME_SECONDS` and 4-hour `SESSION_IDLE_SECONDS` stay. `MAX_SUBSCRIBERS_PER_SESSION = 16`, oldest stream evicted when full.
- A dead VPS, a missing redirect, a busy port or a failed proxy must never stop the game launching.
- The Megapicker (Steam app id `2828500`) is never given the jbvip launch option.
- Full suite stays green: `.venv/bin/pytest -q`, plus, from Task 4 on, the browser test: `cd tests/browser && npm test`. Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

- Opening a different night's link in a tab that already has the page (only the `#fragment` changes, so no reload): the page drops the old session and follows the new one. Test: Task 4, "hashchange".
- Names that need URL encoding or run long (`a&b #1`, 15 characters): stored uppercase and cut to 12, and the frame URL encodes them. Test: Task 4, "name encoding".
- A saved night that belongs to a different relay URL (`relay.env` edited): not reused; a new session is created. Test: Task 5.
- The relay restarted mid-night, so the saved session now 404s: a new session is created, saved and its QR shown. Test: Task 5.
- A pack launched while port 8081 is still held (a previous launcher still cleaning up, or a stray mitmdump): the game starts without capture, with a notification and no QR window. Test: Task 6.

---

### Task 1: Never wrap the Megapicker

**Files:**
- Modify: `extractor/steam.py` (`jackbox_app_ids`, around line 89)
- Test: `tests/test_steam.py`

**Interfaces:**
- Produces: `MEGAPICKER_APP_ID = "2828500"` in `extractor/steam.py`. `jackbox_app_ids()` keeps its signature and never returns that id.

- [ ] **Step 1: Write the failing test** (in `tests/test_steam.py`, next to `test_jackbox_app_ids_filters_by_name`)

```python
def test_jackbox_app_ids_never_includes_the_megapicker(tmp_path):
    # The Megapicker launches each pack through Steam (steam://run/<appid>), so
    # the packs are wrapped already. Wrapping the picker would hold port 8081
    # for as long as it is open and break capture for every pack it launches.
    lib = tmp_path / "steamapps"
    lib.mkdir()
    (lib / "appmanifest_2828500.acf").write_text(
        '"AppState"\n{\n\t"appid"\t"2828500"\n\t"name"\t"The Jackbox Megapicker"\n}\n'
    )
    (lib / "appmanifest_1850960.acf").write_text(
        '"AppState"\n{\n\t"appid"\t"1850960"\n\t"name"\t"The Jackbox Party Pack 9"\n}\n'
    )
    assert jackbox_app_ids([lib]) == {"1850960": "The Jackbox Party Pack 9"}
```

- [ ] **Step 2: Run it.** `.venv/bin/pytest -q tests/test_steam.py -k megapicker`. Expected: FAIL, the dict also contains `"2828500"`.
- [ ] **Step 3: Implement.** Add the constant and skip that id inside `jackbox_app_ids`. `uninstall.py` needs no change: it walks every app in `localconfig.vdf`, and `clear_launch_option` matches by marker, so a wrapper set on the Megapicker by an older install is still removed.
- [ ] **Step 4: Run** `.venv/bin/pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit** `fix(install): never put the jbvip launch option on the Megapicker`.

---

### Task 2: Relay state — codes at any age, "room cleared", roomless expiry

**Files:**
- Modify: `relay/state.py`
- Test: `tests/test_relay_state.py`

**Interfaces:**
- Produces, in `relay/state.py`:
  - `ROOMLESS_SECONDS = 2 * 3600`. `FRESH_SECONDS` is deleted.
  - `CodeRecord(code: str, ts: float)`. `received_at` is deleted; nothing else reads it.
  - `Session.roomless_since: float | None`: `created_at` on creation, `None` after `publish`, the relay's `now` after a clear that removed a code.
  - `RelayState.clear_code(session_id) -> bool`: True when a code was removed, False when there was none. Raises `KeyError` for an unknown or expired session, like `publish`. On True it also sets `last_activity = now`.
  - `RelayState.current_code(session_id) -> CodeRecord | None`: the record, no age gate.
  - `_is_expired` also returns True when `roomless_since is not None and roomless_since <= now - ROOMLESS_SECONDS`. `expires_at` returns the earliest of idle, lifetime and (when roomless) `roomless_since + ROOMLESS_SECONDS`.

- [ ] **Step 1: Write the failing tests.** Replace `test_a_code_older_than_the_window_is_withheld` and `test_freshness_uses_the_relays_clock_not_the_publishers_stamp` with these. The second one survives only as the `ts` assertion.

```python
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


def test_expires_at_reports_the_roomless_deadline_when_it_binds_first():
    clock = Clock(1000.0)
    state = make(clock)
    session = state.create_session()
    assert state.expires_at(session.session_id) == 1000.0 + ROOMLESS_SECONDS
```

- [ ] **Step 2: Run** `.venv/bin/pytest -q tests/test_relay_state.py`. Expected: the new tests FAIL (`ROOMLESS_SECONDS` / `clear_code` missing).
- [ ] **Step 3: Implement** per the Interfaces block. Update the `CodeRecord` docstring: `ts` is the host's stamp, passed through to clients, and nothing on the relay gates on age any more.
- [ ] **Step 4: Fix the existing tests the new rule touches.** Run `.venv/bin/pytest -q tests/test_relay_state.py tests/test_relay_app.py`. Tests about idle timeout, touching or the lifetime ceiling that never publish will now die at `ROOMLESS_SECONDS`. Their subject is the idle and lifetime rules, which a session with a room still obeys, so make each one `publish(...)` a code right after creating its session. Don't loosen their assertions. Expected afterwards: all pass. (Task 3 handles `tests/test_relay_app.py`'s stale-code test and its `expires_at` assertion at line 72. If they fail here, leave them for Task 3.)
- [ ] **Step 5: Commit** `feat(relay): serve the current code at any age, add room-cleared and roomless expiry`.

---

### Task 3: Relay app — clear route, oldest-stream eviction

**Files:**
- Modify: `relay/app.py`, `tests/helpers.py`
- Test: `tests/test_relay_app.py`

**Interfaces:**
- Consumes: `RelayState.clear_code`, `ROOMLESS_SECONDS` (Task 2).
- Produces:
  - Route `DELETE /sessions/{session_id}/code` → `Relay.clear_code(request)`. Publisher-authenticated through `_refuse`, charged to the same per-session publish limiter, returns 204. Returns 404 for a session that is gone (including the `KeyError` race, as in `publish`), 401 for a wrong token, 429 when rate limited. When a code was removed it fans out `{"code": None, "ts": time.time()}`.
  - `Relay._queues: dict[str, dict[asyncio.Queue, None]]`, insertion-ordered so the oldest stream comes first. Use `queues[q] = None` to add and `queues.pop(q, None)` to remove. The isolation property is unchanged: every access is still keyed by session.
  - In `subscribe()`, at `MAX_SUBSCRIBERS_PER_SESSION` the oldest queue is popped and closed with `_close_queue`, and the newcomer is admitted. No more 429.
  - `tests/helpers.py`: `async def next_events(response, count, timeout=3.0) -> list[dict]`, which reads `count` `data:` payloads in one pass. httpx can't iterate a response twice.

- [ ] **Step 1: Write the failing tests** (helper `code_url(session)` = `f"/sessions/{session['session_id']}/code"`):

```python
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


async def test_a_phone_that_joins_after_a_clear_gets_no_code():
    state, app = make()
    async with serving(app) as http:
        session = await new_session(http)
        state.publish(session["session_id"], "WEWW", 1000.0)
        state.clear_code(session["session_id"])
        async with http.stream(
            "GET", subscribe_url(session), headers=auth(session["subscriber_token"])
        ) as response:
            with pytest.raises(asyncio.TimeoutError):
                await first_event(response, timeout=0.5)


async def test_clearing_needs_the_publisher_token():
    _, app = make()
    async with serving(app) as http:
        session = await new_session(http)
        response = await http.delete(
            code_url(session), headers=auth(session["subscriber_token"])
        )
    assert response.status_code == 401


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
                await stack.enter_async_context(http.stream("GET", url, headers=headers))
                for _ in range(MAX_SUBSCRIBERS_PER_SESSION)
            ]
            newcomer = await stack.enter_async_context(
                http.stream("GET", url, headers=headers)
            )
            assert newcomer.status_code == 200
            assert await stream_ends(streams[0])
```

Also add `("DELETE", "/sessions/nope/code")` to the parameter list of `test_an_unknown_session_is_a_404_not_a_401`.

- [ ] **Step 2: Remove what these replace.** Delete `test_a_subscriber_is_not_sent_a_stale_code_on_connect` and `test_the_subscriber_cap_refuses_the_seventeenth_phone`. Change `test_creating_a_session_returns_an_id_and_two_tokens`'s `expires_at` assertion to `1000.0 + ROOMLESS_SECONDS`. In the three queue-hygiene tests (around lines 531–570), build queue containers with `dict.fromkeys([...])` instead of set literals.
- [ ] **Step 3: Run** `.venv/bin/pytest -q tests/test_relay_app.py`. Expected: the new tests FAIL (405 on the route, 429 at the cap).
- [ ] **Step 4: Implement** per the Interfaces block, updating every `.add`/`.discard` on `_queues` to the dict form.
- [ ] **Step 5: Run** `.venv/bin/pytest -q`. Expected: all pass, including `test_the_relay_logs_neither_a_room_code_nor_a_token`.
- [ ] **Step 6: Commit** `feat(relay): add DELETE /sessions/{id}/code and evict the oldest stream when full`.

---

### Task 4: The player page

**Files:**
- Create: `relay/static/player.html` (inline CSS and JS), `relay/static/qrcode.min.js` (vendored), `relay/static/THIRD-PARTY.md`, `tests/browser/package.json`, `tests/browser/player.test.mjs`
- Modify: `relay/app.py` (routes; delete `_JOIN_PAGE` and `join_page`), `.gitignore` (add `tests/browser/node_modules/`)
- Test: `tests/test_relay_app.py`, `tests/browser/player.test.mjs`

**Interfaces:**
- Consumes: the subscribe stream, with payloads `{"code": str, "ts": float}` and `{"code": null, "ts": float}` (Task 3).
- Produces:
  - Routes: `GET /` → one-line HTML containing exactly `Scan the QR code on the host's screen to join.`; `GET /j` → `FileResponse` of `relay/static/player.html` with `Cache-Control: no-store`; `Mount("/static", StaticFiles(directory=STATIC_DIR))` with `STATIC_DIR = Path(__file__).parent / "static"`.
  - The page's element ids, which the browser test relies on: `code`, `status`, `invite-btn`, `menu-btn`, `frame`, `name-panel`, `name-input`, `name-save`, `wait-panel`, `wait-text`, `invite-panel`, `qr`, `share-btn`, `invite-close`, `menu-panel`, `rename-btn`, `reload-btn`, `direct-link`, `menu-close`.
  - The QR library: `qrcode-generator` 1.4.4 (MIT, Kazuhiko Arase). Download `https://cdnjs.cloudflare.com/ajax/libs/qrcode-generator/1.4.4/qrcode.min.js` and verify it is `sha384-mZT2gIty7ZDdOGkxfP6joZcYdMW1Jvj9dRlfpTmaJAKKXTqzygtB22k7FLe+KZC1` before committing. Load it as `<script src="/static/qrcode.min.js">`.

Page behaviour is spec §4.1, items 1–12. Implementation decisions on top of it:

- Session from `location.hash` (`#<sid>.<token>`). On `hashchange`, abort the current stream, forget the current code, hide the frame and start over with the new session.
- Subscribe with `fetch(…, {headers: {Authorization: "Bearer " + token}, cache: "no-store", signal})`. Parse with a `TextDecoder` loop on blank-line-separated events, using `data:` lines only. Back off from 1 s doubling to 15 s. On `visibilitychange` to hidden, abort; on visible, resubscribe. On 401 or 404, stop retrying and set the status. If a code is in the frame, keep the frame; otherwise show the message in `#wait-text`.
- `current` holds the framed code. A code equal to `current` does nothing. `null` sets `current = null`, hides the frame and shows `#wait-panel`. Reload re-assigns `frame.src` to the same URL.
- Name input: `maxlength="12"`. Store `value.trim().toUpperCase().slice(0, 12)`. Renaming applies from the next code.
- The page's own wake lock is requested on the first Join tap and again on `visibilitychange` to visible. Ignore failures.

- [ ] **Step 1: Write the failing route tests** (replacing `test_the_join_page_is_served_without_credentials`):

```python
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
```

- [ ] **Step 2: Run** `.venv/bin/pytest -q tests/test_relay_app.py -k "player or bare or qr_library"`. Expected: FAIL.
- [ ] **Step 3: Write the browser test** `tests/browser/player.test.mjs`, in the style of `injection/test-fixture.mjs`: `check(name, condition)` prints ok/FAIL, and the process exits non-zero on any failure. `package.json` has `"type": "module"`, devDependency `puppeteer-core ^23` and script `"test": "node player.test.mjs"`.
  - Setup: pick a free port with `net`, then spawn `../../.venv/bin/python -m uvicorn --factory relay.app:create_app --host 127.0.0.1 --port <port>` from the repo root. Wait for `GET /` to return 200. Launch puppeteer with `executablePath: process.env.CHROMIUM_PATH || "/usr/bin/chromium"`, headless, `--no-sandbox`, viewport 390×844. Turn on request interception: answer `https://jackbox.tv/*` with a 200 stub page, record each such URL, and let everything else through. Create sessions and publish/clear/delete through the relay's HTTP API with `fetch` from node.
  - Checks, in order, on one page unless stated:
    1. `/j` with no fragment → `#wait-text` contains `QR code on the host's screen`.
    2. Open `/j#<s1>.<sub1>` → `#name-panel` visible. Type `a&b #1 and more`, click Join → `localStorage["jbvip-name"] === "A&B #1 AND MO"` (12 characters) and `#wait-panel` visible. **(name encoding)**
    3. Publish `WXYZ` to s1 → `#frame` visible, `frame.src === "https://jackbox.tv/?code=WXYZ&name=A%26B%20%231%20AND%20MO"`, `frame.getAttribute("allow")` contains `screen-wake-lock`, `#code` text `WXYZ`, one jackbox.tv request recorded.
    4. Fire `visibilitychange` hidden then visible (override `document.visibilityState` via `Object.defineProperty`). The resubscribe gets `WXYZ` seeded again, so there's still exactly one jackbox.tv request.
    5. Publish `ABCD` → `frame.src` has `code=ABCD`; two requests recorded.
    6. Clear (DELETE `/sessions/<s1>/code`) → `#wait-panel` visible and `#frame` hidden.
    7. Publish `QRST`, click `#invite-btn`, screenshot the `#qr` element, run `zbarimg --quiet --raw` on it → output equals `page.url()`.
    8. Click `#menu-btn`, then `#reload-btn` → one more jackbox.tv request for the same URL.
    9. Create s2 and set `location.hash = "#<s2>.<sub2>"`. Publish `MNOP` to s2 → `frame.src` has `code=MNOP`. Then publish `ZZZZ` to s1 → the frame still has `MNOP`. **(hashchange)**
    10. Delete s2 → within 3 s `#status` contains `ended` and `#frame` is still visible.
- [ ] **Step 4: Run** `cd tests/browser && npm install && npm test`. Expected: FAIL (no page yet).
- [ ] **Step 5: Implement** the routes, `player.html` and the vendored library, per the Interfaces and decisions above. The spike that proved the approach on real phones used the same techniques; it isn't in the repo.
- [ ] **Step 6: Run** `cd tests/browser && npm test` (all ok) and `.venv/bin/pytest -q` (all pass).
- [ ] **Step 7: Commit** `feat(relay): serve the browser player page at /j`.

---

### Task 5: Host — reuse tonight's session

**Files:**
- Modify: `host/session.py`, `host/qr_window.py`
- Test: `tests/test_host_session.py`; create `tests/test_qr_window.py`

**Interfaces:**
- Consumes: `DELETE /sessions/{id}/code` (Task 3).
- Produces, in `host/session.py`:
  - `@dataclass(frozen=True) class Night: credentials: SessionCredentials; subscribers: int; created: bool`
  - `save_night(path, credentials) -> None`: JSON object with keys `base_url`, `session_id`, `publisher_token`, `subscriber_token`. Mode 0600 using `os.open` + `os.fchmod`, as `write_session_env` does. Create the parent directory with mode 0700 if missing.
  - `load_night(path) -> SessionCredentials | None`: None when the file is missing, isn't JSON, or a field fails `_normalise`/`_checked`.
  - `resume_or_create(base_url: str, night_path, *, call=_urllib_call) -> Night`: if `load_night` gives credentials whose `base_url` equals `_normalise(base_url)`, return `Night(saved, subscriber_count(saved, call=call), created=False)`. If that count raises `urllib.error.HTTPError` with code 401 or 404, create, save and return `Night(new, 0, created=True)`. Any other exception propagates and the file is left alone. With no usable saved night, create, save and return `created=True`.
  - `clear_room(credentials, *, call=_urllib_call) -> bool`: `DELETE {base_url}/sessions/{id}/code` with the publisher token. Never raises.
  - `main(["--clear"])` replaces `--delete`: reads `credentials_from_env()`, calls `clear_room`, always returns 0. `delete_session` stays; `qr_window` still uses it.
- `host/qr_window.py` `main`: a new required `--night-file PATH`. Call `resume_or_create(args.base_url, args.night_file)` (failure → stderr + return 1, as today), then `write_session_env`. On OSError there, call `delete_session` **only if `night.created`**, then return 1. If `night.subscribers >= 1`, print `jbvip: N phone(s) already connected; no QR needed` to stderr and return 0 without calling `show`. Otherwise call `show(night.credentials)` as today.

- [ ] **Step 1: Teach `RecordingCall` to raise per call:** a queued response that is a `BaseException` instance is raised instead of returned.
- [ ] **Step 2: Write the failing tests** (`gone = urllib.error.HTTPError(url, 404, "Not Found", None, None)`):

```python
def test_a_saved_night_is_reused_while_the_relay_still_has_it(tmp_path):
    path = tmp_path / "night.json"
    save_night(path, CREDENTIALS)
    call = RecordingCall({"subscribers": 2, "expires_at": 1.0})
    night = resume_or_create("https://relay.example", path, call=call)
    assert night == Night(CREDENTIALS, subscribers=2, created=False)
    assert call.calls == [("https://relay.example/sessions/sess-1", "GET", "pub-token")]


def test_a_night_the_relay_forgot_is_replaced_and_saved(tmp_path):
    path = tmp_path / "night.json"
    save_night(path, CREDENTIALS)
    gone = urllib.error.HTTPError("https://relay.example/sessions/sess-1", 404, "Not Found", None, None)
    call = RecordingCall(gone, {**CREATED, "session_id": "sess-2"})
    night = resume_or_create("https://relay.example", path, call=call)
    assert night.created and night.subscribers == 0
    assert night.credentials.session_id == "sess-2"
    assert load_night(path).session_id == "sess-2"


def test_a_night_saved_for_another_relay_is_not_reused(tmp_path):
    path = tmp_path / "night.json"
    save_night(path, CREDENTIALS)
    call = RecordingCall(CREATED)
    night = resume_or_create("https://other.example", path, call=call)
    assert call.calls == [("https://other.example/sessions", "POST", None)]
    assert night.created


def test_with_no_saved_night_one_is_created_and_saved(tmp_path):
    path = tmp_path / "state" / "jbvip" / "night.json"
    night = resume_or_create("https://relay.example", path, call=RecordingCall(CREATED))
    assert night.created and load_night(path) == night.credentials


def test_an_unreachable_relay_is_an_error_not_a_new_night(tmp_path):
    path = tmp_path / "night.json"
    save_night(path, CREDENTIALS)
    call = RecordingCall(exception=urllib.error.URLError("down"))
    with pytest.raises(urllib.error.URLError):
        resume_or_create("https://relay.example", path, call=call)
    assert load_night(path) == CREDENTIALS


def test_the_night_file_is_only_readable_by_its_owner(tmp_path):
    path = tmp_path / "night.json"
    save_night(path, CREDENTIALS)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_corrupt_night_file_counts_as_no_night(tmp_path):
    path = tmp_path / "night.json"
    path.write_text("not json")
    assert load_night(path) is None


def test_clear_room_deletes_the_code_with_the_publisher_token():
    call = RecordingCall()
    assert clear_room(CREDENTIALS, call=call) is True
    assert call.calls == [("https://relay.example/sessions/sess-1/code", "DELETE", "pub-token")]


def test_clear_room_never_raises():
    assert clear_room(CREDENTIALS, call=RecordingCall(exception=OSError("x"))) is False
```

  Rename the two `main(["--delete"])` tests to `--clear`, keeping their assertions. In `tests/test_qr_window.py`, monkeypatch `host.qr_window.resume_or_create`, `show`, `write_session_env` and `delete_session`:
  - `test_no_window_when_phones_are_already_connected`: Night with subscribers=1 and created=False. `show` raises AssertionError if called. `main([...]) == 0`.
  - `test_a_reused_night_is_not_deleted_when_the_env_write_fails`: created=False, `write_session_env` raises OSError → `main(...) == 1` and `delete_session` was never called.
  - `test_a_new_night_is_given_back_when_the_env_write_fails`: created=True, the same failure → `delete_session` called once.

- [ ] **Step 3: Run** `.venv/bin/pytest -q tests/test_host_session.py tests/test_qr_window.py`. Expected: FAIL (names missing).
- [ ] **Step 4: Implement** per the Interfaces block.
- [ ] **Step 5: Run** `.venv/bin/pytest -q`. Expected: all pass, except `tests/test_launcher.py`'s `--delete` check, which Task 6 changes. If it fails here, leave it for Task 6.
- [ ] **Step 6: Commit** `feat(host): keep one relay session for the whole night`.

---

### Task 6: Launcher — night file, clear on exit, plain-launch fallback

**Files:**
- Modify: `jbvip-run.sh`
- Test: `tests/test_launcher.py`

**Interfaces:**
- Consumes: `python3 -m host.qr_window … --night-file PATH`, `python3 -m host.session --clear` (Task 5).
- Produces, in `jbvip-run.sh`:
  - `GAME=("$@")` near the top.
  - `NIGHT_FILE="${XDG_STATE_HOME:-$HOME/.local/state}/jbvip/night.json"`, passed as `--night-file "$NIGHT_FILE"` on the `host.qr_window` line.
  - `run_plain()` (defined as `run_plain() {` … closing `}` on its own line): echo `jbvip: <reason>; starting the game without room-code capture` to stderr. If `notify-send` exists, run `notify-send -a jbvip "Jackbox: no room codes this time" "<reason>"` (ignore failure). Then run `"${GAME[@]}"` in the foreground and `exit` with its status, so the EXIT trap still cleans up.
  - Before the pairing block, in this order, each failing into `run_plain "<reason>"`: the redirect check (`if [ ! -w "$CGROUP/cgroup.procs" ]; then`, reason naming `sudo $REPO/jbvip-redirect-up.sh $PORT`), the `SYS_CA`/`CA_TARGET` selection (reason `no system CA bundle found`), and the `port_up` check (reason `port $PORT is already in use`). So no QR window shows when interception is impossible.
  - mitmdump failures ("exited during startup", "did not come up within 6s") call `run_plain` instead of `exit 1`, after printing the log tail as today.
  - `cleanup()` calls `python3 -m host.session --clear` where it called `--delete`, still guarded by `${SESSION_ID:-}` and `|| true`, still before `rm -rf "$STATE"`.

- [ ] **Step 1: Write the failing tests.** Add two extractors next to the existing helpers: `launcher_function(name)`, which returns the lines from `<name>() {` to the first line that is exactly `}`; and `launcher_if_block(first_line)`, which returns the lines from that exact `if` line to the next line that is exactly `fi`. Prepare a fake `notify-send` in a temp bin that appends its arguments to a log file.

```python
def test_a_missing_redirect_still_starts_the_game_and_says_why(tmp_path): ...
    # script: set -euo pipefail; PORT=8081; REPO=/r; CGROUP=/nonexistent;
    #         GAME=(touch <marker>); + launcher_function("run_plain")
    #         + launcher_if_block('if [ ! -w "$CGROUP/cgroup.procs" ]; then')
    # assert marker exists; assert "jbvip-redirect-up.sh" in notify log


def test_a_busy_proxy_port_still_starts_the_game(tmp_path): ...
    # same, with port_up() { return 0; } defined first and the port_up if-block
    # assert marker exists; assert "8081" in notify log


def test_the_game_still_starts_without_notify_send(tmp_path): ...
    # redirect case with PATH=/usr/bin:/bin minus the fake bin; marker exists


def test_interception_checks_run_before_the_pairing_window():
    text = LAUNCHER.read_text()
    pairing = text.index("host.qr_window")
    for check in ('if [ ! -w "$CGROUP/cgroup.procs" ]; then', "CA_TARGET=", "if port_up; then"):
        assert text.index(check) < pairing


def test_the_qr_window_is_handed_tonights_night_file():
    text = LAUNCHER.read_text().replace("\\\n", " ")
    assert 'NIGHT_FILE="${XDG_STATE_HOME:-$HOME/.local/state}/jbvip/night.json"' in text
    assert '--night-file "$NIGHT_FILE"' in next(l for l in text.splitlines() if "host.qr_window" in l)
```

  Replace `test_the_launcher_deletes_its_session_on_the_way_out` with `test_the_launcher_clears_the_room_on_the_way_out`. It has the same three assertions, with `host.session --clear`.
- [ ] **Step 2: Run** `.venv/bin/pytest -q tests/test_launcher.py`. Expected: the new tests FAIL.
- [ ] **Step 3: Implement** per the Interfaces block. Keep the existing tests green, in particular `test_a_relay_that_cannot_be_reached_does_not_abort_the_launch`, the `ca_target` / `trust_bundle` / `cgroup` tests and the flow-detail pin.
- [ ] **Step 4: Run** `.venv/bin/pytest -q` and `bash -n jbvip-run.sh`. Expected: all pass, syntax OK.
- [ ] **Step 5: Commit** `feat(launcher): reuse tonight's session, clear the room on exit, never block a launch`.

---

### Task 7: Remove the injection script; update the docs

**Files:**
- Delete: `injection/inject.js`, `injection/fixture.html`, `injection/test-fixture.mjs`, `injection/package.json`, `injection/package-lock.json` (and the untracked `injection/node_modules/`)
- Modify: `.gitignore` (drop `injection/node_modules/`), `README.md`, `relay/README.md`, `docs/superpowers/specs/2026-08-01-jackbox-auto-join-design.md` (a one-line "Superseded by 2026-10-27-browser-player-page-design.md" note under the title)

- [ ] **Step 1: Delete** the injection files.
- [ ] **Step 2: `relay/README.md`.** Add the new routes to the Endpoints table: `DELETE /sessions/{id}/code` (publisher; "the host quit a pack; phones go back to waiting"), `GET /` (landing line), `GET /j` (the player page), `GET /static/*`. Replace the Freshness section with "Current code": served at any age, cleared when a pack exits, and why that's safe (spec §4.2). Capacity: a session ends 2 h after its room was cleared (or 2 h after creation with no room), the 12 h cap stays, and a full session evicts its oldest stream. Remove "clients are expected to enforce the same 120-second window".
- [ ] **Step 3: `README.md`.** Replace the "phone app does not exist" paragraph with the real flow:
  - the QR shows once per night; phones join through `/j`; Invite passes access on
  - phones follow across games, packs and the Megapicker; quitting a pack returns them to waiting
  - a launch that can't capture codes still starts, with a notification
  - `install.py` skips the Megapicker

  Keep "file, not export" and the `~/.config/jbvip/relay.env` instructions (`test_the_readme_does_not_tell_the_user_to_export_the_relay_variables` pins them).
- [ ] **Step 4: Verify.** `.venv/bin/pytest -q` passes. `grep -n -i -E 'jbvip app|phone app|FRESH_SECONDS|120-second|inject\.js' README.md relay/README.md` prints nothing.
- [ ] **Step 5: Commit** `docs: describe the player page and night-long sessions; drop the injection script`.

---

### Task 8: Deploy and verify with real phones (needs the user)

Outward-facing: ask the user before pushing and before touching the VPS. Do the VPS work through the user's `vps` tmux session (`root@bne`), batched, because every new SSH login sends an ntfy alert (`~/Documents/homelab/jbvip-relay.md`).

- [ ] **Step 1: Push** the branch (after the user OKs it): `git push -u origin browser-player-page`.
- [ ] **Step 2: Update the relay on `bne`:** `cd /opt/jbvip-relay && git fetch --depth 1 origin browser-player-page && git checkout -B browser-player-page FETCH_HEAD && /root/.local/bin/uv pip install -q --link-mode copy --python .venv/bin/python -r relay/requirements.txt && systemctl restart jbvip-relay`. Expected: `systemctl is-active jbvip-relay` prints `active`. From the desktop, `curl -s https://jb.tinypp.in/` contains `Scan the QR code on the host's screen to join.`
- [ ] **Step 3: Remove the spike.** Restore the plain block in `/root/DockerContainers/Caddy/Caddyfile` by editing it **in place** (single-file bind mount; the inode gotcha in `crafty.md`): `jb.tinypp.in {` / `reverse_proxy 127.0.0.1:8479` / `}`. Run `docker exec caddy rm -r /srv/jbvip-spike`, then validate and reload Caddy. Expected: `curl -s https://jb.tinypp.in/j` contains `id="frame"` and not `THROWAWAY`.
- [ ] **Step 4: Update `~/Documents/homelab/jbvip-relay.md`.** Record the deployed branch and commit, replace the "Throwaway test page" section with "removed <date>", and add the new routes and the 2-hour roomless rule.
- [ ] **Step 5: Real-use checklist with the user:**
  - Run `sudo ./jbvip-redirect-up.sh` if the PC has rebooted.
  - Launch a pack from Steam. The QR appears; scan it on Android; play; switch games.
  - Quit the pack: the phone shows waiting.
  - Launch a game from the Megapicker: no QR, because the phone is still connected, and the phone follows.
  - Join late on an iPhone through Invite.
  - Refresh mid-game, then quit.

  Confirm in Steam's log that the Megapicker launch went through `jbvip-run.sh` with `-launchTo`. Confirm in the relay journal that `DELETE /sessions/<id>/code` appears on each quit and `DELETE /sessions/<id>` doesn't.
- [ ] **Step 6:** Fix anything found (superpowers:systematic-debugging), then superpowers:finishing-a-development-branch.
