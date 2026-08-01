# Jackbox Auto-Join Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Get the host's room code from the extractor to a phone anywhere in the world, and prove the browser-side join script that will consume it.

**Architecture:** The mitmproxy addon fans a extracted code out to two sinks — the existing LAN TCP server, and a new best-effort publisher that POSTs it over outbound HTTPS to a small relay on the user's VPS. The relay holds one code in memory and pushes it to authenticated subscribers over server-sent events. Separately, a self-contained JavaScript file drives the jackbox.tv join form; this plan proves it against an offline Vue fixture and a live room, but does not build the Android app that will inject it.

**Tech Stack:** Python 3.14, pytest 9 + pytest-asyncio (auto mode), Starlette + uvicorn (relay only), stdlib `urllib` (host side), Node + puppeteer-core + system Chromium (injection fixture check only).

**Spec:** `docs/superpowers/specs/2026-08-01-jackbox-auto-join-design.md`

## Global Constraints

- **The extractor gains no new runtime dependency.** `extractor/` runs inside `mitmdump` on the host at game-launch time. Use stdlib `urllib.request` in `asyncio.to_thread`, never `httpx`/`requests`/`aiohttp`.
- **All 56 existing tests must keep passing**, unmodified, at every commit.
- The `RoomCode:<CODE>` TCP contract on port **38469** is unchanged.
- Relay clients (publisher and CLI) **refuse any base URL not starting with `https://`**. TLS is terminated by the reverse proxy in front of the relay, so this is the only place the check can be enforced.
- **Room codes are never written to logs** anywhere in this plan.
- Freshness window: **120 seconds**. Pairing code TTL: **300 seconds**. Pairing code: **6 characters** from `ABCDEFGHJKLMNPQRSTUVWXYZ23456789` (no O/0/I/1 — it gets read off a screen and typed on a phone).
- `pytest.ini` sets `asyncio_mode = auto`, so async tests need **no** `@pytest.mark.asyncio` decorator, and `testpaths = tests`, so all Python tests live in `tests/`.
- Run tests with `./.venv/bin/python -m pytest` — `.venv/bin/activate.fish` does not reliably activate in this shell.

---

## File Structure

```
injection/
  package.json          Node dev deps for the fixture check only (puppeteer-core, vue)
  inject.js             The join script. The ONLY thing the future app injects.
  fixture.html          Offline Vue clone of the jackbox.tv join form
  test-fixture.mjs      Headless check of inject.js against fixture.html
relay/
  __init__.py
  state.py              Pure state machine: freshness, tokens, pairing, rate limiting
  tokens.py             Subscriber-token file persistence (the only disk write)
  app.py                Starlette ASGI layer over state.py
  requirements.txt      What the VPS needs
  README.md             Deployment instructions
extractor/
  relay_publisher.py    NEW — outbound sink, stdlib HTTP, never raises
  addon.py              MODIFIED — fan out to *sinks
  server.py             MODIFIED — one line: `critical = True`
pair.py                 NEW — host CLI that mints a pairing code
requirements-dev.txt    NEW — test-only deps (starlette, httpx, uvicorn)
tests/
  test_relay_state.py       NEW
  test_relay_app.py         NEW
  test_relay_publisher.py   NEW
  test_pair_cli.py          NEW
  test_addon.py             MODIFIED — fan-out tests appended
```

`relay/state.py` is deliberately free of HTTP and disk I/O so that nearly all relay behaviour is testable as plain function calls with an injected clock. `relay/app.py` stays thin enough that only a handful of integration tests are needed.

---

## Task 1: The injection script and its offline fixture

Proves the one claim the whole client design rests on: that assigning a value plus dispatching an `input` event actually moves a Vue `v-model`. Runs entirely offline against a fixture, so it needs no Jackbox game.

**Files:**
- Create: `injection/package.json`
- Create: `injection/inject.js`
- Create: `injection/fixture.html`
- Create: `injection/test-fixture.mjs`
- Modify: `.gitignore` (add `injection/node_modules/`)

**Interfaces:**
- Consumes: nothing.
- Produces: `window.jbvipJoin(code, name)` → `Promise<{ok: boolean, reason?: string}>`. This is the entire contract the Android app will use in the second plan. `reason` is one of `form-not-found`, `join-button-never-enabled`, `join-did-not-complete`, or the page's own visible error text.

- [ ] **Step 1: Create `injection/package.json`**

```json
{
  "name": "jbvip-injection",
  "private": true,
  "type": "module",
  "scripts": {
    "test": "node test-fixture.mjs"
  },
  "devDependencies": {
    "puppeteer-core": "^23.0.0",
    "vue": "^3.4.0"
  }
}
```

`puppeteer-core` (not `puppeteer`) is deliberate: it drives the Chromium already at `/usr/bin/chromium` instead of downloading its own ~150MB copy.

- [ ] **Step 2: Install the dev dependencies**

Run: `cd injection && npm install`
Expected: `node_modules/` appears containing `puppeteer-core` and `vue`.

- [ ] **Step 3: Add `injection/node_modules/` to `.gitignore`**

Append this line to `.gitignore`:

```
injection/node_modules/
```

- [ ] **Step 4: Write the fixture page** `injection/fixture.html`

This mirrors the real jackbox.tv join form: same element ids, Vue `v-model` bindings, a join button disabled until the form validates, and — critically — `v-if` removing the form from the DOM on success, which is exactly the signal `inject.js` uses to detect a completed join. Room code `FAIL` simulates a rejected join so the error path is covered too.

```html
<!doctype html>
<html>
<head><meta charset="utf-8"><title>jbvip injection fixture</title></head>
<body>
  <div id="app">
    <form v-if="!joined" @submit.prevent="join">
      <p class="warnings" v-if="error">{{ error }}</p>
      <label for="roomcode">Room Code</label>
      <input id="roomcode" v-model="code" maxlength="8" autocomplete="off">
      <label for="username">Name</label>
      <input id="username" v-model="name" maxlength="12" autocomplete="off">
      <button id="button-join" :disabled="!ready" type="submit">Play</button>
    </form>
    <p v-else id="joined">joined as {{ name }} in {{ code }}</p>
  </div>
  <script src="./node_modules/vue/dist/vue.global.prod.js"></script>
  <script>
    Vue.createApp({
      data() { return { code: '', name: '', joined: false, error: '' }; },
      computed: {
        ready() { return this.code.length === 4 && this.name.length > 0; }
      },
      methods: {
        join() {
          if (this.code === 'FAIL') { this.error = 'Room not found.'; return; }
          this.joined = true;
        }
      }
    }).mount('#app');
  </script>
</body>
</html>
```

- [ ] **Step 5: Write the injection script** `injection/inject.js`

