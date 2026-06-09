"""Pure-logic tests for the LLM cost model (no network)."""
import types

from grocery_buddy.pricing import cost_of


def test_haiku_input_rate():
    assert round(cost_of("claude-haiku-4-5-20251001", {"input_tokens": 1_000_000}), 4) == 1.0


def test_sonnet_output_rate():
    assert round(cost_of("claude-sonnet-4-6", {"output_tokens": 1_000_000}), 2) == 15.0


def test_cache_read_is_cheap():
    assert round(cost_of("claude-haiku-4-5", {"cache_read_input_tokens": 1_000_000}), 2) == 0.10


def test_dated_snapshot_matches_family():
    # The longest-prefix match resolves a dated snapshot to its family rate.
    assert cost_of("claude-haiku-4-5-20251001", {"input_tokens": 1000}) == cost_of(
        "claude-haiku-4", {"input_tokens": 1000}
    )


def test_unknown_model_defaults_to_sonnet_rate():
    assert round(cost_of("mystery-model-9", {"input_tokens": 1_000_000}), 2) == 3.00


def test_accepts_usage_object():
    u = types.SimpleNamespace(input_tokens=1000, output_tokens=500)
    assert cost_of("claude-haiku-4-5", u) > 0


def test_buckets_are_summed_independently():
    c = cost_of(
        "claude-sonnet-4-6",
        {
            "input_tokens": 1_000_000,
            "output_tokens": 1_000_000,
            "cache_read_input_tokens": 1_000_000,
            "cache_creation_input_tokens": 1_000_000,
        },
    )
    assert round(c, 2) == round(3.00 + 15.00 + 0.30 + 3.75, 2)
