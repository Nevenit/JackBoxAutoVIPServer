> **SUPERSEDED (2026-06-03):** replaced by
> `docs/superpowers/specs/2026-06-03-jackbox-roomcode-extractor-design.md` and
> `docs/superpowers/plans/2026-06-03-jackbox-roomcode-extractor.md`. Kept for history.

# Network-Based Room-Code Extraction — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the fragile per-game memory-pointer room-code reader with a Python tool that intercepts the host game's own ecast traffic (via a transparent TLS proxy) and broadcasts the room code over the existing TCP wire protocol.

**Architecture:** A single Python process runs as a `mitmproxy` addon. `mitmproxy` (transparent mode, fed by a kernel `iptables` REDIRECT) decrypts the game's TLS; the addon extracts the room code from the host's own `…/api/v2/rooms/<CODE>/…` URL and pushes it to an asyncio TCP server that speaks the unchanged `RoomCode:<CODE>` protocol on port 38469.

**Tech Stack:** Python 3.10+, mitmproxy, asyncio, pytest + pytest-asyncio, iptables (Linux), Steam/Proton.

**Companion design doc:** `docs/room-code-extraction-design.md` (read it first — section refs below point to it).

---

## Environment & prerequisites

- **Dev machine (offline tasks):** any OS with Python 3.10+. Tasks 0, 2, 3, 4 need no game and can be built and tested anywhere.
- **Target machine (on-game tasks):** Linux with Steam + Proton and the Jackbox game installed. Tasks 1, 5, 6 must run here. Requires `sudo` (for `iptables`).
- Install dev deps once: `python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt`.

**Gate awareness:** Task 1 is a feasibility GATE. If it shows certificate pinning that can't be worked around, the *interception transport* changes (in-process capture — design §8) and you must re-plan that transport. **Tasks 2–4 (parser + server) are reused unchanged in either transport, so they are never wasted.**

---

## File structure

```
JackBoxAutoVIPServer/
├── requirements.txt          # Python deps (new)
├── pytest.ini                # pytest-asyncio config (new)
├── run.sh                    # launcher: iptables REDIRECT + mitmdump (new)
├── extractor/                # the Python tool (new)
│   ├── __init__.py
│   ├── roomcode.py           # pure room-code parser
│   ├── server.py             # asyncio TCP distribution server
│   └── addon.py              # mitmproxy addon wiring parser → server
├── tests/                    # (new)
│   ├── __init__.py
│   ├── test_roomcode.py
│   ├── test_server.py
│   └── test_addon.py
└── JackBoxAutoVIP/           # obsolete C# project — removed in Task 7
```

---

## Task 0: Python project scaffold

**Files:**
- Create: `requirements.txt`, `pytest.ini`, `extractor/__init__.py`, `tests/__init__.py`

- [ ] **Step 1: Create `requirements.txt`**

```
mitmproxy>=11
pytest>=8
pytest-asyncio>=0.23
```

- [ ] **Step 2: Create `pytest.ini`**

```ini
[pytest]
asyncio_mode = auto
testpaths = tests
```

- [ ] **Step 3: Create empty package files**

Create `extractor/__init__.py` (empty) and `tests/__init__.py` (empty).

- [ ] **Step 4: Set up the venv and verify pytest runs**

Run:
```bash
python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
pytest -q
```
Expected: pytest runs and reports `no tests ran` (exit 5) — confirms the toolchain works.

- [ ] **Step 5: Commit**

```bash
git add requirements.txt pytest.ini extractor/__init__.py tests/__init__.py
git commit -m "Scaffold Python extractor project"
```

---

## Task 1: Phase 0 feasibility spike — THE GATE (target machine)

**Goal:** Determine whether `mitmproxy` can decrypt the game's TLS, and capture a real room URL to use as a parser fixture. No code/tests — this is investigation with a recorded decision.

**Files:**
- Create: `tests/fixtures/sample_flow.txt` (the captured evidence)

- [ ] **Step 1: Install mitmproxy and generate its CA**

Run `pip install mitmproxy` then `mitmdump` once and Ctrl-C it. Confirm the CA exists:
`ls ~/.mitmproxy/mitmproxy-ca-cert.pem`

- [ ] **Step 2: Trust the CA where the game can see it**

