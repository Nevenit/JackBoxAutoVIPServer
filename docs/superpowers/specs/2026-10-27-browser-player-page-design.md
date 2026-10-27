# Browser Player Page — Design

**Date:** 2026-10-27
**Status:** Draft
**Supersedes:** the Android app in `2026-08-01-jackbox-auto-join-design.md` (§2's
conclusion, §5.4) and the "phone app does not exist" caveats in
`2026-08-05-multi-tenant-relay-deferred-issues.md`. The extractor and the relay's session model
stay. The launcher's session lifecycle changes (§4.3).

---

## 1. Intent

Everyone at a Jackbox night (the host plus friends, all in the same room) should be able to
jump into the host's games **on Android and iOS with nothing to install**. Nobody types a room
code, and nobody has to go back to anything between games, **including when the host switches
Party Packs or launches games from the Jackbox Megapicker**. The host (the user) runs Jackbox
on the PC through Steam.

The user rejected anything that needs installs: no app, no "use Firefox with a userscript".
**VIP priority is optional and not important to the user.** If it's off or unused, VIP goes to
whoever taps Play first, which is normal Jackbox behaviour.

Success: on the first launch of the night, the host scans the QR once. Friends scan an Invite QR
from any phone already on the page. From then on, every new game appears on every phone by
itself, from any pack or from the Megapicker, and people just tap Play. Late arrivals get
straight into the running game.

## 2. What's already true (verified 2026-10-27)

