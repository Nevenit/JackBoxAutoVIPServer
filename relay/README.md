# jbvip relay

Receives the host's Jackbox room code and pushes it to subscribed clients.
Deploy behind a reverse proxy that terminates TLS — the service speaks plain
HTTP and cannot verify its own transport, so the clients enforce `https://`
instead.

## Install

Requires Python 3.11 or newer.

Get the repository onto the VPS and work from its root. Every command below
uses paths relative to it, and `--factory relay.app:create_app` needs the
`relay` package importable from the working directory:

```bash
git clone https://github.com/username/JackBoxAutoVIPServer.git
cd JackBoxAutoVIPServer
```

Create the two directories the service needs, owned by the user it will run as
(`<svcuser>` below). `/var/lib/jbvip` is not optional: `save_tokens` calls
`mkdir(parents=True)`, which a non-root service user cannot do against
root-owned `/var/lib`, and the resulting failure is exactly what stops a phone
being paired.

```bash
sudo install -d -o <svcuser> -g <svcuser> -m 700 /var/lib/jbvip
sudo install -d -o <svcuser> -g <svcuser> -m 755 /opt/jbvip-relay
```

Then create the virtualenv and install the dependencies:

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
  subscriber tokens are stored. Must be writable by the service user. If it is
  not, pairing still returns a working token, but that token is lost on the
  next restart and the relay logs a warning saying so.

On the **host PC**, put the same publisher token and the relay's URL in
`~/.config/jbvip/relay.env`; see the main README. It has to be a file rather
than exported variables because Steam launches the game with the environment
the Steam client itself started with.

## Run

```bash
JBVIP_PUBLISHER_TOKEN=... /opt/jbvip-relay/venv/bin/uvicorn \
    --factory relay.app:create_app --host 127.0.0.1 --port 8479
```

Run this from the repository root — uvicorn resolves `relay.app` from the
working directory.

**Run exactly one worker.** All relay state is held in memory per process — the
current code, the subscriber tokens, the pairing codes and all three rate
limiters — so a second worker silently receives none of the first's codes: a
publish lands on one worker while the subscriber is connected to the other, and
nothing is ever delivered, with no error anywhere. Do not pass `--workers`, and
do not put the service behind a process manager that forks it.

Point your reverse proxy at `127.0.0.1:8479`. It must **not** buffer responses,
or the SSE stream will stall — for nginx set `proxy_buffering off;`. Caddy needs
no special configuration.

## Revoking a subscriber

Remove its token from `JBVIP_TOKENS_FILE` and restart the relay. The token set
is only read at startup, so the restart is what makes the removal take effect.

## Endpoints

| Method | Path             | Auth             | Purpose                                               |
|--------|------------------|------------------|-------------------------------------------------------|
| POST   | `/publish`       | publisher token  | Host pushes `{"code"}`; optional `ts` defaults to now |
| GET    | `/subscribe`     | subscriber token | SSE stream of codes                                   |
| POST   | `/pairing-codes` | publisher token  | Mint a pairing code                                   |
| POST   | `/pair`          | pairing code     | Redeem for a durable subscriber token                 |

A subscriber that connects fresh is never handed a code the relay received more
than 120 seconds ago — Jackbox recycles four-letter codes, and a stale one may
belong to a stranger's room by the next day. That window is measured on the
relay's own clock, so skew on the host PC (a dual-boot machine writing localtime
to the RTC is hours out) cannot make every code look stale or every code look
fresh.

The `ts` in each payload is a different thing: it is the host's stamp from the
moment it extracted the code, so a client can run its own end-to-end freshness
check. Clients are expected to enforce the same 120-second window against it
before acting on a code. Codes arriving on an already-open stream are forwarded
as published, ungated.
