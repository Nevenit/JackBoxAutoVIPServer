# Jackbox Room-Code Extraction — Redesign Spec

**Date:** 2026-06-03
**Status:** Draft for review
**Scope of this spec:** Robust, update-resistant extraction of the Jackbox lobby
room code on the host machine. The auto-join "VIP bot" is explicitly a **later,
separate project** (see Non-Goals).

---

## 1. Problem & Goal

`JackBoxAutoVIPServer` currently reads the room code out of the game's memory via
hardcoded multi-level pointers (`Program.cs`), one set of offsets per game *and*
per game version. This is the bottleneck:

- Pointers break on every game update and don't exist for many titles.
- OCR is impractical — each minigame renders the code in a different stylised font.
- Passive packet capture is defeated by TLS (the traffic is encrypted in transit).

**Key insight that reopens the network route:** TLS only protects traffic from a
*passive* observer. The extractor runs on the *same machine as the game*, so we
control that whole environment and can read the game's own TLS traffic by
terminating it ourselves (man-in-the-middle) or by reading it inside the process
after decryption.

**Goal:** a single source of room codes that is *uniform across games* and
*survives game updates*, by reading the code out of the host game's own backend
traffic instead of its memory.

**Success criteria:**
1. When a Jackbox host game (ecast-era) creates a room, the correct room code is
   captured within ~1s of the lobby appearing, with no per-game configuration.
2. The code is delivered to clients over the existing TCP contract
   (`RoomCode:XXXX` on port 38469).
3. A game update does not require code changes to keep extraction working.

---

## 2. Decisions locked during brainstorming

