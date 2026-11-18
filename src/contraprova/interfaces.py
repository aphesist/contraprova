"""Shared interfaces between modules.

Modules depend on these types, not on each other's internals, so they can be
built and tested independently. Change this file only deliberately.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol, Sequence


# ------------------------------------------------------------------ neutral model types


@dataclass(frozen=True)
class ModelRef:
    """A model on a provider: ``ModelRef("anthropic", "claude-haiku-5-5")``, written ``anthropic/claude-haiku-5-5``.

    The id may itself contain ``/`` (a hub model such as ``openrouter/meta-llama/llama-4``): only the
    first ``/`` separates the provider.
    """

    provider: str
    id: str

    def __post_init__(self) -> None:
        if not self.provider or "/" in self.provider or not self.id:
            raise ValueError(f"a model reference needs a provider (without '/') and an id, got {self.provider!r}, {self.id!r}")

    def __str__(self) -> str:
        return f"{self.provider}/{self.id}"

    @classmethod
    def parse(cls, text: str) -> "ModelRef":
        """Inverse of ``str``: ``"provider/id"``. Raises ValueError without a provider."""
        provider, sep, model_id = text.partition("/")
        if not sep:
            raise ValueError(f"model reference must look like 'provider/id', got {text!r}")
        return cls(provider, model_id)


class Effort(str, Enum):
    """How hard a model is asked to think, independent of any provider.

    Each adapter maps a level onto its own knob (a reasoning effort, a thinking budget, or nothing),
    and maps down where a provider has fewer levels; that mapping is not in this package. It is a ``str``, so it serialises as its value.
    """

    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"

    @classmethod
    def parse(cls, value: "str | Effort") -> "Effort":
        """``Effort("low")``-style lookup with a clear error."""
        try:
            return cls(value)
        except ValueError:
            raise ValueError(f"unknown effort {value!r}; expected one of {[e.value for e in cls]}") from None


@dataclass(frozen=True)
class Tier:
    """One rung of a ladder: which model works, how hard, how much it may write, how often it may retry.

    ``retries`` is the number of extra attempts at this rung after a block, before moving up.
    """

    name: str
    model: ModelRef
    effort: Effort = Effort.MEDIUM
    max_tokens: int = 8000
    retries: int = 0

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("a tier needs a name")
        if self.max_tokens <= 0 or self.retries < 0:
            raise ValueError(f"tier {self.name!r}: max_tokens must be positive and retries not negative")


@dataclass(frozen=True)
class TierLadder:
    """Tiers ordered cheapest first, as data (``to_dict`` / ``from_dict``).

    ``same_tier_scope_retry``: when the only thing wrong with a blocked attempt is a scope, protected
    file or contract violation (the work itself may be fine), try the same tier once more with the
    violation named, before moving up.
    """

    tiers: tuple[Tier, ...]
    same_tier_scope_retry: bool = True

    def __post_init__(self) -> None:
        if not self.tiers:
            raise ValueError("a ladder needs at least one tier")
        if len({t.name for t in self.tiers}) != len(self.tiers):
            raise ValueError("tier names in a ladder must be unique")

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(t.name for t in self.tiers)

    @property
    def models(self) -> tuple[ModelRef, ...]:
        return tuple(t.model for t in self.tiers)

    @property
    def model_ids(self) -> tuple[str, ...]:
        """Bare model ids, the form ``usage.recorded`` and ``route.decided`` carry."""
        return tuple(t.model.id for t in self.tiers)

    def index(self, name: str) -> int:
        """Position of the tier called ``name``. Raises KeyError."""
        for i, t in enumerate(self.tiers):
            if t.name == name:
                return i
        raise KeyError(f"no tier named {name!r}")

    def up_to(self, name: str) -> "TierLadder":
        """The ladder cut after tier ``name``: 'never above this tier' for one run."""
        return TierLadder(self.tiers[: self.index(name) + 1], self.same_tier_scope_retry)

    def next_index(self, index: int, *, attempts_at_tier: int, scope_only: bool = False, scope_retried: bool = False) -> int | None:
        """Which tier runs after a blocked attempt at tier ``index``; None when the ladder is spent.

        ``attempts_at_tier`` counts attempts already made at this tier. The same tier runs again while
        it has retries left, and once more for a scope-only block (if the policy allows and
        ``scope_retried`` is false: the caller records that it used its one scope retry).
        """
        tier = self.tiers[index]
        if attempts_at_tier <= tier.retries:
            return index
        if self.same_tier_scope_retry and scope_only and not scope_retried:
            return index
        return index + 1 if index + 1 < len(self.tiers) else None

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form, as written in ``session.opened``'s ``ladder``."""
        return {
            "same_tier_scope_retry": self.same_tier_scope_retry,
            "tiers": [
                {"name": t.name, "model": str(t.model), "effort": t.effort.value, "max_tokens": t.max_tokens,
                 "retries": t.retries}
                for t in self.tiers
            ],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TierLadder":
        """Inverse of ``to_dict``. Raises ValueError (or KeyError) on a malformed ladder."""
        tiers = tuple(
            Tier(t["name"], ModelRef.parse(t["model"]), Effort.parse(t.get("effort", "medium")),
                 int(t.get("max_tokens", 8000)), int(t.get("retries", 0)))
            for t in data["tiers"]
        )
        return cls(tiers, bool(data.get("same_tier_scope_retry", True)))


# Model identifiers of the Claude study (bare ids, as ``usage.recorded`` carries them). The default
# ladder is data built from the types above; HAIKU/SONNET/OPUS/TIERS are kept as the names the
# harness and the router were written against. FABLE is a strong model outside the ladder.
HAIKU = "claude-haiku-5-5"
SONNET = "claude-sonnet-5-5"
OPUS = "claude-opus-5-5"
FABLE = "claude-fable-5-2"
DEFAULT_CLAUDE_LADDER = TierLadder(
    (
        Tier("haiku", ModelRef("anthropic", HAIKU)),
        Tier("sonnet", ModelRef("anthropic", SONNET)),
        Tier("opus", ModelRef("anthropic", OPUS)),
    )
)
TIERS: tuple[str, ...] = DEFAULT_CLAUDE_LADDER.model_ids  # ordered cheapest first


# ------------------------------------------------------------------ models


@dataclass(frozen=True)
class Message:
    role: str  # "user" | "assistant"
    content: str
    # Length (in characters) of a leading part of ``content`` that later requests will repeat byte
    # for byte. A client that supports prompt caching puts a cache breakpoint at the end of it;
    # 0 means none. ``content`` is always the whole text, so clients without caching ignore it.
    cache_upto: int = 0


@dataclass(frozen=True)
class Completion:
    text: str
    model: str
    usage: Mapping[str, Any]  # a usage.recorded payload (see contraprova.usage.usage_payload)
    stop_reason: str = "end_turn"
    request_id: str | None = None


class LLMClient(Protocol):
    """Anything that can answer a prompt. The real one calls the Messages API; tests use fakes."""

    def complete(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[Message],
        max_tokens: int,
        effort: str | None = None,
        tags: Mapping[str, Any] | None = None,
    ) -> Completion: ...


# ------------------------------------------------------------------ gates


@dataclass(frozen=True)
class Submission:
    """A worker's proposed change: new file contents keyed by repo-relative path."""

    task_id: str
    files: Mapping[str, str]
    attempt: int = 1


@dataclass(frozen=True)
class GateResult:
    gate: str  # e.g. "T0.scope", "T0.visible_tests", "T1.private_tests", "T2.review"
    verdict: str  # "pass" | "block"
    reason: str = ""
    evidence: Mapping[str, Any] = field(default_factory=dict)  # hashes, test output summary, tier


# ------------------------------------------------------------------ routing


@dataclass(frozen=True)
class RouteDecision:
    action: str  # one of TIERS (or "retry" semantics expressed as HAIKU)
    propensities: Mapping[str, float]  # probability the policy assigned to each action; sums to 1
    router_hash: str  # hash of the router weights that made the decision
    features: Mapping[str, float] = field(default_factory=dict)


class Router(Protocol):
    def decide(self, features: Mapping[str, float], *, rng_seed: int) -> RouteDecision: ...