Try, in order, and note which you did:
1. System store: `sudo cp ~/.mitmproxy/mitmproxy-ca-cert.pem /usr/local/share/ca-certificates/mitmproxy.crt && sudo update-ca-certificates`
2. Proton/Wine prefix store (path is game-specific under `~/.steam/steam/steamapps/compatdata/<APPID>/pfx/`).
3. Search the game install dir for a bundled bundle: `find ~/.steam -iname 'cacert.pem' -o -iname 'ca-bundle*'` — if found, append the mitmproxy CA to it (back it up first).

- [ ] **Step 3: Add a temporary global REDIRECT and start mitmdump**

```bash
sudo sysctl -w net.ipv4.ip_forward=1
sudo iptables -t nat -A OUTPUT -p tcp --dport 443 -m owner ! --uid-owner "$(id -un)" -j REDIRECT --to-ports 8080
mitmdump --mode transparent --listen-port 8080
```
(Run mitmdump as your normal user here for the quick test; the `! --uid-owner <you>` exclusion stops it looping on its own upstream traffic. Task 5 hardens this with a dedicated user.)

- [ ] **Step 4: Launch the game, create a room, watch the flows**

In mitmdump's output look for `ecast.jackboxgames.com` traffic, especially a WebSocket handshake to a path like `/api/v2/rooms/<CODE>/play`.

- [ ] **Step 5: Record the outcome and remove the rule**

```bash
sudo iptables -t nat -D OUTPUT -p tcp --dport 443 -m owner ! --uid-owner "$(id -un)" -j REDIRECT --to-ports 8080
```

Decide and write the result into `tests/fixtures/sample_flow.txt`:
- **DECRYPTS** → paste the real room WS URL (redact nothing structural; the code itself is ephemeral). **Proceed to Task 2.**
- **TLS handshake errors / game can't reach lobby** → pinning or unpatched bundled CA. If a bundled `cacert.pem` exists and editing it (Step 2.3) fixed it, proceed. Otherwise **STOP**: the proxy transport is blocked — return to design §8 (in-process capture) and write a follow-up plan for that transport. Tasks 2–4 below still apply.
- **Connected but not decrypted** → transparent-mode misconfig; recheck the REDIRECT and `--mode transparent`, retry.

- [ ] **Step 6: Commit the captured fixture**

```bash
git add tests/fixtures/sample_flow.txt
git commit -m "Record Phase 0 interception feasibility result"
```

---

## Task 2: Room-code parser (offline, TDD)

**Files:**
- Create: `extractor/roomcode.py`
- Test: `tests/test_roomcode.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_roomcode.py`:
```python
from extractor.roomcode import extract_room_code_from_url


def test_extracts_code_from_host_websocket_url():
    url = "wss://ecast.jackboxgames.com/api/v2/rooms/ABCD/play?role=host&name=Host"
    assert extract_room_code_from_url(url) == "ABCD"


def test_extracts_code_from_plain_https_room_url():
    url = "https://ecast.jackboxgames.com/api/v2/rooms/WXYZ"
    assert extract_room_code_from_url(url) == "WXYZ"


def test_returns_none_for_create_room_url_without_code():
    assert extract_room_code_from_url("https://ecast.jackboxgames.com/api/v2/rooms") is None


def test_returns_none_when_no_room_path():
    assert extract_room_code_from_url("https://ecast.jackboxgames.com/api/v2/health") is None
    assert extract_room_code_from_url("") is None


def test_parser_is_host_agnostic_path_only():
    # The parser only inspects the path; host filtering is the addon's job (Task 4).
    assert extract_room_code_from_url("https://example.com/api/v2/rooms/ABCD") == "ABCD"
```

> After Task 1: if the real captured URL in `sample_flow.txt` differs in shape (e.g. a longer code or different path version), update the literal in `test_extracts_code_from_host_websocket_url` to match the real sample before implementing.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_roomcode.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'extractor.roomcode'`.

- [ ] **Step 3: Write the implementation**

`extractor/roomcode.py`:
```python
"""Extract a Jackbox room code from an intercepted ecast URL."""
import re

