# Jackbox Room-Code Extractor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Windows-only memory-pointer room-code reader with a Python tool that intercepts the host Jackbox game's own ecast traffic (via a per-game bubblewrap-injected mitmproxy CA, proven in the Phase 0 spike) and broadcasts the room code over the unchanged `RoomCode:<CODE>` TCP protocol on port 38469.

**Architecture:** A single Python process runs as a `mitmproxy` addon, started by `jbvip-run.sh`. The addon extracts the room code from the host's own `POST /api/v2/rooms` response body (primary) or a `/rooms/<CODE>/play` WebSocket URL (fallback), and pushes it to an `asyncio` TCP server. A Python Steam helper sets the launch option (`jbvip-run.sh %command%`) on all installed Jackbox games. The obsolete C# project is removed.

**Tech Stack:** Python 3.10+ (target machine has 3.14), mitmproxy, asyncio, pytest + pytest-asyncio, `vdf`, bubblewrap, Steam/Proton-free native Linux.

**Companion design spec:** `docs/superpowers/specs/2026-06-03-jackbox-roomcode-extractor-design.md` (read it first).

---

## Environment & prerequisites

- **Offline tasks (no game needed):** Tasks 0–4, 7, 8 build and test anywhere with Python 3.10+.
- **Target-machine tasks:** Tasks 5, 6, 9 need Linux + Steam + an installed Jackbox game + bubblewrap + unprivileged user namespaces + mitmproxy on PATH.
- Set up once: `python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt`.
- The proven launcher `jbvip-run.sh` already exists at repo root (currently uses `-w` capture; Task 6 wires in the addon).

## File structure

```
JackBoxAutoVIPServer/
├── requirements.txt              # Python deps (new)
├── pytest.ini                    # pytest-asyncio config (new)
├── jbvip-run.sh                  # launcher (exists; modified in Task 6)
├── install.py                    # CLI: set launch options on all Jackbox games (new)
├── uninstall.py                  # CLI: clear those launch options (new)
├── extractor/                    # the Python tool (new)
│   ├── __init__.py
│   ├── roomcode.py               # pure room-code parser
│   ├── server.py                 # asyncio TCP distribution server
│   ├── addon.py                  # mitmproxy addon wiring parser → server
│   └── steam.py                  # pure Steam VDF helpers (detect games, edit launch opts)
├── tests/                        # (new)
│   ├── __init__.py
│   ├── fixtures/
│   │   ├── create_room_response.json   # real POST /api/v2/rooms body (Task 1)
│   │   └── localconfig_sample.vdf      # minimal Steam localconfig (Task 4)
│   ├── test_roomcode.py
│   ├── test_server.py
│   ├── test_addon.py
│   └── test_steam.py
├── README.md                     # rewritten in Task 7
├── docs/                         # old design+plan marked superseded in Task 7
└── JackBoxAutoVIP/, *.sln        # removed in Task 8
```

---

## Task 0: Python project scaffold

**Files:** Create `requirements.txt`, `pytest.ini`, `extractor/__init__.py`, `tests/__init__.py`, `tests/fixtures/` (dir).

- [ ] **Step 1: Create `requirements.txt`**

```
mitmproxy>=11
pytest>=8
pytest-asyncio>=0.23
vdf>=3.4
```

- [ ] **Step 2: Create `pytest.ini`**

```ini
[pytest]
asyncio_mode = auto
testpaths = tests
```

- [ ] **Step 3: Create empty package files**

Create `extractor/__init__.py` (empty), `tests/__init__.py` (empty), and the directory `tests/fixtures/`.

- [ ] **Step 4: Create venv and verify pytest runs**

```bash
python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
pytest -q
```
Expected: `no tests ran` (exit code 5) — confirms toolchain works.

- [ ] **Step 5: Commit**

```bash
git add requirements.txt pytest.ini extractor/__init__.py tests/__init__.py
git commit -m "Scaffold Python extractor project"
```

---

## Task 1: Room-code parser (offline, TDD)

**Files:** Create `extractor/roomcode.py`, `tests/test_roomcode.py`, `tests/fixtures/create_room_response.json`.

- [ ] **Step 1: Save the real captured fixture** `tests/fixtures/create_room_response.json`

```json
{
  "ok": true,
  "body": {
    "host": "ecast-prod-use2.jackboxgames.com",
    "code": "NBHT",
    "token": "279715f98dceec3b1508bf3d"
  }
}
```