- **jackbox.tv deep-links.** `https://jackbox.tv/?code=ABCD&name=NAME` pre-fills both fields
  (`@connect`'s `populateFromURL()` reads `match.params.code/name/password`). It does **not**
  press Play; one tap per game remains, and that can't be avoided (§6). If the code matches the
  one jackbox.tv has stored, it keeps its reconnect data. If not, it clears it.
- **jackbox.tv runs inside an iframe.** It sends no X-Frame-Options or CSP, and has no
  frame-busting code. From a real https top-level origin its (partitioned) localStorage works,
  so reconnecting works. It asks for Screen Wake Lock, so the iframe needs
  `allow="screen-wake-lock"`. Its CookieYes banner appears once per phone, inside the frame.
- **The relay is live** at `https://jb.tinypp.in`: the `bne` VPS, systemd `jbvip-relay`, behind
  Caddy. The runbook is `~/Documents/homelab/jbvip-relay.md`. The PC's
  `~/.config/jbvip/relay.env` points at it.
- **Spike results.** A throwaway page served at `/j` by a temporary Caddy override (see the
  homelab doc) exercised the whole real pipeline: Steam launch, QR window, phone pairs, extractor
  publishes, relay, iframe. It worked on **Android Chrome and iPhone Safari**. What it showed:
  - Subscribing works with `fetch()` and a streamed body, sending the subscriber token as
    `Authorization: Bearer`. (`EventSource` can't send headers.) Codes arrived instantly
    through Caddy.
  - Swapping the iframe `src` when a new code arrives moves the player to the next game.
  - **Late joiners saw "waiting" while a game was running.** The relay won't hand a code older
    than `FRESH_SECONDS` (120 s) to a fresh subscriber (`relay/state.py:current_code`). That
    has to change (§4.2).
  - After a while in other apps, Android killed the iframe's renderer: a white "sad page" in the
    frame while our page survived. A manual refresh fixed it, and **the user is happy with
    that**. No auto-recovery is wanted.
- **The Megapicker launches packs through Steam.** "The Jackbox Megapicker" (Steam app
  2828500) is a Windows Electron app that runs under Proton. Its code (`resources/app.asar`)
  launches a game with `shell.openExternal("steam://run/<pack appid>// -launchTo <path>
  -jbg.config isBundle=false")`. Steam then starts the pack with the pack's own launch option,
  so `jbvip-run.sh` runs once per pack launch exactly as it does from the Steam library. The
  extra arguments pass through `"$@"`. The Megapicker has no jackbox.tv room of its own (no ecast
  references in its code). **This comes from reading its code; confirm it with one real launch
  from the Megapicker.**
- Two launcher bugs were found and fixed today, uncommitted at time of writing, each covered by
  new tests in `tests/test_launcher.py`:
  - bubblewrap 0.13 refuses to mount onto a symlink, and Arch's
    `/etc/ssl/certs/ca-certificates.crt` is one, so the bind now targets `readlink -f`.
  - **The launcher's own HTTPS never reached the relay after the game started.** The launcher
    joined the redirected net_cls cgroup itself, so its `DELETE` in `cleanup()` was redirected
    into mitmproxy and failed. This happened on both a failed launch and a normal exit (relay
    log). Now only the game joins: a subshell joins, then `exec`s bwrap. Anything the launcher
    sends at exit (§4.3) depends on this. Still to do: confirm with one real launch and quit.

## 3. Decisions

Marked **(user)** where the user decided, **(Claude)** where it's my call and open to change.

- No native app, extension or userscript. A web page served by the relay **(user)**.
- The page iframes jackbox.tv and swaps the room per game **(user's idea, Claude's mechanism)**.
- **One session per night**, following the host across pack launches, including launches from
  the Megapicker **(user)**. How a night ends is Claude's call (§4.3).
- Must work with the Jackbox Megapicker **(user)**.
- Any player can show an **Invite** QR and a Share link, so friends pass access to each other.
  The PC shows the QR at the start of the night for the first person **(user)**.
- VIP priority is optional, with an admin phone paired once and remembered. It can be switched
  off or never set up **(user)**. It's built as a separate second part with its own plan, after
  the core works **(Claude)**.
- In-game layout: a thin bar along the top with the room code, status, Invite and a menu
  **(user, tentative "maybe A")**.
- A name is entered once per phone **(Claude, not objected to)**.
- No auto-reload when coming back to the page. A "Reload game" menu item instead, because a page
  saved to the home screen has no browser refresh button **(user: refresh is fine; Claude: the
  menu item)**.
- An "Open in jackbox.tv" fallback link in the menu **(Claude)**.
- Dropped "hand admin to someone else for a night": turning priority off covers it **(Claude)**.
- Setting up the redirect automatically at boot is **out of this plan**. The no-interception
  fallback in §4.3 covers the failure mode **(Claude)**. See §6 for the notes.

## 4. Part 1 — the player page, night-long sessions, Megapicker support

### 4.1 The page

Served by the relay at `GET /j`, replacing today's "open it in the jbvip app" page. The QR and
Invite link stay `https://<relay>/j#<session_id>.<subscriber_token>`. The token travels in the
fragment, so it never reaches a server, a log or a Referer. That property must hold.

Behaviour:

1. **No fragment, or a malformed one:** say "Open this from the QR code on the host's screen."
   Handle `hashchange` too: a re-scan that only changes the fragment doesn't reload the page.
2. **First visit on this phone:** ask for a name. Store it on the relay origin (localStorage),
   uppercase, at most 12 characters (jackbox.tv's own limit). It can be changed from the menu. A
   change applies from the next game, so it never reloads the current one.
3. **Subscribe** to `GET /sessions/{id}/subscribe` using `fetch` with the Bearer token. Parse
   SSE events and reconnect with backoff (about 1 s doubling to 15 s). A 401 means "This link
   doesn't work"; a 404 means "This game night has ended."
   **Close the stream while the page is hidden** (phone locked, app switched) and reopen it
   when it's visible again, so sleeping phones don't leave dead streams on the relay.
4. **No room right now** (before the first game, or between packs): a "Waiting for the host to
   pick a game…" screen. Nothing loads in the frame.
5. **On a code:** if it differs from the code in the frame, set the iframe's
   `src = https://jackbox.tv/?code=CODE&name=NAME`. If it's the same code (an SSE reconnect
   re-sends it), do nothing. **Never reload a running game for a code it already has.**
6. **On "room cleared"** (the host quit a pack): go back to the waiting screen.
7. **On 404 or 401 while a game is in the frame:** keep the frame and show the state only in the
   bar. Never pull a player out of a running game (§7).
8. **Top bar:** room code, a short status (waiting / in game / reconnecting / ended), **Invite**
   and **⋯**.
9. **Invite:** a QR of the page's own link (`location.href`, fragment included), plus "Share
   link…" (`navigator.share`, falling back to copy-to-clipboard).
10. **⋯ menu:** Change name; Reload game (re-sets the same `src`, and jackbox.tv reconnects
    because the code is unchanged); Open this room in jackbox.tv (a new tab).
11. **iframe:** `allow="screen-wake-lock; autoplay; fullscreen; clipboard-write"`. The page also
    requests its own screen wake lock after the first tap and again on `visibilitychange`.
12. **Layout:** a fixed, full-viewport column: the bar, then the frame filling the rest. Respect
    `env(safe-area-inset-top)`. Inputs at 16px or larger so iOS doesn't zoom.

`GET /` shows a one-line page: "Scan the QR code on the host's screen to join." It currently
returns 404 "Not Found", which confused the user.

No third-party scripts in a page that holds a token. Vendor any QR library into the repo
(e.g. `qrcode-generator`, MIT, about 20 KB) and serve it from the relay. No build step. Static
files served by Starlette are fine, and the systemd unit's read-only filesystem
(`ProtectSystem=strict`) allows reading them.

### 4.2 Relay changes

- **A "room cleared" state.** A publisher-authenticated way to say "no room right now" (the host
  quit a pack). The current code becomes none. Subscribers get an event the page turns into the
  waiting screen, and fresh subscribers get no code. It's a new route or a variant of publish,
  the builder's choice. It must survive the existing de-duplication: publishing the same code
  after a clear must reach phones again.
- **Late joiners and reloads get the current code, whatever its age.** Drop the
  `FRESH_SECONDS` gate in `current_code()` and the "clients should enforce 120 s against `ts`"
  guidance. Why this is safe: the rule dates from the single-tenant relay, where a code could
  outlive the night ("a stranger's room by the next day"). Now a code is only current while its
  pack is running, because the launcher clears it on exit (§4.3). It lives inside a session that
  ends with the night. The leftover risk is a closed room's code between two games of the same
  pack, or after a launcher crash that skipped the clear. jackbox.tv then says the room doesn't
  exist, which is acceptable.
- **How a night ends:** a session ends once it has had **no room for a while**. Suggest 2 hours
  since the last clear, or since creation if nothing was ever published. The 12-hour cap stays.
  Today every SSE keepalive "touches" the session so a long game doesn't expire. That must not
  keep a roomless session alive all night, so base this rule on time since the clear, not on
  activity.
- **A full session admits new joiners.** At `MAX_SUBSCRIBERS_PER_SESSION` (16) the relay
  currently refuses with 429. Phones that sleep or switch networks leave dead streams that count
  until a keepalive write fails. Evict the oldest stream instead of refusing the newest
  **(Claude)**. Together with §4.1's close-when-hidden, a mid-night newcomer never gets
  locked out.
- `/j` serves the page, and `/` serves the landing line.
- Update `relay/README.md` (Freshness, Endpoints, session lifetime) and the main `README.md` (the
  "phone app does not exist" paragraph, pairing, the night-long session, Megapicker support).

### 4.3 Host side: night-long sessions, Megapicker, launch fallback

- **Reuse tonight's session.** On each launch, if the relay is configured, `jbvip-run.sh` looks
  for tonight's session. Store its credentials on the PC in a mode-0600 file under
  `${XDG_STATE_HOME:-~/.local/state}/jbvip/`, not in the per-run `$STATE`, which is deleted. If
  the relay says it's still alive, reuse it. Otherwise create a new one and save it.
- **QR window only when needed:** show it only when the session has no subscribers (the first
  launch of the night, or everyone has left). A pack switch with phones connected shows nothing
  and starts the game straight away.
- **On exit, clear the room; don't delete the session.** `cleanup()` sends "room cleared" so
  phones show "waiting" until the next pack. It must never block or fail the exit (`|| true`, a
  short timeout, the same as the delete today). This relies on the cgroup fix in §2: the
  launcher's own HTTPS must not be redirected.
- **The extractor publishes per launch as today.** De-duplication lives in the publisher process,
  which is new for every launch, so the first code of a new pack always goes out.
- **The Megapicker is never wrapped.** `extractor.steam.jackbox_app_ids()` matches every installed
  app whose name contains "jackbox", so the next `install.py` run would put the jbvip launch
  option on "The Jackbox Megapicker" (2828500). Its mitmdump would then hold port 8081 for as
  long as the Megapicker is open, and every pack launched from it would fail to intercept. It's
  also a Proton app, which this launcher was never built to wrap. Exclude it (by app id or name),
  with a test. `uninstall.py` must still remove a wrapper from it if one was ever set (it matches
  by marker, so check that it does).
- **Launching from the Megapicker** then needs nothing more: each pack it starts goes through
  Steam and therefore through `jbvip-run.sh` (§2). Combined with session reuse, phones follow
  from one Megapicker pick to the next.
- **A launch that can't intercept still starts the game.** Today, if the redirect isn't set up,
  the port is busy or mitmdump fails, `jbvip-run.sh` exits and the game silently never starts.
  The reason only reaches Steam's journal. Instead: show a desktop notification (`notify-send` if
  present) saying what's wrong and how to fix it, then run the game **without** interception (no
  room code; people type it from the TV). Show no QR window in that case. This follows the
  launcher's existing rule that a dead VPS must never cost a game night **(Claude)**.

