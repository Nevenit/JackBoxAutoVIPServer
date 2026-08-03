# jbvip relay

Receives the host's Jackbox room code and pushes it to subscribed clients, so a
phone anywhere can get the code — not just one on the same wifi.

Deploy behind a reverse proxy that terminates TLS. The service speaks plain HTTP
and cannot verify its own transport, so the **clients** are what enforce
`https://`.

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
sudo git clone https://github.com/username/JackBoxAutoVIPServer.git /opt/jbvip-relay
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

You do **not** need to create `/var/lib/jbvip` — the unit's `StateDirectory=`
makes it with the right owner and mode on first start.

## Configure

Generate the publisher token. It is the host PC's credential **and** the admin
credential — anyone holding it can publish codes and mint pairing codes:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Put it in an environment file that only root can read, so it stays out of the
unit file, `ps` output and your shell history:

```bash
sudo install -d -m 755 /etc/jbvip
printf 'JBVIP_PUBLISHER_TOKEN=%s\n' '<the token>' | sudo tee /etc/jbvip/relay.env >/dev/null
sudo chmod 600 /etc/jbvip/relay.env
```

On the **host PC**, the same token and the relay's URL go in
`~/.config/jbvip/relay.env` — see the main README. That has to be a file rather
than exported variables, because Steam launches the game with the environment
the Steam client itself started with.

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
EnvironmentFile=/etc/jbvip/relay.env

# systemd creates and owns /var/lib/jbvip; %S expands to /var/lib.
StateDirectory=jbvip
StateDirectoryMode=0700
Environment=JBVIP_TOKENS_FILE=%S/jbvip/tokens.json
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

`ProtectSystem=strict` makes the whole filesystem read-only except the state
directory. That is deliberate — the relay only ever writes `tokens.json`. If the
service fails to start on an unusual kernel or container runtime, drop the
hardening block first and add it back a line at a time.

### Run exactly one worker

All relay state is held in memory, per process: the current room code, the
subscriber tokens, the pairing codes, and all three rate limiters. A second
worker silently receives none of the first's codes — a publish lands on one
while the subscriber is connected to the other, and nothing is delivered, with
no error anywhere.

So do not add `--workers`, and do not put the service behind anything that forks
it. The unit above runs a single process on purpose.

## Reverse proxy

Point it at `127.0.0.1:8479`. It must **not** buffer responses, or the
server-sent-events stream will stall and clients will never receive a code.

- **Caddy** needs no special configuration.
- **nginx** needs `proxy_buffering off;` in the location block.

## Operating it

**Logs.** Everything goes to the journal:

```bash
journalctl -u jbvip-relay -f
```

Room codes are never logged, by design, so the journal is safe to paste when
asking for help.

**Revoking a subscriber.** Remove its token from
`/var/lib/jbvip/tokens.json` and restart. The token set is read only at startup,
so the restart is what makes the removal take effect:

```bash
sudo systemctl restart jbvip-relay
```

**Updating.**

```bash
cd /opt/jbvip-relay
sudo git pull
sudo uv pip install -r relay/requirements.txt
sudo systemctl restart jbvip-relay
```

Restarting drops connected subscribers and forgets the current code; both
recover on their own, since clients reconnect and the next room republishes.
Unredeemed pairing codes do not survive a restart — mint a fresh one.

**If pairing appears to work but does not stick.** A `tokens.json` that cannot be
written makes `/pair` still return a working token, but that token is lost on the
next restart, and the relay logs a warning saying exactly that. Check the journal
and the ownership of `/var/lib/jbvip`.

## Endpoints

| Method | Path             | Auth             | Purpose                                               |
|--------|------------------|------------------|-------------------------------------------------------|
| POST   | `/publish`       | publisher token  | Host pushes `{"code"}`; optional `ts` defaults to now |
| GET    | `/subscribe`     | subscriber token | SSE stream of codes                                   |
| POST   | `/pairing-codes` | publisher token  | Mint a pairing code                                   |
| POST   | `/pair`          | pairing code     | Redeem for a durable subscriber token                 |

`/pair` is deliberately unauthenticated: a phone redeeming a pairing code has no
credential yet, so the single-use five-minute code *is* the credential. It has
its own rate-limit budget, so hammering it cannot lock you out of minting.

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