- [ ] **Step 2: Write the failing tests** `tests/test_roomcode.py`

```python
import json
from pathlib import Path

from extractor.roomcode import (
    extract_code_from_url,
    code_from_create_response,
    looks_like_code,
)

FIXTURE = Path(__file__).parent / "fixtures" / "create_room_response.json"


# --- primary: REST create-room response body ---

def test_code_from_real_create_response_fixture():
    body = json.loads(FIXTURE.read_text())
    url = "https://ecast.jackboxgames.com/api/v2/rooms"
    assert code_from_create_response(url, body) == "NBHT"


def test_code_from_create_response_ignores_non_create_url():
    body = {"ok": True, "body": {"code": "ABCD"}}
    # a play/get URL is not the create endpoint
    assert code_from_create_response(
        "https://ecast.jackboxgames.com/api/v2/rooms/ABCD/play", body
    ) is None


def test_code_from_create_response_handles_missing_fields():
    url = "https://ecast.jackboxgames.com/api/v2/rooms"
    assert code_from_create_response(url, {"ok": True, "body": {}}) is None
    assert code_from_create_response(url, {"ok": True}) is None
    assert code_from_create_response(url, None) is None
    assert code_from_create_response(url, {"ok": False, "body": {"code": "ABCD"}}) == "ABCD"  # ok flag not required


# --- fallback: WebSocket / room URL ---

def test_extracts_code_from_host_websocket_url():
    url = "wss://ecast-prod-use2.jackboxgames.com/api/v2/rooms/ABCD/play?role=host"
    assert extract_code_from_url(url) == "ABCD"


def test_extracts_code_from_plain_room_url():
    assert extract_code_from_url("https://ecast.jackboxgames.com/api/v2/rooms/WXYZ") == "WXYZ"


def test_url_returns_none_for_create_url_without_code():
    assert extract_code_from_url("https://ecast.jackboxgames.com/api/v2/rooms") is None


def test_url_returns_none_when_no_room_path():
    assert extract_code_from_url("https://ecast.jackboxgames.com/api/v2/health") is None
    assert extract_code_from_url("") is None


# --- sanity check ---

def test_looks_like_code():
    assert looks_like_code("NBHT")
    assert looks_like_code("abcd1")
    assert not looks_like_code("")
    assert not looks_like_code("a")            # too short
    assert not looks_like_code("TOOLONGCODE")  # too long
    assert not looks_like_code("AB-CD")        # non-alnum
```

> If, after a fresh capture, the real shape differs (e.g. a longer code), update the fixture and the literals here before implementing.

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_roomcode.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'extractor.roomcode'`.

- [ ] **Step 4: Write the implementation** `extractor/roomcode.py`

```python
"""Extract a Jackbox room code from intercepted ecast traffic.

Two pure entry points, no I/O:
- code_from_create_response: PRIMARY. The host's POST /api/v2/rooms response
  body carries {"body": {"code": ...}} (proven in the Phase 0 spike).
- extract_code_from_url: FALLBACK. Pull <CODE> from a /rooms/<CODE>/play URL.
"""
import re

# room-id segment, e.g. /api/v2/rooms/ABCD/play  or  /api/v2/rooms/ABCD
_ROOM_PATH = re.compile(r"/api/v\d+/rooms/([A-Za-z0-9]+)(?:[/?]|$)")
# the create endpoint itself: /api/v2/rooms  (no code segment)
_CREATE_PATH = re.compile(r"/api/v\d+/rooms/?(?:\?|$)")


def looks_like_code(code: str) -> bool:
    """Sanity check: short and alphanumeric. NOT hardcoded to 4/uppercase."""
    return isinstance(code, str) and 2 <= len(code) <= 8 and code.isalnum()


def code_from_create_response(url: str, body) -> str | None:
    """Return the room code from a POST /api/v2/rooms JSON response body."""
    if not url or not _CREATE_PATH.search(url):
        return None
    if not isinstance(body, dict):
        return None
    inner = body.get("body")
    if not isinstance(inner, dict):
        return None
    code = inner.get("code")
    return code if isinstance(code, str) and looks_like_code(code) else None