# Matches the room-id segment in ecast paths, e.g.
#   wss://ecast.jackboxgames.com/api/v2/rooms/ABCD/play
#   https://ecast.jackboxgames.com/api/v2/rooms/ABCD
_ROOM_PATH = re.compile(r"/api/v\d+/rooms/([A-Za-z0-9]+)(?:[/?]|$)")


def extract_room_code_from_url(url: str) -> str | None:
    """Return the room code embedded in an ecast room URL, else None."""
    if not url:
        return None
    match = _ROOM_PATH.search(url)
    if not match:
        return None
    code = match.group(1)
    return code if _looks_like_code(code) else None


def _looks_like_code(code: str) -> bool:
    """Sanity check: short and alphanumeric (avoids hardcoding the 4-letter format)."""
    return 2 <= len(code) <= 8 and code.isalnum()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_roomcode.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add extractor/roomcode.py tests/test_roomcode.py
git commit -m "Add room-code URL parser"
```

---

## Task 3: TCP distribution server (offline, TDD)

**Files:**
- Create: `extractor/server.py`
- Test: `tests/test_server.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_server.py`:
```python
import asyncio
import pytest
from extractor.server import RoomCodeServer


async def _connect(server):
    return await asyncio.open_connection("127.0.0.1", server.bound_port)


async def test_new_client_receives_current_code():
    server = RoomCodeServer(port=0)
    await server.start()
    await server.set_code("ABCD")
    reader, writer = await _connect(server)
    data = await asyncio.wait_for(reader.read(100), timeout=1)
    assert data == b"RoomCode:ABCD"
    writer.close()


async def test_set_code_broadcasts_to_connected_client():
    server = RoomCodeServer(port=0)
    await server.start()
    reader, writer = await _connect(server)
    await asyncio.sleep(0.05)  # let the server register the client
    await server.set_code("WXYZ")
    data = await asyncio.wait_for(reader.read(100), timeout=1)
    assert data == b"RoomCode:WXYZ"
    writer.close()


async def test_duplicate_code_not_resent():
    server = RoomCodeServer(port=0)
    await server.start()
    await server.set_code("ABCD")
    reader, writer = await _connect(server)
    assert await asyncio.wait_for(reader.read(100), timeout=1) == b"RoomCode:ABCD"
    await server.set_code("ABCD")  # same code → no broadcast
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(reader.read(100), timeout=0.2)
    writer.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_server.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'extractor.server'`.

- [ ] **Step 3: Write the implementation**

`extractor/server.py`:
```python
"""TCP server that broadcasts the current room code to connected clients.

Wire protocol (unchanged from the original C# server): each client receives the
ASCII bytes ``RoomCode:<CODE>`` (no terminator) on connect (if a code is known)
and again whenever the code changes.
"""
import asyncio


class RoomCodeServer:
    def __init__(self, port: int = 38469, host: str = "0.0.0.0"):
        self._port = port
        self._host = host
        self._code: str | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._server: asyncio.AbstractServer | None = None

    @property
    def bound_port(self) -> int:
        assert self._server is not None
        return self._server.sockets[0].getsockname()[1]

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._on_client, self._host, self._port)

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        if self._code is not None:
            await self._safe_send(writer, self._code)
        try:
            while await reader.read(4096):
                pass
        finally:
            self._writers.discard(writer)
            writer.close()

    async def set_code(self, code: str) -> None:
        if code == self._code:
            return
        self._code = code
        for writer in list(self._writers):
            await self._safe_send(writer, code)

    async def _safe_send(self, writer: asyncio.StreamWriter, code: str) -> None:
        try:
            writer.write(f"RoomCode:{code}".encode())
            await writer.drain()
        except (ConnectionError, RuntimeError):
            self._writers.discard(writer)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_server.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add extractor/server.py tests/test_server.py
git commit -m "Add asyncio room-code distribution server"
```

---

## Task 4: mitmproxy addon wiring (offline, TDD)

**Files:**
- Create: `extractor/addon.py`
- Test: `tests/test_addon.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_addon.py`:
```python
from types import SimpleNamespace
from extractor.addon import RoomCodeExtractor
from extractor.server import RoomCodeServer


def _flow(host, url):
    return SimpleNamespace(request=SimpleNamespace(host=host, pretty_url=url))


