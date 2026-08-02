# jbvip relay

Receives the host's Jackbox room code and pushes it to subscribed clients.
Deploy behind a reverse proxy that terminates TLS — the service speaks plain
HTTP and cannot verify its own transport, so the clients enforce `https://`
instead.

## Install

Requires Python 3.11 or newer.

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
|--------|------------------|------------------|-------------------------------------------|
| POST   | `/publish`       | publisher token  | Host pushes `{"code","ts"}`              |
| GET    | `/subscribe`     | subscriber token | SSE stream of codes                      |
| POST   | `/pairing-codes` | publisher token  | Mint a pairing code                      |
| POST   | `/pair`          | pairing code     | Redeem for a durable subscriber token    |

Codes older than 120 seconds are never delivered — Jackbox recycles four-letter
codes, and a stale one may belong to a stranger's room.
