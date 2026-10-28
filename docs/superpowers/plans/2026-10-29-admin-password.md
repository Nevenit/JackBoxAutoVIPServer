# Admin Password Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A phone that enters the host's password once becomes the admin on every later night, and gets each new room 0.5 s (tweakable) before the guests so it lands VIP.

**Architecture:**
- **Admin key.** The TV PC and the phone both derive the same key from the password: `sha256("jbvip-admin:" + password)` in hex.
- **Registration.** The launcher's setup step registers the key and the head start with tonight's relay session.
- **Admin streams.** The phone sends the key in a header when it subscribes. The relay marks matching streams as admin and reports the outcome in a response header.
- **Delayed delivery.** When a new room is published, the relay gives it to admin streams at once and to guests after the head start.

**Tech Stack:** Python stdlib (`hashlib`, `urllib`), Starlette + asyncio (relay), bash (launcher), vanilla JS with WebCrypto (page). Tests use pytest and puppeteer-core/Chromium.

**Spec:** `docs/superpowers/specs/2026-10-29-admin-password-design.md`. It builds on `2026-10-27-browser-player-page-design.md` and `2026-10-28-tv-qr-overlay-design.md`.

## Global Constraints

**Exact values**
- Key derivation: `sha256(("jbvip-admin:" + password).encode("utf-8")).hexdigest()`, which is 64 lowercase hex characters. Test vector: password `hunter2` → `d2f597d1c562c90fdcb4f02255abdf38aee33a342ed215663554309fb17091c6`.
- Settings in `~/.config/jbvip/relay.env`: `JBVIP_ADMIN_PASSWORD=` (optional; empty or unset means no admin feature) and `JBVIP_ADMIN_HEAD_START=` (optional seconds, default `0.5`). The relay clamps the head start to `0`–`5` s.
- Request header carrying the key: `X-Jbvip-Admin: <key>`, on the subscribe request only.
- Response header on every successful subscribe: `X-Jbvip-Admin: admin | guest | wrong | off | limited`.
  - `guest`: no key was sent.
  - `off`: a key was sent but the session has no registration.
  - `limited`: the wrong-key budget is spent.
- **Page copy:**
  - menu field placeholder `Admin password`;
  - button `Become admin`;
  - `Priority: on` / `Priority: off`;
  - `Stop being admin`;
  - status lines `Wrong admin password`, `No admin tonight`, `Too many tries; try again in a minute`;
  - bar marker `admin`.

**Hard rules**
- The password and the key never appear in a URL, a QR, a log or a command line.
  - Only the key crosses the network: in a header, or in a request body.
  - The launcher passes the password to Python through the environment, never through argv.
- No password set means nothing changes for anyone (spec §5).
- No event may give a guest the new room before its head start ends (spec §3.2). Admin state never reloads or swaps a framed game (2026-10-27 §7).
- A relay error, a wrong password or a failed registration never stops or delays a game.
- No new dependencies.

**Every task**
- After each task, `.venv/bin/pytest -q`, `cd tests/browser && npm test` and `bash -n jbvip-run.sh` all pass.
- Each commit ends with the committing model's own `Co-Authored-By:` trailer.
- Never touch the main checkout `/home/nevenit/Programming/Python/JackBoxAutoVIPServer`, the VPS or karaokePC before Task 5.

## Review Focus

Most likely to bite first:
1. **A guest reconnecting or joining during the head start** sees the room early: through the seed, through a QR-flag event, or through a second publish. Covered by Task 2's tests.
2. **The admin phone locked when a room starts**, so no admin is connected. Guests must not wait. Covered by Task 2's test.
3. **The feature switched off for a night** (no password in `relay.env`). Admin phones must keep their key (`off`, not `wrong`). Covered by Task 1's relay test and Task 4's page check.
4. **Someone with the QR link guessing passwords.** The budget must stop them, and it must not let them make the real admin forget its key (`limited` keeps the key). Covered by Tasks 1 and 4.
5. **A password with spaces, quotes or a `$` in `relay.env`.** The launcher must pass it to Python intact. Covered by Task 3's launcher test.

---

### Task 1: Relay — registration and admin streams

**Files:**
- Modify: `relay/state.py`, `relay/app.py`, `relay/README.md`
- Test: `tests/test_relay_state.py`, `tests/test_relay_app.py`

