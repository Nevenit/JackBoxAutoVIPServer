# jbvip relay

Receives a host's Jackbox room code and pushes it to a paired phone, so it can
get the code from anywhere — not just from the same wifi. The unit of tenancy
is a *session*: one game night, created when the launcher starts and deleted
when it exits, with its own publisher and subscriber tokens that die with it.
Two sessions can never see each other's codes.

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
to write, ever. If the service fails to start on an unusual kernel or container
runtime, drop the hardening block first and add it back a line at a time.

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
connection, the launcher's next run creates a fresh session, and its phone
scans a fresh QR code. There is no persisted state to clean up or migrate.

## Capacity

Everything the relay knows lives in one process's memory: up to 500 concurrent
sessions, each capped at 16 subscribers (a Jackbox room holds about ten
players), each idled out four hours after its last publish or subscribe. All
three numbers are `relay/state.py` constants, not runtime configuration.

Because none of it is on disk, a restart — a deploy, a crash, a reboot — drops
every session at once, exactly as described above. There is no recovery beyond
the launcher creating a new one and the phone re-scanning its QR code.

## Endpoints

| Method | Path                         | Auth              | Purpose                                                                     |
|--------|------------------------------|-------------------|------------------------------------------------------------------------------|
| POST   | `/sessions`                  | none              | Create a session → 201 `{session_id, publisher_token, subscriber_token, expires_at}` |
| GET    | `/sessions/{id}`             | publisher token   | `{subscribers, expires_at}`, for the launcher's pairing window to poll       |
| POST   | `/sessions/{id}/publish`     | publisher token   | Push `{code, ts?}` → 204                                                     |
| GET    | `/sessions/{id}/subscribe`   | subscriber token  | SSE stream of `{code, ts}`                                                   |
| DELETE | `/sessions/{id}`             | publisher token   | End the session immediately → 204                                            |
| GET    | `/j`                         | none              | The QR code's landing page (HTML)                                            |

`POST /sessions` is deliberately unauthenticated — see "This is a public
service" above. `GET /j` is too: the subscriber token travels in the URL
**fragment** (`/j#<session_id>.<subscriber_token>`), which a browser never
sends to any server, so the page that receives the request never sees it
either.

A session ID alone is not a capability: `GET`/`POST`/`DELETE` on
`/sessions/{id}` with no token, or the wrong one, gets a 401. A session ID that
does not exist — expired, deleted, or never created — gets a 404, so a caller
can always tell "wrong token" apart from "this session is gone".

## Freshness

A subscriber that connects fresh is never handed a code the relay received more
than 120 seconds ago — Jackbox recycles four-letter codes, and a stale one may
belong to a stranger's room by the next day. That window is measured on the
relay's own clock, so skew on the host PC (a dual-boot machine writing localtime
to the RTC is hours out) cannot make every code look stale, or every code look
fresh.

The `ts` in each payload is a different thing: the host's stamp from the moment
it extracted the code, so a client can run its own end-to-end freshness check.
Clients are expected to enforce the same 120-second window against it before
acting on a code. Codes arriving on an already-open stream are forwarded as
published, ungated.
