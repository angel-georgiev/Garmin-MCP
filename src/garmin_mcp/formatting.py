"""Turn Garmin's (often enormous) JSON into text that fits a context window."""

from __future__ import annotations

import json
from typing import Any

TRUNCATION_NOTE = "__truncated__"


def _dumps(data: Any, *, indent: int | None = 2) -> str:
    separators = (",", ": ") if indent else (",", ":")
    return json.dumps(data, indent=indent, default=str, ensure_ascii=False, separators=separators)


def to_json_text(data: Any, max_chars: int) -> str:
    """Serialize ``data``, shrinking it if it exceeds ``max_chars``.

    Shrinking happens in three escalating steps so the result always stays
    valid JSON where possible: drop the pretty-printing, drop trailing items of
    a list, and only as a last resort hard-truncate the string.
    """
    if data is None:
        return "null"

    text = _dumps(data)
    if len(text) <= max_chars:
        return text

    compact = _dumps(data, indent=None)
    if len(compact) <= max_chars:
        return compact

    if isinstance(data, list) and data:
        kept = _fit_list(data, max_chars)
        if kept:
            payload = {
                "items": kept,
                TRUNCATION_NOTE: (
                    f"showing {len(kept)} of {len(data)} items; "
                    "narrow the date range or lower the limit to see the rest"
                ),
            }
            candidate = _dumps(payload, indent=None)
            if len(candidate) <= max_chars:
                return candidate

    if isinstance(data, dict):
        summary = _fit_dict(data, max_chars)
        if summary is not None:
            return summary

    keep = max(max_chars - 120, 0)
    return compact[:keep] + f"\n… response truncated at {keep} characters (invalid JSON tail)."


def _fit_list(items: list[Any], max_chars: int) -> list[Any]:
    """Return the longest prefix of ``items`` whose JSON fits the budget."""
    low, high, best = 0, len(items), []
    while low <= high:
        mid = (low + high) // 2
        if mid == 0:
            low = 1
            continue
        candidate = items[:mid]
        if len(_dumps(candidate, indent=None)) <= max_chars - 200:
            best = candidate
            low = mid + 1
        else:
            high = mid - 1
    return best


def _fit_dict(data: dict[str, Any], max_chars: int) -> str | None:
    """Drop the largest keys from a dict until the rest fits."""
    sizes = sorted(
        ((key, len(_dumps(value, indent=None))) for key, value in data.items()),
        key=lambda pair: pair[1],
        reverse=True,
    )
    dropped: list[str] = []
    kept = dict(data)
    for key, _ in sizes:
        note = f"omitted large fields: {dropped}"
        candidate = _dumps({**kept, TRUNCATION_NOTE: note}, indent=None)
        if len(candidate) <= max_chars:
            return candidate
        kept.pop(key, None)
        dropped.append(key)
    return None


def drop_keys(data: Any, keys: set[str]) -> Any:
    """Recursively remove ``keys`` from dicts (used to strip heavy time series)."""
    if isinstance(data, dict):
        return {k: drop_keys(v, keys) for k, v in data.items() if k not in keys}
    if isinstance(data, list):
        return [drop_keys(item, keys) for item in data]
    return data


def pick(data: Any, keys: list[str]) -> dict[str, Any]:
    """Return the subset of ``keys`` present in ``data`` with non-null values."""
    if not isinstance(data, dict):
        return {}
    return {key: data[key] for key in keys if data.get(key) is not None}