def test_code_from_flow_reads_ecast_room_url():
    ext = RoomCodeExtractor(RoomCodeServer(port=0))
    flow = _flow("ecast.jackboxgames.com",
                 "wss://ecast.jackboxgames.com/api/v2/rooms/ABCD/play")
    assert ext.code_from_flow(flow) == "ABCD"


def test_code_from_flow_ignores_non_jackbox_host():
    ext = RoomCodeExtractor(RoomCodeServer(port=0))
    flow = _flow("example.com", "https://example.com/api/v2/rooms/ABCD")
    assert ext.code_from_flow(flow) is None


def test_code_from_flow_returns_none_without_code():
    ext = RoomCodeExtractor(RoomCodeServer(port=0))
    flow = _flow("ecast.jackboxgames.com", "https://ecast.jackboxgames.com/api/v2/rooms")
    assert ext.code_from_flow(flow) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_addon.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'extractor.addon'`.

- [ ] **Step 3: Write the implementation**

`extractor/addon.py`:
```python
"""mitmproxy addon: watch ecast traffic, extract the room code, broadcast it.

Run with: mitmdump --mode transparent -s extractor/addon.py
(PYTHONPATH must include the project root so `extractor` is importable.)
"""
import asyncio

from extractor.roomcode import extract_room_code_from_url
from extractor.server import RoomCodeServer

_JACKBOX_HOSTS = ("jackboxgames.com", "jackbox.tv")


class RoomCodeExtractor:
    def __init__(self, server: RoomCodeServer):
        self.server = server

    def code_from_flow(self, flow) -> str | None:
        host = getattr(flow.request, "host", "") or ""
        if not any(h in host for h in _JACKBOX_HOSTS):
            return None
        return extract_room_code_from_url(flow.request.pretty_url)

    def _broadcast(self, flow) -> None:
        code = self.code_from_flow(flow)
        if code:
            asyncio.ensure_future(self.server.set_code(code))

    # --- mitmproxy event hooks ---
    def running(self):
        asyncio.ensure_future(self.server.start())

    def websocket_start(self, flow):
        self._broadcast(flow)

    def request(self, flow):
        self._broadcast(flow)


addons = [RoomCodeExtractor(RoomCodeServer(port=38469))]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_addon.py -v`
Expected: 3 passed. Then run the full suite: `pytest -q` → all pass.

- [ ] **Step 5: Commit**

```bash
git add extractor/addon.py tests/test_addon.py
git commit -m "Add mitmproxy addon wiring parser to distribution server"
```

---

## Task 5: Launcher script (target machine)

**Goal:** One script that sets up the REDIRECT, runs `mitmdump` as a dedicated user (so the proxy's own upstream traffic isn't re-redirected — design §5.1), and tears the rule down on exit.

**Files:**
- Create: `run.sh`

- [ ] **Step 1: Write `run.sh`**

```bash
#!/usr/bin/env bash
# Transparent room-code interception launcher (Linux).
# mitmdump runs as a dedicated user so its upstream connections are not
# re-redirected into itself (avoids an infinite proxy loop). See design §5.1.
set -euo pipefail

PROXY_PORT="${PROXY_PORT:-8080}"
MITM_USER="${MITM_USER:-mitmproxyuser}"
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"

if ! id "$MITM_USER" &>/dev/null; then
  echo "Creating dedicated proxy user: $MITM_USER"
  sudo useradd --create-home --shell /usr/sbin/nologin "$MITM_USER"
fi

cleanup() {
  sudo iptables -t nat -D OUTPUT -p tcp --dport 443 \
    -m owner ! --uid-owner "$MITM_USER" -j REDIRECT --to-ports "$PROXY_PORT" 2>/dev/null || true
  echo "Removed REDIRECT rule."
}
trap cleanup EXIT INT TERM

sudo sysctl -w net.ipv4.ip_forward=1 >/dev/null

# Redirect ALL outbound :443 except mitmproxy's own user. NOTE: this intercepts
# every app's HTTPS while running — only run it while gaming (design §5.1).
sudo iptables -t nat -A OUTPUT -p tcp --dport 443 \
  -m owner ! --uid-owner "$MITM_USER" -j REDIRECT --to-ports "$PROXY_PORT"