```js
/* jbvip auto-join, injected into jackbox.tv.
 *
 * Sole contract with the client:
 *   window.jbvipJoin(code, name) -> Promise<{ok: boolean, reason?: string}>
 *
 * Selectors are ids verified against the shipped @connect bundle on 2026-08-01.
 * The inputs are Vue `modelValue`-bound: assigning `.value` alone does NOT
 * update Vue's model, so a bubbling `input` event must be dispatched. */
(function () {
  'use strict';

  var SEL_CODE = '#roomcode';
  var SEL_NAME = '#username';
  var SEL_JOIN = '#button-join';
  var ENABLE_TIMEOUT_MS = 3000;
  var JOIN_TIMEOUT_MS = 8000;
  var POLL_MS = 50;

  function setModelValue(el, value) {
    var desc = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), 'value');
    if (desc && desc.set) { desc.set.call(el, value); } else { el.value = value; }
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }

  function waitFor(predicate, timeoutMs) {
    return new Promise(function (resolve) {
      var deadline = Date.now() + timeoutMs;
      (function poll() {
        if (predicate()) { return resolve(true); }
        if (Date.now() > deadline) { return resolve(false); }
        setTimeout(poll, POLL_MS);
      })();
    });
  }

  function visibleFailureText() {
    var el = document.querySelector('.warnings') || document.querySelector('.status');
    var text = el && el.textContent ? el.textContent.trim() : '';
    return text || 'join-did-not-complete';
  }

  window.jbvipJoin = function (code, name) {
    var codeEl = document.querySelector(SEL_CODE);
    var nameEl = document.querySelector(SEL_NAME);
    var joinEl = document.querySelector(SEL_JOIN);
    if (!codeEl || !nameEl || !joinEl) {
      return Promise.resolve({ ok: false, reason: 'form-not-found' });
    }
    setModelValue(codeEl, code);
    setModelValue(nameEl, name);
    return waitFor(function () { return !joinEl.disabled; }, ENABLE_TIMEOUT_MS)
      .then(function (enabled) {
        if (!enabled) { return { ok: false, reason: 'join-button-never-enabled' }; }
        joinEl.click();
        return waitFor(function () {
          return document.querySelector(SEL_CODE) === null;
        }, JOIN_TIMEOUT_MS).then(function (joined) {
          return joined ? { ok: true } : { ok: false, reason: visibleFailureText() };
        });
      });
  };
})();
```

- [ ] **Step 6: Write the fixture check** `injection/test-fixture.mjs`

```js
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import puppeteer from 'puppeteer-core';

const here = dirname(fileURLToPath(import.meta.url));
const CHROMIUM = process.env.CHROMIUM_PATH || '/usr/bin/chromium';
const inject = readFileSync(join(here, 'inject.js'), 'utf8');
const FIXTURE = 'file://' + join(here, 'fixture.html');

let failures = 0;
function check(name, condition) {
  console.log(`${condition ? 'ok  ' : 'FAIL'} ${name}`);
  if (!condition) failures++;
}

async function freshPage(browser, url) {
  const page = await browser.newPage();
  if (url) { await page.goto(url); } else { await page.setContent('<p>no form here</p>'); }
  await page.evaluate(inject);
  return page;
}

const browser = await puppeteer.launch({
  executablePath: CHROMIUM,
  headless: true,
  args: ['--no-sandbox'],
});

try {
  let page = await freshPage(browser, FIXTURE);
  let result = await page.evaluate(() => window.jbvipJoin('WEWW', 'Michael'));
  const joinedText = await page.$eval('#joined', (el) => el.textContent);
  check('reports success', result.ok === true);
  check('vue model received the code', joinedText.includes('WEWW'));
  check('vue model received the name', joinedText.includes('Michael'));

  page = await freshPage(browser, FIXTURE);
  result = await page.evaluate(() => window.jbvipJoin('FAIL', 'Michael'));
  check('rejected join reports failure', result.ok === false);
  check('rejected join surfaces the page message', /Room not found/.test(result.reason));

  page = await freshPage(browser, null);
  result = await page.evaluate(() => window.jbvipJoin('WEWW', 'Michael'));
  check('missing form is detected', result.reason === 'form-not-found');
} finally {
  await browser.close();
}

process.exit(failures === 0 ? 0 : 1);
```

- [ ] **Step 7: Run the check and confirm it passes**

Run: `cd injection && npm test`
Expected: six `ok` lines and exit status 0. The rejected-join case takes ~8s because it waits out `JOIN_TIMEOUT_MS`; total runtime around 9 seconds.

If the "vue model received the code" check fails, **stop and report it** — that is the design-invalidating outcome the spec's risk-first ordering exists to catch, and it means the Android app needs a different approach to driving the form.

- [ ] **Step 8: Commit**

```bash
git add injection/ .gitignore
git commit -m "Add jackbox.tv join-injection script with an offline Vue fixture check"
```

---

## Task 2: Verify the injection against a live room (manual)

The fixture proves the technique; only a real lobby proves the selectors. This task is hand-run and produces knowledge plus any script corrections.

**Files:**
- Modify: `injection/inject.js` (only if the live run reveals a mismatch)
- Modify: `docs/superpowers/specs/2026-08-01-jackbox-auto-join-design.md` (append findings)

**Interfaces:**
- Consumes: `window.jbvipJoin` from Task 1.
- Produces: confirmation that the selectors match live jackbox.tv, and a recorded answer on whether the old Blobcast packs differ.

- [ ] **Step 1: Bring up the extractor and create a room**

```bash
sudo ./jbvip-redirect-up.sh
```

Then launch a Jackbox pack from Steam and create a room. Confirm the extractor sees the code:

```bash
nc 127.0.0.1 38469
```

Expected: `RoomCode:<CODE>`.

- [ ] **Step 2: Run the script against the live join page**

Open `https://jackbox.tv` in Chromium, open DevTools, paste the entire contents of `injection/inject.js` into the console, then run:

```js
await window.jbvipJoin('<CODE>', 'Michael')
```

Expected: `{ok: true}`, and the browser lands in the lobby.

- [ ] **Step 3: Confirm the VIP seat**

Check that the game's lobby screen shows this player as VIP (the crown / "start the game" control). Expected: yes, because nobody else has joined.

- [ ] **Step 4: Repeat against an old Blobcast pack**

Repeat steps 1–3 with Party Pack 1 or 5. These route to the Blobcast backend, and the spec flags that their join screen might differ.

Expected: `{ok: true}`. If the form differs, record exactly how — element ids, whether the code field is split into separate boxes — and add a second selector case to `inject.js`, then re-run `npm test` in `injection/` to confirm the fixture path still passes.

- [ ] **Step 5: Record the findings in the spec**

Append a short section to `docs/superpowers/specs/2026-08-01-jackbox-auto-join-design.md` under a new heading `## 12. Live verification (2026-08-01)` stating: which packs were tested, whether the selectors held, whether VIP was obtained, and any script changes made. Replace the corresponding bullet in `## 10. Open risks` with the answer.

- [ ] **Step 6: Commit**

```bash
git add injection/ docs/superpowers/specs/2026-08-01-jackbox-auto-join-design.md
git commit -m "Verify join injection against live rooms and record findings"
```

---

## Task 3: Relay state machine (pure, TDD)

All of the relay's decisions — is this token valid, is this code still fresh, is this pairing code spent — with no HTTP and no disk, so they can be tested as ordinary function calls with an injected clock.

**Files:**
- Create: `relay/__init__.py` (empty)
- Create: `relay/state.py`
- Test: `tests/test_relay_state.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `FRESH_SECONDS: float = 120.0`, `PAIRING_TTL_SECONDS: float = 300.0`, `PAIRING_CODE_LENGTH: int = 6`, `PAIRING_ALPHABET: str`
  - `CodeRecord(code: str, ts: float)` — frozen dataclass
  - `RateLimiter(limit: int, window_seconds: float, now=time.monotonic)` with `allow() -> bool`
  - `RelayState(publisher_token: str, subscriber_tokens: Iterable[str] = (), now=time.time)` with `is_publisher(token) -> bool`, `is_subscriber(token) -> bool`, `subscriber_tokens -> set[str]` (property), `publish(code: str, ts: float) -> bool`, `current_code() -> CodeRecord | None`, `mint_pairing_code() -> str`, `redeem_pairing_code(code: str) -> str | None`

- [ ] **Step 1: Create the empty package file**

```bash
touch relay/__init__.py
```

- [ ] **Step 2: Write the failing tests** `tests/test_relay_state.py`

```python
import pytest

from relay.state import (
    FRESH_SECONDS,
    PAIRING_ALPHABET,
    PAIRING_CODE_LENGTH,
    RateLimiter,
    RelayState,
)


class Clock:
    """A hand-cranked clock so no test ever sleeps."""

    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_publish_then_current_code_returns_it():
    state = RelayState("pub", now=Clock())
    state.publish("WEWW", 1000.0)
    record = state.current_code()
    assert record.code == "WEWW"
    assert record.ts == 1000.0