def extract_code_from_url(url: str) -> str | None:
    """Return the room code embedded in a /rooms/<CODE>[/...] URL, else None."""
    if not url:
        return None
    match = _ROOM_PATH.search(url)
    if not match:
        return None
    code = match.group(1)
    return code if looks_like_code(code) else None
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_roomcode.py -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add extractor/roomcode.py tests/test_roomcode.py tests/fixtures/create_room_response.json
git commit -m "Add room-code parser (REST body primary, URL fallback)"
```

---

## Task 2: TCP distribution server (offline, TDD)

**Files:** Create `extractor/server.py`, `tests/test_server.py`.

- [ ] **Step 1: Write the failing tests** `tests/test_server.py`

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
    await asyncio.sleep(0.05)
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
    await server.set_code("ABCD")
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(reader.read(100), timeout=0.2)
    writer.close()


async def test_disconnected_client_is_pruned():
    server = RoomCodeServer(port=0)
    await server.start()
    reader, writer = await _connect(server)
    await asyncio.sleep(0.05)
    writer.close()
    await writer.wait_closed()
    await asyncio.sleep(0.05)
    await server.set_code("ZZZZ")  # must not raise on the dead writer
    assert server.client_count == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_server.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'extractor.server'`.

- [ ] **Step 3: Write the implementation** `extractor/server.py`

```python
"""TCP server broadcasting the current room code to connected clients.

Wire protocol (unchanged from the original C# server): each client receives the
ASCII bytes ``RoomCode:<CODE>`` (no terminator) on connect (if a code is known)
and again whenever the code changes. Binds 0.0.0.0 so LAN clients can connect.
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

    @property
    def client_count(self) -> int:
        return len(self._writers)

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
        except (ConnectionError, RuntimeError, OSError):
            self._writers.discard(writer)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_server.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add extractor/server.py tests/test_server.py
git commit -m "Add asyncio room-code distribution server"
```

---

## Task 3: mitmproxy addon wiring (offline, TDD)

**Files:** Create `extractor/addon.py`, `tests/test_addon.py`.

- [ ] **Step 1: Write the failing tests** `tests/test_addon.py`

```python
import json
from types import SimpleNamespace
from extractor.addon import RoomCodeExtractor
from extractor.server import RoomCodeServer


def _response_flow(host, url, method, body_obj):
    content = json.dumps(body_obj).encode() if body_obj is not None else b""
    return SimpleNamespace(
        request=SimpleNamespace(host=host, pretty_url=url, method=method),
        response=SimpleNamespace(content=content),
    )


def _ws_flow(host, url):
    return SimpleNamespace(request=SimpleNamespace(host=host, pretty_url=url, method="GET"))


def test_code_from_create_response_flow():
    ext = RoomCodeExtractor(RoomCodeServer(port=0))
    flow = _response_flow(
        "ecast.jackboxgames.com",
        "https://ecast.jackboxgames.com/api/v2/rooms",
        "POST",
        {"ok": True, "body": {"code": "ABCD"}},
    )
    assert ext.code_from_response_flow(flow) == "ABCD"


def test_response_flow_ignores_non_jackbox_host():
    ext = RoomCodeExtractor(RoomCodeServer(port=0))
    flow = _response_flow(
        "evil.com", "https://evil.com/api/v2/rooms", "POST",
        {"ok": True, "body": {"code": "ABCD"}},
    )
    assert ext.code_from_response_flow(flow) is None


def test_response_flow_ignores_non_post():
    ext = RoomCodeExtractor(RoomCodeServer(port=0))
    flow = _response_flow(
        "ecast.jackboxgames.com", "https://ecast.jackboxgames.com/api/v2/rooms", "GET",
        {"ok": True, "body": {"code": "ABCD"}},
    )
    assert ext.code_from_response_flow(flow) is None


def test_response_flow_tolerates_non_json_body():
    ext = RoomCodeExtractor(RoomCodeServer(port=0))
    flow = SimpleNamespace(
        request=SimpleNamespace(
            host="ecast.jackboxgames.com",
            pretty_url="https://ecast.jackboxgames.com/api/v2/rooms",
            method="POST",
        ),
        response=SimpleNamespace(content=b"not json"),
    )
    assert ext.code_from_response_flow(flow) is None


def test_code_from_ws_flow_fallback():
    ext = RoomCodeExtractor(RoomCodeServer(port=0))
    flow = _ws_flow(
        "ecast-prod-use2.jackboxgames.com",
        "wss://ecast-prod-use2.jackboxgames.com/api/v2/rooms/WXYZ/play",
    )
    assert ext.code_from_ws_flow(flow) == "WXYZ"


def test_ws_flow_ignores_non_jackbox_host():
    ext = RoomCodeExtractor(RoomCodeServer(port=0))
    flow = _ws_flow("evil.com", "wss://evil.com/api/v2/rooms/WXYZ/play")
    assert ext.code_from_ws_flow(flow) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_addon.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'extractor.addon'`.

