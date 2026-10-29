# Admin Early Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Guests are held when a new room starts with an admin phone connected, until the first player joins that room (the admin tapping Play) or the hold runs out (10 s by default).

**Architecture:**
- **Relay.** A new publisher-token route ends an open head-start window early, but only for the room the guests are being held for.
- **Proxy.** The TV PC's mitmproxy addon already sees every websocket frame the game exchanges with Jackbox. It spots the first player joining a room and calls that route, once per room.
- **Defaults.** The default hold goes from 0.5 s to 10 s, and the relay's clamp from 5 s to 30 s.

The phone page does not change.

**Tech Stack:** Python stdlib (`json`, `urllib`), Starlette and asyncio (relay), mitmproxy addon hooks. Tests use pytest.

**Spec:** `docs/superpowers/specs/2026-10-29-admin-password-design.md`, as amended the same day: §2 "The head start", §3.2 "Early release", §3.3 "Spotting the first join". This plan follows `docs/superpowers/plans/2026-10-29-admin-password.md`, which is complete except for its real-use step.

## Global Constraints

**Exact values**
- Route: `POST /sessions/{session_id}/release`, publisher token (`Authorization: Bearer <publisher_token>`), body `{"code": "<CODE>"}`.
  - It returns 204 whether or not it released anything, 400 for a bad body, and 401/404 like the other publisher routes.
  - It is charged to the publish budget (`_over_budget(session_id)`).
- `ADMIN_HEAD_START_MAX = 30.0` (relay/state.py). `ADMIN_HEAD_START_DEFAULT = 10.0` (host/session.py).
- **ecast join:**
  - The frame is on a flow whose URL holds `/rooms/<CODE>/`. It is JSON with `"opcode": "client/connected"`.
  - It counts unless `result.role == "audience"`. A missing `result` or `role` still counts.
  - Example: `{"pc":42,"opcode":"client/connected","result":{"id":2,"userId":"u1","name":"ALICE","role":"player","reconnect":false}}`
- **Blobcast join:**
  - A socket.io frame `5:::<json>` whose `args` holds a dict with `"event": "CustomerJoinedRoom"` and a plausible `roomId` (`looks_like_code`).
  - Example: `5:::{"name":"msg","args":[{"type":"Event","event":"CustomerJoinedRoom","roomId":"ABCD","customerUserId":"u1","customerName":"ALICE","options":{"roomcode":"","name":"ALICE"}}]}`
  - A `CreateRoom` frame is **not** a join.

**Hard rules**
- No event may give a guest the new room before its head start ends, except an early release naming that same room (spec §3.2).
- A missed join, a failed release call or a dead relay never stops, delays or crashes the game or mitmproxy. The hold's timer is the fallback.
- Room codes, tokens and keys are never logged. A release failure logs with `LOG_MARKER` and without the code.
- No new dependencies. The phone page (`relay/static/player.html`) is not touched.

**Every task**
- After each task, `.venv/bin/pytest -q` passes (437 before), `cd tests/browser && npm test` passes all checks (63 before), and `bash -n jbvip-run.sh` passes.
- Each commit ends with the committing model's own `Co-Authored-By:` trailer.
- Never touch the main checkout `/home/nevenit/Programming/Python/JackBoxAutoVIPServer`, the VPS or karaokePC before Task 3. Never run the launcher, Steam, sudo or the redirect scripts. Never use `git stash`.

## Review Focus

Most likely to bite first:
1. **A stale release.** A release for the previous game's room, or for a room replaced during the hold, must not let guests into the current room early. Covered by Task 1's "another room" test.
2. **Many join frames.** Several players, reconnects, or the same frame seen twice must produce one release call per room. Covered by Task 2's repeat test.
3. **A release with no window open.** No admin was connected, or the hold already ran out. It must do nothing and send guests no second event. Covered by Task 1.
4. **The relay unreachable when the release fires.** It must never raise into mitmproxy. Covered by Task 2's publisher test.
5. **An audience member joining on ecast.** It must not count as a player. Covered by Task 2's parser test.

---

### Task 1: Relay — early release and the 30 s clamp