def test_current_code_is_none_before_anything_is_published():
    assert RelayState("pub", now=Clock()).current_code() is None


def test_current_code_is_none_once_stale():
    clock = Clock(1000.0)
    state = RelayState("pub", now=clock)
    state.publish("WEWW", 1000.0)
    clock.t = 1000.0 + FRESH_SECONDS + 1
    assert state.current_code() is None


def test_current_code_survives_right_up_to_the_window():
    clock = Clock(1000.0)
    state = RelayState("pub", now=clock)
    state.publish("WEWW", 1000.0)
    clock.t = 1000.0 + FRESH_SECONDS
    assert state.current_code().code == "WEWW"


def test_publish_reports_whether_the_code_changed():
    state = RelayState("pub", now=Clock())
    assert state.publish("WEWW", 1000.0) is True
    assert state.publish("WEWW", 1001.0) is False
    assert state.publish("ZZZZ", 1002.0) is True


@pytest.mark.parametrize("bad", ["", "A", "TOOMANYCHARS", "AB CD", "ABC!", "ＡＢＣＤ", None, 42])
def test_publish_rejects_implausible_codes(bad):
    state = RelayState("pub", now=Clock())
    with pytest.raises(ValueError):
        state.publish(bad, 1000.0)


def test_publisher_token_recognised_and_others_rejected():
    state = RelayState("pub", now=Clock())
    assert state.is_publisher("pub") is True
    assert state.is_publisher("nope") is False
    assert state.is_publisher("") is False
    assert state.is_publisher(None) is False


def test_subscriber_tokens_recognised_and_others_rejected():
    state = RelayState("pub", ["sub-a", "sub-b"], now=Clock())
    assert state.is_subscriber("sub-a") is True
    assert state.is_subscriber("sub-b") is True
    assert state.is_subscriber("pub") is False
    assert state.is_subscriber("") is False


def test_minted_pairing_code_has_the_documented_shape():
    code = RelayState("pub", now=Clock()).mint_pairing_code()
    assert len(code) == PAIRING_CODE_LENGTH
    assert all(character in PAIRING_ALPHABET for character in code)


def test_redeeming_a_pairing_code_yields_a_working_subscriber_token():
    state = RelayState("pub", now=Clock())
    token = state.redeem_pairing_code(state.mint_pairing_code())
    assert token
    assert state.is_subscriber(token)
    assert token in state.subscriber_tokens


def test_a_pairing_code_can_only_be_redeemed_once():
    state = RelayState("pub", now=Clock())
    code = state.mint_pairing_code()
    assert state.redeem_pairing_code(code)
    assert state.redeem_pairing_code(code) is None


def test_an_expired_pairing_code_is_refused():
    clock = Clock(1000.0)
    state = RelayState("pub", now=clock)
    code = state.mint_pairing_code()
    clock.t = 1000.0 + 301
    assert state.redeem_pairing_code(code) is None


def test_an_unknown_pairing_code_is_refused():
    assert RelayState("pub", now=Clock()).redeem_pairing_code("ZZZZZZ") is None


def test_empty_publisher_token_is_rejected_at_construction():
    with pytest.raises(ValueError):
        RelayState("")


def test_subscriber_tokens_property_does_not_expose_internal_state():
    state = RelayState("pub", ["sub"], now=Clock())
    state.subscriber_tokens.add("smuggled")
    assert state.is_subscriber("smuggled") is False


def test_rate_limiter_allows_up_to_the_limit_then_blocks():
    clock = Clock(0.0)
    limiter = RateLimiter(limit=3, window_seconds=60.0, now=clock)
    assert [limiter.allow() for _ in range(4)] == [True, True, True, False]


def test_rate_limiter_recovers_once_the_window_passes():
    clock = Clock(0.0)
    limiter = RateLimiter(limit=1, window_seconds=60.0, now=clock)
    assert limiter.allow() is True
    assert limiter.allow() is False
    clock.t = 61.0
    assert limiter.allow() is True
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_relay_state.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'relay'`.

- [ ] **Step 4: Write the implementation** `relay/state.py`

```python
"""Relay decisions with no HTTP and no disk, so they test as plain calls.

The room-code plausibility check duplicates `extractor.roomcode.looks_like_code`
on purpose: the relay deploys to a VPS on its own and must not import the
extractor package.
"""
import secrets
import time
from dataclasses import dataclass
from typing import Iterable

FRESH_SECONDS = 120.0
PAIRING_TTL_SECONDS = 300.0
PAIRING_CODE_LENGTH = 6
# No O/0 or I/1 — this gets read off a screen and typed on a phone.
PAIRING_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


@dataclass(frozen=True)
class CodeRecord:
    code: str
    ts: float


def _looks_like_code(code) -> bool:
    return (
        isinstance(code, str)
        and 2 <= len(code) <= 8
        and code.isascii()
        and code.isalnum()
    )


class RateLimiter:
    """Sliding-window counter. `now` is injected so tests need not sleep."""

    def __init__(self, limit: int, window_seconds: float, now=time.monotonic):
        self._limit = limit
        self._window = window_seconds
        self._now = now
        self._hits: list[float] = []

    def allow(self) -> bool:
        cutoff = self._now() - self._window
        self._hits = [hit for hit in self._hits if hit > cutoff]
        if len(self._hits) >= self._limit:
            return False
        self._hits.append(self._now())
        return True


class RelayState:
    def __init__(
        self,
        publisher_token: str,
        subscriber_tokens: Iterable[str] = (),
        now=time.time,
    ):
        if not publisher_token:
            raise ValueError("publisher_token must not be empty")
        self._publisher_token = publisher_token
        self._subscriber_tokens: set[str] = set(subscriber_tokens)
        self._now = now
        self._record: CodeRecord | None = None
        self._pairing_codes: dict[str, float] = {}

    # --- authentication ---
    def is_publisher(self, token) -> bool:
        if not isinstance(token, str) or not token:
            return False
        return secrets.compare_digest(token, self._publisher_token)

    def is_subscriber(self, token) -> bool:
        if not isinstance(token, str) or not token:
            return False
        return any(
            secrets.compare_digest(token, known) for known in self._subscriber_tokens
        )

    @property
    def subscriber_tokens(self) -> set[str]:
        return set(self._subscriber_tokens)

    # --- room codes ---
    def publish(self, code: str, ts: float) -> bool:
        """Store the code. Returns True when this changed the current code."""
        if not _looks_like_code(code):
            raise ValueError("implausible room code")
        changed = self._record is None or self._record.code != code
        self._record = CodeRecord(code=code, ts=float(ts))
        return changed

    def current_code(self) -> CodeRecord | None:
        """The current code, or None when absent or older than the window."""
        if self._record is None:
            return None
        if self._now() - self._record.ts > FRESH_SECONDS:
            return None
        return self._record

    # --- pairing ---
    def mint_pairing_code(self) -> str:
        code = "".join(
            secrets.choice(PAIRING_ALPHABET) for _ in range(PAIRING_CODE_LENGTH)
        )
        self._pairing_codes[code] = self._now() + PAIRING_TTL_SECONDS
        return code

    def redeem_pairing_code(self, code: str) -> str | None:
        """Burn a valid pairing code and return a fresh subscriber token."""
        self._expire_pairing_codes()
        if not isinstance(code, str) or code not in self._pairing_codes:
            return None
        del self._pairing_codes[code]
        token = secrets.token_urlsafe(32)
        self._subscriber_tokens.add(token)
        return token

    def _expire_pairing_codes(self) -> None:
        now = self._now()
        expired = [code for code, exp in self._pairing_codes.items() if exp <= now]
        for code in expired:
            del self._pairing_codes[code]
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_relay_state.py -q`
Expected: 24 passed (17 test functions, one of which is parametrized 8 ways).

- [ ] **Step 6: Run the whole suite**

Run: `./.venv/bin/python -m pytest -q`
Expected: 80 passed (56 existing + 24 new).

- [ ] **Step 7: Commit**

```bash
git add relay/__init__.py relay/state.py tests/test_relay_state.py
git commit -m "Add pure relay state machine: freshness, tokens, pairing, rate limiting"
```

---

## Task 4: Subscriber-token persistence (TDD)

The single thing the relay writes to disk. Atomic and mode-0600, because a torn write here means re-pairing every phone.

**Files:**
- Create: `relay/tokens.py`
- Test: `tests/test_relay_tokens.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `load_tokens(path) -> list[str]`, `save_tokens(path, tokens: Iterable[str]) -> None`

