# Jackbox Auto-Join VIP Client — Design Spec

**Date:** 2026-08-01
**Status:** Approved
**Depends on:** `2026-06-03-jackbox-roomcode-extractor-design.md` (the extractor,
now feature-complete: all three room-code protocols verified live).

---

## 1. Goal & context

The extractor reliably produces the host's lobby room code. This project consumes
it: an Android client that joins the room automatically the instant it opens, so
the host reliably lands the **VIP seat** (Jackbox assigns VIP to the first player
to join) instead of racing whichever friend types fastest.

The auto-joiner **is the host's own controller** — it joins under the host's name
and the host then plays the game from that same device. It is not a headless bot
occupying a separate player slot.

Scope for this iteration is **the host only**. The eventual goal is that everyone
including remote friends auto-joins, with the host flagged as admin so they are
guaranteed to land first. Nothing here should foreclose that, but none of it is
built now.

## 2. The decisive constraint (verified 2026-08-01)

Read directly from jackbox.tv's shipped bundles — the loader at `jackbox.tv/`,
whose inlined `window.tv.manifest` resolves apps to
`https://bundles.jackbox.tv/<base>/<file>`, and the join app `@connect`:

- **There is no room-code deep link.** The loader's route table has no `/:code`
  route (only `/`, `/past-games`, `/gallery/*`, `/render/*`, `/moderator`,
  `/manifest`, `/debug/*`), and `executeMatch` redirects unmatched paths to `/`.
  Both bundles read exactly four query params — `server`, `s`, `ns`,
  `namespace` — none of which is a code. The origin serves the same SPA shell for
  every path, so HTTP status codes reveal nothing; the router must be read.
- **The join form is trivially automatable once script runs in the page:** inputs
  `id="roomcode"` and `id="username"`, button `id="button-join"`.
- Those inputs are **Vue `modelValue` / `onUpdate:modelValue` bound**. Assigning
  `input.value` does **not** update Vue's model; a real `input` event must be
  dispatched.
- **No bot protection** on join — no recaptcha/hcaptcha/turnstile. (Grep hits for
  "captcha" are the game *Let Me Finish*, internal tag `ppad/captcha`.)

**Consequence:** a LAN-served web page cannot auto-join, because it can neither
navigate to a URL carrying the code nor script jackbox.tv across origins. Joining
requires injecting into the jackbox.tv page from a context exempt from the
same-origin policy — a native app's WebView, or a browser extension. The host
plays on a phone, so: **a native Android app holding jackbox.tv in a WebView.**

Toolchain note: Flutter and React Native were considered and rejected. Neither
avoids the Android SDK/Gradle/JDK setup (`flutter build apk` requires it), both
add a plugin layer over the one component this product is entirely about, and
cross-platform buys nothing since iOS is out of reach without a Mac and a paid
Apple account.

## 3. Non-goals

- Friends / multiple players; admin-priority join ordering. One publisher, one
  subscriber. The token model leaves room; nothing more.
- iOS. Play Store distribution.
- LAN discovery (UDP broadcast, mDNS) — superseded by the relay.
- QR pairing in v1 (see §5.5).
- Any reimplementation of the Jackbox player protocol. The controller UI is
  game-specific and shipped by Jackbox; we drive their page, we do not replace it.
- A database on the relay. Room-code state is in-memory; the only thing written
  to disk is the subscriber-token list, in a config file (§5.1).

## 4. Architecture

```
  Jackbox game (host PC)
        │ TLS, transparently redirected (existing)
        ▼
  mitmdump + extractor/addon.py         ← unchanged extraction
        │ fans out to sinks
        ├──────────────► RoomCodeServer      (existing, LAN TCP :38469)
        └──────────────► RelayPublisher      (new)
                              │ outbound HTTPS POST /publish
                              ▼
                         Relay on VPS        (new)
                              │ SSE GET /subscribe
                              ▼
                         Android app         (new)
                              │ WebView JS injection
                              ▼
                         jackbox.tv → lobby, as VIP
```

Outbound HTTPS only from the host PC: no port forwarding, no inbound exposure,
and the phone need not share a network with the PC.

