import io
import json

import pytest

import pair


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False


def opener_returning(payload):
    calls = []

    def opener(request, timeout=None):
        calls.append(request)
        return FakeResponse(json.dumps(payload).encode())

    opener.calls = calls
    return opener


def test_mint_returns_the_pairing_code():
    opener = opener_returning({"pairing_code": "ABC234"})
    assert pair.mint("https://relay.example", "pub", opener=opener) == "ABC234"


def test_mint_targets_the_pairing_codes_endpoint():
    opener = opener_returning({"pairing_code": "ABC234"})
    pair.mint("https://relay.example/", "pub", opener=opener)
    assert opener.calls[0].full_url == "https://relay.example/pairing-codes"


def test_mint_sends_the_publisher_token_as_a_bearer():
    opener = opener_returning({"pairing_code": "ABC234"})
    pair.mint("https://relay.example", "pub", opener=opener)
    assert opener.calls[0].get_header("Authorization") == "Bearer pub"


def test_mint_refuses_a_plaintext_url():
    with pytest.raises(ValueError):
        pair.mint("http://relay.example", "pub", opener=opener_returning({}))


def test_main_reports_missing_configuration(monkeypatch, capsys):
    monkeypatch.delenv("JBVIP_RELAY_URL", raising=False)
    monkeypatch.delenv("JBVIP_RELAY_TOKEN", raising=False)
    assert pair.main() == 2
    assert "JBVIP_RELAY_URL" in capsys.readouterr().err


def test_main_prints_the_host_and_code(monkeypatch, capsys):
    monkeypatch.setenv("JBVIP_RELAY_URL", "https://relay.example")
    monkeypatch.setenv("JBVIP_RELAY_TOKEN", "pub")
    monkeypatch.setattr(pair, "mint", lambda url, token: "ABC234")
    assert pair.main() == 0
    out = capsys.readouterr().out
    assert "relay.example" in out
    assert "ABC234" in out


def test_main_reports_a_failed_mint(monkeypatch, capsys):
    monkeypatch.setenv("JBVIP_RELAY_URL", "https://relay.example")
    monkeypatch.setenv("JBVIP_RELAY_TOKEN", "pub")

    def explode(url, token):
        raise OSError("relay unreachable")

    monkeypatch.setattr(pair, "mint", explode)
    assert pair.main() == 1
    assert "relay unreachable" in capsys.readouterr().err