- [ ] **Step 1: Write the failing tests** `tests/test_relay_tokens.py`

```python
import os

from relay.tokens import load_tokens, save_tokens


def test_loading_a_missing_file_yields_no_tokens(tmp_path):
    assert load_tokens(tmp_path / "absent.json") == []


def test_saved_tokens_round_trip(tmp_path):
    path = tmp_path / "tokens.json"
    save_tokens(path, ["b-token", "a-token"])
    assert load_tokens(path) == ["a-token", "b-token"]


def test_saving_creates_missing_parent_directories(tmp_path):
    path = tmp_path / "nested" / "deeper" / "tokens.json"
    save_tokens(path, ["token"])
    assert load_tokens(path) == ["token"]


def test_saved_file_is_not_world_readable(tmp_path):
    path = tmp_path / "tokens.json"
    save_tokens(path, ["token"])
    assert os.stat(path).st_mode & 0o077 == 0


def test_corrupt_file_yields_no_tokens_rather_than_raising(tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text("{not json at all")
    assert load_tokens(path) == []


def test_non_string_entries_are_discarded(tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text('{"subscriber_tokens": ["good", 42, null]}')
    assert load_tokens(path) == ["good"]


def test_saving_replaces_previous_contents(tmp_path):
    path = tmp_path / "tokens.json"
    save_tokens(path, ["old"])
    save_tokens(path, ["new"])
    assert load_tokens(path) == ["new"]


def test_no_temporary_files_are_left_behind(tmp_path):
    path = tmp_path / "tokens.json"
    save_tokens(path, ["token"])
    assert [p.name for p in tmp_path.iterdir()] == ["tokens.json"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_relay_tokens.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'relay.tokens'`.

- [ ] **Step 3: Write the implementation** `relay/tokens.py`

```python
"""Subscriber-token persistence — the relay's only disk write.

Written atomically via a temp file plus os.replace so an interrupted save
cannot leave a truncated token list, which would silently unpair every phone.
"""
import json
import os
import tempfile
from pathlib import Path
from typing import Iterable


def load_tokens(path) -> list[str]:
    path = Path(path)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (ValueError, OSError):
        return []
    if not isinstance(data, dict):
        return []
    tokens = data.get("subscriber_tokens")
    if not isinstance(tokens, list):
        return []
    return sorted(token for token in tokens if isinstance(token, str))


def save_tokens(path, tokens: Iterable[str]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".tokens-", suffix=".json"
    )
    try:
        with os.fdopen(handle, "w") as stream:
            json.dump({"subscriber_tokens": sorted(tokens)}, stream)
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    except BaseException:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
        raise
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_relay_tokens.py -q`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add relay/tokens.py tests/test_relay_tokens.py
git commit -m "Add atomic subscriber-token persistence for the relay"
```

---

## Task 5: Relay HTTP layer (TDD)

A thin Starlette app over `RelayState`, with SSE fan-out to subscribers.

**Files:**
- Create: `relay/app.py`
- Create: `relay/requirements.txt`
- Create: `requirements-dev.txt`
- Test: `tests/test_relay_app.py`

**Interfaces:**
- Consumes: `RelayState`, `RateLimiter` (Task 3); `load_tokens`, `save_tokens` (Task 4).
- Produces: `build_app(state: RelayState, tokens_path, *, publish_limiter=None, pair_limiter=None) -> Starlette` (used by tests), `create_app() -> Starlette` (used by uvicorn, reads env). HTTP surface: `POST /publish`, `GET /subscribe`, `POST /pairing-codes`, `POST /pair`.

The limiters are injectable purely so the rate-limit wiring can be tested with a limit of 1 instead of firing 61 requests.

- [ ] **Step 1: Create `requirements-dev.txt`**

```
-r requirements.txt
starlette>=0.37
uvicorn>=0.30
httpx>=0.27
```

- [ ] **Step 2: Create `relay/requirements.txt`**

```
starlette>=0.37
uvicorn[standard]>=0.30
```

- [ ] **Step 3: Install the dev dependencies**

Run: `./.venv/bin/pip install -r requirements-dev.txt`
Expected: starlette, uvicorn and httpx install successfully.

- [ ] **Step 4: Write the failing tests** `tests/test_relay_app.py`

```python
import asyncio
import json

import httpx
import pytest

from relay.app import build_app
from relay.state import RateLimiter, RelayState


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def make(tmp_path, clock=None, subscribers=("sub",), **limiters):
    state = RelayState("pub", subscribers, now=clock or Clock())
    return state, build_app(state, tmp_path / "tokens.json", **limiters)


def client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://relay.test"
    )


async def first_event(response, timeout=3.0):
    async def read():
        async for line in response.aiter_lines():
            if line.startswith("data: "):
                return json.loads(line[len("data: "):])
        return None

    return await asyncio.wait_for(read(), timeout)


async def test_publish_requires_the_publisher_token(tmp_path):
    _, app = make(tmp_path)
    async with client(app) as http:
        response = await http.post(
            "/publish",
            json={"code": "WEWW", "ts": 1000.0},
            headers={"Authorization": "Bearer wrong"},
        )
    assert response.status_code == 401


async def test_publish_accepts_a_valid_code(tmp_path):
    state, app = make(tmp_path)
    async with client(app) as http:
        response = await http.post(
            "/publish",
            json={"code": "WEWW", "ts": 1000.0},
            headers={"Authorization": "Bearer pub"},
        )
    assert response.status_code == 204
    assert state.current_code().code == "WEWW"


async def test_publish_rejects_an_implausible_code(tmp_path):
    _, app = make(tmp_path)
    async with client(app) as http:
        response = await http.post(
            "/publish",
            json={"code": "not a code", "ts": 1000.0},
            headers={"Authorization": "Bearer pub"},
        )
    assert response.status_code == 400


async def test_subscribe_requires_a_subscriber_token(tmp_path):
    _, app = make(tmp_path)
    async with client(app) as http:
        response = await http.get(
            "/subscribe", headers={"Authorization": "Bearer pub"}
        )
    assert response.status_code == 401


async def test_subscriber_receives_the_current_code_on_connect(tmp_path):
    state, app = make(tmp_path)
    state.publish("WEWW", 1000.0)
    async with client(app) as http:
        async with http.stream(
            "GET", "/subscribe", headers={"Authorization": "Bearer sub"}
        ) as response:
            assert response.status_code == 200
            assert await first_event(response) == {"code": "WEWW", "ts": 1000.0}


async def test_subscriber_is_not_sent_a_stale_code_on_connect(tmp_path):
    clock = Clock(1000.0)
    state, app = make(tmp_path, clock=clock)
    state.publish("WEWW", 1000.0)
    clock.t = 2000.0
    async with client(app) as http:
        async with http.stream(
            "GET", "/subscribe", headers={"Authorization": "Bearer sub"}
        ) as response:
            with pytest.raises(asyncio.TimeoutError):
                await first_event(response, timeout=0.5)


