"""Neutral model, effort and ladder types, and the Claude defaults built from them."""

import json

import pytest

from contraprova import interfaces
from contraprova.interfaces import (
    DEFAULT_CLAUDE_LADDER,
    HAIKU,
    OPUS,
    SONNET,
    TIERS,
    Effort,
    ModelRef,
    Tier,
    TierLadder,
)


def test_model_ref_string_form_round_trips():
    ref = ModelRef("anthropic", "claude-haiku-5-5")
    assert str(ref) == "anthropic/claude-haiku-5-5"
    assert ModelRef.parse(str(ref)) == ref
    hub = ModelRef.parse("openrouter/meta-llama/llama-4")  # only the first '/' splits
    assert (hub.provider, hub.id) == ("openrouter", "meta-llama/llama-4")
    assert str(hub) == "openrouter/meta-llama/llama-4"
    assert ModelRef("a", "b") == ModelRef("a", "b") and len({ModelRef("a", "b"), ModelRef("a", "b")}) == 1


@pytest.mark.parametrize("bad", ["claude-haiku-5-5", "/id", "provider/"])
def test_model_ref_rejects_a_missing_part(bad):
    with pytest.raises(ValueError):
        ModelRef.parse(bad)
    with pytest.raises(ValueError):
        ModelRef("a/b", "c")


def test_effort_is_abstract_and_serialises_as_its_value():
    assert [e.value for e in Effort] == ["none", "low", "medium", "high", "xhigh", "max"]
    assert json.dumps({"effort": Effort.HIGH}) == '{"effort": "high"}'
    assert Effort.parse("low") is Effort.LOW and Effort.parse(Effort.MAX) is Effort.MAX
    assert Effort.parse("xhigh") is Effort.XHIGH
    with pytest.raises(ValueError, match="turbo"):
        Effort.parse("turbo")
    assert all(t.effort is Effort.MEDIUM for t in DEFAULT_CLAUDE_LADDER.tiers)  # defaults unchanged


def test_the_claude_constants_are_the_default_ladder():
    assert (HAIKU, SONNET, OPUS) == ("claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5")
    assert TIERS == (HAIKU, SONNET, OPUS) == DEFAULT_CLAUDE_LADDER.model_ids
    assert interfaces.FABLE == "claude-fable-5-2" and interfaces.FABLE not in TIERS
    assert DEFAULT_CLAUDE_LADDER.names == ("haiku", "sonnet", "opus")
    assert all(m.provider == "anthropic" for m in DEFAULT_CLAUDE_LADDER.models)
    assert DEFAULT_CLAUDE_LADDER.same_tier_scope_retry is True


def ladder(**kw):
    return TierLadder((Tier("cheap", ModelRef("openai", "gpt-mini"), Effort.LOW, 4000, 1),
                       Tier("mid", ModelRef("google", "gemini-flash")),
                       Tier("strong", ModelRef("openai", "gpt-big"), Effort.HIGH)), **kw)


def test_a_ladder_is_data_and_round_trips():
    lad = ladder(same_tier_scope_retry=False)
    d = lad.to_dict()
    assert json.loads(json.dumps(d)) == d
    assert d["tiers"][0] == {"name": "cheap", "model": "openai/gpt-mini", "effort": "low", "max_tokens": 4000, "retries": 1}
    assert TierLadder.from_dict(d) == lad
    assert TierLadder.from_dict(DEFAULT_CLAUDE_LADDER.to_dict()) == DEFAULT_CLAUDE_LADDER


def test_ladder_validation():
    with pytest.raises(ValueError):
        TierLadder(())
    with pytest.raises(ValueError, match="unique"):
        TierLadder((Tier("a", ModelRef("p", "x")), Tier("a", ModelRef("p", "y"))))
    with pytest.raises(ValueError):
        Tier("a", ModelRef("p", "x"), max_tokens=0)
    with pytest.raises(ValueError):
        Tier("a", ModelRef("p", "x"), retries=-1)
    with pytest.raises(ValueError):
        TierLadder.from_dict({"tiers": [{"name": "a", "model": "no-provider"}]})


def test_a_ceiling_cuts_the_ladder():
    lad = ladder()
    assert lad.up_to("mid").names == ("cheap", "mid")
    assert lad.index("strong") == 2
    with pytest.raises(KeyError):
        lad.up_to("nope")


def test_next_index_follows_retries_then_the_scope_retry_then_up():
    lad = ladder()  # cheap has one retry
    assert lad.next_index(0, attempts_at_tier=1) == 0  # retry budget
    assert lad.next_index(0, attempts_at_tier=2) == 1
    assert lad.next_index(1, attempts_at_tier=1) == 2
    assert lad.next_index(2, attempts_at_tier=1) is None  # spent
    # A scope-only block: the same tier once more, if the policy allows.
    assert lad.next_index(1, attempts_at_tier=1, scope_only=True) == 1
    assert lad.next_index(1, attempts_at_tier=2, scope_only=True, scope_retried=True) == 2
    off = ladder(same_tier_scope_retry=False)
    assert off.next_index(1, attempts_at_tier=1, scope_only=True) == 2
    # The default Claude ladder: one attempt per tier, one scope retry, never skip a tier.
    d = DEFAULT_CLAUDE_LADDER
    assert d.next_index(0, attempts_at_tier=1) == 1 and d.next_index(2, attempts_at_tier=1) is None
    assert d.next_index(0, attempts_at_tier=1, scope_only=True) == 0
