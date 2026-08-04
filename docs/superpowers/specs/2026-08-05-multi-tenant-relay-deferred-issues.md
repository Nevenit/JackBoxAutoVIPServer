# Multi-Tenant Relay — Known Deferred Issues

**Date:** 2026-08-05
**Branch:** `multi-tenant-relay`
**Companion to:** `2026-08-03-multi-tenant-relay-design.md`
**Supersedes for the relay:** the relay portions of
`2026-08-02-auto-join-known-deferred-issues.md`. The extraction and injection
sections of that document still stand.

Every task on this branch was reviewed individually, each review's Critical and
Important findings were fixed and re-verified, and a whole-branch review then
triaged everything that was left. What follows is what was deliberately **not**
fixed, with the reason — so nobody rediscovers these as if they were unknown.

Final state: 252 tests passing, 19 commits from `908a653`.

## One verification step still outstanding

**A real end-to-end run with a deployed relay and a live Jackbox game.** Needs a
human, a VPS and a real room; nothing in CI can stand in for it. Everything up to
that point is covered:

- The relay was smoke-tested against a real `uvicorn` process, not only the test
  harness.
- The QR payload was verified by *decoding* it — `render_qr_png` output through
  `zbarimg --quiet --raw` returns exactly `qr_payload(credentials)`, fragment
  included, with the publisher token absent.
- The launcher's dead-relay path was verified by running the real session block
  under `set -euo pipefail` with a `python3` stub that exits 1: it logs, continues,
  and the game would launch LAN-only.

The steps for the live run are in the plan's Verification section.

## Pairing does not work end to end yet, by construction

The Android app does not exist. Nothing today can make `subscribers >= 1`: a plain
camera app scanning the QR loads `/j`, which serves a one-line page telling the
reader to open it in the app, and merely loading that page does not subscribe.

So **every launch with `JBVIP_RELAY_URL` set will sit on the pairing window for the
full `PAIRING_TIMEOUT_SECONDS` (120s) unless Skip is pressed.** Publishing itself
works fine; it is only pairing that has no consumer. This is documented in
`README.md` where someone configuring the relay will read it first.

`PAIRING_TIMEOUT_SECONDS` was deliberately **left at the spec's 120 seconds**
rather than shortened to paper over this. Shortening it is a reasonable call once
the app exists or if the wait proves annoying — but it should be a deliberate
decision, not a silent one.

## One deliberate addition beyond the spec

`MAX_SESSION_LIFETIME_SECONDS = 12 * 3600` in `relay/state.py` is **not** in the
design spec's constants table. It was added by the whole-branch review's fix wave
to close a real denial of service:

