# JackBoxAutoVIPServer

## Introduction

JackBoxAutoVIPServer extracts the current Jackbox lobby room code on the host
machine and serves it over a tiny TCP protocol so other tools on your LAN can
read it automatically. It is the first piece of an auto-VIP setup: once a client
or bot can reliably read the room code, it can join the room and claim the VIP
seat for you. (The auto-join VIP bot itself is future work — see below.)

## What It Does

When you launch a Jackbox game, the server intercepts the game's own backend
(ecast) traffic, reads the room code straight out of the `POST /api/v2/rooms`
response, and broadcasts it to every connected client as the ASCII bytes
`RoomCode:<CODE>` (no terminator) on TCP port **38469**. New clients receive the
current code the moment they connect; whenever the code changes (a new room) the
new code is broadcast. The server binds `0.0.0.0`, so a client elsewhere on the
LAN can connect.

Because it reads the code from the game's own network traffic, it works
uniformly across every Jackbox game and survives game updates — there is nothing
per-game or per-version to maintain.

## Requirements

- Linux
- Steam, with at least one Jackbox game installed
- [bubblewrap](https://github.com/containers/bubblewrap) (`bwrap`) with
  unprivileged user namespaces enabled
- [mitmproxy](https://mitmproxy.org/) on your `PATH` — `pipx install mitmproxy`

## Setup

1. Clone the repository and install the Python dependencies:

   ```bash
   git clone https://github.com/username/JackBoxAutoVIPServer.git
   cd JackBoxAutoVIPServer
   python -m venv .venv && . .venv/bin/activate
   pip install -r requirements.txt
   ```

2. With **Steam fully closed** (Steam rewrites its config on exit and would
   clobber the change), run the installer once:

   ```bash
   python3 install.py
   ```

   This finds every installed Jackbox game and sets its Steam launch option to
   `<repo>/jbvip-run.sh %command%`.

3. Start Steam and launch any Jackbox game normally. The room code is now served
   on TCP `:38469`. Test it with:

   ```bash
   nc 127.0.0.1 38469
   ```

   Create a room in the game and the client prints `RoomCode:<CODE>`.

## How It Works

Each Jackbox game is launched through `jbvip-run.sh`, which:

- starts `mitmproxy` (loading `extractor/addon.py`) only while the game runs;
- builds a CA bundle of your system CAs plus the mitmproxy CA, and injects it
  into the game's **own mount namespace only** via
  `bwrap --bind <bundle> /etc/ssl/certs/ca-certificates.crt`;
- routes the game's HTTPS through the proxy with the `HTTPS_PROXY` env var.

No system-wide trust changes, no root, and nothing persists: the injected CA is
visible only to that one game process and vanishes when the game exits. The
addon watches the game's ecast traffic, extracts the room code, and hands it to
the asyncio TCP server that speaks the `RoomCode:<CODE>` protocol.

## Uninstall

With Steam closed, clear the launch options the installer added:

```bash
python3 uninstall.py
```

## Future Work

- An auto-join VIP bot that consumes the `RoomCode:<CODE>` stream and takes the
  VIP seat for you automatically.

## Contributions

I welcome contributors who want to make this project better and bigger. If you
would like to contribute, please fork the repository, make your changes on a
branch, and submit a pull request with a clear description of the changes.

## Support

Please feel free to submit an issue for any questions, bug reports, or feature
requests.

## License

MIT License

## Appreciation

Whether you're helping me fix bugs, proposing new features, improving the
documentation, or spreading the word, thank you! Please enjoy the program.
