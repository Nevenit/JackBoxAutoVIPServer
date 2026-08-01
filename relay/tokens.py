"""Subscriber-token persistence — the relay's only disk write.

Written atomically via a temp file plus os.replace so an interrupted save
cannot leave a truncated token list, which would silently unpair every phone.
"""
import json
import os
import tempfile
from pathlib import Path
from typing import Iterable


def load_tokens(path) -> list[str]:
    path = Path(path)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (ValueError, OSError):
        return []
    if not isinstance(data, dict):
        return []
    tokens = data.get("subscriber_tokens")
    if not isinstance(tokens, list):
        return []
    return sorted(token for token in tokens if isinstance(token, str))


def save_tokens(path, tokens: Iterable[str]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".tokens-", suffix=".json"
    )
    try:
        with os.fdopen(handle, "w") as stream:
            json.dump({"subscriber_tokens": sorted(tokens)}, stream)
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    except BaseException:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
        raise