## 5. Components

### 5.1 `relay/` — the VPS service (new)

Python, matching the repo's existing stack, deployed to the user's VPS behind
whatever already terminates TLS.

Two long-lived secrets, both generated at deploy time and held in the relay's
config: the **publisher token** (lives on the host PC; also the admin credential)
and whatever **subscriber tokens** have been minted through pairing.

- `POST /publish` — authenticated by the publisher token. Body carries the code
  and its timestamp. Stores `{code, received_at}` in memory for the single
  channel.
- `GET /subscribe` — authenticated by a subscriber token. Server-sent events.
  Emits the current code immediately on connect (if fresh), then on every change.
- `POST /pairing-codes` — authenticated by the publisher token. Mints a
  six-character pairing code with a five-minute expiry and returns it. This is
  what the host-side CLI calls.
- `POST /pair` — unauthenticated but single-use: redeems a valid, unexpired
  pairing code for a durable subscriber token, and burns the pairing code.
- State is in-memory and deliberately not persisted: if the relay restarts, the
  next room created republishes within seconds. Subscriber tokens are the one
  exception and are persisted to a config file, so a restart does not force
  re-pairing.
- Publishes and pairing attempts are both rate-limited. Room codes are never
  written to logs.

TLS is terminated by the reverse proxy in front of the relay, so the service
itself cannot verify it is being served securely. The check therefore lives where
it can actually be enforced: **the publisher and the app both refuse a relay URL
that is not `https://`.** Deploying the relay without TLS in front of it is a
documented deployment requirement.

### 5.2 `extractor/relay_publisher.py` — the publisher (new)

Implements the same shape `RoomCodeServer` already exposes (`start()`,
`set_code(code)`) so it drops into the sink list unchanged.

- POSTs the code plus a timestamp to the relay.
- Retries with exponential backoff.
- **Never raises into mitmproxy.** A dead relay degrades the system to the LAN
  server; it does not interrupt a running game.

### 5.3 `extractor/addon.py` — sink fan-out (small refactor)

`RoomCodeExtractor.__init__` currently takes one `RoomCodeServer` and
`_broadcast` calls `self.server.set_code(code)`. Generalise to a sequence of
sinks so the TCP server and the relay publisher sit side by side.

- `running()` starts every sink; a sink failing to start is logged. The existing
  hard-fail-on-bind behaviour for `RoomCodeServer` is preserved (a bind failure
  still shuts mitmproxy down), because a silently unbound local server is the
  bug that behaviour was added to prevent.
- One sink raising must not prevent the others from receiving the code.
- No observable behaviour change; the existing 56 tests must keep passing.

### 5.4 `android/` — the client (new)

Native Kotlin. One activity, one WebView, one SSE client, one preferences screen.
No Compose, no architecture components, no DI — the app is small enough that
structure would cost more than it saves.

- WebView pinned to jackbox.tv, JavaScript and DOM storage enabled (the site uses
  `localStorage`), configured to survive rotation so a live game session is not
  destroyed.
- Preferences: display name, relay URL, subscriber token.
- On a code arriving: check freshness (§5.6), check it differs from the last code
  acted on, check the join form is present, then inject.
- Injection sets `#roomcode` and `#username` using the native `HTMLInputElement`
  value setter **and** dispatches a bubbling `input` event, then clicks
  `#button-join`. (The native-setter step is belt-and-braces — Vue reads
  `event.target.value`, so the event is the load-bearing part.)
- Logic worth testing (SSE parsing, freshness, de-duplication, retry policy)
  lives in plain Kotlin classes; only the WebView/Activity glue is hand-verified.

### 5.5 Pairing

A CLI on the host PC calls `POST /pairing-codes` with the publisher token and
prints the resulting **six-character pairing code**. The app's first-run screen
asks for two things: the relay host, and that pairing code. It redeems the code
at `POST /pair` for a durable subscriber token and stores it. Both are entered
once; the pairing code is then dead.

The relay host has to be typed because a pairing code alone cannot convey where
to redeem it — roughly one short hostname plus six characters, once, in about
fifteen seconds.