- [ ] **Step 3: Write the implementation** `extractor/addon.py`

```python
"""mitmproxy addon: watch ecast traffic, extract the room code, broadcast it.

Run via:  mitmdump --listen-port <port> -s extractor/addon.py
(PYTHONPATH must include the project root so `extractor` is importable.)
"""
import asyncio
import json

from extractor.roomcode import code_from_create_response, extract_code_from_url
from extractor.server import RoomCodeServer

_JACKBOX = "jackboxgames.com"


class RoomCodeExtractor:
    def __init__(self, server: RoomCodeServer):
        self.server = server

    # --- pure-ish extraction (host-filtered), unit tested ---
    def _is_jackbox(self, flow) -> bool:
        host = getattr(flow.request, "host", "") or ""
        return _JACKBOX in host

    def code_from_response_flow(self, flow) -> str | None:
        if not self._is_jackbox(flow):
            return None
        if getattr(flow.request, "method", "") != "POST":
            return None
        try:
            body = json.loads(flow.response.content)
        except (ValueError, TypeError, AttributeError):
            return None
        return code_from_create_response(flow.request.pretty_url, body)

    def code_from_ws_flow(self, flow) -> str | None:
        if not self._is_jackbox(flow):
            return None
        return extract_code_from_url(flow.request.pretty_url)

    def _broadcast(self, code: str | None) -> None:
        if code:
            asyncio.ensure_future(self.server.set_code(code))

    # --- mitmproxy event hooks ---
    def running(self):
        asyncio.ensure_future(self.server.start())

    def response(self, flow):
        self._broadcast(self.code_from_response_flow(flow))

    def websocket_start(self, flow):
        self._broadcast(self.code_from_ws_flow(flow))


addons = [RoomCodeExtractor(RoomCodeServer(port=38469))]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_addon.py -v` then `pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add extractor/addon.py tests/test_addon.py
git commit -m "Add mitmproxy addon wiring parser to distribution server"
```

---

## Task 4: Steam launch-option helper (offline, TDD)

**Files:** Create `extractor/steam.py`, `tests/test_steam.py`, `tests/fixtures/localconfig_sample.vdf`.

- [ ] **Step 1: Save the fixture** `tests/fixtures/localconfig_sample.vdf`

```
"UserLocalConfigStore"
{
	"Software"
	{
		"Valve"
		{
			"Steam"
			{
				"apps"
				{
					"1148350"
					{
						"LastPlayed"		"1700000000"
					}
					"331670"
					{
						"LaunchOptions"		"existing %command%"
					}
				}
			}
		}
	}
}
```

- [ ] **Step 2: Write the failing tests** `tests/test_steam.py`

```python
import vdf
from pathlib import Path
from extractor.steam import set_launch_option, clear_launch_option, jackbox_app_ids

FIX = Path(__file__).parent / "fixtures" / "localconfig_sample.vdf"
WRAP = "/opt/jbvip/jbvip-run.sh %command%"


def _load():
    return vdf.loads(FIX.read_text())


def _apps(cfg):
    return cfg["UserLocalConfigStore"]["Software"]["Valve"]["Steam"]["apps"]


def test_set_launch_option_adds_to_app_without_one():
    cfg = set_launch_option(_load(), "1148350", WRAP)
    assert _apps(cfg)["1148350"]["LaunchOptions"] == WRAP


def test_set_launch_option_overwrites_existing():
    cfg = set_launch_option(_load(), "331670", WRAP)
    assert _apps(cfg)["331670"]["LaunchOptions"] == WRAP


def test_set_launch_option_creates_app_block_if_absent():
    cfg = set_launch_option(_load(), "999999", WRAP)
    assert _apps(cfg)["999999"]["LaunchOptions"] == WRAP


def test_clear_launch_option_removes_only_when_it_matches():
    cfg = set_launch_option(_load(), "1148350", WRAP)
    cfg = clear_launch_option(cfg, "1148350", WRAP)
    assert "LaunchOptions" not in _apps(cfg)["1148350"]


def test_clear_launch_option_leaves_unrelated_value():
    cfg = clear_launch_option(_load(), "331670", WRAP)  # existing value != WRAP
    assert _apps(cfg)["331670"]["LaunchOptions"] == "existing %command%"


def test_jackbox_app_ids_filters_by_name(tmp_path):
    # build a fake steam library tree with two appmanifests
    lib = tmp_path / "steamapps"
    lib.mkdir()
    (lib / "appmanifest_1.acf").write_text(
        '"AppState"\n{\n\t"appid"\t"1"\n\t"name"\t"The Jackbox Party Pack 9"\n}\n'
    )
    (lib / "appmanifest_2.acf").write_text(
        '"AppState"\n{\n\t"appid"\t"2"\n\t"name"\t"Half-Life"\n}\n'
    )
    ids = jackbox_app_ids([lib])
    assert ids == {"1": "The Jackbox Party Pack 9"}
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_steam.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'extractor.steam'`.