async def test_subscriber_receives_a_code_published_after_connecting(tmp_path):
    _, app = make(tmp_path)
    async with client(app) as http:
        async with http.stream(
            "GET", "/subscribe", headers={"Authorization": "Bearer sub"}
        ) as response:
            await http.post(
                "/publish",
                json={"code": "ZZZZ", "ts": 1000.0},
                headers={"Authorization": "Bearer pub"},
            )
            assert await first_event(response) == {"code": "ZZZZ", "ts": 1000.0}


async def test_minting_a_pairing_code_requires_the_publisher_token(tmp_path):
    _, app = make(tmp_path)
    async with client(app) as http:
        response = await http.post(
            "/pairing-codes", headers={"Authorization": "Bearer sub"}
        )
    assert response.status_code == 401


async def test_pairing_round_trip_yields_a_usable_subscriber_token(tmp_path):
    state, app = make(tmp_path, subscribers=())
    async with client(app) as http:
        minted = await http.post(
            "/pairing-codes", headers={"Authorization": "Bearer pub"}
        )
        pairing_code = minted.json()["pairing_code"]
        paired = await http.post("/pair", json={"pairing_code": pairing_code})
        token = paired.json()["subscriber_token"]

        assert state.is_subscriber(token)
        response = await http.get(
            "/subscribe", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200


async def test_pairing_persists_the_token_to_disk(tmp_path):
    from relay.tokens import load_tokens

    _, app = make(tmp_path, subscribers=())
    async with client(app) as http:
        minted = await http.post(
            "/pairing-codes", headers={"Authorization": "Bearer pub"}
        )
        paired = await http.post(
            "/pair", json={"pairing_code": minted.json()["pairing_code"]}
        )
    assert load_tokens(tmp_path / "tokens.json") == [
        paired.json()["subscriber_token"]
    ]


async def test_an_unknown_pairing_code_is_refused(tmp_path):
    _, app = make(tmp_path)
    async with client(app) as http:
        response = await http.post("/pair", json={"pairing_code": "ZZZZZZ"})
    assert response.status_code == 400


async def test_publishing_is_rate_limited(tmp_path):
    _, app = make(
        tmp_path, publish_limiter=RateLimiter(limit=1, window_seconds=60.0)
    )
    body = {"code": "WEWW", "ts": 1000.0}
    headers = {"Authorization": "Bearer pub"}
    async with client(app) as http:
        first = await http.post("/publish", json=body, headers=headers)
        second = await http.post("/publish", json=body, headers=headers)
    assert first.status_code == 204
    assert second.status_code == 429


async def test_pairing_attempts_are_rate_limited(tmp_path):
    _, app = make(tmp_path, pair_limiter=RateLimiter(limit=1, window_seconds=60.0))
    async with client(app) as http:
        first = await http.post("/pair", json={"pairing_code": "ZZZZZZ"})
        second = await http.post("/pair", json={"pairing_code": "ZZZZZZ"})
    assert first.status_code == 400
    assert second.status_code == 429
```

- [ ] **Step 5: Run the tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_relay_app.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'relay.app'`.

- [ ] **Step 6: Write the implementation** `relay/app.py`

```python
"""ASGI relay: takes room codes from the host PC, pushes them to subscribers.

Thin by design — every decision lives in relay.state. Room codes are never
logged here or anywhere else in the relay.
"""
import asyncio
import json
import os
import time

from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from relay.state import RateLimiter, RelayState
from relay.tokens import load_tokens, save_tokens

KEEPALIVE_SECONDS = 20.0
DEFAULT_TOKENS_PATH = "/var/lib/jbvip/tokens.json"


def _bearer(request) -> str:
    header = request.headers.get("authorization", "")
    prefix = "Bearer "
    return header[len(prefix):] if header.startswith(prefix) else ""


class Relay:
    def __init__(
        self,
        state: RelayState,
        tokens_path,
        *,
        publish_limiter=None,
        pair_limiter=None,
    ):
        self.state = state
        self.tokens_path = tokens_path
        self._subscribers: set[asyncio.Queue] = set()
        self._publish_limiter = publish_limiter or RateLimiter(
            limit=60, window_seconds=60.0
        )
        self._pair_limiter = pair_limiter or RateLimiter(
            limit=10, window_seconds=60.0
        )

    def _fan_out(self, payload: dict) -> None:
        for queue in list(self._subscribers):
            queue.put_nowait(payload)

    async def publish(self, request):
        if not self.state.is_publisher(_bearer(request)):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if not self._publish_limiter.allow():
            return JSONResponse({"error": "rate limited"}, status_code=429)
        try:
            body = await request.json()
            code = body["code"]
            ts = float(body.get("ts") or time.time())
        except (ValueError, TypeError, KeyError):
            return JSONResponse({"error": "bad request"}, status_code=400)
        try:
            changed = self.state.publish(code, ts)
        except ValueError:
            return JSONResponse({"error": "bad request"}, status_code=400)
        if changed:
            self._fan_out({"code": code, "ts": ts})
        return Response(status_code=204)

    async def subscribe(self, request):
        if not self.state.is_subscriber(_bearer(request)):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(queue)
        current = self.state.current_code()
        if current is not None:
            queue.put_nowait({"code": current.code, "ts": current.ts})

        async def stream():
            try:
                while True:
                    try:
                        payload = await asyncio.wait_for(
                            queue.get(), KEEPALIVE_SECONDS
                        )
                    except TimeoutError:
                        yield b": keepalive\n\n"
                        continue
                    yield f"data: {json.dumps(payload)}\n\n".encode()
            finally:
                self._subscribers.discard(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    async def mint_pairing_code(self, request):
        if not self.state.is_publisher(_bearer(request)):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse({"pairing_code": self.state.mint_pairing_code()})

    async def pair(self, request):
        if not self._pair_limiter.allow():
            return JSONResponse({"error": "rate limited"}, status_code=429)
        try:
            pairing_code = (await request.json())["pairing_code"]
        except (ValueError, TypeError, KeyError):
            return JSONResponse({"error": "bad request"}, status_code=400)
        token = self.state.redeem_pairing_code(pairing_code)
        if token is None:
            return JSONResponse(
                {"error": "invalid or expired pairing code"}, status_code=400
            )
        save_tokens(self.tokens_path, self.state.subscriber_tokens)
        return JSONResponse({"subscriber_token": token})


def build_app(
    state: RelayState, tokens_path, *, publish_limiter=None, pair_limiter=None
) -> Starlette:
    relay = Relay(
        state,
        tokens_path,
        publish_limiter=publish_limiter,
        pair_limiter=pair_limiter,
    )
    app = Starlette(
        routes=[
            Route("/publish", relay.publish, methods=["POST"]),
            Route("/subscribe", relay.subscribe, methods=["GET"]),
            Route("/pairing-codes", relay.mint_pairing_code, methods=["POST"]),
            Route("/pair", relay.pair, methods=["POST"]),
        ]
    )
    app.state.relay = relay
    return app


def create_app() -> Starlette:
    publisher_token = os.environ.get("JBVIP_PUBLISHER_TOKEN", "")
    if not publisher_token:
        raise RuntimeError("JBVIP_PUBLISHER_TOKEN is not set")
    tokens_path = os.environ.get("JBVIP_TOKENS_FILE", DEFAULT_TOKENS_PATH)
    state = RelayState(publisher_token, load_tokens(tokens_path))
    return build_app(state, tokens_path)
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_relay_app.py -q`
Expected: 13 passed.

- [ ] **Step 8: Run the whole suite**

Run: `./.venv/bin/python -m pytest -q`
Expected: 101 passed (80 + 8 token tests + 13 app tests).

- [ ] **Step 9: Commit**

```bash
git add relay/app.py relay/requirements.txt requirements-dev.txt tests/test_relay_app.py
git commit -m "Add relay HTTP layer with SSE fan-out and pairing endpoints"
```

---

## Task 6: Relay publisher sink (TDD)

The host-side sink that forwards codes to the relay. Best-effort by contract: a dead relay must never disturb a running game.

**Files:**
- Create: `extractor/relay_publisher.py`
- Test: `tests/test_relay_publisher.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (it talks to the relay over HTTP, not in-process).
- Produces: `RelayPublisher(base_url: str, token: str, *, post=..., attempts=3, backoff=0.5, sleep=asyncio.sleep, now=time.time)` with class attribute `critical = False`, and the sink shape `async start() -> None` / `async set_code(code: str) -> None`.

- [ ] **Step 1: Write the failing tests** `tests/test_relay_publisher.py`

```python
import pytest

from extractor.relay_publisher import RelayPublisher


class RecordingPost:
    """Stands in for the urllib POST so no test touches the network."""

    def __init__(self, failures=0, exception=OSError("relay down")):
        self.calls = []
        self.failures = failures
        self.exception = exception

    def __call__(self, url, token, payload):
        self.calls.append((url, token, payload))
        if len(self.calls) <= self.failures:
            raise self.exception


async def no_sleep(_seconds):
    return None


def make(post, **kwargs):
    return RelayPublisher(
        "https://relay.example/",
        "pub-token",
        post=post,
        sleep=no_sleep,
        now=lambda: 1000.0,
        **kwargs,
    )


def test_a_plaintext_base_url_is_refused():
    with pytest.raises(ValueError):
        RelayPublisher("http://relay.example", "pub-token")


def test_an_empty_token_is_refused():
    with pytest.raises(ValueError):
        RelayPublisher("https://relay.example", "")


def test_the_publisher_is_not_a_critical_sink():
    assert RelayPublisher.critical is False


async def test_set_code_posts_the_code_to_the_publish_endpoint():
    post = RecordingPost()
    await make(post).set_code("WEWW")
    url, token, payload = post.calls[0]
    assert url == "https://relay.example/publish"
    assert token == "pub-token"
    assert payload == {"code": "WEWW", "ts": 1000.0}


async def test_a_trailing_slash_does_not_double_up():
    post = RecordingPost()
    await make(post).set_code("WEWW")
    assert post.calls[0][0] == "https://relay.example/publish"


async def test_a_transient_failure_is_retried_and_then_succeeds():
    post = RecordingPost(failures=2)
    await make(post).set_code("WEWW")
    assert len(post.calls) == 3


async def test_exhausting_every_attempt_never_raises():
    post = RecordingPost(failures=99)
    await make(post).set_code("WEWW")
    assert len(post.calls) == 3


async def test_the_attempt_count_is_configurable():
    post = RecordingPost(failures=99)
    await make(post, attempts=5).set_code("WEWW")
    assert len(post.calls) == 5


async def test_start_is_a_no_op():
    assert await make(RecordingPost()).start() is None


async def test_the_room_code_is_never_logged(caplog):
    post = RecordingPost(failures=99)
    with caplog.at_level("DEBUG"):
        await make(post).set_code("SECRET1")
    assert "SECRET1" not in caplog.text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_relay_publisher.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'extractor.relay_publisher'`.

- [ ] **Step 3: Write the implementation** `extractor/relay_publisher.py`

```python
"""Forward room codes to the relay. Best-effort: never raises into mitmproxy.

Uses stdlib urllib on a worker thread so the extractor picks up no new runtime
dependency — it runs inside mitmdump on the host at game-launch time.
"""
import asyncio
import json
import logging
import time
import urllib.request

logger = logging.getLogger(__name__)

DEFAULT_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = 0.5
REQUEST_TIMEOUT_SECONDS = 5.0


def _urllib_post(url: str, token: str, payload: dict) -> None:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS):
        pass


