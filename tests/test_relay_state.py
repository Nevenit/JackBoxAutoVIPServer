import pytest

from relay.state import (
    FRESH_SECONDS,
    PAIRING_ALPHABET,
    PAIRING_CODE_LENGTH,
    PAIRING_TTL_SECONDS,
    RateLimiter,
    RelayState,
)


class Clock:
    """A hand-cranked clock so no test ever sleeps."""

    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_publish_then_current_code_returns_it():
    state = RelayState("pub", now=Clock())
    state.publish("WEWW", 1000.0)
    record = state.current_code()
    assert record.code == "WEWW"
    assert record.ts == 1000.0


def test_current_code_is_none_before_anything_is_published():
    assert RelayState("pub", now=Clock()).current_code() is None


def test_current_code_is_none_once_stale():
    clock = Clock(1000.0)
    state = RelayState("pub", now=clock)
    state.publish("WEWW", 1000.0)
    clock.t = 1000.0 + FRESH_SECONDS + 1
    assert state.current_code() is None


def test_current_code_survives_right_up_to_the_window():
    clock = Clock(1000.0)
    state = RelayState("pub", now=clock)
    state.publish("WEWW", 1000.0)
    clock.t = 1000.0 + FRESH_SECONDS
    assert state.current_code().code == "WEWW"


def test_publish_reports_whether_the_code_changed():
    state = RelayState("pub", now=Clock())
    assert state.publish("WEWW", 1000.0) is True
    assert state.publish("WEWW", 1001.0) is False
    assert state.publish("ZZZZ", 1002.0) is True


@pytest.mark.parametrize("bad", ["", "A", "TOOMANYCHARS", "AB CD", "ABC!", "ＡＢＣＤ", None, 42])
def test_publish_rejects_implausible_codes(bad):
    state = RelayState("pub", now=Clock())
    with pytest.raises(ValueError):
        state.publish(bad, 1000.0)


def test_publisher_token_recognised_and_others_rejected():
    state = RelayState("pub", now=Clock())
    assert state.is_publisher("pub") is True
    assert state.is_publisher("nope") is False
    assert state.is_publisher("") is False
    assert state.is_publisher(None) is False


def test_subscriber_tokens_recognised_and_others_rejected():
    state = RelayState("pub", ["sub-a", "sub-b"], now=Clock())
    assert state.is_subscriber("sub-a") is True
    assert state.is_subscriber("sub-b") is True
    assert state.is_subscriber("pub") is False
    assert state.is_subscriber("") is False


def test_minted_pairing_code_has_the_documented_shape():
    code = RelayState("pub", now=Clock()).mint_pairing_code()
    assert len(code) == PAIRING_CODE_LENGTH
    assert all(character in PAIRING_ALPHABET for character in code)


def test_redeeming_a_pairing_code_yields_a_working_subscriber_token():
    state = RelayState("pub", now=Clock())
    token = state.redeem_pairing_code(state.mint_pairing_code())
    assert token
    assert state.is_subscriber(token)
    assert token in state.subscriber_tokens


def test_a_pairing_code_can_only_be_redeemed_once():
    state = RelayState("pub", now=Clock())
    code = state.mint_pairing_code()
    assert state.redeem_pairing_code(code)
    assert state.redeem_pairing_code(code) is None


def test_an_expired_pairing_code_is_refused():
    clock = Clock(1000.0)
    state = RelayState("pub", now=clock)
    code = state.mint_pairing_code()
    clock.t = 1000.0 + 301
    assert state.redeem_pairing_code(code) is None


def test_minting_sweeps_expired_pairing_codes():
    # Memory-bound property with no public surface, so this asserts on the
    # private dict deliberately: unredeemed codes must not accumulate.
    clock = Clock(1000.0)
    state = RelayState("pub", now=clock)
    state.mint_pairing_code()
    clock.t = 1000.0 + PAIRING_TTL_SECONDS + 1
    state.mint_pairing_code()
    assert len(state._pairing_codes) == 1


def test_an_unknown_pairing_code_is_refused():
    assert RelayState("pub", now=Clock()).redeem_pairing_code("ZZZZZZ") is None


def test_empty_publisher_token_is_rejected_at_construction():
    with pytest.raises(ValueError):
        RelayState("")


def test_subscriber_tokens_property_does_not_expose_internal_state():
    state = RelayState("pub", ["sub"], now=Clock())
    state.subscriber_tokens.add("smuggled")
    assert state.is_subscriber("smuggled") is False


def test_rate_limiter_allows_up_to_the_limit_then_blocks():
    clock = Clock(0.0)
    limiter = RateLimiter(limit=3, window_seconds=60.0, now=clock)
    assert [limiter.allow() for _ in range(4)] == [True, True, True, False]


def test_rate_limiter_recovers_once_the_window_passes():
    clock = Clock(0.0)
    limiter = RateLimiter(limit=1, window_seconds=60.0, now=clock)
    assert limiter.allow() is True
    assert limiter.allow() is False
    clock.t = 61.0
    assert limiter.allow() is True