| Decision | Choice |
|---|---|
| Work scope this round | Extraction only; bot deferred |
| Host OS | **Linux**, game via Steam/**Proton** (drops Windows target) |
| Primary mechanism | **Transparent TLS-intercepting proxy** (mitmproxy) |
| Mechanism gate | Phase 0 feasibility test decides primary vs fallback |
| Fallback mechanism | In-process plaintext capture (eBPF SSL uprobes / Frida) |
| Traffic forcing | Kernel `iptables`/`nftables` REDIRECT (no network namespace) |
| External tooling | Allowed (mitmproxy is Python) |
| Protocol coverage | **ecast** first; legacy **Blobcast** parser only if needed |
| Distribution layer | Recommended: fold into the Python process, keep wire protocol (see §6) |

---

## 3. Non-Goals

- **Auto-join VIP bot.** A separate follow-up project will consume the room code
  and join the lobby first to claim VIP. Out of scope here.
- **Windows / console support.** This design targets Linux + Proton only.
- **Memory reading and OCR.** Both are abandoned.
- **Decrypting other machines' traffic.** We only ever intercept the local host's
  own game, on a machine the user owns.

---

## 4. Architecture overview

```
  ┌─────────────────────────┐
  │  Jackbox game (Proton)  │   thinks it's talking to ecast.jackboxgames.com
  └────────────┬────────────┘
               │ TCP :443
        (kernel REDIRECT, iptables nat OUTPUT)
               ▼
  ┌─────────────────────────────────────────────┐
  │  mitmproxy (transparent mode) + addon        │
  │   • terminates game-side TLS (mitm CA cert)  │──► real ecast.jackboxgames.com
  │   • re-encrypts to the real server           │◄── (upstream TLS)
  │   • addon watches flows, extracts room code  │
  │   • addon runs the TCP distribution server   │
  └────────────┬────────────────────────────────┘
               │ "RoomCode:XXXX"  (TCP :38469, unchanged wire protocol)
               ▼
        connected clients
```

The game is unaware of the proxy. mitmproxy holds two TLS sessions
(game↔proxy, proxy↔server) and sees cleartext in between. A small Python addon
extracts the code and broadcasts it on the existing TCP contract.

---

## 5. Components

### 5.1 Traffic interception (kernel REDIRECT)
- A `nat`/`OUTPUT` REDIRECT rule sends outbound `:443` to mitmproxy's transparent
  port; mitmproxy reads the original destination via `SO_ORIGINAL_DST`.
- **Loop avoidance (required on a single box):** mitmproxy's *own* upstream
  connections also target `:443`, so the rule must exclude them or it loops
  forever. The standard fix is to run **mitmproxy as a dedicated user** and match
  `-m owner ! --uid-owner <mitmproxy-user>`. The game keeps running as your normal
  user — no second *Steam* login is needed, only mitmproxy gets its own user.
- **Scoping:**
  - **Default: redirect all :443 except the mitmproxy user.** Simplest. mitmproxy
    transparently relays *all* the machine's HTTPS while running; the addon only
    *acts* on Jackbox flows. Fine on a personal box where the proxy runs only while
    playing. Caveat: other apps that pin certs break *while the proxy runs*.
  - **Hardening (optional) — scope to the game:** run Steam as its own user and
    match `--uid-owner <game-user>` (this also avoids the loop), or place the game
    in a cgroup (`systemd-run --scope`) and match `-m cgroup`. Limits interception
    to the game; cost is a second Steam login or more setup.
- The rule is added on start and removed on stop by the launcher script.

### 5.2 TLS interception (mitmproxy)
- `mitmdump --mode transparent -s extractor.py`.
- mitmproxy's CA must be trusted by the game. **Where the game looks is the
  central unknown (see §7 / §9):** the Proton/Wine cert store, the host system CA
  store, and/or a `cacert.pem` bundled inside the game's `*_Data` directory.
  Phase 0 determines which (if any) works.

### 5.3 Extraction addon (Python)
Reliability strategy — **extract from the host's own room WebSocket URL, with the
create-room REST response as a secondary source.** Rationale: regardless of
whether the room is created via REST or over a socket, the host game must
eventually open the lobby WebSocket to a URL of the form
`wss://ecast.jackboxgames.com/api/v2/rooms/<CODE>/play...`. That path segment is
the authoritative room code and is visible to mitmproxy at the WS handshake.

- Hook points in the addon:
  - `websocket_start` / the WS handshake request URL → parse the `/rooms/<CODE>/`
    segment.
  - `response` for `POST .../api/v2/rooms` (or equivalent) → read the code field
    from the JSON body as a secondary/confirming source.
- **Do not hardcode code length.** Extract the room-id path segment as-is and
  sanity-check it (e.g. short, alphanumeric, uppercase) rather than assuming the
  historic 4-letter format, which newer titles may not always follow.
- Because the code comes from the host's *own* room connection, it is
  authoritative — no guessing as in the memory approach. The previous
  `IsCodeValid` round-trip to the ecast API becomes an optional sanity check, not
  a necessity.
- De-duplicate: only broadcast when the code changes (mirrors current
  `SetRoomCode` behaviour).

### 5.4 Distribution server (client-facing)
- **Wire contract is unchanged:** TCP listener on port 38469, sends
  `RoomCode:XXXX`, pushes the current code to each newly connected client, and
  re-broadcasts on change. This preserves any existing/future client.
- **Recommended implementation: in the same Python process as the addon**
  (mitmproxy runs an asyncio loop; the addon starts an `asyncio` TCP server).
  One process to launch, one language, no IPC. Retires the Windows-only C# code.
- **Alternative (if you'd rather keep the C# server):** the addon pushes each
  code to the existing C# app over a trivial local ingest channel
  (e.g. `POST http://127.0.0.1:<port>` → `TcpServer.SetRoomCode`). Cost: two
  runtimes and an extra hop. Not recommended for a personal tool.

---

## 6. Data flow

1. Wrapper script installs the REDIRECT rule and starts `mitmdump` with the addon
   (which also opens the :38469 distribution server).
2. User launches the Jackbox game via Steam/Proton and creates a room.
3. Game opens its lobby WebSocket to `.../rooms/<CODE>/play`; the kernel diverts
   it to mitmproxy; mitmproxy presents its CA-signed cert; handshake succeeds.
4. Addon reads `<CODE>` from the handshake URL, sanity-checks it, stores it as the
   current code.
5. Addon broadcasts `RoomCode:<CODE>` to all connected clients; new clients get
   the current code on connect.
6. On game close / new room, the code updates and re-broadcasts.
7. Wrapper script removes the REDIRECT rule on shutdown.

---

## 7. Phase 0 — feasibility test (GATE)

The whole design branches on one fact: **does the game accept mitmproxy's CA?**
This is built and run *before* committing to the full implementation.

Steps:
1. `pip install mitmproxy`; run `mitmdump --mode transparent`.
2. Add a global `:443` REDIRECT rule.
3. Install mitmproxy's CA into the candidate trust locations (host store; Wine
   prefix; and check the game dir for a bundled `cacert.pem`).
4. Launch one Jackbox game, create a room, observe mitmdump flows.

Outcomes:
- **Decrypted `ecast` flows incl. the room WS/create call** → Approach A confirmed;
  proceed with §5.
- **TLS handshake errors / game can't reach the lobby** → cert pinning or a
  bundled CA. If it's an editable bundled file, patch it and retry; otherwise
  fall back to §8.
- **Connection present but not decrypted** → transparent-mode/config issue; fix
  and retry.

Deliverable: a recorded answer (works / needs bundle patch / pinned) plus, if it
works, a saved sample flow to use as a parser test fixture.

---

## 8. Fallback design (Approach B) — only if Phase 0 shows pinning

Read the plaintext *inside* the process, bypassing cert validation entirely.
Two candidate tools (choose after identifying the TLS library — see §9):

- **eBPF SSL uprobes** (e.g. `ecapture`): attach to `SSL_read`/`SSL_write` in the
  game's TLS library to dump plaintext. Works cleanly when TLS is a known,
  dynamically-linked lib; statically-linked TLS needs offset discovery.
- **Frida hooks:** hook the relevant network/parse functions. More flexible but
  fiddlier against a Windows PE running under Wine.

Same downstream design (§5.3–5.4): extract `<CODE>`, broadcast over the wire
contract. Detailed only if Phase 0 forces this path.

---

## 9. Open questions / unknowns to verify

(The background research agent had no web access; these are to be confirmed by
Phase 0 and light investigation, not assumed.)

1. **Cert behaviour** — does the game trust the OS/Wine store, or a bundled
   `cacert.pem`, or pin? *Decides A vs B.* (Phase 0)
2. **ecast room-creation mechanism** — REST `POST /api/v2/rooms` vs socket-based.
   *Design hedges by keying off the room WS URL regardless.*
3. **TLS library** (UnityWebRequest+libcurl/BoringSSL vs Mono `HttpClient` vs
   websocket-sharp) and whether it's static or dynamic. *Needed only for the §8
   fallback.*
4. **Game coverage** — which of the user's games are ecast vs legacy Blobcast,
   and whether Blobcast support is needed at all.

---

## 10. Error handling & edge cases

- **Code changes mid-session / new room** — broadcast on change only (dedupe).
- **Game restart / multiple launches** — addon is stateless per code; latest
  observed host room wins.
- **Client disconnects** — drop from broadcast list (mirror current behaviour);
  reuse the existing connected/cleanup logic semantics.
- **mitmproxy not capturing** — log loudly; the wrapper verifies the REDIRECT
  rule is present and mitmproxy is up before declaring ready.
- **CA not trusted at runtime** — surfaces as TLS handshake failures in mitmproxy
  logs and the game failing to reach the lobby; treat as the Phase 0 "pinned"
  branch.
- **Stale code on startup** — no code is broadcast until a room is actually
  observed; clients connecting early simply receive nothing until then.
- **Global REDIRECT breaking other pinning apps** — documented; mitigated by
  running the proxy only while gaming, or by uid/cgroup scoping (§5.1).

---

## 11. Testing strategy

- **Phase 0 spike** (§7) — the gating manual test; produces a sample flow fixture.
- **Parser unit tests** — feed the addon's extraction function saved sample WS
  URLs / create-room responses (incl. malformed/edge cases); assert the right
  code (or none) comes out. No live game needed.
- **Distribution server test** — connect a test TCP client, assert it receives
  the current code on connect and on change, and that disconnects are handled.
- **End-to-end** — real game via Proton: create a room, confirm a connected
  client receives the correct `RoomCode:XXXX` within ~1s.

---

## 12. Future work (out of scope here)

- Auto-join VIP bot consuming the extracted code (the original project goal).
- Legacy Blobcast protocol support, if older packs are in scope.
- Packaging/automation of CA install + REDIRECT teardown into one launcher.