class RelayPublisher:
    """A code sink that forwards to the relay, mirroring RoomCodeServer's shape.

    Not critical: if it cannot start or cannot deliver, the extractor carries on
    and the LAN TCP server keeps serving. Losing the relay must never interrupt
    a game in progress.
    """

    critical = False

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        post=_urllib_post,
        attempts: int = DEFAULT_ATTEMPTS,
        backoff: float = DEFAULT_BACKOFF_SECONDS,
        sleep=asyncio.sleep,
        now=time.time,
    ):
        if not base_url.startswith("https://"):
            raise ValueError(
                "relay base_url must be https:// — TLS is terminated in front "
                "of the relay, so this is the only place it can be enforced"
            )
        if not token:
            raise ValueError("relay token must not be empty")
        self._url = base_url.rstrip("/") + "/publish"
        self._token = token
        self._post = post
        self._attempts = attempts
        self._backoff = backoff
        self._sleep = sleep
        self._now = now

    async def start(self) -> None:
        return None

    async def set_code(self, code: str) -> None:
        payload = {"code": code, "ts": self._now()}
        delay = self._backoff
        for attempt in range(1, self._attempts + 1):
            try:
                await asyncio.to_thread(self._post, self._url, self._token, payload)
                return
            except Exception as exc:
                if attempt >= self._attempts:
                    # Deliberately does not log the code itself.
                    logger.warning(
                        "relay publish failed after %d attempts: %s",
                        self._attempts,
                        exc,
                    )
                    return
                await self._sleep(delay)
                delay *= 2
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_relay_publisher.py -q`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add extractor/relay_publisher.py tests/test_relay_publisher.py
git commit -m "Add best-effort relay publisher sink to the extractor"
```

---

## Task 7: Fan the addon out to multiple sinks (TDD)

`RoomCodeExtractor` currently holds one `RoomCodeServer`. Widen it to any number of sinks so the LAN server and the relay publisher sit side by side.

A `*sinks` signature is used deliberately: all 20 existing `RoomCodeExtractor(RoomCodeServer(port=0))` call sites in `tests/test_addon.py` keep working untouched.

**Files:**
- Modify: `extractor/server.py` (add one class attribute)
- Modify: `extractor/addon.py:25-39,93-124`
- Test: `tests/test_addon.py` (append; do not alter existing tests)

**Interfaces:**
- Consumes: `RelayPublisher` (Task 6); the existing `RoomCodeServer`.
- Produces: `RoomCodeExtractor(*sinks)` with `self.sinks: list`. A sink is anything with `async start()`, `async set_code(code)` and a `critical: bool` class attribute. `RoomCodeServer.critical = True` (a failure to bind is fatal, preserving existing behaviour); `RelayPublisher.critical = False`.

- [ ] **Step 1: Write the failing tests — append to `tests/test_addon.py`**

```python
# --- sink fan-out ---


class FakeSink:
    critical = False

    def __init__(self, fail_on_start=False, fail_on_set=False):
        self.started = False
        self.codes = []
        self._fail_on_start = fail_on_start
        self._fail_on_set = fail_on_set

    async def start(self):
        if self._fail_on_start:
            raise OSError("cannot start")
        self.started = True

    async def set_code(self, code):
        if self._fail_on_set:
            raise RuntimeError("cannot accept")
        self.codes.append(code)


class FakeCriticalSink(FakeSink):
    critical = True


async def test_a_code_reaches_every_sink():
    first, second = FakeSink(), FakeSink()
    ext = RoomCodeExtractor(first, second)
    ext._broadcast("WEWW")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert first.codes == ["WEWW"]
    assert second.codes == ["WEWW"]


async def test_one_failing_sink_does_not_starve_the_others():
    broken, healthy = FakeSink(fail_on_set=True), FakeSink()
    ext = RoomCodeExtractor(broken, healthy)
    ext._broadcast("WEWW")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert healthy.codes == ["WEWW"]


async def test_running_starts_every_sink():
    first, second = FakeSink(), FakeSink()
    await RoomCodeExtractor(first, second).running()
    assert first.started and second.started


async def test_a_non_critical_sink_failing_to_start_is_survivable():
    broken, healthy = FakeSink(fail_on_start=True), FakeSink()
    await RoomCodeExtractor(broken, healthy).running()
    assert healthy.started


async def test_a_critical_sink_failing_to_start_is_fatal(monkeypatch):
    shutdowns = []

    class FakeMaster:
        def shutdown(self):
            shutdowns.append(True)

    monkeypatch.setattr(addon_module.ctx, "master", FakeMaster(), raising=False)
    with pytest.raises(OSError):
        await RoomCodeExtractor(FakeCriticalSink(fail_on_start=True)).running()
    assert shutdowns == [True]


def test_the_lan_server_is_a_critical_sink():
    assert RoomCodeServer.critical is True


def test_constructing_with_no_sinks_is_refused():
    with pytest.raises(ValueError):
        RoomCodeExtractor()
```