**Files:**
- Modify: `relay/state.py` (`ADMIN_HEAD_START_MAX`)
- Modify: `relay/app.py` (new handler and route)
- Modify: `relay/README.md` (route table row for `/release`; the `/admin` row's clamp `0 to 5` → `0 to 30`; mention the early release where the head start is described)
- Test: `tests/test_relay_state.py`, `tests/test_relay_app.py`

**Interfaces:**
- Produces: `POST /sessions/{session_id}/release` as in Global Constraints. Task 2's publisher calls it.

- [ ] **Step 1: Update the clamp test.** In `test_register_admin_clamps_head_start_and_replaces`, the upper clamp becomes `30`: a registration with `head_start=99` gives `s.head_start == 30`.

- [ ] **Step 2: Write the failing relay tests** in `tests/test_relay_app.py`, next to the head-start tests. Use the existing `night()`, `guest_stream()`, `put_room()`, `feed_event()` and `timed()` helpers, plus a small `release(http, s, code, token=None)` helper that posts `{"code": code}` to `/sessions/{id}/release` with the publisher token by default.
  - `test_an_early_release_lets_held_guests_in_at_once`:
    - `night(http, head_start=5)` with a guest stream; `put_room(..., "ABCD")`.
    - The admin gets `ABCD`. The guest gets nothing within 0.1 s.
    - `release(..., "ABCD")` → 204. The guest's next event has `code == "ABCD"` and arrives in under 0.5 s.
    - No further guest event within 0.3 s.
  - `test_a_release_for_another_room_changes_nothing`:
    - `night(http, head_start=1)`, a guest, `put_room(..., "ABCD")`.
    - `release(..., "WXYZ")` → 204. The guest gets nothing within 0.3 s.
    - The guest then gets `ABCD` once the hold ends.
  - `test_a_release_with_no_window_open_is_a_no_op`:
    - `night(http, admin=False)`, a guest, `put_room(..., "ABCD")`. The guest gets `ABCD` at once.
    - `release(..., "ABCD")` → 204. No further guest event within 0.3 s.
  - `test_release_needs_the_publisher_token_and_a_code`:
    - the subscriber token → 401; an unknown session → 404;
    - bodies `{}`, `{"code": 5}` and `[1]` → 400.

- [ ] **Step 3: Run them and see them fail.** `.venv/bin/pytest tests/test_relay_app.py -q -k release tests/test_relay_state.py`. Expected: they fail with 404/405 for the route, and on the clamp value.

- [ ] **Step 4: Implement.**
  - Set `ADMIN_HEAD_START_MAX = 30.0`.
  - Add `async def release_guests(self, request)` to the relay class, in the style of `clear_code`/`register_admin`:
    1. `_refuse(request, session_id)`, then `_over_budget(session_id)`.
    2. Parse the body; it needs a `str` `code`, otherwise 400.
    3. If the session expired inside the `await`, return 404 as `register_admin` does.
    4. If `session_id in self._guest_room` and `self.state.current_code(session_id)` is not None and its `.code == code`: cancel and pop the window's timer, then call `self._guests_catch_up(session_id)`.
    5. Return 204.
  - Register `Route("/sessions/{session_id}/release", relay.release_guests, methods=["POST"])`.

- [ ] **Step 5: Run the focused tests, then the full suites** (Every task). All pass.

- [ ] **Step 6: Commit.** `feat(relay): an early release ends the head start for the held room; clamp to 30 s`

### Task 2: TV PC — spot the first join, send the release, 10 s default

**Files:**
- Modify: `extractor/roomcode.py` (two pure parsers)
- Modify: `extractor/addon.py` (a websocket helper and the release fan-out)
- Modify: `extractor/relay_publisher.py` (`release`)
- Modify: `host/session.py` (`ADMIN_HEAD_START_DEFAULT = 10.0`)
- Modify: `README.md` (the admin-password paragraph)
- Test: `tests/test_roomcode.py`, `tests/test_addon.py`, `tests/test_relay_publisher.py`, `tests/test_host_session.py`

**Interfaces:**
- Consumes: Task 1's route.
- Produces:
  - `joined_room_from_ecast(url: str, message: str) -> str | None`: the room code from `url` (via `extract_code_from_url`) when `message` is an ecast join per Global Constraints; else None. Never raises on junk.
  - `joined_room_from_blobcast_socketio(message: str) -> str | None`: the `roomId` of a `CustomerJoinedRoom` frame; else None.
  - `RoomCodeExtractor.joined_room_from_websocket_message(self, flow) -> str | None`: host-filtered like `code_from_websocket_message`, reading the newest frame. It tries ecast with `flow.request.pretty_url`, then Blobcast. Decoding the newest frame's text is shared with `code_from_websocket_message` through one private helper; don't duplicate it.
  - `RelayPublisher.release(self, code: str) -> async None`:
    - It POSTs `{"code": code}` with the publisher token to `<base>/sessions/<id>/release`.
    - One attempt, because the hold's timer is the fallback. It never raises.
    - It skips a code it already released, and does nothing once the publisher is dead.
    - A failure logs `LOG_MARKER` without the code.

- [ ] **Step 1: Write the failing parser tests** in `tests/test_roomcode.py`:
  - **The ecast example** on `wss://ecast.jackboxgames.com/api/v2/rooms/ABCD/play?role=host&name=x` gives `"ABCD"`.
  - **Also `"ABCD"`:** the same frame with no `role`, and with no `result`.
  - **`None`:**
    - `role` `"audience"`;
    - opcode `client/welcome`;
    - non-JSON;
    - a JSON list;
    - a URL with no room.
  - **Blobcast:**
    - the example gives `"ABCD"`;
    - a `CreateRoom` frame gives `None`;
    - a `CustomerJoinedRoom` with `roomId` `"!!"` gives `None`;
    - a non-`5:::` string gives `None`.

- [ ] **Step 2: Write the failing addon tests** in `tests/test_addon.py`. Build flows like `_ws_message_flow`, but let the URL be chosen.
  - The `websocket_message` hook, fed the ecast example on a jackbox host's `/rooms/ABCD/play` flow, awaits `release("ABCD")` once on a fake sink that has `release`.
  - The same hook with a sink that has no `release` (only `set_code`/`start`) raises nothing.
  - A non-jackbox host releases nothing.

- [ ] **Step 3: Write the failing publisher tests** in `tests/test_relay_publisher.py`, with the existing `make(post)` and recording-post helpers:
  - `release("ABCD")` posts once to `https://relay.example/sessions/<id>/release` with `{"code": "ABCD"}` and the token.
  - A second `release("ABCD")` posts nothing.
  - A post that raises `OSError` doesn't propagate, and isn't retried. The log has `LOG_MARKER` and no `ABCD`.
  - After a publish has latched the publisher dead (a 404), `release` posts nothing.

- [ ] **Step 4: Update the default test.** In `tests/test_host_session.py`, the pinned default becomes `10.0`, and so do the parametrized expectations for unset, empty and non-numeric values.

- [ ] **Step 5: Run them and see them fail.** `.venv/bin/pytest -q tests/test_roomcode.py tests/test_addon.py tests/test_relay_publisher.py tests/test_host_session.py`.

- [ ] **Step 6: Implement** the interfaces above.
  - In the addon, add `_release(code)`. It spawns `sink.release(code)` through the existing `_spawn`, for each sink that has a `release` attribute, wrapped like `_set_code_safely`.
  - The `websocket_message` hook calls `self._release(self.joined_room_from_websocket_message(flow))` after its existing broadcast.
  - Build the release URL in `RelayPublisher.__init__` beside the publish URL.

- [ ] **Step 7: Update `README.md`.** Admins get each new room first. Guests are held until the first player joins (normally the admin tapping Play), or for at most `JBVIP_ADMIN_HEAD_START` seconds (default 10, at most 30). Keep the paragraph's other content and wrap it like its neighbours.

- [ ] **Step 8: Run the focused tests, then the full suites** (Every task). All pass.

- [ ] **Step 9: Commit.** `feat(extractor): release held guests when the first player joins; 10 s default hold`

### Task 3: Deploy and real use (controller, after the user's go-ahead)

- [ ] **Step 1:** Push `browser-player-page`.
- [ ] **Step 2: Relay on bne** (one batched key-SSH login, which sends one ntfy alert): run the update commands from `~/Documents/homelab/jbvip-relay.md`, then a live smoke test.
  - Release with the phone token → 401.
  - Release with the PC token and no window → 204.
  - Register `head_start` 99, then GET shows nothing broken.
  - Delete the session.
- [ ] **Step 3: karaokePC.** Over key SSH, `git -C ~/Projects/JackBoxAutoVIPServer pull --ff-only`. The redirect scripts are unchanged, so no root step is needed.
- [ ] **Step 4: Homelab docs.** Update `jbvip-relay.md` (the release route, clamp 30, the deployed commit) and `karaoke-pc.md` (the commit).
- [ ] **Step 5: Real use** (the user, on the TV).
  - The admin taps Play and is VIP. Guests' phones show the room about a second later.
  - The relay journal shows `POST /sessions/<id>/release`.
  - A pack whose guests always wait the full 10 s has a different join format: capture it.
