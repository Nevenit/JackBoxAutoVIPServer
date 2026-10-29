# Admin Password (VIP Priority) — Design

**Date:** 2026-10-29
**Status:** Draft
**Amended (same day):** the head start became a *hold*: guests wait until the first player
joins the new room, or until the hold runs out (§2 "The head start", §3.2, §3.3). The user
saw that a 0.5 s head start can't work: the page only fills in jackbox.tv's join form, and
the admin still has to tap Play before anyone else.
**Replaces:** §5 ("Part 2 — optional VIP priority") of
`2026-10-27-browser-player-page-design.md`. That section's pairing design (a host-side admin
QR, an `/a` page and a host follow stream) is dropped. Its goal stays: the admin lands VIP.
Builds on what's deployed: the player page, night-long sessions and the TV QR card
(`2026-10-28-tv-qr-overlay-design.md`).

---

## 1. Intent

Jackbox gives VIP to whoever joins the room first. The host wants their own phone to win that
race without a separate pairing step. They scan the TV's QR like everyone else, type a
password into the page's menu **once**, and from then on that phone is admin on every night.
The admin gets each new room a moment before the guests, so tapping Play makes them VIP.

This was never important to the user ("it doesn't really matter who is the VIP, but might as
well add it"). It must be possible to turn it off or never set it up, and then VIP goes to
whoever joins first, exactly as today.

## 2. Decisions

Marked **(user)** where the user decided, **(Claude)** where it's Claude's call and open to
change.

**Becoming admin**
- The admin scans the same QR as everyone and enters a password in the page's ⋯ menu. The
  phone remembers it, so on later nights scanning is enough **(user)**.
- The phone does **not** find tonight's session by itself. A new night means scanning the new
  QR like everyone else **(user: "not worth it")**.
- Any phone that knows the password can be admin, so it can be shared **(Claude, not
  objected to)**.
- The menu has **Stop being admin**, which forgets the password on that phone. A wrong
  password just says so, and that phone carries on as a guest **(Claude, not objected to)**.

**The head start**
- When a new room starts, admin phones get it first and guests are **held** **(user)**. The
  hold ends as soon as the first player joins the new room, which is the admin tapping Play
  **(user)**, or when it runs out. It's tweakable, so a later night can try another length
  **(user)**.
- "The first player joins", not "the admin joins" **(Claude, user agreed)**: during the hold
  only admin phones have the room, and if someone typed the code off the TV and joined first,
  VIP is gone and holding guests helps nobody.
- The hold lasts **10 s** at most by default **(Claude)**. With the early release, guests only
  wait that long when the admin is slow. (It was first 0.5 s with no early release; the user
  chose 0.5 s over a suggested 10 s before seeing why a fixed delay can't work.)
- The head start only applies while at least one admin phone is connected. A locked or absent
  admin phone means nobody waits **(Claude, not objected to)**.
- The admin phone has a **Priority on/off** switch in the menu for nights it doesn't matter.
  Off means that phone connects as a guest **(Claude, not objected to)**.

**Settings on the TV PC**
- The password is set as `JBVIP_ADMIN_PASSWORD=...` in `~/.config/jbvip/relay.env`, the file
  the launcher already reads **(Claude, not objected to)**.
- The hold's length is set as an optional `JBVIP_ADMIN_HEAD_START=10` in the same file, in
  seconds, defaulting to 10 **(Claude)**. It's set there and not in the relay so it can be
  changed without a deploy.
- **No password set means no admin feature**: everyone gets each room at the same moment,
  exactly as today **(user's rule; Claude's mechanism)**.

**On iPhone**
- Safari deletes a site's saved data after about 7 days without a visit, unless the page is
  saved to the Home Screen. An iPhone admin may have to re-enter the password now and then.
  That's accepted; no workaround **(Claude, not objected to)**.

## 3. How it works

### 3.1 The admin key

- The phone and the PC never send the password itself. Both turn it into the same **admin
  key**: a SHA-256 of a fixed project prefix plus the password, via WebCrypto on the phone and
  `hashlib` on the PC.
- The phone stores the key, not the password, in `localStorage`, next to the name it already
  stores.
- The PC registers the key with tonight's session. The phone presents it when it subscribes.
- The relay compares the two in constant time, and never sees or stores the password.

### 3.2 Relay

- **Registration.** A publisher-token call registers the admin key and the head start with a
  session, and replaces any earlier registration. It's idempotent, because the launcher
  registers on every launch: a relay restart loses everything in memory, and a reused night
  already has it.
  - The relay clamps the head start to a sane range (Claude's call: 0 to 30 s).
  - No registration means no admin feature for that session.
- **Subscribing as admin.** A phone presents its key in a header with its subscribe request,
  never in the URL. If it matches, the stream is an admin stream. If not, the stream is a
  plain guest, and the page is told the key was wrong so it can say so.
- **A key sent to a session with no registration** (no password set on the PC tonight) is
  neither accepted nor wrong. The stream is a guest, and the page is told "no admin tonight",
  so it **keeps** its stored key. Treating it as wrong would make every admin phone forget the
  password the first night the feature is switched off **(Claude)**.
- **Wrong keys are rate-limited** per session, charged after the subscriber-token check like
  the other budgets. Only people who scanned tonight's QR can try at all.
- **The head start.** When a publish changes the room and at least one admin stream is
  connected:
  - admin streams get the new room at once;
  - guest streams get it after the head start;
  - a guest who subscribes during the window also waits for it to end.

  Otherwise everything is delivered at once, as today. A clear ("no room") always goes to
  everyone at once.
  - **Early release.** A publisher-token call names a room and says a player has joined it.
    If guests are being held for that room (it is the current room and a window is open),
    the window ends at once and guests get the room. Otherwise it does nothing: no window,
    a different room, or a window that already ended **(Claude)**.
  - A guest waiting out the window keeps the room it is on; nothing else is held back.
  - A second room published during the window replaces the pending one for the guests. It
    doesn't queue a second delivery or restart the timer **(Claude)**.
  - An admin phone with Priority off connects as a guest, so it doesn't count as "an admin
    connected" **(Claude)**.
- **No early leaks.** No event may give a guest the new room before its head start ends (or
  an early release for that room arrives). That
  includes the subscribe seed and QR-flag events, which repeat the current room (2026-10-28
  §5.1). During the window, a guest's events carry the room that guest is still on.
- **Everything else is unchanged:**
  - admin streams count towards the 16-stream cap and its oldest-first eviction;
  - the QR card's subscriber count includes them;
  - a hide works the same from an admin phone.
- **Storage.** All of it is in memory, with one worker, like the rest of the relay.

### 3.3 Launcher (TV PC)

- `relay.env` gains the two optional lines. `jbvip-run.sh` sources that file without
  exporting anything, so it must hand both values to the Python setup step explicitly, the way
  it already hands over `JBVIP_RELAY_URL`.
- When the night's session is ready (created or resumed), the setup step registers the admin
  key and head start if a password is set.
- A failed registration costs only the head start tonight. It never stops or delays the game.
- **Spotting the first join.** The proxy already reads every message the game exchanges with
  Jackbox's servers. When it sees the first player join a room, it sends the early release
  for that room, once per room. Two formats **(Claude, from what's known of the protocols;
  to be confirmed in real use)**:
  - ecast: the host's websocket (`/rooms/<CODE>/play`) receives an `opcode: "client/connected"`
    message; one whose `result.role` is `"audience"` doesn't count;
  - Blobcast: the host's socket.io websocket receives a `CustomerJoinedRoom` event carrying
    `roomId`.
- A missed join (a pack whose format differs) only means guests wait the full hold. A failed
  release call is the same. Neither ever stops or delays the game.
- The password stays on the PC, in the mode-600 `relay.env`. Only the derived key goes over
  the network, in a request body or header, never in a command line or a log.

### 3.4 Phone page

- **The ⋯ menu** gains:
  - **Admin password** (shown when the phone isn't admin): a password field and a button.
    Entering it stores the key and reconnects with it. If the relay says the key is wrong, the
    page says so and forgets it.
  - **Priority on/off** and **Stop being admin** (shown when the phone is admin).
- **The bar** shows that the phone is admin, so the host can tell at a glance. The marker is
  small; its look is the builder's call.
- **Nothing else on the page changes.** Above all, a game in the frame is never reloaded or
  swapped because of admin state (2026-10-27 §7).

### 3.5 Docs

- The README covers the two `relay.env` lines and how to become admin.
- `relay/README.md` covers the new route and header.

## 4. Non-goals

- Finding tonight's session automatically from the password (the user declined).
- Different passwords for different admins, roles, kicking players, or showing who's admin to
  others.
- Anything inside jackbox.tv. The page still can't tap Play for anyone.
- Telling whether the admin actually got VIP.

## 5. Anti-goals: it fails even if it technically works when…

- a guest gets the new room before the head start ends or a player has joined it, by any
  route (seed, reconnect, QR event, Invite page);
- the head start delays guests when no admin phone is connected as the room starts (guests already waiting finish the window, at most the head start, even if the admin then leaves);
- anything changes for anyone when no password is set;
- a wrong password, a relay error or a failed registration stops anyone playing or delays the
  game;
- the password or the admin key appears in a URL, a QR, a log or a command line;
- a game in the frame is reloaded or swapped because of admin state.

## 6. Verification

- **Relay** (pytest):
  - registration, including replacing and clamping;
  - a matching key makes an admin stream, and a wrong key makes a guest stream with the page
    told;
  - the head start: the admin gets the room at once, a guest gets it after the head start, a
    guest who joins during the window gets it when the window ends, and nobody waits with no
    admin connected;
  - no early leak through the seed or a QR event;
  - an early release for the held room lets guests in at once; one for another room, with
    no window, or with the phone's token does nothing (or 401);
  - clears go out at once;
  - wrong keys are rate-limited after the token check;
  - admin streams count towards the cap.
- **Launcher and host** (pytest):
  - the key is derived the same way as the page derives it, using a known vector shared with
    the browser test;
  - registration happens with a password set, and not without one;
  - a failed registration doesn't affect the launch;
  - no key appears in a command line;
  - the proxy spots a first join in both formats and sends one release per room, and a
    failed release never raises into the proxy.
- **Browser test:**
  - entering the password makes the page admin and survives a reload;
  - a wrong password says so;
  - Priority off and Stop being admin work;
  - an admin page and a guest page on the same session receive a new room in that order;
  - the key never appears in a request URL.
- **Real use:** with the password set on the TV PC, the admin phone taps Play and lands VIP;
  guests' phones show the room within about a second of that, not after the full hold. If a
  pack's guests always wait the full hold, its join format differs: capture it and add it.

## 7. Left to the builder

- route, header and field names;
- the fixed prefix in the key derivation;
- how the page is told its key was wrong;
- the wrong-key budget size;
- the exact menu and bar copy and styling;
- how the relay schedules the delayed delivery;
- test structure.
