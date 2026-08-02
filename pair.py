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