- [ ] **Step 4: Write the implementation** `extractor/steam.py`

```python
"""Pure helpers for detecting installed Jackbox games and editing Steam's
per-game LaunchOptions in localconfig.vdf. No process side effects here; the
install.py / uninstall.py CLIs do the file I/O and Steam-running checks.
"""
from pathlib import Path
import vdf


def _apps(cfg: dict) -> dict:
    store = cfg.setdefault("UserLocalConfigStore", {})
    soft = store.setdefault("Software", {})
    # Steam writes the key as "Valve" but reads case-insensitively; match what's there.
    valve = soft.get("Valve") or soft.setdefault("Valve", {})
    steam = valve.get("Steam") or valve.setdefault("Steam", {})
    return steam.setdefault("apps", {})


def set_launch_option(cfg: dict, app_id: str, value: str) -> dict:
    """Return cfg with apps.<app_id>.LaunchOptions set to value."""
    apps = _apps(cfg)
    apps.setdefault(app_id, {})["LaunchOptions"] = value
    return cfg


def clear_launch_option(cfg: dict, app_id: str, value: str) -> dict:
    """Remove apps.<app_id>.LaunchOptions only if it equals value (our wrapper)."""
    apps = _apps(cfg)
    block = apps.get(app_id)
    if block and block.get("LaunchOptions") == value:
        block.pop("LaunchOptions", None)
    return cfg


def jackbox_app_ids(steamapps_dirs) -> dict:
    """Map app_id -> name for installed apps whose name contains 'jackbox'."""
    found: dict[str, str] = {}
    for d in steamapps_dirs:
        for acf in Path(d).glob("appmanifest_*.acf"):
            try:
                state = vdf.loads(acf.read_text()).get("AppState", {})
            except Exception:
                continue
            name = state.get("name", "")
            app_id = state.get("appid", "")
            if app_id and "jackbox" in name.lower():
                found[app_id] = name
    return found
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_steam.py -v` then `pytest -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add extractor/steam.py tests/test_steam.py tests/fixtures/localconfig_sample.vdf
git commit -m "Add Steam VDF helpers for launch-option management"
```

---

## Task 5: install / uninstall CLIs (target machine)

**Files:** Create `install.py`, `uninstall.py`.

- [ ] **Step 1: Write `install.py`**

