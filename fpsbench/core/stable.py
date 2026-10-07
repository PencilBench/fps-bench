"""Stable hashing for the data contracts (``trace`` and ``route``).

A hash has to survive a JSON round trip: ``0`` and ``0.0`` are the same
timestamp, and a route written by hand as ``"hold_ms": 4000`` must hash the same
as the same route produced by ``--simplify`` with ``4000.0``.  Numbers are
therefore canonicalised before hashing (integral floats become integers, dict
keys are sorted) instead of hashing the raw ``json.dumps`` output.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

__all__ = ["canonicalise", "stable_hash"]


def canonicalise(value: Any) -> Any:
    """Recursively normalise a JSON-ish structure for hashing."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float):
        if value.is_integer() and abs(value) < 2 ** 53:
            return int(value)
        return value
    if isinstance(value, dict):
        return {str(key): canonicalise(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [canonicalise(item) for item in value]
    return value


def stable_hash(payload: Any, *, length: int = 16) -> str:
    """Short, stable hex digest of ``payload``."""
    text = json.dumps(canonicalise(payload), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]