Chosen over QR for v1 because scanning costs the app a camera permission and a
barcode library for a one-time action. A QR would carry host and code together
and is a drop-in upgrade later, since the underlying exchange is identical.

### 5.6 Code freshness

Codes travel with a timestamp and carry a **two-minute** auto-join window.

This is a safety property, not an optimisation: Jackbox recycles four-letter room
codes, so a code left over from the previous session may belong to a stranger's
room by the next day. Outside the window the app displays the code but will not
act on it. Both the relay (when deciding what to send on connect) and the app
(when deciding whether to inject) enforce it.

## 6. Data flow

1. Host launches a Jackbox game; the game creates a room.
2. The addon extracts the code — unchanged, all three protocols.
3. `_broadcast` fans out to `RoomCodeServer` and `RelayPublisher`.
4. The publisher POSTs code + timestamp to the relay over HTTPS.
5. The relay pushes it to the subscribed app over SSE.
6. The app validates freshness, novelty and page state, then injects.
7. The host is in the lobby as VIP, before the code could have been read aloud.

## 7. Error handling

Every failure degrades to "the code is on your screen, paste it" — never to a
dead app that does nothing.

| Failure | Behaviour |
|---|---|
| Relay unreachable from PC | Publisher retries with backoff, logs, swallows. LAN server unaffected. |
| SSE stream drops | App reconnects with backoff + jitter. Relay re-sends the current code on connect, so a room created during the outage is not missed. |
| Injection fails (selectors changed, page not ready) | Show the code large with a copy button and state why. |
| Already in a game | Join form absent from the DOM — that absence is the signal. Display the code; never clobber a live session. |
| Duplicate code | Ignored. |
| Code older than the window | Displayed, never auto-joined. |
| Room not ready yet | Two or three quick retries — the app can genuinely outrun the lobby's creation. |
| Name already taken | No retry. Fall back to manual: silently joining as "Michael2" is worse than saying so. |
| Token invalid or revoked | Prompt to re-pair. |

Join failure is detected by observing that the join form is still present a few
seconds after clicking.

## 8. Testing strategy

Ordinary pytest work, in the style already in the repo, for the relay, the
publisher and the refactor:

- Relay: publish and subscribe token rejection; current code delivered on
  connect; pushed on change; TTL suppresses stale codes; pairing codes are
  single-use and expire; publishes are rate-limited.
- Publisher: retries with backoff; **never raises** when the relay is down;
  satisfies the sink shape.
- Addon: a code reaches every sink; one failing sink does not block another; all
  56 existing tests still pass.

The injection script is the honest exception — **its real test is a live lobby.**
What is automatable is the narrow technical claim underneath it: that the native
setter plus a dispatched `input` event actually moves a Vue `v-model`. That gets
a fixture page with real Vue. Everything above it is hand-verified against a real
room.

Android: JUnit over the plain-Kotlin logic classes; the WebView glue is
hand-verified.

## 9. Build order (risk first)

1. **Prove the injection by hand**, in a desktop browser console against a real
   lobby. If Vue resists, or the WebView mangles touch input in the drawing
   games, the design changes shape — learn that in an afternoon, not after the
   relay and app are built.
2. Relay + tests.
3. Publisher + addon refactor + tests.
4. Android app.

## 10. Open risks

- **Old packs may join differently.** The form was verified against current
  jackbox.tv. Party Pack 1 and 5 route to the Blobcast backend, and if their join
  screen differs the injection needs a second case. Cheap to check against a live
  room during step 1.
- **Injection is inherently brittle.** We drive a page we do not control; a
  Jackbox redesign will eventually break the selectors. Mitigated, not solved, by
  the copy-the-code fallback and by selecting on stable ids rather than
  structure.
- **WebView fidelity.** Drawing games need canvas touch input and some games need
  audio; both must be confirmed in a real WebView, not assumed.

## 11. Future work (explicitly out of scope)

- Remote friends auto-joining, with admin-priority ordering. The relay is the
  natural home: it decides who learns the code and when, so the admin token gets
  it instantly and friend tokens after a delay. That is a scheduling rule on
  existing machinery, not a rewrite.
- QR pairing (§5.5).
- iOS.
