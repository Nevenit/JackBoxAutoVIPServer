# TV QR Overlay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep the night's QR on the TV, top-right over the game, until any phone taps **Hide QR on TV**. Bring it back when a game launches with no phone connected.

**Architecture:**
- **Relay:** each session gets a `qr_hidden` flag. Any phone (or the PC) can hide it; only the PC can show it again. Every SSE event carries the flag next to the room code.
- **Launcher:** sets up the night as today, then starts the QR card in the background once interception is up. The card is an unmanaged (override-redirect) Tk window. It decides for itself whether to appear, then polls the relay until it's hidden.
- **Phone page:** gains one menu button.

**Tech Stack:** Python 3 standard library and Tk 8.6, the `qrencode` binary, Starlette and uvicorn (relay), bash (launcher), and vanilla JS (page). Tests use pytest, plus puppeteer-core and Chromium for the page.

**Spec:** `docs/superpowers/specs/2026-10-28-tv-qr-overlay-design.md`. It builds on `docs/superpowers/specs/2026-10-27-browser-player-page-design.md`.

## Global Constraints

**Dependencies and safety**
- No new dependencies. Host side: Python stdlib, Tk 8.6 and `qrencode`. Relay: unchanged. Page: no new library and no new third-party request.
- Never run the real launcher, Steam or sudo. Never open windows or notifications on the real desktop. Visual checks run in a headless nested KWin (`tests/visual/card_in_kwin.sh`, Task 3).
- No token may appear in a command line, a log, or any file other than the existing mode-600 night and session files. The QR image is never written to disk.
- A relay or VPS problem must never stop or delay the game. The card must never hold up the launch.

**The phone page**
- A QR event must never reload, empty or swap the frame on any phone (2026-10-27 spec §7).

**Exact copy**
- Phone button: `Hide QR on TV`.
- Phone failure text: `Couldn't hide it. Try again`.
- Card caption, two lines: `Scan to join` and `Hide it from ⋯ on your phone`.

**Every task**
- After each task, `.venv/bin/pytest -q`, `cd tests/browser && npm test` and `bash -n jbvip-run.sh` all pass.
- Each commit ends with the committing model's own `Co-Authored-By:` trailer.

**Hands off**
- The main checkout `/home/nevenit/Programming/Python/JackBoxAutoVIPServer` is what Steam runs. Nothing touches it, or the VPS, before Task 6.

## Review Focus