### 4.4 Cleanup

- Delete `injection/inject.js`, its fixture and its node test. There's nothing left to script
  inside jackbox.tv. The puppeteer-core setup there is a reasonable pattern for testing the new
  page against a locally running relay (its fixture test already drives system Chromium).
- After deploying, remove the spike's Caddy override and `/srv/jbvip-spike` from the caddy
  container, and update `~/Documents/homelab/jbvip-relay.md` (its "Throwaway test page" section
  says how). Redeploy the relay as that runbook describes.

## 5. Part 2 — optional VIP priority (its own plan, after Part 1 works)

What the user wants: their phone is paired once as **admin** and remembered. On later nights it
follows the host with nothing to scan. While it's connected with priority switched on, it gets
each new room **before** the guests, so the host taps Play first and lands VIP. With priority
off, or no admin phone at all, behaviour is exactly Part 1.

Intended shape (protocol details are the builder's; the constraints aren't):

- **Pairing, once:** a host-side command (e.g. `python3 -m host.pair_admin`) creates a long-lived
  host secret in `~/.config/jbvip/` (mode 600) if there isn't one, and shows an **admin QR**:
  `https://<relay>/a#<host_id>.<admin_token>`. The admin page stores both on the phone. Running it
  with a reset option rotates the secret and unpairs old phones.