**Interfaces:**
- Produces:
  - `Session.admin_key: str | None = None` and `Session.head_start: float = 0.0`.
  - `RelayState.register_admin(session_id, key: str, head_start: float) -> None`:
    - raises `ValueError` unless `key` is 64 lowercase hex characters and `head_start` is a finite number;
    - clamps `head_start` to `[0, 5]`;
    - replaces any earlier registration;
    - raises `KeyError` for an unknown session.
  - `RelayState.admin_status(session_id, key: str | None) -> str`:
    - returns `"guest"` when `key` is None;
    - returns `"off"` when the session has no registration;
    - returns `"admin"` when the key matches, compared in constant time with `_token_matches`;
    - otherwise returns `"wrong"`.
  - `PUT /sessions/{id}/admin` with the publisher token and JSON body `{"key": str, "head_start": number}` → 204.
    - Errors: 400 for a bad body, 404 for a dead session, 401 for a wrong token.
    - It is charged to the existing publish budget, after `_refuse`.
  - Subscribe reads `X-Jbvip-Admin`.
    - A wrong-key check is charged to a new per-session budget, `ADMIN_GUESS_LIMIT_PER_MINUTE = 10`, after the subscriber-token check.
    - When that budget is spent, no key is checked and the status is `limited`.
    - The response always carries `X-Jbvip-Admin: <status>`.
    - The stream's role is stored as the value in `self._queues[session_id]`, which is today a `dict[Queue, None]`: `"admin"` when the status is `admin`, else `"guest"`.
  - `_release` also drops the session's guess limiter.

- [ ] **Step 1: Write the failing tests**
  - **State:**
    - register then `admin_status` with the right key → `"admin"`, and with another valid-format key → `"wrong"`;
    - no registration → `"off"`;
    - `None` → `"guest"`;
    - clamping: `-1` → `0` and `9` → `5`;
    - a bad key (wrong length or uppercase) → `ValueError`;
    - an unknown session → `KeyError`.
  - **App:**
    - PUT with the publisher token → 204, with the subscriber token → 401, with a bad body → 400.
    - Subscribe with no header → response header `guest`.
    - Subscribe with the matching key → `admin`, with another key → `wrong`, and with a key while unregistered → `off`.
    - With `admin_guess_limiter_factory` set to a limit of 2: three wrong subscribes give `wrong, wrong, limited`, and the real key after that also gives `limited`.
    - Wrong subscriber tokens with a key don't spend the guess budget.
    - An admin stream counts in GET's `subscribers` and in the 16-stream cap.

    Add `admin_guess_limiter_factory=None` to `Relay`/`build_app`, as with the other limiter factories.

- [ ] **Step 2: Run them to verify they fail:** `.venv/bin/pytest -q tests/test_relay_state.py tests/test_relay_app.py`.

- [ ] **Step 3: Implement the Interfaces.** Then document the PUT route, the request header and the response header in `relay/README.md`'s Endpoints section.

- [ ] **Step 4: Run the whole suite:** `.venv/bin/pytest -q`.

- [ ] **Step 5: Commit:** `feat(relay): register an admin key and mark admin streams`.

### Task 2: Relay — the head start

**Files:**
- Modify: `relay/app.py`, `relay/README.md`
- Test: `tests/test_relay_app.py`

**Interfaces:**
- Consumes (Task 1): stream roles in `self._queues`, `Session.admin_key` and `Session.head_start`.
- Produces the delivery rules (spec §3.2).

  **When the window opens.** A publish that changes the room starts a window when all three hold:
  - the session has a registration;
  - `head_start > 0`;
  - at least one `"admin"` stream is connected.

  **During the window:**
  - Admin queues get the event at once.
  - The relay remembers the room guests are still on in `self._guest_room[session_id] = (code, ts)`, keeping the existing entry if a window is already open.
  - It schedules one guest delivery with `asyncio.get_running_loop().call_later(head_start, ...)` in `self._guest_timer[session_id]`. A second publish inside the window updates the room but doesn't reschedule.
  - Any event built for a guest uses the guest room: the subscribe seed and QR-flag events. Admins always get the current room.

  **When the timer fires,** guest queues get the current room, and both dicts drop the session.

  **A clear during the window** cancels the timer, drops both entries and goes to everyone at once.

  **Session end:** `_release` cancels a pending timer.

  **No admin connected,** or no window: everything goes out at once, as today.
- Helper: `_room_event(session_id, *, admin: bool)`. The existing callers pass the stream's role.
- Constant: `ADMIN_HEAD_START_MAX = 5.0` is shared with Task 1's clamp. Move that clamp to use it if Task 1 hard-coded 5.

