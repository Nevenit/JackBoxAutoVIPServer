# JackBoxAutoVIPServer

## Introduction

JackBoxAutoVIPServer extracts the current Jackbox lobby room code on the host
machine and serves it over a tiny TCP protocol so other tools on your LAN can
read it automatically. It is the first piece of an auto-VIP setup: once a client
or bot can reliably read the room code, it can join the room and claim the VIP
seat for you. (The auto-join VIP bot itself is future work — see below.)

## What It Does

When you launch a Jackbox game, the server intercepts the game's own backend
traffic, reads the room code straight out of it, and broadcasts it to every
connected client as the ASCII bytes `RoomCode:<CODE>` (no terminator) on TCP port
**38469**. New clients receive the current code the moment they connect; whenever
the code changes (a new room) the new code is broadcast. The server binds
`0.0.0.0`, so a client elsewhere on the LAN can connect.

It understands all three room-code protocols Jackbox packs use, so it works
across both current and older packs:

- **ecast** (newer packs) — the code is in the `POST /api/v2/rooms` response body.
- **Blobcast over REST** (some older packs) — the code is the `roomId` in the
  host's `POST /accessToken` request.
- **Blobcast over socket.io** (oldest packs) — the code arrives in the
  `CreateRoom` websocket frame.

Because it reads the code from the game's own network traffic, it survives game
updates — there is nothing per-game or per-version to maintain.

## Requirements