echo "REDIRECT active → mitmdump :$PROXY_PORT (user=$MITM_USER)."
echo "First run: trust ~$MITM_USER/.mitmproxy/mitmproxy-ca-cert.pem per design §5.2 / Task 1."

sudo -u "$MITM_USER" -H env PYTHONPATH="$PROJECT_DIR" \
  mitmdump --mode transparent --listen-port "$PROXY_PORT" -s "$PROJECT_DIR/extractor/addon.py"
```

- [ ] **Step 2: Make it executable**

Run: `chmod +x run.sh`

- [ ] **Step 3: First-run CA trust for the dedicated user**

Generate the dedicated user's CA, then trust it where the game looks (mirror Task 1, Step 2, but using `~mitmproxyuser/.mitmproxy/...`):
```bash
sudo -u "${MITM_USER:-mitmproxyuser}" -H mitmdump --version   # creates its CA dir
```
Trust that CA via the method that worked in Task 1.

- [ ] **Step 4: Smoke-test the script (no game yet)**

Run `./run.sh`, confirm it prints "REDIRECT active", then Ctrl-C and confirm it prints "Removed REDIRECT rule." Verify no leftover rule:
`sudo iptables -t nat -L OUTPUT -n | grep 8080` → expect no match.

- [ ] **Step 5: Commit**

```bash
git add run.sh
git commit -m "Add transparent-proxy launcher script"
```

---

## Task 6: End-to-end verification (target machine)

**Goal:** Prove a connected client receives the correct code within ~1s of the lobby appearing.

- [ ] **Step 1: Start the tool**

Run `./run.sh` (leave it running).

- [ ] **Step 2: Connect a test client**

In another terminal: `nc 127.0.0.1 38469` (or `ncat`). Leave it open.

- [ ] **Step 3: Launch the game and create a room**

Start the Jackbox game via Steam/Proton and create a lobby.

- [ ] **Step 4: Verify**

Expected: within ~1s of the lobby code appearing on screen, the `nc` terminal prints `RoomCode:<CODE>` matching the on-screen code. Create a second room and confirm the client receives the new code.

- [ ] **Step 5: Record the result**

Append the outcome (success / observed latency / any issues) to `tests/fixtures/sample_flow.txt` and commit:
```bash
git add tests/fixtures/sample_flow.txt
git commit -m "Record end-to-end verification result"
```

---

## Task 7: Remove obsolete C# project + update docs

**Goal:** Retire the Windows-only memory-reading implementation now that the Python tool is verified.

**Files:**
- Delete: `JackBoxAutoVIP/`, `JackBoxRoomCodeExtractor.sln`
- Modify: `README.md`

- [ ] **Step 1: Remove the C# project**

```bash
git rm -r JackBoxAutoVIP JackBoxRoomCodeExtractor.sln
```

- [ ] **Step 2: Update `README.md`**

Replace the "How It Works" / "Requirements" sections to describe the new approach: Linux + Steam/Proton, `pip install -r requirements.txt`, run `./run.sh`, clients connect to TCP `:38469` and read `RoomCode:<CODE>`. Remove the Windows/memory-pointer description.

- [ ] **Step 3: Verify the suite still passes**

Run: `pytest -q` → all pass (the C# removal must not affect Python tests).

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "Remove obsolete C# memory-reader; document network-based tool"
```

---

## Self-review notes (author)

- **Spec coverage:** transport+gate (§4/§7 → Tasks 1,5), parser keyed off room WS URL (§5.3 → Task 2), distribution server with unchanged wire protocol (§5.4/§6 → Task 3), addon wiring (§5.3 → Task 4), dedupe + new-client-on-connect + disconnect pruning (§10 → Task 3 tests), E2E ≤1s success criterion (§1 → Task 6), C# retirement (§5.4 → Task 7). Fallback (§8) is intentionally a re-plan trigger in Task 1, not built speculatively.
- **Not covered by design intentionally:** legacy Blobcast parser and the auto-VIP bot are future work (design §12).
- **Type consistency:** `extract_room_code_from_url(url) -> str | None`, `RoomCodeServer(port).set_code(code)/start()/bound_port`, `RoomCodeExtractor(server).code_from_flow(flow)` are used identically across tasks and tests.
```
