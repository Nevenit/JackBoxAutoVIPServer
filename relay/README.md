# jbvip relay

Receives a host's Jackbox room code and pushes it to the phones following that
game night, so they get the code from anywhere — not just from the same wifi. It
also serves the page those phones open (`/j`), which frames jackbox.tv and swaps
in each new room as its code arrives. The unit of tenancy is a *session*: one
game night, created by the host's first launch of the night and kept across
every pack launched after it, with its own publisher and subscriber tokens that
die with it. Two sessions can never see each other's codes.

Deploy behind a reverse proxy that terminates TLS. The service speaks plain HTTP
and cannot verify its own transport, so the **clients** are what enforce
`https://`.

**This is a public service.** Anyone who can reach it can `POST /sessions` and
get back a working session — there is no admin credential, no invite step, and
nothing to provision per host. That is intended: sessions are cheap, short-lived
and self-contained, so there is no long-lived secret worth gatekeeping at that
door. What the relay does police is *resource use* — see Capacity below — and
per-client abuse, which belongs to the reverse proxy in front of it (see
Reverse proxy). Once this is deployed, the only host-side configuration is this
relay's URL, in `~/.config/jbvip/relay.env` — see the main `README.md`.

## What you need

- A box with systemd and a reverse proxy already terminating TLS for a hostname.
- [`uv`](https://docs.astral.sh/uv/). If it is not installed:

  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin sh
  ```

Python 3.11 or newer is required, but you do not need it preinstalled — `uv`
fetches a managed build if the distro's is too old.

## Install

```bash
sudo git clone https://github.com/Nevenit/JackBoxAutoVIPServer.git /opt/jbvip-relay
cd /opt/jbvip-relay
sudo uv venv --python '>=3.11'
sudo uv pip install -r relay/requirements.txt
```

If that used the system Python, you are done here. **If `uv` reports that it
downloaded a managed Python, redo it with a shared install directory** — a
managed interpreter otherwise lands under root's home, where the service account
cannot read it and `ProtectHome=yes` would block it regardless:

```bash
sudo rm -rf .venv
sudo env UV_PYTHON_INSTALL_DIR=/opt/uv/python uv python install 3.12
sudo env UV_PYTHON_INSTALL_DIR=/opt/uv/python uv venv --python 3.12
sudo uv pip install -r relay/requirements.txt
```

Either way, check the interpreter the venv points at is somewhere the service can
reach — anything under `/root` or `/home` will fail at first start:

```bash
readlink -f /opt/jbvip-relay/.venv/bin/python
```

Create the service account. It owns nothing and cannot log in:

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin jbvip
```

There is nothing else to prepare. `relay.app:create_app` takes **no**
environment variables and no config file — every credential a session needs is
minted at `POST /sessions` time and lives only in the process's memory.

## Run

Write `/etc/systemd/system/jbvip-relay.service`:

```ini
[Unit]
Description=jbvip relay (Jackbox room-code relay)
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
User=jbvip
Group=jbvip
WorkingDirectory=/opt/jbvip-relay

Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1

# No --workers. See the warning below; it is not a tuning knob here.
ExecStart=/opt/jbvip-relay/.venv/bin/uvicorn --factory relay.app:create_app \
          --host 127.0.0.1 --port 8479
Restart=on-failure
RestartSec=2

NoNewPrivileges=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectSystem=strict
ProtectHome=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
RestrictNamespaces=yes
LockPersonality=yes
SystemCallArchitectures=native
SystemCallFilter=@system-service

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now jbvip-relay
systemctl status jbvip-relay
```

`ProtectSystem=strict` makes the whole filesystem read-only for this process,
with no exception carved out anywhere. That is not a hardening compromise here —
`create_app()` takes no configuration and the relay keeps every session, its
tokens, and its rate limiters in memory only, so there is nothing on disk for it
to write, ever. (It does read the player page and the QR library under
`relay/static/`, which strict mode allows.) If the service fails to start on an
unusual kernel or container runtime, drop the hardening block first and add it
back a line at a time.

### Run exactly one worker

All relay state is held in memory, per process: every session, its publisher and
subscriber tokens, the current code, and all the rate limiters. A second worker
does not share any of that with the first.

With sessions this is worse than the old single-tenant design, not just as bad.
Before, a second worker meant the one room code sometimes went to the wrong
process. Now, a session created on worker A simply does not exist on worker B: a
host's `POST /sessions` can land on A while its phone's
`GET /sessions/{id}/subscribe` lands on B, and B returns a plain 404 for a
session ID it has never heard of. Neither side gets an error that points at the
real cause — the QR code was shown, the phone scanned it, and it still doesn't
work.

So do not add `--workers`, and do not put the service behind anything that forks
it, load-balances across processes, or otherwise runs more than one instance.
The unit above runs a single process on purpose.

## Reverse proxy

Point it at `127.0.0.1:8479`. It must **not** buffer responses, or the
server-sent-events stream will stall and clients will never receive a code.

- **Caddy** needs no special configuration for buffering.
- **nginx** needs `proxy_buffering off;` in the location block.

**Per-IP rate limiting belongs here, not in the relay.** `POST /sessions` is
unauthenticated by design (see above), so the relay can only cap its *total*
call rate across every caller — it has no way to single out one abusive IP
behind a shared reverse proxy, which is the layer that actually knows the true
client address. A concrete nginx example:

```nginx
# http {} block
limit_req_zone $binary_remote_addr zone=jbvip_sessions:10m rate=6r/m;

# site's location block
location = /sessions {
    limit_req zone=jbvip_sessions burst=3 nodelay;
    proxy_pass http://127.0.0.1:8479;
    proxy_buffering off;
}
```

Caddy needs the third-party [`caddy-ratelimit`](https://github.com/mholt/caddy-ratelimit)
module to do the same; there is no equivalent in stock Caddy.

## Operating it

**Logs.** Everything goes to the journal:

```bash
journalctl -u jbvip-relay -f
```

Room codes are never logged, by design, so the journal is safe to paste when
asking for help.

**Updating.**

```bash
cd /opt/jbvip-relay
sudo git pull
sudo uv pip install -r relay/requirements.txt
sudo systemctl restart jbvip-relay
```

Restarting drops **every** live session, not just the current code — the whole
in-memory table, and every publisher and subscriber token issued so far. There
is nothing to reconcile afterwards: any game in progress loses its relay
connection, and phones already on the page are told the night has ended. The
launcher's next run asks the relay about its saved session, gets a 404, makes a
fresh one and shows its QR code. There is no persisted state on the relay to
clean up or migrate.

## Capacity

Everything the relay knows lives in one process's memory: up to 500 concurrent
sessions, each capped at 16 subscribers (a Jackbox room holds about ten
players). A session ends at the first of these:

- **2 hours without a room.** Counted from when its room was cleared, or from
  when it was created if it never had one. Activity does not move this clock: a
  phone listening to an empty session touches it every keepalive, and that must
  not keep a finished night alive.
- **4 hours idle.** Counted from its last publish or clear, or from the last
  time a phone connected or its stream sent a keepalive.
- **12 hours old.** A ceiling on a session's whole life, however busy.

All of those numbers are `relay/state.py` constants, not runtime configuration.

When a session ends its subscribers' streams are closed (within one 20-second
keepalive), and the next request for it gets a 404, which the page reports as
the night having ended.

A full session admits a newcomer rather than refusing it: at 16 streams the
relay drops the session's oldest one. A phone that slept or changed network
leaves a stream that stays counted until a keepalive write to it fails, so a
full session is likelier to hold dead streams than 16 live phones. A dropped
page reconnects by itself, with a backoff.

Because none of it is on disk, a restart — a deploy, a crash, a reboot — drops
every session at once, exactly as described above. There is no recovery beyond
the launcher making a new session on its next run and the phones scanning its
QR code.

## Endpoints

| Method | Path                         | Auth              | Purpose                                                                     |
|--------|------------------------------|-------------------|------------------------------------------------------------------------------|
| POST   | `/sessions`                  | none              | Create a session → 201 `{session_id, publisher_token, subscriber_token, expires_at}` |
| GET    | `/sessions/{id}`             | publisher token   | `{subscribers, expires_at}`: the launcher checks tonight's session is alive, and polls it while the QR window waits for a phone |
| POST   | `/sessions/{id}/publish`     | publisher token   | Push `{code, ts?}` → 204                                                     |
| DELETE | `/sessions/{id}/code`        | publisher token   | Clear the room: the host quit a pack; phones go back to waiting → 204        |
| GET    | `/sessions/{id}/subscribe`   | subscriber token  | SSE stream of `{code, ts}`; `code` is `null` when there is no room           |
| DELETE | `/sessions/{id}`             | publisher token   | End the session immediately → 204                                            |
| GET    | `/`                          | none              | A one-line landing page                                                      |
| GET    | `/j`                         | none              | The player page (`relay/static/player.html`)                                 |
| GET    | `/static/*`                  | none              | Files under `relay/static/`, including the vendored QR library              |

`POST /sessions` is deliberately unauthenticated — see "This is a public
service" above. `GET /j` is too: the subscriber token travels in the URL
**fragment** (`/j#<session_id>.<subscriber_token>`), which a browser never
sends to any server, so the page that receives the request never sees it
either. The page then sends the token in an `Authorization: Bearer` header when
it subscribes, and loads no third-party script (the QR library is served from
`/static/`).

A session ID alone is not a capability: `GET`/`POST`/`DELETE` on
`/sessions/{id}` with no token, or the wrong one, gets a 401. A session ID that
does not exist — expired, deleted, or never created — gets a 404, so a caller
can always tell "wrong token" apart from "this session is gone".

## Current code

A subscriber is told the session's current room the moment it connects, however
long ago the host published it: `{"code": "ABCD", "ts": …}`. When the session has
no room (before the first game, or after a pack quit) it is told
`{"code": null, "ts": …}` instead. That matters because a phone closes its
stream while its page is hidden, so a clear can happen while it isn't listening;
on reconnecting it learns the room is gone. A code stays current until the host
publishes another or clears it, and publishing the same code after a clear
reaches the phones again.

Serving a code at any age is safe because a code is only current while its pack
is running: the launcher clears it when the pack exits, with
`DELETE /sessions/{id}/code`. The relay used to refuse a code older than 120
seconds, a rule from the single-tenant relay, where a code could outlive the
night and belong to a stranger's room by the next day. Now the code lives inside
a session that ends with the night. What is left is a closed room's code between
two games of the same pack, or after a launcher crash that skipped the clear.
jackbox.tv then says the room doesn't exist, which is acceptable.

The `ts` in each payload is the host's stamp from the moment it extracted the
code (for a `null` code, the relay's own clock), passed through unchanged.
Nothing gates on it: not the relay, and not the player page.