- [ ] **Step 1: Write the failing tests.** Use real streams as the existing tests do. Register with a head start of `0.3`. Use `asyncio` timing with margins of a few hundred ms, and keep it robust.
  - An admin stream gets a new room at once. A guest stream gets it only after about 0.3 s: not within 0.1 s, but within 1 s.
  - With no admin connected, the guest gets it at once.
  - A guest who subscribes during the window is seeded with the **old** room (or null), then gets the new room when the window ends.
  - A QR hide during the window repeats the old room to the guest and the new room to the admin.
  - Two publishes inside one window give the guest exactly one event, carrying the second room.
  - A clear during the window reaches both at once, and no late room event follows.
  - A head start of `0` means no window.
  - Deleting the session during the window leaves no timer firing into a dead session.

- [ ] **Step 2: Run them to verify they fail.**

- [ ] **Step 3: Implement the Interfaces.** Add one paragraph to the README's "Current code" section on the head start.

- [ ] **Step 4: Run the whole suite.**

- [ ] **Step 5: Commit:** `feat(relay): admins get each new room before the guests`.

### Task 3: Host and launcher — derive and register the key

**Files:**
- Modify: `host/session.py`, `host/qr_window.py`, `jbvip-run.sh`, `README.md`
- Test: `tests/test_host_session.py`, `tests/test_qr_window.py`, `tests/test_launcher.py`

**Interfaces:**
- Consumes (Task 1): `PUT /sessions/{id}/admin`.
- Produces:
  - `admin_key(password: str) -> str`, per Global Constraints.
  - `ADMIN_HEAD_START_DEFAULT = 0.5`.
  - `admin_head_start(raw: str | None) -> float`: empty or None gives the default; an unparsable or non-finite value gives the default plus a stderr warning. Clamping is left to the relay.
  - `register_admin(credentials, key: str, head_start: float, *, call=_urllib_call) -> bool`: a PUT with the publisher token and a JSON body. It never raises, returning False on any failure.
  - `_urllib_call(url, method, token, body: dict | None = None)` sends `body` as JSON with `Content-Type: application/json`. Every existing caller is unchanged. `RecordingCall` in the tests accepts and records `body`.
  - **Setup mode** of `host/qr_window.py`:
    - after `write_session_env` succeeds, it reads `JBVIP_ADMIN_PASSWORD` and `JBVIP_ADMIN_HEAD_START` from `os.environ`;
    - if the password is non-empty, it calls `register_admin`, printing one stderr line (no key) on failure;
    - the exit code is unaffected.
  - **`jbvip-run.sh`:**
    - In the relay-config block, before the `JBVIP_RELAY_URL="${JBVIP_RELAY_URL:-}"` line, add `JBVIP_ADMIN_PASSWORD="${JBVIP_ADMIN_PASSWORD:-}"` and `JBVIP_ADMIN_HEAD_START="${JBVIP_ADMIN_HEAD_START:-}"`.
    - Declare them `+x`, so they never reach mitmdump or the game.
    - The setup call passes them as environment assignments in front of `python3 -m host.qr_window` (prefix them on the `if python3 …` line as `JBVIP_ADMIN_PASSWORD="$JBVIP_ADMIN_PASSWORD" JBVIP_ADMIN_HEAD_START="$JBVIP_ADMIN_HEAD_START" python3 -m host.qr_window …`).
    - Ruling 2 of the QR plan still holds: the first `host.qr_window` line must be the setup call and must start with `if `. Keep the prefix on that same line.

- [ ] **Step 1: Write the failing tests**
  - `admin_key("hunter2")` equals the vector.
  - `admin_head_start`: None or `""` → 0.5, `"1.25"` → 1.25, `"abc"` → 0.5, `"nan"` → 0.5.
  - `register_admin` makes a PUT to `.../sessions/sess-1/admin` with the publisher token and body `{"key": ..., "head_start": 0.5}`, and gives False on a URLError.
  - A real-relay seam test (as in the file): register, then a subscribe with the key reads `X-Jbvip-Admin: admin` through httpx.
  - Setup mode: password set → one `register_admin` call with `admin_key(password)`; no password → none; `register_admin` returning False → still exit 0.
  - **Launcher:**
    - a `relay.env` containing `JBVIP_ADMIN_PASSWORD='p w"$x'` reaches the setup step's environment intact. Use a fake `python3` that prints `os.environ`, through the existing block-extraction helpers.
    - neither variable is exported to mitmdump or the game (text checks like the existing session-token test);
    - the password is not in any command line.

- [ ] **Step 2: Run them to verify they fail.**

- [ ] **Step 3: Implement the Interfaces.** In `README.md`'s relay section, document the two lines and how to become admin. Point to the menu and say that no password set means no change.

