# Auto-Join Pipeline — Known Deferred Issues

**Date:** 2026-08-02
**Branch:** `auto-join-client`
**Companion to:** `2026-08-01-jackbox-auto-join-design.md`

Every task on this branch was reviewed, and a whole-branch review then triaged
each finding. The Critical and Important ones were fixed. What follows is what
was deliberately **not** fixed, with the reason — so nobody rediscovers these as
if they were unknown, and so the next person can weigh them rather than guess.

## Two verification steps still outstanding

Both need a human at the machine with a real Jackbox game running; neither can be
automated.

1. ~~**Live injection check.**~~ **DONE 2026-08-03.** `injection/inject.js` was run
   against a real room (Party Pack 2 / Fibbage 2) in Firefox:
   `await window.jbvipJoin('<CODE>', 'Michael')` returned `{ok: true}` and the
   client joined — websocket `client/welcome` reported `id: 2`,
   `roles.player.name: "MICHAEL"`, `playerIndex: 0`. The selectors and the Vue
   `input`-event technique hold on the real page. A bogus code correctly returned
   `{ok: false, reason: "join-button-never-enabled"}` (jackbox.tv 404s the room
   lookup and leaves the button disabled), which also closed the fixture's
   untested-branch gap below.

   Still open within this: nobody has yet confirmed the **VIP marker** visually,
   and Party Packs 1 and 5 have not been tried. See the protocol note below —
   Pack 2 turned out to be on ecast, so it does not answer the Blobcast question.
2. **Real-game end-to-end run.** A code has travelled from a real relay to a
   subscriber over real HTTP, and the extractor's own path is long-proven, but the
   two have never been joined up in one run with a real game.

While doing (2), capture one number worth having: **how many times
`RelayPublisher.set_code` fires for a single room on a Blobcast pack.** The
de-duplication guard makes repeats cheap, but the raw count tells us whether the
`websocket_message` hook is firing a handful of times or thousands.

## Deferred with reasons

### Behaviour under unusual configuration

- **`termlog_verbosity: error` in `~/.mitmproxy/config.yaml` silently disables the
  keep-the-log trigger.** The launcher decides whether to preserve the proxy log by
  grepping it for a marker the addon writes at warning level; suppressing warnings
  removes the marker. Same option-precedence quirk that made `--set flow_detail=1`
  inert (see `jbvip-run.sh`). A diagnostics degradation, not a leak, and the
  dedicated-flag remedy overshoots.
- **The log redaction is anchored on `/rooms/` only.** A future pack using a
  different code-bearing path segment — `/api/v2/audience/<CODE>/play`, say — would
  pass through unredacted. Not reachable today; the host game only uses `/rooms/`
  (`extractor/roomcode.py`). **Re-check this when adding support for any new pack.**

## Protocol assumption corrected (2026-08-03)

"Older pack" does **not** reliably mean Blobcast. Party Pack 2 (appid `397460`,
Fibbage 2), a 2015 title, was observed running on **ecast**: player traffic goes to
`ecast.jackboxgames.com/api/v2/rooms/<CODE>` and the websocket uses the modern
protocol (`opcode: "client/welcome"`). Its entity keys still carry `bc:` prefixes
(`bc:room`, `bc:customer:<uuid>`) — vestigial Blobcast naming over the newer
transport, so **the `bc:` prefix is not a signal of which stack a pack uses**.
Jackbox appears to have migrated at least some older packs since the June 2026
protocol survey. Verify per pack rather than inferring from release year.
- **The relay must run exactly one worker.** All state is per-process. Documented in
  `relay/README.md`; there is no code-level guard.

### Concurrency and lifecycle

- **De-duplication closes sequential repeats, not a race.** Two extractor hooks
  firing while a publish is in flight can both post. Deliberate: the alternative
  risked a transient failure permanently suppressing a code.
- **An evicted SSE subscriber stays connected but silent.** When `_fan_out` drops a
  subscriber whose queue is full, the connection remains open with no way for the
  client to notice. The client's reconnect logic is the intended backstop.
- **A queue can leak if a streaming generator is cancelled before it starts.** The
  bounded queue caps the damage rather than removing the window.

### Durability

- **A crash mid-write can orphan a `.tokens-*.json` temp file.** Harmless —
  `load_tokens` only ever reads the real path.
- **No test injects an `os.replace` failure** to positively prove the previous good
  token file survives an interrupted save. The property holds by inspection.

### Test coverage gaps, judged acceptable

- The offline fixture never exercises `join-button-never-enabled` or the
  `join-did-not-complete` fallback. The former was **confirmed working live on
  2026-08-03** by passing a nonexistent room code; only the
  `join-did-not-complete` fallback remains unexercised.
- The SSE keepalive branch is untested; doing so means a 20-second wait or injecting
  the constant.
- `attempts <= 0` makes `RelayPublisher.set_code` a silent no-op. Unreachable —
  nothing wires configuration to `attempts`.

### Cosmetic

- The pairing TTL expires on `exp <= now` while the freshness window uses a strict
  `>`. Both defensible; merely inconsistent with each other.
- `app.state.relay` is assigned and never read.
- `pair.py`'s `main(argv=None)` never reads `argv`.
- A malformed or HTML relay response (a proxy 502 page) surfaces a raw JSON parse
  error rather than a friendly message.
- The commit body of `cc6dede` contains the typo "unrated-limited". Not worth
  rewriting history over.

## Deliberate design decisions that look like defects

Recorded because each was queried during review and upheld:

- **`relay/state.py` duplicates `extractor/roomcode.py`'s plausibility check.** The
  relay deploys to a VPS without the extractor package. Both sides now pin the
  length boundaries so drift is loud. **Do not consolidate them.**
- **`relay/requirements.txt` declares Python 3.11+ although the true floor is
  3.10.** Conservative on purpose: nothing below 3.14 has ever been exercised by the
  test suite.
- **`/pair` is unauthenticated.** A phone redeeming a pairing code has no credential
  yet; the single-use, five-minute code is the credential. It has its own rate-limit
  budget so it cannot be used to lock the operator out of minting.
