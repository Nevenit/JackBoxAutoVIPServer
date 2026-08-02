"""Subscriber-token persistence — the relay's only disk write.

Written atomically via a temp file, fsync and os.replace so neither an
interrupted save nor a power loss can leave a truncated token list, which
would silently unpair every phone.
"""
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)


def load_tokens(path) -> list[str]:
    """Every known subscriber token, or [] if the file is missing or unusable.

    An unusable file is warned about, because it is otherwise indistinguishable
    from a missing one: every phone silently stops working, and the next
    successful /pair overwrites the file with just the new token. The warning
    never includes the file's contents.
    """
    path = Path(path)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (ValueError, OSError) as exc:
        logger.warning(
            "could not read subscriber tokens from %s (%s); continuing with "
            "none, and the next pairing will overwrite the file",
            path,
            exc,
        )
        return []
    tokens = data.get("subscriber_tokens") if isinstance(data, dict) else None
    if not isinstance(tokens, list):
        logger.warning(
            "%s has no subscriber_tokens list; continuing with none, and the "
            "next pairing will overwrite the file",
            path,
        )
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
            # Without this the rename can land before the data does, so a power
            # loss leaves an empty file where the token list was — the very
            # thing os.replace is here to prevent.
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    except BaseException:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
        raise