- [ ] **Step 4: Run the whole suite and `bash -n jbvip-run.sh`.**

- [ ] **Step 5: Commit:** `feat(host): register the admin key with each night's session`.

### Task 4: Phone page — become admin

**Files:**
- Modify: `relay/static/player.html`
- Test: `tests/browser/player.test.mjs`

**Interfaces:**
- Consumes (Task 1): request header `X-Jbvip-Admin`, response header `X-Jbvip-Admin`, and `PUT /sessions/{id}/admin` (the test registers through it with the publisher token). Also (Task 2) the delivery order.
- Produces:
  - **Storage keys:** `jbvip-admin` holds the hex key; `jbvip-priority` holds `"off"` when off and is absent otherwise. Every access is wrapped in try/catch, like `jbvip-name`.
  - **Key derivation:** `crypto.subtle.digest("SHA-256", new TextEncoder().encode("jbvip-admin:" + password))` → lowercase hex.
  - **`subscribe()`** sends `X-Jbvip-Admin` when a key is stored and priority isn't off. It reads the response header into `adminState`:
    - `wrong` → remove the key, show `Wrong admin password`;
    - `off` → keep it, `No admin tonight`;
    - `limited` → keep it, `Too many tries; try again in a minute`;
    - `admin` → the bar marker `admin` shows.
  - **The menu:**
    - with no key: a password `<input type="password">` with placeholder `Admin password` and a `Become admin` button. It stores the derived key, then reconnects (`subscribe()`).
    - with a key: a `Priority: on`/`Priority: off` toggle, which stores and reconnects, and `Stop being admin`, which removes both keys and reconnects.
  - **Reconnecting never reloads a framed game.** The re-seeded room equals `current`, so `show()` ignores it.

- [ ] **Step 1: Write the failing browser checks.** Give them their own section, a session registered with the vector key and a head start of `0.3`, and their own browser contexts.
  - Entering `hunter2` makes the bar show `admin`, and a reload keeps it.
  - A wrong password says `Wrong admin password` and clears the stored key.
  - With no registration, the page says `No admin tonight` and keeps the key.
  - Priority off connects without the header, so the marker goes.
  - Stop being admin clears the key.
  - An admin page and a guest page get a newly published room in that order. The guest's frame `src` changes at least about 0.2 s after the admin's.
  - Becoming admin while a game is framed doesn't reload it: the jackbox.tv count is unchanged.
  - No request URL contains the key.

- [ ] **Step 2: Run them to verify they fail:** `cd tests/browser && npm test`.

- [ ] **Step 3: Implement the Interfaces.**

- [ ] **Step 4: Run every suite.**

- [ ] **Step 5: Commit:** `feat(page): become admin with a password in the menu`.

### Task 5: Deploy and check on the TV (needs the user)

Outward-facing: ask the user before pushing, before touching the VPS and before touching karaokePC.
- **Desktop to karaokePC:** key SSH (`ssh nevenit@192.168.20.238`) now works. Check first that the karaoke session isn't driving the `tvpc` pane. Non-root steps need no pane.
- **VPS:** batch the work through the `vps` tmux pane (`~/Documents/homelab/jbvip-relay.md`).

- [ ] **Step 1:** Push the branch.
- [ ] **Step 2:** Update the relay on `bne` with the documented update command.
- [ ] **Step 3:** On karaokePC, `git -C ~/Projects/JackBoxAutoVIPServer pull`.
  - **The user** adds `JBVIP_ADMIN_PASSWORD=<their choice>` to `~/.config/jbvip/relay.env` there. They choose and type the password; never put it in a command or a log.
  - Optionally they also add `JBVIP_ADMIN_HEAD_START=`.
- [ ] **Step 4:** On the desktop, `git -C /home/nevenit/Programming/Python/JackBoxAutoVIPServer checkout --detach browser-player-page`.
- [ ] **Step 5:** Update `~/Documents/homelab/jbvip-relay.md` (the new route and header) and `~/Documents/homelab/karaoke-pc.md` (the password line exists; never its value).
- [ ] **Step 6: Real use on the TV.**
  - The admin scans, enters the password, and the bar shows admin.
  - The admin taps Play as soon as the room appears and lands VIP over a guest tapping at once.
  - Next night or relaunch: scanning is enough.
  - Tweak `JBVIP_ADMIN_HEAD_START` if 0.5 s isn't enough.
  - Then run the TV QR card checklist from `2026-10-28-tv-qr-overlay.md` Task 6 Step 5.
- [ ] **Step 7:** Fix anything found, then superpowers:finishing-a-development-branch.