- Linux
- Steam, with at least one Jackbox game installed
- [bubblewrap](https://github.com/containers/bubblewrap) (`bwrap`)
- [mitmproxy](https://mitmproxy.org/) on your `PATH` — `mitmproxy` on Arch, or
  `pipx install mitmproxy`
- Python's `vdf` module, which `install.py` uses to edit Steam's config
  (`python-vdf` on Arch, or `pip install vdf`)
- `iptables` and the cgroup v1 `net_cls` controller (the redirect script mounts
  it if nothing has) — used to redirect **only the game's** traffic to the proxy.
  Setting this up is one `sudo` command per session, or a boot-time service.
- [`qrencode`](https://fukuchi.org/works/qrencode/) — only needed if you use
  the relay (below); it renders the QR code shown on the TV. Most distros
  package it (`qrencode` on Arch/Debian/Fedora).
- Python's `tkinter` — also only needed for the relay; it draws the QR card
  itself. Arch/CachyOS need the `tk` package (the `python` package has the
  module but not the Tk library); Debian/Ubuntu need `python3-tk`; Fedora
  needs `python3-tkinter`.

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
   `<repo>/jbvip-run.sh %command%`. It leaves The Jackbox Megapicker alone on
   purpose: the Megapicker starts each pack through Steam, so every pack it
   launches is wrapped already, and wrapping the Megapicker itself would hold the
   proxy port for as long as it stays open.

3. **Once per session** (e.g. after a reboot), activate the game-scoped traffic
   redirect. It needs `sudo` and stays active until you remove it or reboot:

   ```bash
   sudo ./jbvip-redirect-up.sh
   ```

   This redirects only the Jackbox game's `:443` and `:38203` to the proxy —
   nothing else on the machine is touched. Remove it any time with
   `sudo ./jbvip-redirect-down.sh`.

   On a machine that should just work after a reboot, such as a PC under the
   TV, install a service that does this at boot instead. Run this from the repo
   folder:

   ```bash
   sudo install -m 755 -o root -g root jbvip-redirect-up.sh jbvip-redirect-down.sh /usr/local/sbin/
   sudo tee /etc/systemd/system/jbvip-redirect.service >/dev/null <<EOF
   [Unit]
   Description=Redirect Jackbox games to the jbvip room-code proxy
   After=network-pre.target ufw.service firewalld.service

   [Service]
   Type=oneshot
   RemainAfterExit=yes
   ExecStart=/usr/local/sbin/jbvip-redirect-up.sh 8081 $USER
   ExecStop=/usr/local/sbin/jbvip-redirect-down.sh 8081

   [Install]
   WantedBy=multi-user.target
   EOF
   sudo systemctl daemon-reload && sudo systemctl enable --now jbvip-redirect.service
   ```

   The copies are root-owned on purpose. A script in your home folder run as
   root at boot would let anything running as you become root. After a
   `git pull` that changes either script, re-run the `install` line.

   The proxy listens on port 8081. If something else is using that port, move to
   another (say 8082) in this order: remove the old redirect with
   `sudo ./jbvip-redirect-down.sh 8081` (or reboot), then run
   `sudo ./jbvip-redirect-up.sh 8082`, then add `JBVIP_PROXY_PORT=8082` to
   `~/.config/jbvip/relay.env`, the file the relay section below describes (the
   launcher reads it on every launch, relay or no relay). The removal has to
   come first because `jbvip-redirect-down.sh` also deletes the cgroup the new
   redirect needs; skipping it would leave the old port's rules catching the
   game's traffic, since `jbvip-redirect-up.sh` only adds rules. Pass the port
   to `jbvip-redirect-down.sh` whenever it isn't 8081.

4. Start Steam and launch any Jackbox game normally. The room code is now served
   on TCP `:38469`. Test it with:

   ```bash
   nc 127.0.0.1 38469
   ```

   Create a room in the game and the client prints `RoomCode:<CODE>`.

## How It Works

Each Jackbox game is launched through `jbvip-run.sh`, which:

- starts `mitmproxy` in transparent mode (loading `extractor/addon.py`) only
  while the game runs;
- builds a CA bundle of your system CAs plus the mitmproxy CA and injects it into
  the game's **own mount namespace only** via
  `bwrap --bind <bundle> /etc/ssl/certs/ca-certificates.crt`;
- puts the game in a `net_cls` cgroup whose `:443`/`:38203` traffic the kernel
  redirect (`jbvip-redirect-up.sh`) sends to the proxy.

The game process is never modified or injected into — the redirect happens in the
kernel, scoped to the game's cgroup — so even fragile older (Adobe AIR) packs run
normally. The mitmproxy CA is trusted **only** inside that one game's mount
namespace and vanishes when the game exits; the rest of the system never trusts
it. The addon watches the game's traffic, extracts the room code (ecast or
Blobcast), and hands it to the asyncio TCP server that speaks `RoomCode:<CODE>`.

A transparent redirect (rather than the `HTTPS_PROXY` env var the project first
used) is required because the oldest packs open their room-code websocket as a
raw TLS socket that ignores proxy settings — only a kernel-level redirect catches
it, and unlike injecting a library into the game it doesn't crash the AIR runtime.

If a launch can't capture codes — the redirect from step 3 isn't up, there is no
system CA bundle, bubblewrap can't start the game's sandbox, the proxy port is
already in use, or mitmdump won't start — the game still starts, just without
room-code capture, and a desktop notification (if `notify-send` is installed)
says what is wrong and how to fix it. None of these launches shows the QR card
(below), so there is nothing to scan. A busy port is usually the last launch's
proxy still closing after you quit a pack: wait a few seconds and launch again.

## Reaching a Phone That Isn't on Your Wifi (optional)

By default the room code is served only on your LAN, over TCP 38469. If you want
a client to receive it from anywhere — a phone on cellular, or a friend's
house — you can run the small relay in `relay/` on a VPS. The host pushes the
code out over ordinary outbound HTTPS, so there is nothing to port-forward and
nothing new listening on your machine. The relay also serves a web page that puts
each new room on the players' phones, so nobody installs anything or types a
code.

See `relay/README.md` for deployment. Once it is running, add its URL to
`~/.config/jbvip/relay.env` — the launcher reads it on every game launch. The
`>>` below creates the file if it is missing and keeps what is already in it,
such as a `JBVIP_PROXY_PORT` line:

```bash
mkdir -p ~/.config/jbvip
cat >> ~/.config/jbvip/relay.env <<'EOF'
JBVIP_RELAY_URL=https://your-relay.example
EOF
chmod 600 ~/.config/jbvip/relay.env
```

A file rather than `export` because Steam launches the game with the environment
the Steam client itself started with, so variables exported in a terminal never
reach it.

**Optional: an admin password.** Add `JBVIP_ADMIN_PASSWORD='your-password'` to
`relay.env` and each night's launch registers a key derived from it (the
password itself never leaves this PC). `relay.env` is read by bash, so single
quotes keep spaces, `$`, quotes and backticks literal; a literal `'` has to be
written `'\''`. Use a password you don't use anywhere else. Then open the
page's menu on a phone, type the same password under `Admin password` and press
`Become admin` (the menu then offers `Priority: on/off` and `Stop being
admin`). Admins get each new room first. Guests are held until the first
player joins (normally the admin tapping Play), or for at most
`JBVIP_ADMIN_HEAD_START` seconds (default 10, at most 30). With no password
set nothing is registered and nothing changes for anyone.

**There is no token to configure any more.** The relay mints every credential.
The first launch of the night creates a *session* on it, and the launcher keeps
that one session for the whole night, across every pack you launch, in
`${XDG_STATE_HOME:-~/.local/state}/jbvip/night.json` (readable only by you).
Later launches reuse it for as long as the relay still has it, and make a new
one if it doesn't (the relay restarted, say). Its credentials work for that one
night and nothing else. When you quit a pack, the launcher clears the room, not
the session, so phones stay on it and go back to waiting. The relay ends a
session on its own once it has had no room for 2 hours; `relay/README.md` has
the other limits.

Once `JBVIP_RELAY_URL` is configured, the first launch of the night puts a QR
code on the TV, over the game. Scan it with a phone's camera: it opens a
page on the relay (`/j`) that asks for a name — once per phone — and then shows
jackbox.tv in a frame with the room code and that name already filled in. You
still tap Play yourself. From then on the phone follows the host from game to
game, across packs and Megapicker launches, with nothing to scan again. When a
pack is quit, the page goes back to "Waiting for the host to pick a game…" until
the next one starts.

Any player can bring in more people: the page's **Invite** button shows a QR code
of the page's own link, and **Share link…** sends it on. Whoever opens that link
joins the same night, so share it only with people you would give the room code
to.

The QR sits on a white card in the top-right of the screen, over the game, until
someone decides everyone is in: tap **Hide QR on TV** in the page's ⋯ menu on a
phone, or click the × on the card at the PC. On a PC with several monitors the
card goes in the top-right of the primary monitor, found with `xrandr` (Arch
package `xorg-xrandr`); without it, the card goes in the top-right of the whole
desktop, which can be a different monitor. The QR stays hidden for the rest of
the night, through every pack you switch to. A launch while no phone is connected
(everyone has left or locked their phone; a page stays connected only while it
is on screen) brings it back, and so does the first launch of a new night. The
card never takes the keyboard from the game, and the game never waits for it: a
dead or unreachable relay changes nothing about that, and the game **always**
launches, LAN-only if the relay can't be reached and with phones following it if
it can.

If the relay does not seem to be working, launch a game once and then look at
`~/.local/state/jbvip/last-run.log`. The launcher keeps the proxy log there —
and only there, and only when that run actually reported a relay problem, with
room codes redacted out of it. Nothing is kept when the relay is working or
when it is not configured at all.

The relay is entirely optional. Without that file, everything works exactly as
before, LAN-only.

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