These need three imports at the top of `tests/test_addon.py`. Add whichever are not already present:

```python
import pytest

from extractor import addon as addon_module
```

(`asyncio` and `RoomCodeExtractor`/`RoomCodeServer` are already imported by the existing tests.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_addon.py -q`
Expected: the new tests fail — `TypeError` about too many positional arguments, plus `AttributeError` for `RoomCodeServer.critical`.

- [ ] **Step 3: Mark the LAN server critical in `extractor/server.py`**

Add the class attribute immediately inside `class RoomCodeServer:`, above `__init__`:

```python
class RoomCodeServer:
    # A failure to bind is fatal: a silently unbound local server is exactly
    # the bug the shutdown-on-bind-failure behaviour was added to prevent.
    critical = True

    def __init__(self, port: int = 38469, host: str = "0.0.0.0"):
```

- [ ] **Step 4: Widen the constructor in `extractor/addon.py`**

Replace `__init__` (currently lines 26-28):

```python
    def __init__(self, *sinks):
        if not sinks:
            raise ValueError("RoomCodeExtractor requires at least one sink")
        self.sinks = list(sinks)
        self._tasks: set[asyncio.Task] = set()
```

- [ ] **Step 5: Fan `_broadcast` out across the sinks**

Replace `_broadcast` (currently lines 93-95):

```python
    def _broadcast(self, code: str | None) -> None:
        if not code:
            return
        for sink in self.sinks:
            self._spawn(self._set_code_safely(sink, code))

    async def _set_code_safely(self, sink, code: str) -> None:
        try:
            await sink.set_code(code)
        except Exception:
            # Deliberately does not log the code itself.
            logger.exception("sink %s rejected the room code", type(sink).__name__)
```

- [ ] **Step 6: Start every sink in `running`**

Replace `running` (currently lines 98-107):

```python
    async def running(self):
        for sink in self.sinks:
            try:
                await sink.start()
            except Exception as exc:
                if getattr(sink, "critical", False):
                    logger.error(
                        "critical sink %s failed to start (%s); shutting down.",
                        type(sink).__name__,
                        exc,
                    )
                    ctx.master.shutdown()
                    raise
                logger.warning(
                    "sink %s failed to start (%s); continuing without it.",
                    type(sink).__name__,
                    exc,
                )
```

- [ ] **Step 7: Build the sink list from the environment**

Replace the final line of `extractor/addon.py` (`addons = [RoomCodeExtractor(RoomCodeServer(port=38469))]`):

```python
def _build_sinks():
    """The LAN server always; the relay publisher when it is configured."""
    sinks = [RoomCodeServer(port=38469)]
    base_url = os.environ.get("JBVIP_RELAY_URL", "")
    token = os.environ.get("JBVIP_RELAY_TOKEN", "")
    if base_url and token:
        try:
            sinks.append(RelayPublisher(base_url, token))
        except ValueError as exc:
            logger.error("relay publishing disabled: %s", exc)
    return sinks


addons = [RoomCodeExtractor(*_build_sinks())]
```

Add the two imports this needs to the top of the file — `import os` alongside the other stdlib imports, and:

```python
from extractor.relay_publisher import RelayPublisher
```

- [ ] **Step 8: Run the addon tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_addon.py -q`
Expected: all pass — the 20 pre-existing tests plus 7 new ones.

- [ ] **Step 9: Run the whole suite**

Run: `./.venv/bin/python -m pytest -q`
Expected: 118 passed, with none of the original 56 modified.

- [ ] **Step 10: Confirm the addon still loads under real mitmproxy**

Run: `PYTHONPATH=. ./.venv/bin/mitmdump -s extractor/addon.py --listen-port 8099 &` then `sleep 3` and check the log, then kill it.
Expected: no import error, and the TCP server binds 38469.

- [ ] **Step 11: Commit**

```bash
git add extractor/addon.py extractor/server.py tests/test_addon.py
git commit -m "Fan extracted room codes out to multiple sinks"
```

---

## Task 8: Pairing CLI (TDD)

What the host runs once per phone.

**Files:**
- Create: `pair.py`
- Test: `tests/test_pair_cli.py`

**Interfaces:**
- Consumes: the relay's `POST /pairing-codes` (Task 5).
- Produces: `mint(base_url: str, token: str, *, opener=urllib.request.urlopen) -> str` and `main(argv=None) -> int`.

- [ ] **Step 1: Write the failing tests** `tests/test_pair_cli.py`

```python
import io
import json

import pytest

import pair


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False


def opener_returning(payload):
    calls = []

    def opener(request, timeout=None):
        calls.append(request)
        return FakeResponse(json.dumps(payload).encode())

    opener.calls = calls
    return opener


def test_mint_returns_the_pairing_code():
    opener = opener_returning({"pairing_code": "ABC234"})
    assert pair.mint("https://relay.example", "pub", opener=opener) == "ABC234"


def test_mint_targets_the_pairing_codes_endpoint():
    opener = opener_returning({"pairing_code": "ABC234"})
    pair.mint("https://relay.example/", "pub", opener=opener)
    assert opener.calls[0].full_url == "https://relay.example/pairing-codes"


def test_mint_sends_the_publisher_token_as_a_bearer():
    opener = opener_returning({"pairing_code": "ABC234"})
    pair.mint("https://relay.example", "pub", opener=opener)
    assert opener.calls[0].get_header("Authorization") == "Bearer pub"


def test_mint_refuses_a_plaintext_url():
    with pytest.raises(ValueError):
        pair.mint("http://relay.example", "pub", opener=opener_returning({}))


def test_main_reports_missing_configuration(monkeypatch, capsys):
    monkeypatch.delenv("JBVIP_RELAY_URL", raising=False)
    monkeypatch.delenv("JBVIP_RELAY_TOKEN", raising=False)
    assert pair.main() == 2
    assert "JBVIP_RELAY_URL" in capsys.readouterr().err


def test_main_prints_the_host_and_code(monkeypatch, capsys):
    monkeypatch.setenv("JBVIP_RELAY_URL", "https://relay.example")
    monkeypatch.setenv("JBVIP_RELAY_TOKEN", "pub")
    monkeypatch.setattr(pair, "mint", lambda url, token: "ABC234")
    assert pair.main() == 0
    out = capsys.readouterr().out
    assert "relay.example" in out
    assert "ABC234" in out


def test_main_reports_a_failed_mint(monkeypatch, capsys):
    monkeypatch.setenv("JBVIP_RELAY_URL", "https://relay.example")
    monkeypatch.setenv("JBVIP_RELAY_TOKEN", "pub")

    def explode(url, token):
        raise OSError("relay unreachable")

    monkeypatch.setattr(pair, "mint", explode)
    assert pair.main() == 1
    assert "relay unreachable" in capsys.readouterr().err
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_pair_cli.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'pair'`.

- [ ] **Step 3: Write the implementation** `pair.py`

```python
#!/usr/bin/env python3
"""Mint a pairing code so the phone app can subscribe to the relay.

Usage:
    JBVIP_RELAY_URL=https://relay.example \
    JBVIP_RELAY_TOKEN=<publisher token> \
    python3 pair.py

Prints the relay host and a six-character pairing code. Enter both in the app
once; the pairing code is single-use and expires in five minutes.
"""
import json
import os
import sys
import urllib.request

REQUEST_TIMEOUT_SECONDS = 10.0


def mint(base_url: str, token: str, *, opener=urllib.request.urlopen) -> str:
    if not base_url.startswith("https://"):
        raise ValueError("JBVIP_RELAY_URL must start with https://")
    request = urllib.request.Request(
        base_url.rstrip("/") + "/pairing-codes",
        data=b"{}",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    with opener(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        return json.loads(response.read())["pairing_code"]


def main(argv=None) -> int:
    base_url = os.environ.get("JBVIP_RELAY_URL", "")
    token = os.environ.get("JBVIP_RELAY_TOKEN", "")
    if not base_url or not token:
        print(
            "Set JBVIP_RELAY_URL and JBVIP_RELAY_TOKEN before running pair.py.",
            file=sys.stderr,
        )
        return 2
    try:
        pairing_code = mint(base_url, token)
    except Exception as exc:
        print(f"Could not mint a pairing code: {exc}", file=sys.stderr)
        return 1
    host = base_url.split("://", 1)[1].rstrip("/")
    print()
    print(f"  Relay host:   {host}")
    print(f"  Pairing code: {pairing_code}")
    print()
    print("  Enter both in the app. Single use, valid for 5 minutes.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_pair_cli.py -q`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add pair.py tests/test_pair_cli.py
git commit -m "Add pairing-code CLI for the host"
```

---

## Task 9: Deployment documentation and an end-to-end check

**Files:**
- Create: `relay/README.md`
- Modify: `README.md`
- Modify: `jbvip-run.sh` (pass the relay environment through to mitmdump)

**Interfaces:**
- Consumes: everything above.
- Produces: a documented deployment, and one verified end-to-end run.

- [ ] **Step 1: Write `relay/README.md`**

````markdown
# jbvip relay

Receives the host's Jackbox room code and pushes it to subscribed clients.
Deploy behind a reverse proxy that terminates TLS — the service speaks plain
HTTP and cannot verify its own transport, so the clients enforce `https://`
instead.

## Install

```bash
python3 -m venv /opt/jbvip-relay/venv
/opt/jbvip-relay/venv/bin/pip install -r relay/requirements.txt
```

## Configure

Generate a publisher token and keep it secret; it is also the admin credential:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Environment:

- `JBVIP_PUBLISHER_TOKEN` (required) — the host PC's credential.
- `JBVIP_TOKENS_FILE` (default `/var/lib/jbvip/tokens.json`) — where redeemed
  subscriber tokens are stored. Must be writable by the service user.

## Run

```bash
JBVIP_PUBLISHER_TOKEN=... /opt/jbvip-relay/venv/bin/uvicorn \
    --factory relay.app:create_app --host 127.0.0.1 --port 8479
```

Point your reverse proxy at `127.0.0.1:8479`. It must **not** buffer responses,
or the SSE stream will stall — for nginx set `proxy_buffering off;`. Caddy needs
no special configuration.

## Endpoints

| Method | Path             | Auth             | Purpose                                  |
|--------|------------------|------------------|------------------------------------------|
| POST   | `/publish`       | publisher token  | Host pushes `{"code","ts"}`              |
| GET    | `/subscribe`     | subscriber token | SSE stream of codes                      |
| POST   | `/pairing-codes` | publisher token  | Mint a pairing code                      |
| POST   | `/pair`          | pairing code     | Redeem for a durable subscriber token    |

Codes older than 120 seconds are never delivered — Jackbox recycles four-letter
codes, and a stale one may belong to a stranger's room.
````

- [ ] **Step 2: Pass the relay environment through in `jbvip-run.sh`**

`jbvip-run.sh:23` exports `PYTHONPATH` before forking `mitmdump` at line 56. Insert these two lines directly after line 23, so `_build_sinks()` can see them in the forked process:

```bash
# Optional relay. Unset means LAN-only, which is the supported default.
export JBVIP_RELAY_URL="${JBVIP_RELAY_URL:-}"
export JBVIP_RELAY_TOKEN="${JBVIP_RELAY_TOKEN:-}"
```

- [ ] **Step 3: Document the relay in the top-level `README.md`**

Insert this section immediately before the existing `## Uninstall` heading:

````markdown
## Reaching a Phone That Isn't on Your Wifi (optional)

By default the room code is served only on your LAN, over TCP 38469. If you want
a client to receive it from anywhere — a phone on cellular, or a friend's
house — you can run the small relay in `relay/` on a VPS. The host pushes the
code out over ordinary outbound HTTPS, so there is nothing to port-forward and
nothing new listening on your machine.

See `relay/README.md` for deployment. Once it is running, set two variables
before launching a game:

```bash
export JBVIP_RELAY_URL=https://your-relay.example
export JBVIP_RELAY_TOKEN=<the publisher token>
```

Then pair a client once:

```bash
python3 pair.py
```

That prints a relay host and a six-character pairing code to enter in the
client. The pairing code is single-use and expires after five minutes.

The relay is entirely optional. With these variables unset, everything works
exactly as before, LAN-only.
````

- [ ] **Step 4: Run the whole suite**

Run: `./.venv/bin/python -m pytest -q`
Expected: 125 passed.

- [ ] **Step 5: End-to-end check against a locally-run relay**

In one terminal:

```bash
JBVIP_PUBLISHER_TOKEN=testtoken JBVIP_TOKENS_FILE=/tmp/jbvip-tokens.json \
  ./.venv/bin/uvicorn --factory relay.app:create_app --port 8479
```

In a second, mint and redeem a pairing code, then subscribe:

```bash
CODE=$(curl -sS -X POST localhost:8479/pairing-codes \
  -H 'Authorization: Bearer testtoken' | python3 -c 'import json,sys; print(json.load(sys.stdin)["pairing_code"])')
TOKEN=$(curl -sS -X POST localhost:8479/pair -H 'Content-Type: application/json' \
  -d "{\"pairing_code\":\"$CODE\"}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["subscriber_token"])')
curl -sS -N localhost:8479/subscribe -H "Authorization: Bearer $TOKEN"
```

In a third, publish:

```bash
curl -sS -X POST localhost:8479/publish -H 'Authorization: Bearer testtoken' \
  -H 'Content-Type: application/json' -d '{"code":"WEWW"}'
```

Expected: the second terminal prints `data: {"code": "WEWW", "ts": ...}` immediately.

Note this uses `http://localhost` deliberately — the https requirement is enforced in the *clients*, and `curl` is not one of them. `RelayPublisher` would refuse this URL, which is the intended behaviour.

- [ ] **Step 6: End-to-end check with a real game**

With the relay reachable over HTTPS, export `JBVIP_RELAY_URL` and `JBVIP_RELAY_TOKEN`, run `sudo ./jbvip-redirect-up.sh`, launch a Jackbox pack, and create a room. Keep a `curl -N .../subscribe` running.

Expected: the room code appears on the SSE stream within a second of the room being created, and `nc 127.0.0.1 38469` still shows `RoomCode:<CODE>` — both sinks working.

- [ ] **Step 7: Commit**

```bash
git add relay/README.md README.md jbvip-run.sh
git commit -m "Document relay deployment and wire relay env through the launcher"
```

---

## Done when

- `./.venv/bin/python -m pytest -q` reports 125 passed.
- `cd injection && npm test` reports six `ok` lines.
- A code created by a real game appears on the relay's SSE stream and on TCP 38469.
- `injection/inject.js` has joined a real lobby and taken the VIP seat, with the outcome for old Blobcast packs recorded in the spec.

## What this plan does not build

The Android app. It gets its own plan, written after Task 2 has established what the live page actually does. Its contract with this plan is exactly two things: subscribe to `GET /subscribe` with a bearer token, and call `window.jbvipJoin(code, name)` in a WebView.