- **Each night:** when the launcher creates the night's session, it also proves the host
  identity, e.g. by sending the host secret, or a value derived from it, plus a hash of the
  admin token. The relay maps `host_id → tonight's session` **in memory only**. The admin page
  holds a follow stream on the host (Bearer admin token) and receives tonight's session (so it
  can subscribe and show Invite) and its codes.
- **Priority:** with an admin follower connected and priority on, the relay delivers each new
  code to the admin immediately and to guests after `GUEST_DELAY_SECONDS` (suggest 10). Guests
  who subscribe during that window wait for it to end. The toggle lives on the admin phone and
  is sent with the follow request. No admin connected means no delay.
- **The PC's QR window** counts a connected admin follower as a subscriber, so on a night where
  the admin phone is already following, no QR appears at all **(Claude's guess, not objected
  to)**.
- On iPhone, Safari deletes a site's storage after about 7 days without a visit. A page saved
  to the home screen is exempt. The admin page should suggest Add to Home Screen on iOS.

Constraints: the relay keeps everything in memory, single worker, no database. After a relay
restart, the admin re-links on the PC's next session with no re-pairing. The PC holds the only
long-lived secret. Admin credentials travel only in a URL fragment and the Authorization
header. Admin followers count towards the subscriber cap and its eviction rule.