```python
#!/usr/bin/env python3
"""Set the jbvip launch option on every installed Jackbox game.

Usage: python3 install.py        (Steam MUST be closed — it rewrites
localconfig.vdf on exit, clobbering edits made while it runs.)
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import vdf

from extractor.steam import jackbox_app_ids, set_launch_option

REPO = Path(__file__).resolve().parent
WRAP = f"{REPO / 'jbvip-run.sh'} %command%"


def steam_root() -> Path:
    for c in ("~/.steam/steam", "~/.local/share/Steam", "~/.steam/root"):
        p = Path(os.path.expanduser(c))
        if (p / "steamapps").exists():
            return p
    sys.exit("Could not find a Steam install under ~/.steam or ~/.local/share/Steam")


def library_steamapps(root: Path) -> list:
    dirs = [root / "steamapps"]
    lf = root / "steamapps" / "libraryfolders.vdf"
    if lf.exists():
        data = vdf.loads(lf.read_text()).get("libraryfolders", {})
        for entry in data.values():
            if isinstance(entry, dict) and "path" in entry:
                dirs.append(Path(entry["path"]) / "steamapps")
    return [d for d in dirs if d.exists()]


def preflight():
    if not shutil.which("bwrap"):
        sys.exit("bubblewrap (bwrap) is not installed — install it and retry.")
    if not shutil.which("mitmdump"):
        sys.exit("mitmproxy (mitmdump) not on PATH — `pipx install mitmproxy` and retry.")
    try:
        if int(Path("/proc/sys/user/max_user_namespaces").read_text()) <= 0:
            raise ValueError
    except (FileNotFoundError, ValueError):
        sys.exit("Unprivileged user namespaces appear disabled — required for bwrap.")
    if subprocess.run(["pgrep", "-x", "steam"], capture_output=True).returncode == 0:
        sys.exit("Steam is running. Quit Steam fully, then re-run install.py.")


def localconfig_paths(root: Path) -> list:
    return list((root / "userdata").glob("*/config/localconfig.vdf"))


def main():
    preflight()
    root = steam_root()
    games = jackbox_app_ids(library_steamapps(root))
    if not games:
        sys.exit("No installed Jackbox games found.")
    print("Found Jackbox games:", ", ".join(f"{n} ({i})" for i, n in games.items()))
    configs = localconfig_paths(root)
    if not configs:
        sys.exit("No Steam userdata/localconfig.vdf found — log into Steam once first.")
    for cfg_path in configs:
        cfg = vdf.loads(cfg_path.read_text())
        for app_id in games:
            cfg = set_launch_option(cfg, app_id, WRAP)
        shutil.copy2(cfg_path, str(cfg_path) + ".jbvip.bak")
        cfg_path.write_text(vdf.dumps(cfg, pretty=True))
        print(f"Updated {cfg_path} (backup: {cfg_path}.jbvip.bak)")
    print("Done. Launch any Jackbox game from Steam — the room code serves on TCP :38469.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Write `uninstall.py`**

```python
#!/usr/bin/env python3
"""Clear the jbvip launch option from every Jackbox game it was set on."""
import shutil
import sys
from pathlib import Path

import vdf

from extractor.steam import jackbox_app_ids, clear_launch_option
from install import WRAP, library_steamapps, localconfig_paths, steam_root


