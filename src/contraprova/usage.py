"""Turn an Anthropic Messages API ``usage`` object into a ``usage.recorded`` payload.

Cost is never taken from a harness estimate. It is recomputed later from these
token counts, which come straight from the API response.
"""

from __future__ import annotations

from typing import Any, Mapping


def _get(obj: Any, name: str, default: Any = 0) -> Any:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def usage_payload(usage: Any, model: str, **tags: Any) -> dict[str, Any]:
    """Accepts the SDK's Usage object or a plain dict. ``tags`` might be role, arm, task_id."""
    creation = _get(usage, "cache_creation", None)
    w5 = _get(creation, "ephemeral_5m_input_tokens", None)
    w1 = _get(creation, "ephemeral_1h_input_tokens", None)
    if w5 is None and w1 is None:
        # Older responses only report a total; the default cache TTL is 5 minutes.
        w5, w1 = _get(usage, "cache_creation_input_tokens", 0) or 0, 0
    payload = {
        "model": model,
        "input_tokens": int(_get(usage, "input_tokens", 0) or 0),
        "output_tokens": int(_get(usage, "output_tokens", 0) or 0),
        "cache_read_tokens": int(_get(usage, "cache_read_input_tokens", 0) or 0),
        "cache_write_5m_tokens": int(w5 or 0),
        "cache_write_1h_tokens": int(w1 or 0),
    }
    payload.update(tags)
    return payload
