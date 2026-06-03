# Jackbox Room-Code Extractor (Linux) — Design Spec

**Date:** 2026-06-03
**Status:** Approved (supersedes `docs/room-code-extraction-design.md` and
`docs/room-code-extraction-implementation-plan.md`, whose Proton + iptables +
system-store assumptions were disproved by the Phase 0 spike).

---

## 1. Goal & context

Extract the Jackbox lobby room code on the host machine, uniformly across games
and resiliently across game updates, and deliver it to clients over the existing
TCP contract (`RoomCode:<CODE>` on port 38469). The auto-join "VIP bot" remains a
separate future project (Non-Goals).

The original tool read the code from game memory via per-game, per-version
pointer chains — fragile and Windows-only. This redesign reads the code from the
game's own backend traffic instead.

## 2. What the Phase 0 spike established (the proven mechanism)

Verified live on CachyOS with Party Pack 1/5/9 (Steam):

- The Linux games are **native ELF** (Adobe AIR titles), **not** Proton/Wine.
- TLS = **statically-linked libcurl 7.73.0-DEV + OpenSSL 3.0**, baked into a
  stripped binary. User-Agent: `JackboxGames/1.00 libcurl/7.73.0-DEV OpenSSL/3.0.0 … (Linux)`.
- The game's curl **ignores `SSL_CERT_FILE`** and reads the compiled-in path
  `/etc/ssl/certs/ca-certificates.crt`. It **honors `HTTPS_PROXY`**. **No cert
  pinning** — mitmproxy decrypts cleanly once its CA is trusted.
- **Room code source:** `POST https://ecast.jackboxgames.com/api/v2/rooms` →
  `200` JSON body `{ "ok": true, "body": { "host": "<regional>", "code": "<CODE>", "token": "…" } }`.
  The lobby WebSocket connects to the **regional host** in `body.host` (e.g.
  `ecast-prod-use2.jackboxgames.com`), not `ecast.jackboxgames.com`.

**Interception transport (safe, no root, nothing persists):**
- Route the game's HTTPS with the **`HTTPS_PROXY` env var** (no iptables needed).
- Inject the mitmproxy CA into the game's **own mount namespace only**, via
  `bwrap --dev-bind / / --bind <bundle> /etc/ssl/certs/ca-certificates.crt`
  (unprivileged; the rest of the system never trusts the CA; it vanishes when
  the game exits). The bundle is `system-CAs + mitmproxy-CA`.
- mitmproxy runs only while the game runs.

This is implemented today in `jbvip-run.sh` (proven end-to-end: captured codes
`ISAE`, `NBHT` with a **clean** system trust store).

## 3. Non-goals

- Auto-join VIP bot (separate future project; consumes `RoomCode:<CODE>`).
- Legacy Blobcast protocol support.
- Windows / console support; memory reading; OCR. All abandoned.
- Decrypting any traffic other than the local host's own game.

## 4. Architecture

A single Python process runs as a **mitmproxy addon**, started by `jbvip-run.sh`
when a game launches. mitmproxy decrypts the game's ecast HTTPS; the addon
extracts the room code and pushes it to an **asyncio TCP server** that speaks the
unchanged `RoomCode:<CODE>` protocol on port 38469. One process, one language,
no IPC.

```
game (bwrap: per-game CA + HTTPS_PROXY) ──► mitmproxy + addon ──► real ecast
                                                  │ extract body.code
                                                  ▼
                                 asyncio TCP server :38469 ──► clients ("RoomCode:NBHT")
```

## 5. Components

Each unit has one purpose, a well-defined interface, and is testable in isolation.

### 5.1 `extractor/roomcode.py` — pure extraction (no I/O)
- `code_from_create_response(url: str, body: dict | None) -> str | None`
  **Primary.** When `url` is a `…/api/v2/rooms` create call and `body` is
  `{"ok": true, "body": {"code": …}}`, return the sanity-checked code.
- `extract_code_from_url(url: str) -> str | None`
  **Fallback.** Parse `<CODE>` from a `…/rooms/<CODE>/play` (or `…/rooms/<CODE>`)
  path segment.
- `looks_like_code(code: str) -> bool`
  Sanity check: short and alphanumeric (e.g. `2 <= len <= 8 and code.isalnum()`).
  **Not** hardcoded to the historic 4-letter uppercase format.

### 5.2 `extractor/server.py` — `RoomCodeServer` (asyncio TCP)
- Interface: `start()`, `set_code(code)`, `bound_port`.
- Wire: ASCII `RoomCode:<CODE>` (no terminator). Sends the current code to each
  new client on connect (if known); broadcasts on change; **dedupes** (no resend
  of an unchanged code); prunes disconnected clients.
- Binds `0.0.0.0:38469` (matches the original C# `IPAddress.Any`, so a client/bot
  elsewhere on the LAN can connect). It only ever serves a 4-ish-char room code.