def main():
    root = steam_root()
    games = jackbox_app_ids(library_steamapps(root))
    for cfg_path in localconfig_paths(root):
        cfg = vdf.loads(cfg_path.read_text())
        for app_id in games:
            cfg = clear_launch_option(cfg, app_id, WRAP)
        cfg_path.write_text(vdf.dumps(cfg, pretty=True))
        print(f"Cleared jbvip launch options in {cfg_path}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Smoke-test import (no Steam writes)**

Run: `python -c "import install, uninstall; print('import ok')"`
Expected: prints `import ok` (verifies modules load and `extractor` is importable).

- [ ] **Step 4: Commit**

```bash
git add install.py uninstall.py
git commit -m "Add install/uninstall CLIs for Steam launch options"
```

---

## Task 6: Wire the addon into `jbvip-run.sh` (target machine)

**Files:** Modify `jbvip-run.sh`.

- [ ] **Step 1: Replace the mitmproxy startup + port handling**

In `jbvip-run.sh`, replace the `port_up` / start block so the launcher loads the addon and **fails loudly** if the port is taken by a non-jbvip process. The new middle of the script:

```bash
port_up() { ss -ltn 2>/dev/null | grep -qE "[:.]$PORT[[:space:]]"; }

# Refuse to silently reuse a stray proxy that has no addon loaded.
if port_up; then
  echo "jbvip: port $PORT is already in use; refusing to start. Set JBVIP_PROXY_PORT to a free port." >&2
  exit 1
fi

mitmdump --listen-port "$PORT" -s "$(dirname "$(readlink -f "$0")")/extractor/addon.py" \
  >"$STATE/mitm.log" 2>&1 &
MITM=$!
STARTED=1
for _ in $(seq 1 60); do { [ -f "$CA" ] && port_up; } && break; sleep 0.1; done
trap '[ "$STARTED" = 1 ] && kill "$MITM" 2>/dev/null || true' EXIT

export PYTHONPATH="$(dirname "$(readlink -f "$0")")${PYTHONPATH:+:$PYTHONPATH}"
```

Keep the existing `SYS_CA` discovery, `cat "$SYS_CA" "$CA" > "$BUNDLE"`, and the final `bwrap … -- "$@"` block unchanged. Remove the old `-w "$FLOWS"` capture line.

- [ ] **Step 2: Lint the script**

Run: `bash -n jbvip-run.sh`
Expected: no output (syntax OK).

- [ ] **Step 3: Commit**

```bash
git add jbvip-run.sh
git commit -m "Load extractor addon in launcher; fail loudly on port conflict"
```

---

## Task 7: Docs / README rewrite

**Files:** Modify `README.md`; mark old `docs/room-code-extraction-*.md` superseded.

- [ ] **Step 1: Rewrite `README.md`** to describe the new design

Replace the body with: what it does (serves the current Jackbox room code on TCP `:38469` as `RoomCode:<CODE>`); requirements (Linux, Steam, a Jackbox game, bubblewrap + unprivileged userns, `pipx install mitmproxy`); setup (`pip install -r requirements.txt`; run `python3 install.py` once with Steam closed); how it works (per-game bwrap-injected mitmproxy CA + `HTTPS_PROXY`, no system-wide trust, no root); uninstall (`python3 uninstall.py`); that the auto-VIP bot is future work. Remove all Windows/memory-pointer text.

- [ ] **Step 2: Add a superseded banner to the old docs**

At the top of both `docs/room-code-extraction-design.md` and `docs/room-code-extraction-implementation-plan.md`, add:
```markdown
> **SUPERSEDED (2026-06-03):** replaced by
> `docs/superpowers/specs/2026-06-03-jackbox-roomcode-extractor-design.md` and
> `docs/superpowers/plans/2026-06-03-jackbox-roomcode-extractor.md`. Kept for history.
```

- [ ] **Step 3: Commit**

```bash
git add README.md docs/room-code-extraction-design.md docs/room-code-extraction-implementation-plan.md
git commit -m "Rewrite README and mark old design docs superseded"
```

---

## Task 8: Remove obsolete C# project

**Files:** Delete `JackBoxAutoVIP/`, `JackBoxRoomCodeExtractor.sln`.

- [ ] **Step 1: Remove the C# project**

```bash
git rm -r JackBoxAutoVIP JackBoxRoomCodeExtractor.sln
```

- [ ] **Step 2: Verify the Python suite still passes**

Run: `pytest -q`
Expected: all pass (C# removal does not affect Python tests).

- [ ] **Step 3: Commit**

```bash
git commit -m "Remove obsolete C# memory-reader project"
```

---

## Task 9: Full verification

- [ ] **Step 1: Full offline suite**

Run: `pytest -q`
Expected: all tests pass.

- [ ] **Step 2: Launcher syntax + addon import**

Run: `bash -n jbvip-run.sh && PYTHONPATH=. python -c "import extractor.addon; print('addon ok')"`
Expected: prints `addon ok`.

- [ ] **Step 3: End-to-end (manual, target machine)**

Start a `nc 127.0.0.1 38469` client; launch a Jackbox game via Steam (launch option set by `install.py`); create a room. Expected: within ~1s the client prints `RoomCode:<CODE>` matching the on-screen code. Create a second room; confirm the new code is received.

- [ ] **Step 4: Commit any final notes** (if E2E surfaced fixture/parse tweaks, fold them back into Task 1 and re-run `pytest -q`).

---

## Self-review notes (author)

- **Spec coverage:** parser REST+fallback (§5.1 → Task 1), server unchanged contract + dedupe + prune (§5.2/§9 → Task 2), addon host-filter + both hooks (§5.3 → Task 3), launcher addon wiring + loud port handling (§5.4 → Task 6), installer detect+edit with VDF test (§5.5 → Tasks 4–5), docs + C# removal (§5.6 → Tasks 7–8), E2E ≤1s (§8 → Task 9).
- **Type consistency:** `extract_code_from_url`, `code_from_create_response`, `looks_like_code`; `RoomCodeServer(port).start()/set_code()/bound_port/client_count`; `RoomCodeExtractor(server).code_from_response_flow()/code_from_ws_flow()`; `set_launch_option/clear_launch_option/jackbox_app_ids` — used identically across tasks and tests.
- **Deferred (by design):** auto-VIP bot, legacy Blobcast (spec §10).
```