## 6. Non-goals

- Pressing Play automatically. The frame is cross-origin, so our page can't touch it, and
  jackbox.tv's URL parameters only pre-fill. One tap per game is accepted.
- Auto-reloading the frame when returning to the page (the user is fine with refresh).
- Detecting that a room closed between two games of the same pack.
- Remote players, accounts, moderation, more than one host per session.
- Wrapping the Megapicker itself, or extracting anything from it.
- Per-IP rate limiting on the relay (a known gap, documented in the homelab runbook).
- Blobcast-era packs (Party Packs 1 and 5 are uninstalled; everything installed is ecast).
- **Redirect at boot (later, if the per-boot `sudo` annoys).** Today `sudo ./jbvip-redirect-up.sh`
  has to run after every reboot. On this PC the net_cls v1 hierarchy at `/sys/fs/cgroup/net_cls`
  is mounted by the **Mullvad VPN daemon** (for its split tunnelling), not by this project.
  Without Mullvad running, `jbvip-redirect-up.sh` fails with "net_cls cgroup v1 not mounted". A
  boot-time setup would be a root systemd unit that mounts net_cls itself when it's missing,
  creates the cgroup and adds the iptables rules. Its install needs sudo, and any hard-coded repo
  path must come from `install.py`, because the repo moved on 2026-10-27.

## 7. Anti-goals: it fails even if it technically works when…

- a player has to install anything or make an account;
- a running game gets reloaded or swapped out from under a player, for example on an SSE
  reconnect, a repeated code or a relay error;
- the QR window pops up on a pack switch while phones are already connected;
- a token appears in a server log, a Referer or a third-party request;
- a relay or VPS problem stops the game launching on the PC.

## 8. Verification

- Relay: pytest, extending `tests/test_relay_app.py` and `tests/test_relay_state.py`. In
  particular: a fresh subscriber gets a code published long ago; "room cleared" reaches
  subscribers, and a fresh subscriber after it gets no code; the same code re-published after a
  clear is delivered; a roomless session ends after the window while one with a room doesn't; a
  full session evicts the oldest stream; the page and landing routes serve HTML with no-store or
  short caching.
- Launcher: tests in the style of `tests/test_launcher.py`, which run real blocks of the script.
  Cover session reuse vs. creation, no QR window with subscribers present, clear on exit, and the
  no-interception fallback. `install.py` / `extractor.steam`: the Megapicker is excluded.
- Page: an automated browser test against a locally running relay at phone size, covering
  waiting → code → frame `src` → new code swaps → same code doesn't reload → clear goes back to
  waiting → Invite QR decodes to the page's link (`zbarimg` is installed) → a 404 with a game
  in the frame keeps the frame. The spike was checked exactly this way by hand.
- Real use, after deploying: one Android and one iPhone. Launch from Steam, scan, play, switch
  games, **switch packs**, **launch from the Megapicker**, join late through Invite, refresh
  mid-game, quit (phones go to waiting).

## 9. Left to the builder

File layout and names, storage keys, styling, wording, the vendored QR library, the exact SSE
reconnect policy, the "room cleared" API shape, the state-file format, how the page is tested,
and the Part 2 protocol within its constraints.
