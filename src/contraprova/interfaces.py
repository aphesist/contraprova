"""Shared interfaces between modules.

Modules depend on these types, not on each other's internals, so they can be
built and tested independently. Change this file only deliberately.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence

# Model identifiers used throughout. Tiers are ordered cheapest first.
HAIKU = "claude-haiku-5-5"
SONNET = "claude-sonnet-5-5"
OPUS = "claude-opus-5-5"
FABLE = "claude-fable-5-2"
TIERS: tuple[str, ...] = (HAIKU, SONNET, OPUS)


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