### 5.3 `extractor/addon.py` — mitmproxy addon
- Host-filters to `jackboxgames.com`.
- Hooks: `response` (REST create-room → parse JSON body via
  `code_from_create_response`) and `websocket_start` (fallback → parse handshake
  URL via `extract_code_from_url`).
- On a code that **changed**, calls `server.set_code(code)`.
- `running()` starts the `RoomCodeServer` in mitmproxy's asyncio loop.
- Module exposes `addons = [RoomCodeExtractor(RoomCodeServer(port=38469))]`.

### 5.4 `jbvip-run.sh` — launcher (exists; to be updated)
- Change the mitmproxy invocation to load the addon
  (`mitmdump -s extractor/addon.py` with `PYTHONPATH=<repo root>`) instead of the
  `-w` capture used during the spike.
- Keep the proven bwrap per-game CA injection, `HTTPS_PROXY`, and
  start-only-if-needed + teardown-on-exit lifecycle.
- **Port handling:** the launcher must ensure *its* addon-loaded mitmproxy is the
  one serving the game. Use a dedicated proxy port; if it is already in use,
  **fail loudly** with a clear message rather than silently reusing a stray proxy
  (which would have no addon). (During the spike a stray TUI proxy on :8080 was
  silently reused — acceptable for a capture test, not for the real tool.)

### 5.5 `install.sh` / `uninstall.sh` — convenience
- Scan Steam libraries (`~/.steam/steam/steamapps/libraryfolders.vdf` → each
  library's `steamapps/appmanifest_*.acf`) for installed games whose name
  contains "Jackbox".
- For each, write `<abs path>/jbvip-run.sh %command%` into that game's
  `LaunchOptions` in `~/.steam/steam/userdata/<id>/config/localconfig.vdf`.
  **Refuses to run while Steam is open** (Steam rewrites `localconfig.vdf` on
  exit). `uninstall.sh` clears the options it set.
- Preflight checks: bubblewrap present, unprivileged user namespaces enabled,
  mitmproxy installed (offer `pipx install mitmproxy`).

### 5.6 Docs & cleanup
- Rewrite `README.md` to the new Linux + Steam + bwrap design (run `install.sh`
  once, launch the game, clients connect to TCP `:38469` and read
  `RoomCode:<CODE>`).
- Mark/replace the two old `docs/room-code-extraction-*.md` files as superseded
  by this spec.
- Remove the obsolete C# project: `JackBoxAutoVIP/` and
  `JackBoxRoomCodeExtractor.sln`.

## 6. Data flow

1. `install.sh` (once) sets the launch option for all installed Jackbox games.
2. User launches a game via Steam → `jbvip-run.sh` starts mitmproxy+addon, builds
   the CA bundle, bwraps the game with `HTTPS_PROXY` + per-game CA.
3. Game creates a room → `POST /api/v2/rooms` → mitmproxy decrypts → addon reads
   `body.code`.
4. Addon calls `server.set_code` → server broadcasts `RoomCode:<CODE>`; new
   clients receive the current code on connect.
5. New room / code change → re-broadcast (deduped). Game closes → bwrap exits →
   launcher tears down mitmproxy.

## 7. Error handling & edge cases

- **Code change / new room** — broadcast on change only (dedupe).
- **Malformed body / non-Jackbox host** — extraction returns `None`; ignored.
- **Proxy port already in use** — launcher fails loudly (see §5.4).
- **Client disconnects** — pruned from the broadcast set.
- **Stale code on startup** — nothing broadcast until a room is observed.
- **bwrap missing / userns disabled / Steam running** — `install.sh` aborts with
  guidance.

## 8. Testing strategy (TDD)

- **Parser unit tests** — feed real captured fixtures (`ISAE`, `NBHT` create
  responses; `…/rooms/<CODE>/play` URLs; malformed/edge cases). No live game.
- **Server tests** (pytest-asyncio) — new client receives current code on
  connect; broadcast on change; duplicate code not resent; disconnect pruning.
- **Addon tests** — REST response, WS handshake, non-Jackbox host, and no-code
  flows produce the right extraction.
- **vdf editor test** — the `LaunchOptions`-writing function against a sample
  `localconfig.vdf` fixture.
- **End-to-end** — real game via Steam: create a room, a connected client
  receives the correct `RoomCode:<CODE>` within ~1s (already essentially shown).

## 9. Wire contract (unchanged)

TCP listener on port **38469**; sends ASCII `RoomCode:<CODE>` (no terminator);
pushes the current code to each newly connected client and re-broadcasts on
change. Preserves any existing/future client and the auto-join bot to come.

## 10. Future work (out of scope here)

- Auto-join VIP bot consuming the extracted code.
- Legacy Blobcast protocol support.
- Packaging (e.g. a distro package or one-shot installer bundling mitmproxy).
