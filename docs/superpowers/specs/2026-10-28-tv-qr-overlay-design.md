# TV QR Overlay — Design

**Date:** 2026-10-28
**Status:** Draft
**Builds on:** `2026-10-27-browser-player-page-design.md`, on the same branch
(`browser-player-page`, not yet merged). **Supersedes there:**
- §3: "The PC shows the QR at the start of the night for the first person".
- §4.3: the "QR window only when needed" bullet.
- §7: the anti-goal "the QR window pops up on a pack switch while phones are already connected".
- The SDD ledger's Ruling 8: the 15 s QR timeout on a reused night.

---

## 1. Intent

At the start of a night everyone is looking at the TV. Today the QR window closes as soon as
the first phone connects. That person then has to pass their phone around with the Invite QR,
which the user called "a bit dumb". Instead, the QR stays on the TV, on top of the game, until
someone decides everyone is in and hides it from their phone.

Success: the whole room scans the TV and one person taps Hide. Nobody sees the QR again that
night, unless a game is launched while no phone is connected.

## 2. Where it runs

- **Target:** a mini PC running CachyOS, connected to one display, the TV **(user)**. It runs
  KDE Plasma on Wayland, like the dev PC. That's Claude's assumption: the user said "CachyOS as
  well" and nothing about the desktop.
- **Built and tested on the dev PC:** CachyOS with KDE Plasma (KWin 6.8, Wayland) and three
  monitors.
- **Clients:** Jackbox packs and the Megapicker are X11 clients under XWayland. That covers the
  native builds and Proton.

## 3. What's already true (verified 2026-10-28)

A spike ran in a nested, headless KWin 6.8 with XWayland
(`kwin_wayland --virtual --xwayland`). It used a fake fullscreen X11 "game" that re-raised and
re-focused itself every second.
- **Unmanaged window works.** An unmanaged (override-redirect) Tk window stays above the active
  fullscreen game, at the position it asks for. The game keeps keyboard focus and stays
  fullscreen.
- **Today's window doesn't.** The current window is managed, with `-topmost`. KWin places it
  itself (centred, with a title bar) instead of where it asks, and takes keyboard focus away from
  the game. That's why today's window is shown before the game starts.
- **One big X screen.** XWayland sees all monitors as one X screen: 6720×3600 on the dev PC.
  `xrandr --listactivemonitors` marks the primary monitor (`*DP-2 3840x2160+0+900`). The
  top-right of the whole X screen is on a different monitor from the primary.
- **Every event is read as a room code.** The page (`relay/static/player.html`) treats every SSE
  `data:` event as a room code: `onCode(JSON.parse(...).code)`. An event without `code` would
  empty the frame.
- **Polling is free.** `GET /sessions/{id}` doesn't extend a session, so polling it can't keep a
  night alive.

**Not verified:** real Jackbox packs and the Megapicker under this window. The real-use check in
§8 covers them.

## 4. Decisions

Marked **(user)** where the user decided, **(Claude)** where it's Claude's call and open to
change.

**What the QR does**
- The QR stays on screen over the game until someone hides it **(user)**.
- It sits in the top-right corner, so the game's own menu stays usable **(user)**. It's about a
  third of the screen's height, with a margin from the edges **(Claude; tune it on the TV)**.
- A caption tells the room how to hide it, for example "Scan to join · hide it from ⋯ on your
  phone" **(Claude; wording open)**.

**Hiding it**
- Any player can hide it from the page's menu with **Hide QR on TV** **(user)**.
- Once hidden, it stays hidden for the night. Latecomers use someone's Invite. A phone can't show
  it again **(user)**.
- The phone's Hide button only shows while the QR isn't hidden **(Claude)**.
- A small × on the card hides it from the PC, for the night, like the phone button. If the relay
  can't be reached, × just closes it for this launch **(Claude, not objected to)**.