The keepalive branch touches a session every 20 seconds while a subscriber holds
its stream, which is correct and necessary — a whole game night is normally a
single publish, so without it a session with a phone listening all evening would be
evicted mid-game. But nothing bounded it from above, and `create_session` refuses
at capacity rather than evicting (spec §8, "never evict a live session to admit a
stranger"). Composed, 500 clients each holding one idle stream would have locked
out every future host permanently, with no timeout that ever fired.

The ceiling is measured from `created_at` and `touch()` cannot move it. 12 hours is
comfortably longer than any game night and well above the 4-hour idle window. It
is consistent with spec §3's promise that the relay caps total resource use.

## Deferred findings, with reasons

All were triaged by the whole-branch review as fine to defer. None blocks merge.

**Tests**

- `tests/test_relay_state.py`'s bad-room-code list no longer includes a case that
  reaches the `isascii()` branch of `_looks_like_code` — the old list had a
  fullwidth string; every current case short-circuits on length or type first. The
  branch is defensive on a check the relay duplicates on purpose, and a miss yields
  a 400 either way. One-line fix if anyone touches that file.
- `test_the_real_publisher_cannot_reach_another_session` passes `attempts=1`, so it
  cannot distinguish "latched" from "ran out of its single attempt". It does prove
  cross-session rejection over a real socket, and the latch is proven at unit level
  and end-to-end via the 404 case. **This is the one worth tightening first** if
  anything is tightened, because it guards the headline property.
- The session-id collision retry in `create_session` has no dedicated test —
  triggering it needs `secrets.token_urlsafe` mocked, for a 72-bit collision.
- A rate-limiter test dropped a mid-window assertion that a sibling test covers.
- The seam-test imports in `tests/test_relay_app.py` sit mid-file rather than at the
  top. Purely stylistic.

**Code**

- `relay/app.py` declares a `logger` it never uses, so the "never logs a code or
  token" test passes vacuously *for that module*. The declaration still guards a
  future regression, and the module genuinely logging nothing is the property you
  want.
- `host/session.py`'s `credentials_from_env` does not re-run the session id and
  token through `_checked`. They round-trip a mode-0600 file the host itself wrote
  from already-checked values, so it is closed in practice — but adding `_checked`
  would make it closed structurally, and it is one line.
- `delete_session`'s `except Exception` spans the URL f-string as well as the
  network call, so a local coding error would surface as a returned `False`. It runs
  on an exit path where never raising is the correct behaviour.
- `host/qr_window.py` imports `tkinter` at module scope, so a missing `tkinter` is
  an `ImportError` before `main()` runs. The failure is safe — no session is created
  and nothing leaks — and it is documented in `README.md`. The cheap improvement, if
  the generic "no relay session" message ever proves confusing, is to have
  `jbvip-run.sh` print the child's stderr rather than its own message.
- `render_qr_png`'s docstring undersells its catch surface (it also returns `False`
  on a timeout or non-zero exit, not only when `qrencode` is absent), and one of its
  parameters is untyped.
- A `qrencode` that hangs would leave the already-created window blank for up to its
  10-second timeout.

**Launcher**

- `. "$SESSION_ENV"` sits in an `if` *body* rather than a condition, so under
  `set -e` a genuine I/O error mid-read would abort the launcher — the one remaining
  path where a relay-side problem could cost a game night. Essentially impossible in
  practice: the file is two `shlex.quote`-escaped literal assignments on a tmpfs the
  script created moments earlier. Note that if it ever did fire, `SESSION_ID` stays
  unset, so the EXIT trap skips the delete and the session leaks a slot until it
  expires.
- `host.session --delete` runs synchronously in `cleanup()` with a 5-second request
  timeout, so an unreachable VPS adds up to 5 seconds to game exit. The session
  self-expires regardless.
- `test_the_session_credentials_reach_mitmdump_but_not_the_game` asserts the absence
  of an `export` string rather than inspecting a live `bwrap` child's environment.
  A reasonable proxy: the script needs root-configured cgroups to reach that line.

**Not reviewed for interactions with the session model:** `install.py`,
`uninstall.py`, `jbvip-redirect-up.sh`/`-down.sh`, `extractor/server.py`,
`extractor/roomcode.py`, `injection/`. None was modified by this branch.

## Properties established, so nobody re-derives them

- **Session isolation is structural, not merely tested.** The only queue container
  in `relay/app.py` is `dict[str, set[asyncio.Queue]]`; there is no flat subscriber
  set anywhere, and every access is session-keyed. No code path iterates all queues,
  so cross-session delivery is unreachable without adding one. The isolation test
  guards the property; the data structure creates it.
- **The four cross-component contracts agree**, checked by hand: the three
  environment variable names, the six routes, the JSON keys in both directions, and
  the status-code semantics. `JBVIP_SESSION_TOKEN` carries the *publisher* token on
  every leg.
- **Credential paths.** The subscriber token exists only in the QR image and the Tk
  process's memory; the publisher token only in a mode-0600 file, `mitmdump`'s
  environment, and the relay. Neither reaches a command line — the QR payload goes
  to `qrencode` on stdin — and the game process (`bwrap -- "$@"`) sees neither,
  because the launcher never exports them.