Most likely to bite first:
1. **A hide while a phone has a game framed.** The game must not reload. Covered by the event-shape test in Task 1 and the browser test in Task 5.
2. **The launcher killed hard** (Steam's Stop, SIGKILL) while the card is up. The card must close by itself. Covered by `launcher_gone` in Task 3.
3. **The relay unreachable or slow** while the card is up. The card stays up, and × still closes it. Covered by `card_should_stay` in Task 2.
4. **A phone hammering Hide.** Room codes must still publish. Covered by the budget test in Task 1.
5. **An older relay without the flag** (for example, the launcher deployed before the relay). The card shows and phones show no button. Neither may crash. Covered by Task 2's "missing flag" test and Task 5's page rule.

---

### Task 1: Relay — the QR-hidden flag

**Files:**
- Modify: `relay/state.py` (`Session`, new `RelayState.set_qr_hidden`)
- Modify: `relay/app.py` (`_refuse`, budgets, events, `get_session`, two routes, `build_app`)
- Modify: `relay/README.md` (Endpoints table, Current code section)
- Test: `tests/test_relay_state.py`, `tests/test_relay_app.py`

**Interfaces:**
- Produces:
  - `POST /sessions/{id}/qr/hide`: publisher **or** subscriber token → 204.
  - `POST /sessions/{id}/qr/show`: publisher token only → 204. A subscriber token gets 401.
  - Both routes return 404 for a dead session, 401 for a wrong token and 429 over budget, and both are idempotent.
  - `GET /sessions/{id}` → `{"subscribers": int, "expires_at": float, "qr_hidden": bool}`.
  - Every SSE `data:` event is `{"code": str | null, "ts": float, "qr_hidden": bool}`. That covers the seed, publish, clear and QR changes. A QR change repeats the current room and its `ts`, or `null` and `time.time()` when there's none, so the page sees an unchanged room.
  - `Session.qr_hidden: bool = False`.
  - `RelayState.set_qr_hidden(session_id, hidden: bool) -> bool`. It returns True when the flag changed and raises KeyError for an unknown session. It never changes `last_activity` or `roomless_since`.
  - `QR_LIMIT_PER_MINUTE = 30`, with a separate per-session limiter shared by hide and show. It is charged after the token check and never touches the publish budget.
  - `build_app(state, *, create_limiter=None, publish_limiter_factory=None, qr_limiter_factory=None)`.

- [ ] **Step 1: Write the failing state tests** in `tests/test_relay_state.py`

```python
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

def test_the_qr_flag_of_an_unknown_session_is_a_key_error():
    with pytest.raises(KeyError):
        RelayState(now=Clock()).set_qr_hidden("nope", True)
```

- [ ] **Step 2: Write the failing app tests** in `tests/test_relay_app.py`. Add a `qr_url(session, action)` helper that returns `/sessions/{id}/qr/{action}`.
  - `test_the_host_can_read_the_qr_flag`: a new session's GET (publisher token) has `"qr_hidden": False`.
  - `test_any_phone_can_hide_the_qr`: hide with the subscriber token → 204, then GET → `qr_hidden is True`.
  - `test_the_host_can_hide_the_qr`: hide with the publisher token → 204.
  - `test_only_the_host_can_show_the_qr_again`: hide, then show with the subscriber token → 401. Show with the publisher token → 204, then GET → `qr_hidden is False`.
  - `test_the_qr_routes_check_the_session_and_the_token`: parametrized over `hide` and `show`. An unknown session id → 404. A wrong token on a live session → 401.
  - `test_a_phone_that_connects_learns_the_qr_state`: hide, then subscribe. The first event has `code is None` and `qr_hidden is True`.
  - `test_a_qr_change_reaches_phones_once_and_keeps_the_room`:
    - Arrange: `state.publish(sid, "WXYZ", 1000.0)`, subscribe, hide twice over HTTP, then publish `ABCD` over HTTP.
    - Assert: `next_events(response, 3)` returns:
      - `{"code": "WXYZ", "ts": 1000.0, "qr_hidden": False}`
      - `{"code": "WXYZ", "ts": 1000.0, "qr_hidden": True}`
      - an event with `code == "ABCD"` and `qr_hidden is True`
  - `test_hide_spam_cannot_use_up_the_publish_budget`:
    - Arrange: `make(qr_limiter_factory=lambda: RateLimiter(limit=2, window_seconds=60.0), publish_limiter_factory=lambda: RateLimiter(limit=2, window_seconds=60.0))`.
    - Assert: three hides give `[204, 204, 429]`, and a publish after that still gets 204.
  - `test_the_qr_budget_is_charged_after_the_token_check`: with a limit of 2, three wrong-token hides give 401 each, and then a valid hide gets 204.
  - Update the two existing shape assertions, `assert set(...) == {"code", "ts"}` (in `test_clearing_the_room_tells_listening_phones` and `test_a_phone_that_joins_after_a_clear_is_told_there_is_no_room`), to `{"code", "ts", "qr_hidden"}`.

- [ ] **Step 3: Run them to verify they fail**

Run: `.venv/bin/pytest -q tests/test_relay_state.py tests/test_relay_app.py`
Expected: the new tests fail. Expect AttributeError or TypeError for `qr_hidden` and `set_qr_hidden`, and 404/405 for the routes.

- [ ] **Step 4: Implement the flag in `relay/state.py`**
  - Add `qr_hidden: bool = False` as the last `Session` field.
  - Add `set_qr_hidden` as specified under Interfaces, next to `clear_code`.

- [ ] **Step 5: Implement the relay side in `relay/app.py`**
  - `_refuse(request, session_id, *, subscriber=False, either=False)`: `either=True` accepts a publisher or a subscriber token.
  - Add `qr_limiter_factory` to `Relay.__init__` and `build_app`, with a `self._qr_limiters` dict. Give `_over_budget(session_id, *, qr=False)` a parameter that picks the dict. `_release` pops both dicts.
  - One helper builds every event: `_event(session_id, code, ts) -> dict` returns `{"code": code, "ts": ts, "qr_hidden": <session's flag>}`. Publish, clear and the subscribe seed all use it.
  - Add `hide_qr` and `show_qr` handlers. Each one runs `_refuse`, then `_over_budget(..., qr=True)`, then `state.set_qr_hidden`; a KeyError there means 404. If the flag changed, fan out `_event` with the current room (or `None` and `time.time()`). Return 204.
  - `get_session` adds `"qr_hidden"`.
  - Register `Route("/sessions/{session_id}/qr/hide", relay.hide_qr, methods=["POST"])` and `Route("/sessions/{session_id}/qr/show", relay.show_qr, methods=["POST"])`.

- [ ] **Step 6: Document it in `relay/README.md`**
  - Add the two routes to the Endpoints table.
  - The GET row reads `{subscribers, expires_at, qr_hidden}` and is polled by the QR card, not "the QR window".
  - Add one sentence under "Current code": every event carries `qr_hidden`, and a QR change repeats the current room.

- [ ] **Step 7: Run the whole suite**

Run: `.venv/bin/pytest -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add relay/ tests/test_relay_state.py tests/test_relay_app.py
git commit -m "feat(relay): a night-long QR-hidden flag that any phone can set"
```

### Task 2: Host — QR status, hide/show calls and the card's decisions

**Files:**
- Modify: `host/session.py`
- Test: `tests/test_host_session.py`

**Interfaces:**
- Consumes (Task 1): the two routes, the `qr_hidden` field of GET.
- Produces:
  - `@dataclass(frozen=True) class SessionStatus: subscribers: int; qr_hidden: bool`.
  - `session_status(credentials, *, call=_urllib_call) -> SessionStatus`: GET with the publisher token. A missing `qr_hidden` (an older relay) reads as `False`.
  - `subscriber_count(credentials, *, call=_urllib_call) -> int`: now `session_status(...).subscribers`.
  - `hide_qr(credentials, *, call=_urllib_call) -> bool` and `show_qr(credentials, *, call=_urllib_call) -> bool`: POST `{base}/sessions/{id}/qr/hide` and `.../qr/show` with the publisher token. They never raise; they return False on any failure, like `clear_room`. Fold them and the existing `_delete` into one never-raising helper, `_quietly(credentials, method, suffix, call)`.
  - `card_wanted(credentials, status: SessionStatus, *, show=show_qr) -> bool`:
    - not hidden → True;
    - hidden and `subscribers == 0` → `show(credentials)`, so True only when the relay agreed;
    - otherwise False.
  - `card_should_stay(credentials, *, call=_urllib_call) -> bool`:
    - a 404 or 401 HTTPError → False;
    - any other exception → True, so a blip never closes the card;
    - otherwise `not status.qr_hidden`.
  - `CARD_POLL_SECONDS = 2.0`.

- [ ] **Step 1: Write the failing tests**, with `RecordingCall` and `http_error` as the file already uses them
  - `test_session_status_reads_the_count_and_the_qr_flag`: `{"subscribers": 1, "expires_at": 1.0, "qr_hidden": True}` gives `SessionStatus(1, True)`. `call.calls == [("https://relay.example/sessions/sess-1", "GET", "pub-token")]`.
  - `test_an_older_relay_without_the_flag_reads_as_showing`: a body without `qr_hidden` gives `qr_hidden is False`.
  - `test_hide_qr_posts_with_the_publisher_token`: `call.calls == [("https://relay.example/sessions/sess-1/qr/hide", "POST", "pub-token")]` and the result is `True`.
  - `test_show_qr_posts_with_the_publisher_token`: the same, with `/qr/show`.
  - `test_hide_and_show_never_raise`: parametrized over both. `RecordingCall(exception=urllib.error.URLError("down"))` gives `False`.
  - `card_wanted`, where `show` is a recording stub returning a set value:
    - `test_a_qr_nobody_has_hidden_is_shown`: `card_wanted(CREDENTIALS, SessionStatus(3, False), show=stub)` is True, and the stub isn't called.
    - `test_a_hidden_qr_stays_hidden_while_a_phone_is_connected`: `SessionStatus(1, True)` gives False, and the stub isn't called.
    - `test_a_hidden_qr_comes_back_when_nobody_is_connected`: `SessionStatus(0, True)` with the stub returning True gives True. The stub was called once with `CREDENTIALS`.
    - `test_a_hidden_qr_stays_hidden_if_the_relay_refuses_to_show_it`: the same, with the stub returning False, gives False.
  - `card_should_stay`:
    - `test_the_card_stays_while_the_qr_is_not_hidden`: `qr_hidden: False` → True.
    - `test_the_card_closes_once_a_phone_hides_the_qr`: `qr_hidden: True` → False.
    - `test_the_card_closes_when_the_night_is_gone`: parametrized over 401 and 404 via `http_error(code)` → False.
    - `test_the_card_stays_up_through_a_relay_blip`: parametrized over `URLError("down")`, `http_error(500)` and `ValueError("bad json")` → True.
  - `test_the_real_client_hides_and_shows_a_real_qr`: written in the style of the file's client/server seam tests, with `downgrading_call` and `asyncio.to_thread`. Status starts not hidden. `hide_qr` gives True and the status is hidden. `show_qr` gives True and the status is no longer hidden.

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest -q tests/test_host_session.py`
Expected: ImportError or NameError for the new names.

- [ ] **Step 3: Implement the Interfaces above in `host/session.py`.** Leave `PairingWaiter` and the pairing timeouts alone; Task 3 removes them along with their only user.

- [ ] **Step 4: Run the whole suite**

Run: `.venv/bin/pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add host/session.py tests/test_host_session.py
git commit -m "feat(host): read and set the QR flag; decide when the card shows and closes"
```

### Task 3: The QR card process

**Files:**
- Modify: `host/qr_window.py`
- Modify: `host/session.py` (remove `PairingWaiter`, `PAIRING_TIMEOUT_SECONDS`, `REUSED_NIGHT_PAIRING_TIMEOUT_SECONDS`, and `POLL_SECONDS` if unused)
- Create: `tests/visual/card_in_kwin.sh`
- Test: `tests/test_qr_window.py` (rewrite its window and timeout tests), `tests/test_host_session.py` (drop the `PairingWaiter` tests)

**Interfaces:**
- Consumes (Task 2): `SessionStatus`, `session_status`, `card_wanted`, `card_should_stay`, `hide_qr`, `CARD_POLL_SECONDS`, `load_night`. Also the existing `resume_or_create`, `write_session_env`, `delete_session` and `qr_payload`.
- Produces:
  - **Setup mode**, `python3 -m host.qr_window --base-url URL --session-env PATH --night-file PATH`: the CLI and exit codes are unchanged. It resumes or creates the night and writes the session env. It opens no window and doesn't look at subscribers. It returns 0 once the env is written, and 1 when there is no session or the write failed. On a write failure it deletes the session only if this launch created it, as today.
  - **Card mode**, `python3 -m host.qr_window --card --night-file PATH`: it takes no other arguments and always returns 0. It loads the night file; no usable night means return. It gets `session_status`, where any exception means return. If `card_wanted` says no, it returns. Otherwise it calls `show_card`. Any exception from `show_card` is printed, never a traceback, and it still returns 0.
  - `qr_png(payload: str) -> bytes | None`:
    - runs `qrencode -t PNG -s 1 -m 2 -o -` with the payload on **stdin**;
    - returns the PNG bytes;
    - returns None when qrencode is missing or fails.

    It replaces `render_qr_png`; no file is ever written.
  - `primary_monitor(xrandr_text: str) -> tuple[int, int, int, int] | None`: the `(width, height, x, y)` of the `*` monitor in `xrandr --listactivemonitors` output, or None.
  - `card_monitor(xrandr_text: str, screen_w: int, screen_h: int) -> tuple[int, int, int, int]`: the primary monitor, else `(screen_w, screen_h, 0, 0)`.
  - `card_position(monitor, card_w: int, card_h: int) -> tuple[int, int]`: the top-right corner, with the margin `round(monitor_h * CARD_MARGIN_FRACTION)`.
  - `qr_zoom(png_side: int, monitor_h: int) -> int`: `max(1, round(monitor_h * QR_SCREEN_FRACTION / png_side))`.
  - `launcher_gone(original_ppid: int, *, getppid=os.getppid) -> bool`.
  - `show_card(credentials, *, keeps_showing=None, hide=None) -> None`. The defaults are `card_should_stay(credentials)` and `hide_qr`.
  - Constants: `QR_SCREEN_FRACTION = 1 / 3`, `CARD_MARGIN_FRACTION = 0.03`.

- [ ] **Step 1: Write the failing tests** in `tests/test_qr_window.py`. Keep the file's style: every collaborator is monkeypatched, so no window opens and no request goes out.
  - **Setup mode**, keeping the existing unreachable-relay and env-write tests:
    - `test_setup_writes_the_session_env_and_opens_no_window`: `show_card` is patched to fail the test if called. Returns 0, and `did.written == [(SESSION_ENV, CREDENTIALS)]`.
    - Delete the tests about subscribers and timeouts.
  - **Card mode:**
    - `test_card_mode_needs_only_the_night_file`: `main(["--card", "--night-file", NIGHT_FILE])` parses, with no `--base-url` or `--session-env`.
    - `test_card_mode_with_no_usable_night_does_nothing`: `load_night` → None. Returns 0, with no status call and no `show_card`.
    - `test_card_mode_shows_the_card_when_it_is_wanted`: `card_wanted` → True. `show_card` is called once with `CREDENTIALS`, and it returns 0.
    - `test_card_mode_shows_nothing_when_it_is_not_wanted`: `card_wanted` → False. No `show_card`, returns 0.
    - `test_card_mode_survives_an_unreachable_relay`: `session_status` raises `URLError`. Returns 0, no `show_card`.
    - `test_card_mode_survives_a_broken_display`: `show_card` raises `tk.TclError("no display")`. Returns 0.
  - **Pure helpers.** Use `SAMPLE = "Monitors: 3\n 0: +*DP-2 3840/699x2160/395+0+900  DP-2\n 1: +DP-1 2880/518x1800/324+3840+0  DP-1\n 2: +HDMI-A-2 2880/518x1800/324+3840+1800  HDMI-A-2\n"`.
    - `primary_monitor(SAMPLE) == (3840, 2160, 0, 900)`.
    - `primary_monitor(SAMPLE.replace("*", "")) is None`.
    - `primary_monitor("") is None`.
    - `card_monitor("", 1920, 1080) == (1920, 1080, 0, 0)`.
    - `card_position((3840, 2160, 0, 900), 800, 900) == (2975, 965)`.
    - `qr_zoom(45, 2160) == 16`, `qr_zoom(45, 1080) == 8` and `qr_zoom(500, 300) == 1`.
    - `launcher_gone(41, getppid=lambda: 41) is False` and `launcher_gone(41, getppid=lambda: 1) is True`.
  - **`qr_png`:**
    - `test_qr_png_sends_the_payload_on_stdin_not_the_command_line`: monkeypatch `subprocess.run` to record `args` and `input` and return PNG-ish bytes on stdout. The payload is absent from `args`, `input == payload.encode()`, and `"-o", "-"` is in `args`.
    - `test_qr_png_without_qrencode_is_none`.
  - In `tests/test_host_session.py`, delete the `PairingWaiter` tests.

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest -q tests/test_qr_window.py`
Expected: failures and ImportErrors for the new names.

- [ ] **Step 3: Implement `host/qr_window.py`.** The module docstring changes: the card now sits over the game, because an unmanaged window stays above a fullscreen one and never takes focus (spec §3). `show_card`:
  - Build a Tk root with `overrideredirect(True)`. Never call `focus_force`. Make the × a `Label` bound to `<Button-1>`, not a `Button`.
  - Load the QR image with `tk.PhotoImage(data=base64.b64encode(png).decode("ascii"), format="png")`, then `.zoom(qr_zoom(image.width(), monitor_h))`.
  - Without qrencode, show `qr_payload(credentials)` as a text `Label`.
  - Use a white card with the two-line caption from Global Constraints and a small grey × in its top-right corner. Size the fonts from the monitor height; the exact sizes are your call.
  - Get the monitor from `card_monitor(<xrandr --listactivemonitors stdout, or "" on any failure; timeout 3 s>, root.winfo_screenwidth(), root.winfo_screenheight())`. Place the card with `root.geometry(f"+{x}+{y}")` after `update_idletasks()`, using `card_position`.
  - Every `CARD_POLL_SECONDS`, do one of three things: destroy the card if `launcher_gone(<ppid at start>)`; destroy it if `not keeps_showing()`; otherwise reschedule.
  - On ×, call `hide(credentials)` and destroy the card whatever the result.

- [ ] **Step 4: Remove `PairingWaiter` and the pairing timeouts** from `host/session.py`, together with any import of them.

- [ ] **Step 5: Write `tests/visual/card_in_kwin.sh OUT.png`.** This is a manual check, not part of pytest. Adapt the spike at `/tmp/claude-1000/-home-nevenit-Programming-Python-JackBoxAutoVIPServer/310c27fd-e37b-48e4-86e6-073258d1252e/scratchpad/spike-overlay/` (`run.sh`, `fake_game.py`), which proved the approach.
  - It runs `dbus-run-session -- kwin_wayland --virtual --xwayland --no-lockscreen --no-global-shortcuts --width 1920 --height 1080 --socket <unique> --exit-with-session <the script itself, in an inner mode>`.
  - `XDG_CONFIG_HOME`, `XDG_CACHE_HOME` and `XDG_STATE_HOME` point into a `mktemp -d` that is removed on exit.
  - **Inside:**
    - a fake fullscreen Tk "game" that re-raises and re-focuses itself every second;
    - the real `show_card` with fake credentials (`base_url="https://relay.invalid"`, 43-character tokens), `keeps_showing` true for about 6 s, and `hide=lambda c: True`;
    - after about 3 s, `spectacle -b -n -f -o OUT.png`;
    - a printout of the active window's `WM_NAME` and `_NET_WM_STATE`.
  - Nothing appears on the real desktop.

- [ ] **Step 6: Run the visual check and look at the screenshot**

Run: `bash tests/visual/card_in_kwin.sh "$PWD/.superpowers/card.png"`, then view the PNG.
Expected:
- the white card sits in the top-right with the margin, its QR about a third of the height, and both caption lines and the × visible;
- the printed active window is the fake game, with `_NET_WM_STATE_FULLSCREEN`.

Put the screenshot path in your report.

- [ ] **Step 7: Run the whole suite**

Run: `.venv/bin/pytest -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add host/ tests/test_qr_window.py tests/test_host_session.py tests/visual/card_in_kwin.sh
git commit -m "feat(host): the QR card, an unmanaged window that sits over the game until hidden"
```

### Task 4: Launcher — start the card in the background, stop it on exit

**Files:**
- Modify: `jbvip-run.sh`
- Modify: `README.md` (the QR window paragraph, around lines 185-196)
- Test: `tests/test_launcher.py`

**Interfaces:**
- Consumes (Task 3): card mode, `python3 -m host.qr_window --card --night-file PATH`.
- Produces:
  - `CARD=""`, declared next to `MITM=""`, before the trap.
  - `start_qr_card()`: if `$SESSION_ID` is empty, it returns 0. Otherwise it runs `python3 -m host.qr_window --card --night-file "$NIGHT_FILE" &` and sets `CARD=$!`.
  - Define it and call it right after the `if [ "$ready" != 1 ]; then … fi` block and before `tmpbundle="$(mktemp`, so it comes after every `run_plain` call. The call is a line of its own, `start_qr_card`. Define the function after the setup block, because existing tests find the setup call as the first `host.qr_window` line.
  - `cleanup()` stops the card first, with `kill "$CARD" 2>/dev/null || true` when it's set, ahead of `kill "$MITM"`.

- [ ] **Step 1: Write the failing tests** in `tests/test_launcher.py`
  - `test_the_qr_card_starts_once_interception_is_up`: in the text, the `start_qr_card` call line comes after `if [ "$ready" != 1 ]; then` and before `tmpbundle="$(mktemp`. Every `run_plain "` occurrence comes before it.
  - `test_the_launch_does_not_wait_for_the_qr_card(tmp_path)`:
    - A fake `python3` on a private PATH writes `"$@"` to a file and then `exec sleep 30`.
    - Run `set -euo pipefail`, `SESSION_ID=s`, `NIGHT_FILE=/x/night.json`, `CARD=""`, then `launcher_function("start_qr_card")`, `start_qr_card`, `echo "card=$CARD"` and `kill "$CARD"`.
    - It finishes in under 2 s, with exit 0 and a non-empty `card=`. The recorded args are exactly `-m host.qr_window --card --night-file /x/night.json`.
  - `test_no_qr_card_without_a_relay_session(tmp_path)`: with `SESSION_ID=""`, the fake `python3` never runs and `CARD` stays empty.
  - `test_cleanup_stops_the_qr_card(tmp_path)`: run `cleanup_block()` after `sleep 30 & CARD=$!` (with `STATE` set to a temp dir and `SESSION_ID` empty) and call `cleanup`. Afterwards `kill -0 "$CARD"` fails.
  - `test_cleanup_stops_the_qr_card_before_the_slow_clear`: in `cleanup()`, `kill "$CARD"` comes before `host.session --clear`.

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest -q tests/test_launcher.py`
Expected: the new tests fail, because `start_qr_card` isn't defined.

- [ ] **Step 3: Implement the Interfaces above in `jbvip-run.sh`.**
  - Rewrite the setup block's comments: it no longer shows anything, and the card comes later, over the game.
  - Give the new block a comment saying the card is backgrounded so the game never waits for it, and that it starts after interception so a fallback launch never shows it.

- [ ] **Step 4: Rewrite the README paragraph.**
  - The QR sits in the top-right over the game until someone taps **Hide QR on TV** on their phone, or clicks × on the PC.
  - It stays hidden for the night.
  - A launch with no phone connected brings it back.
  - Remove Skip, the 120 s and the 15 s.

- [ ] **Step 5: Run everything**

Run: `.venv/bin/pytest -q && bash -n jbvip-run.sh`
Expected: all pass, with no output from `bash -n`.

- [ ] **Step 6: Commit**

```bash
git add jbvip-run.sh README.md tests/test_launcher.py
git commit -m "feat(launcher): run the QR card next to the game and stop it on exit"
```

### Task 5: Phone page — Hide QR on TV

**Files:**
- Modify: `relay/static/player.html`
- Test: `tests/browser/player.test.mjs`

**Interfaces:**
- Consumes (Task 1): every event's `qr_hidden`; `POST /sessions/{id}/qr/hide` with the page's subscriber token → 204; `POST .../qr/show` with the publisher token (for the test).
- Produces:
  - `<button id="qr-hide-btn" type="button">Hide QR on TV</button>`, the first item in `#menu-panel`.
  - A state variable `qrHidden`. It is `true` until an event says `qr_hidden: false`, and `start()` resets it. An event missing the field counts as hidden.
  - `readEvents` parses each `data:` line once and hands it to `onEvent(event)`, which sets `qrHidden` and then calls `onCode(event.code)`.
  - `render()` sets `$("qr-hide-btn").hidden = qrHidden || connection !== "live"`.
  - Click handler:
    - POST with `Authorization: Bearer <token>` and `cache: "no-store"`;
    - on 2xx, set `qrHidden = true`, close the menu and render;
    - otherwise, set the button text to `Couldn't hide it. Try again` and keep it.

    Opening the menu resets the label to `Hide QR on TV`.

- [ ] **Step 1: Write the failing browser checks.** Add a section with its own session and page (name stored), in the file's `check(...)`/`until(...)` style.
  - 'the menu offers Hide QR on TV while the TV QR is up'.
  - 'Hide QR on TV hides it on the relay': GET with the publisher token → `qr_hidden === true`.
  - 'the button is gone after hiding'.
  - 'a second open phone loses the button too': a second page on the same fragment, opened before the hide.
  - 'hiding does not reload the running game': publish a code first. jackbox.tv's request count and `frame.src` are unchanged after the hide event.
  - 'the host showing it again brings the button back': POST `/qr/show` with the publisher token.
  - 'a failed hide says so and keeps the button': intercept `/qr/hide` once with a 503. The button text is `Couldn't hide it. Try again`, and it stays visible.

- [ ] **Step 2: Run to verify they fail**

Run: `cd tests/browser && npm test`
Expected: the new checks report not ok.

- [ ] **Step 3: Implement the Interfaces above in `relay/static/player.html`.**

- [ ] **Step 4: Run every suite**

Run: `cd tests/browser && npm test && cd ../.. && .venv/bin/pytest -q`
Expected: every check ok, all pass.

- [ ] **Step 5: Commit**

```bash
git add relay/static/player.html tests/browser/player.test.mjs
git commit -m "feat(page): Hide QR on TV in the menu"
```

### Task 6: Deploy and check with real phones (needs the user)

Outward-facing: **ask the user before pushing and before touching the VPS or the main checkout.** The VPS work goes through the user's `vps` tmux pane, batched, as on 2026-10-28 (`~/Documents/homelab/jbvip-relay.md`). This task replaces Task 8 steps 6-7 of `docs/superpowers/plans/2026-10-27-browser-player-page.md`.

- [ ] **Step 1: Push:** `git push origin browser-player-page`.
- [ ] **Step 2: Update the relay on `bne`.** Use the documented update command in `jbvip-relay.md`. Expected:
  - `systemctl is-active jbvip-relay` prints `active`.
  - From the desktop, a throwaway `POST /sessions` followed by `GET` (publisher token) shows `"qr_hidden": false`.
  - Then `DELETE` the session.
- [ ] **Step 3: Point Steam at the branch:** `git -C /home/nevenit/Programming/Python/JackBoxAutoVIPServer checkout --detach browser-player-page`. Expected: its HEAD equals the branch tip, and `status --short` prints nothing.
- [ ] **Step 4: Update `~/Documents/homelab/jbvip-relay.md`:** the deployed commit, and the two QR routes in "What it serves".
- [ ] **Step 5: Real-use checklist with the user**, on the dev PC:
  - **First launch:** the card appears top-right of the primary monitor, above a real pack. The game keeps the keyboard and controller.
  - **Group join:** everyone scans the card. **Hide QR on TV** on Android removes it within a few seconds, and the button disappears on the iPhone too.
  - **Pack switch:** the QR stays hidden.
  - **Safety net:** quit, lock every phone, launch again: the card comes back.
  - **Megapicker:** launch from it. The card shows above the pack when it should, and never stays over the Megapicker after a pack quits.
  - **× on the PC** hides it. After ×, the game still has the keyboard.
  - **Steam's Stop:** press it while the card is up. The card goes, and Steam returns to Play.
  - **What the card covers:** for each pack, note what it covers in the lobby: the room code, the player list, the VIP's start prompt. It takes about 21% of the width and 46% of the height.
  - **Safety net over a real Megapicker night:** note how often it brings the QR back.
  - **A pack that covers the card:** if any pack ever does, record which one (re-raising on each poll should prevent it).
  - Then run the rest of the 2026-10-27 checklist: refresh mid-game, a late join via Invite (and the iPhone's bottom edge), quitting goes to waiting, and dead-stream watching. Confirm in the relay journal that `POST /sessions/<id>/qr/hide` appears and that no session `DELETE` appears on a quit.
- [ ] **Step 6:** Fix anything found (superpowers:systematic-debugging), then superpowers:finishing-a-development-branch.