**When it comes back**
- **Safety net:** a launch while no phone is connected shows the QR again, even if it was
  hidden. It then stays until someone hides it again **(user: "a new qr code at game relaunch is
  fine"; Claude: the rule)**.
- No "new night" button **(Claude, not objected to)**.
- The QR belongs to the launch. It closes when the pack quits and comes back with the next launch
  unless it's hidden. So it isn't shown over the Megapicker between packs **(Claude)**.

**The launch**
- The game starts straight away and nothing waits for a scan. The pairing wait goes away (120 s
  for a new night, 15 s for a reused one), and so does Skip **(Claude, not objected to)**.

**How it's built**
- It's an unmanaged Tk window, the spike's winner, so there are no new dependencies
  **(Claude)**.
- It goes in the top-right of the primary monitor, as `xrandr --listactivemonitors` reports it.
  If xrandr is missing or reports nothing usable, it uses the whole X screen instead. On the mini
  PC's single display, the two are the same **(Claude)**.
- The work stays on the same branch, `browser-player-page`. The real-phone checklist runs once,
  after this lands **(Claude)**.

## 5. How it works

### 5.1 Relay

- **The flag.** A session has a "QR hidden" flag, false when the session is created. It lasts as
  long as the session.
- **Who can change it.** Hiding: either token, so any phone or the PC. Showing again: the
  publisher token only. Both are idempotent, and phones are told only when the flag actually
  changes.
- **Subscribers get a small write.** Until now, subscribers could only read. Letting them hide is
  fine: the worst a player can do is what the button is for. The subscriber token still can't
  publish or clear.
- **Hide spam can't block room codes.** A phone must not be able to use up the PC's publish
  budget, because that would stop room codes from reaching anyone. Hide and show therefore get a
  budget of their own, charged after the token check like the existing one.
- **The PC can read it.** `GET /sessions/{id}` returns the flag along with the subscriber count.
- **Phones are told.** Phones get the flag when they subscribe, alongside the seeded room, and
  whenever it changes.
- **It can't touch a running game.** The event must be impossible to mistake for a room change
  on the page. A hide or show must never empty, reload or swap the frame (2026-10-27 §7). Pages
  already open when this deploys don't matter: deploying restarts the relay, which ends every
  session.
- **Errors work as elsewhere:** 404 for a dead session, 401 for a wrong token, 429 over budget.

### 5.2 Launcher and the QR card

**At launch,** when a relay is configured:
1. Resume or create tonight's session and write the session env, as today. This still happens
   before mitmdump, which needs the credentials.
2. Decide whether to show the card:

   | QR hidden? | Phone connected? | What happens |
   |---|---|---|
   | No | either | Show the card. |
   | Yes | none | Tell the relay to show it again, then show the card. Phones that connect later see the Hide button again. If that request fails, skip the card for this launch. |
   | Yes | at least one | No card. |

   A new night starts not hidden, so its first launch always shows the card.

**The card never holds up the game.**
- It starts once interception is up, just before the game. So a launch that falls back to no
  interception never shows it.
- It runs in the background next to the game.
- The launcher stops it on exit. It also closes by itself if the launcher is gone (Steam's Stop,
  a crash, SIGKILL), so it can never outlive the pack.

**It closes when:**
- the relay says the QR is hidden (polled every couple of seconds; network errors keep it up);
- the session is gone (404/401);
- someone clicks ×, which first tells the relay "hidden";
- the launcher exits.

**Other rules for the card:**
- It stays outside the redirect cgroup, like the launcher, so its HTTPS doesn't go through
  mitmproxy.
- It never takes keyboard focus.
- Its credentials come from files, never the command line. That's the existing rule, because a
  command line is world-readable.
- If the card fails (no display, a Tk error, no qrencode), only the card is lost: the game and
  the night carry on. Without qrencode it shows the URL as text, as today.
- `PairingWaiter`, the pairing timeouts and the "phones already connected, no QR" exit go away
  along with the wait.

### 5.3 Phone page

- The menu gets **Hide QR on TV** while the QR isn't hidden.
- Tapping it hides the QR. On success, the button disappears on every phone, via the stream.
- On failure, the page says so briefly and keeps the button.
- Nothing else on the page changes.

### 5.4 Docs

Update:
- the README's QR window section;
- the API in `relay/README.md`;
- after deploying, the route list in `~/Documents/homelab/jbvip-relay.md`.

## 6. Non-goals

- The QR over the Megapicker between packs, since it belongs to the launch.
- Showing the QR again from a phone, or a "new night" button.
- Other desktops: GNOME, gamescope or SteamOS Game Mode, wlroots compositors.
- Finding which monitor the game is on. It uses the primary only, and the mini PC has one.
- Setting up the mini PC (install, redirect at boot). That comes later; see 2026-10-27 §6.
- A count of joined phones on the card.

## 7. Anti-goals: it fails even if it technically works when…

- the card takes keyboard or controller input from the game, or sits anywhere but the top-right
  corner;
- a hide or show reloads, empties or swaps the game on any phone;
- the card is still on screen after the pack quits;
- the card delays the game's start, or a relay problem stops the game;
- a phone can stop room codes from being published;
- a token appears in a command line or a log;
- the QR is hidden, nobody is connected, and launching a game doesn't bring it back.

## 8. Verification

- **Relay** (pytest):
  - hiding works with either token;
  - showing works only with the publisher's token, and a phone's attempt is refused;
  - GET reports the flag, and a new subscriber gets it;
  - a change reaches subscribers once, and a repeat doesn't;
  - spamming hide doesn't use up the publish budget;
  - 404 and 401 as usual.
- **Host** (pytest, no display):
  - the launch decision in all four cases: new night; not hidden; hidden with phones connected;
    hidden with none, which tells the relay;
  - the card's close decision: hidden, 404/401, and a network error that keeps it up;
  - placement from sample `xrandr` output: a primary found, no primary, xrandr missing.
- **Launcher** (tests in the style of `tests/test_launcher.py`):
  - the card starts in the background just before the game, and the game doesn't wait for it;
  - cleanup stops it;
  - there's no card on any no-interception fallback path;
  - no token appears in its arguments.
- **Browser test:**
  - Hide shows while the QR isn't hidden, and a tap sends the request;
  - the button disappears on this page and on a second open page;
  - a hide while a game is framed changes nothing in the frame (no extra jackbox.tv request).
- **Real use, on the dev PC first:**
  - the card sits top-right of the primary monitor, above a real pack;
  - the game keeps the keyboard and controller;
  - Hide from Android removes the card within a few seconds;
  - a pack switch keeps it hidden;
  - a relaunch with no phone connected brings it back;
  - a Megapicker launch shows it above the pack;
  - × works.

  Then run the rest of the 2026-10-27 real-use checklist. Later, repeat on the mini PC.

## 9. Left to the builder

Left open:
- route and field names;
- the SSE event shape, within §5.1's rule;
- the poll interval;
- the card's size, margins, caption wording and styling;
- how the card is started and stopped, and how it notices that the launcher has exited;
- file layout and test structure.
