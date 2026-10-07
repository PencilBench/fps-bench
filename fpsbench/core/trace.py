"""Lossless `trace` format: the recording produced by a `record` run.

A trace is the *reference* artefact.  It is never edited by hand; `--simplify`
produces a declarative route from it (see :mod:`fpsbench.core.simplify`).

Strictness contract
-------------------
Decoding rejects anything it does not fully understand: unknown top level keys,
unknown event keys, unknown ``kind`` values, wrong types, non monotonic
timestamps, key names outside the canonical vocabulary.  A silently dropped
field would make two runs incomparable, which defeats the point of the tool.

Schema versions
---------------
* **1** - keyboard/mouse only (``key``/``look``/``scroll``/``button``/``label``).
* **2** - adds ``gamepad`` events (a full controller snapshot per change) and an
  explicit ``duration_ms``.  A recording that only holds a stick at the end has
  no later event to derive the end from, and shortening it would shorten the
  replay.  Version 1 payloads keep loading unchanged; version 1 cannot carry
  gamepad state (there would be no way to express its sign conventions).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .gamepad import GamepadError, GamepadState
from .keycodes import KeycodeError, is_canonical, normalize
from .stable import stable_hash

__all__ = [
    "TraceError",
    "TraceEvent",
    "Trace",
    "TRACE_FORMAT",
    "SCHEMA_VERSION",
    "SCHEMA_VERSIONS",
    "EVENT_KINDS",
    "loads",
    "dumps",
    "load",
    "save",
    "new_trace",
]

TRACE_FORMAT = "trace"
SCHEMA_VERSION = 2
#: Versions this build can read (it always writes :data:`SCHEMA_VERSION`).
SCHEMA_VERSIONS = (1, 2)

EVENT_KINDS = ("key", "look", "scroll", "button", "label", "gamepad")

#: kind -> (required fields, optional fields)
_EVENT_FIELDS: Dict[str, tuple] = {
    "key": (("t_ms", "key", "down"), ("native", "mods")),
    "look": (("t_ms", "dx", "dy"), ("mods",)),
    "scroll": (("t_ms", "dy"), ("dx", "mods")),
    "button": (("t_ms", "button", "down"), ("mods",)),
    "label": (("t_ms", "text"), ()),
    "gamepad": (("t_ms", "lx", "ly", "rx", "ry", "l2", "r2"), ("buttons",)),
}

#: Field names a ``gamepad`` event carries besides ``kind``/``t_ms``.
_GAMEPAD_EVENT_FIELDS = ("lx", "ly", "rx", "ry", "l2", "r2", "buttons")

_BUTTONS = ("left", "right", "middle", "x1", "x2")
_MODS = ("shift", "ctrl", "alt", "cmd")


class TraceError(ValueError):
    """Raised when a trace payload violates the schema."""


def _require_type(value: Any, types: tuple, where: str, what: str) -> Any:
    # bool is a subclass of int: reject it explicitly where a number is expected.
    if isinstance(value, bool) and bool not in types:
        raise TraceError("%s: %s must be %s, got bool" % (where, what, _type_names(types)))
    if not isinstance(value, types):
        raise TraceError(
            "%s: %s must be %s, got %s" % (where, what, _type_names(types), type(value).__name__)
        )
    return value


def _type_names(types: tuple) -> str:
    names = []
    for t in types:
        if t is int:
            names.append("integer")
        elif t is float:
            names.append("number")
        elif t is str:
            names.append("string")
        elif t is bool:
            names.append("boolean")
        elif t is dict:
            names.append("object")
        elif t is list:
            names.append("array")
        else:
            names.append(t.__name__)
    return " or ".join(names)


def _number(value: Any, where: str, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TraceError("%s: %s must be a number" % (where, what))
    return float(value)


def _mods(value: Any, where: str) -> List[str]:
    _require_type(value, (list,), where, "mods")
    out: List[str] = []
    for item in value:
        _require_type(item, (str,), where, "mods entry")
        name = item.strip().lower()
        if name not in _MODS:
            raise TraceError(
                "%s: unknown modifier %r (expected one of %s)" % (where, item, _MODS)
            )
        if name not in out:
            out.append(name)
    return out


@dataclass(frozen=True)
class TraceEvent:
    """One recorded input event on the trace's millisecond timeline."""

    kind: str
    t_ms: float
    key: Optional[str] = None
    down: Optional[bool] = None
    dx: Optional[float] = None
    dy: Optional[float] = None
    text: Optional[str] = None
    button: Optional[str] = None
    mods: List[str] = field(default_factory=list)
    native: Optional[Dict[str, Any]] = None
    state: Optional[GamepadState] = None

    def __post_init__(self) -> None:
        if self.kind not in EVENT_KINDS:
            raise TraceError("unknown event kind %r (expected one of %s)" % (self.kind, EVENT_KINDS))
        if self.kind == "key":
            if self.key is None or self.down is None:
                raise TraceError("key event requires key and down")
            if not is_canonical(self.key):
                raise TraceError("key event uses non canonical name %r" % (self.key,))
        elif self.kind == "label":
            if self.text is None:
                raise TraceError("label event requires text")
        elif self.kind == "button":
            if self.button not in _BUTTONS or self.down is None:
                raise TraceError("button event requires button in %s and down" % (_BUTTONS,))
        elif self.kind == "look":
            if self.dx is None or self.dy is None:
                raise TraceError("look event requires dx and dy")
        elif self.kind == "gamepad":
            if self.state is None:
                raise TraceError("gamepad event requires a full state")

    # -- serialisation ----------------------------------------------------- #
    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"t_ms": self.t_ms, "kind": self.kind}
        if self.kind == "key":
            out.update({"key": self.key, "down": bool(self.down)})
            out["native"] = dict(self.native) if self.native else None
            out["mods"] = list(self.mods)
        elif self.kind == "look":
            out.update({"dx": self.dx, "dy": self.dy})
            if self.mods:
                out["mods"] = list(self.mods)
        elif self.kind == "scroll":
            out["dy"] = self.dy
            if self.dx is not None:
                out["dx"] = self.dx
            if self.mods:
                out["mods"] = list(self.mods)
        elif self.kind == "button":
            out.update({"button": self.button, "down": bool(self.down)})
            if self.mods:
                out["mods"] = list(self.mods)
        elif self.kind == "label":
            out["text"] = self.text
        else:  # gamepad
            assert self.state is not None
            out.update(self.state.to_dict())
        return out

    @classmethod
    def from_dict(cls, data: Any, where: str) -> "TraceEvent":
        _require_type(data, (dict,), where, "event")
        if "kind" not in data:
            raise TraceError("%s: event is missing 'kind'" % where)
        kind = data["kind"]
        _require_type(kind, (str,), where, "kind")
        if kind not in EVENT_KINDS:
            raise TraceError("%s: unknown event kind %r (expected one of %s)" % (where, kind, EVENT_KINDS))
        required, optional = _EVENT_FIELDS[kind]
        allowed = set(required) | set(optional) | {"kind"}
        for field_name in sorted(set(data) - allowed):
            raise TraceError("%s: unknown field %r on %s event" % (where, field_name, kind))
        for field_name in required:
            if field_name not in data:
                raise TraceError("%s: %s event is missing %r" % (where, kind, field_name))

        t_ms = _number(data["t_ms"], where, "t_ms")
        if t_ms < 0:
            raise TraceError("%s: t_ms must not be negative" % where)

        if kind == "key":
            key_raw = _require_type(data["key"], (str,), where, "key")
            try:
                key = normalize(key_raw)
            except KeycodeError as exc:
                raise TraceError("%s: %s" % (where, exc)) from None
            down = _require_type(data["down"], (bool,), where, "down")
            native = data.get("native")
            if native is not None:
                _require_type(native, (dict,), where, "native")
                for native_key in native:
                    _require_type(native_key, (str,), where, "native key")
            return cls(
                kind=kind,
                t_ms=t_ms,
                key=key,
                down=down,
                native=dict(native) if native else None,
                mods=_mods(data.get("mods") or [], where),
            )
        if kind == "look":
            return cls(
                kind=kind,
                t_ms=t_ms,
                dx=_number(data["dx"], where, "dx"),
                dy=_number(data["dy"], where, "dy"),
                mods=_mods(data.get("mods") or [], where),
            )
        if kind == "scroll":
            dx = data.get("dx")
            return cls(
                kind=kind,
                t_ms=t_ms,
                dy=_number(data["dy"], where, "dy"),
                dx=_number(dx, where, "dx") if dx is not None else None,
                mods=_mods(data.get("mods") or [], where),
            )
        if kind == "button":
            button = _require_type(data["button"], (str,), where, "button")
            if button not in _BUTTONS:
                raise TraceError("%s: unknown button %r (expected one of %s)" % (where, button, _BUTTONS))
            return cls(
                kind=kind,
                t_ms=t_ms,
                button=button,
                down=_require_type(data["down"], (bool,), where, "down"),
                mods=_mods(data.get("mods") or [], where),
            )
        if kind == "gamepad":
            payload = {name: data[name] for name in _GAMEPAD_EVENT_FIELDS if name in data}
            try:
                state = GamepadState.from_dict(payload, where)
            except GamepadError as exc:
                raise TraceError(str(exc)) from None
            return cls(kind=kind, t_ms=t_ms, state=state)
        text = _require_type(data["text"], (str,), where, "text")
        return cls(kind=kind, t_ms=t_ms, text=text)


@dataclass
class Trace:
    """A recorded route: metadata plus a strict, ordered event list."""

    name: str
    events: List[TraceEvent]
    created: Optional[str] = None
    origin_platform: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    format: str = TRACE_FORMAT
    schema_version: int = SCHEMA_VERSION
    #: Explicit end of the recording (JSON ``duration_ms``), needed when a held
    #: gamepad state outlives the last change.  ``None`` means "ends with the
    #: last event", which is all a version 1 trace can express.
    end_ms: Optional[float] = None

    def __post_init__(self) -> None:
        if self.format != TRACE_FORMAT:
            raise TraceError("format must be %r, got %r" % (TRACE_FORMAT, self.format))
        if self.schema_version not in SCHEMA_VERSIONS:
            raise TraceError(
                "unsupported schema_version %r (this build understands %s)"
                % (self.schema_version, SCHEMA_VERSIONS)
            )
        self.events = list(self.events)
        self._check_ordering()
        if self.end_ms is not None:
            if self.schema_version < 2:
                raise TraceError("duration_ms requires schema_version 2")
            if isinstance(self.end_ms, bool) or not isinstance(self.end_ms, (int, float)):
                raise TraceError("duration_ms must be a number")
            if self.end_ms < 0:
                raise TraceError("duration_ms must not be negative")
            last = float(self.events[-1].t_ms) if self.events else 0.0
            if float(self.end_ms) < last:
                raise TraceError(
                    "duration_ms (%.3f) must not end before the last event (%.3f)"
                    % (self.end_ms, last)
                )
        if self.schema_version < 2 and any(event.kind == "gamepad" for event in self.events):
            raise TraceError("gamepad events require schema_version 2")

    def _check_ordering(self) -> None:
        last = None
        for index, event in enumerate(self.events):
            if last is not None and event.t_ms < last:
                raise TraceError(
                    "events must be sorted by t_ms (event %d goes back in time)" % index
                )
            last = event.t_ms

    @property
    def duration_ms(self) -> float:
        if self.end_ms is not None:
            return float(self.end_ms)
        if not self.events:
            return 0.0
        return float(self.events[-1].t_ms)

    def hash(self) -> str:
        """Stable identity of the *playback* content (name/metadata excluded).

        Survives a JSON round trip, so a trace and the trace re-loaded from disk
        always hash the same.  A version 2 trace also hashes its explicit
        duration (it changes how long a held state is replayed); version 1
        hashes stay exactly what earlier reports recorded.
        """
        content: Any = [event.to_dict() for event in self.events]
        if self.schema_version >= 2:
            content = [content, self.end_ms]
        return stable_hash(content)

    def labels(self) -> List[tuple]:
        return [(event.t_ms, event.text or "") for event in self.events if event.kind == "label"]

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {kind: 0 for kind in EVENT_KINDS}
        for event in self.events:
            out[event.kind] = out.get(event.kind, 0) + 1
        return out

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "format": self.format,
            "schema_version": self.schema_version,
            "name": self.name,
            "created": self.created,
            "origin_platform": self.origin_platform,
            "metadata": dict(self.metadata),
            "events": [event.to_dict() for event in self.events],
        }
        if self.end_ms is not None:
            out["duration_ms"] = float(self.end_ms)
        return out

    @classmethod
    def from_dict(cls, data: Any) -> "Trace":
        _require_type(data, (dict,), "trace", "root")
        allowed = {
            "format",
            "schema_version",
            "name",
            "created",
            "origin_platform",
            "metadata",
            "events",
            "duration_ms",
        }
        for field_name in sorted(set(data) - allowed):
            raise TraceError("trace: unknown field %r" % (field_name,))
        if data.get("format") != TRACE_FORMAT:
            raise TraceError(
                "trace: format must be %r (a declarative route is loaded with load_route)"
                % (TRACE_FORMAT,)
            )
        version = data.get("schema_version")
        if version not in SCHEMA_VERSIONS:
            raise TraceError(
                "trace: unsupported schema_version %r (this build understands %s)"
                % (version, SCHEMA_VERSIONS)
            )
        if version < 2 and "duration_ms" in data:
            raise TraceError("trace: duration_ms requires schema_version 2")
        name = data.get("name")
        _require_type(name, (str,), "trace", "name")
        created = data.get("created")
        if created is not None:
            _require_type(created, (str,), "trace", "created")
        origin = data.get("origin_platform")
        if origin is not None:
            _require_type(origin, (str,), "trace", "origin_platform")
            if origin not in ("macos", "windows"):
                raise TraceError("trace: unknown origin_platform %r" % (origin,))
        metadata = data.get("metadata")
        if metadata is None:
            metadata = {}
        _require_type(metadata, (dict,), "trace", "metadata")
        end_ms = data.get("duration_ms")
        if end_ms is not None:
            end_ms = _number(end_ms, "trace", "duration_ms")
        events_raw = data.get("events")
        _require_type(events_raw, (list,), "trace", "events")
        events = [
            TraceEvent.from_dict(item, "events[%d]" % index) for index, item in enumerate(events_raw)
        ]
        if version < 2 and any(event.kind == "gamepad" for event in events):
            raise TraceError("trace: gamepad events require schema_version 2")
        return cls(
            name=name,
            events=events,
            created=created,
            origin_platform=origin,
            metadata=dict(metadata),
            schema_version=version,
            end_ms=end_ms,
        )

    def save(self, path: str) -> None:
        save(self, path)

    @classmethod
    def load(cls, path: str) -> "Trace":
        return load(path)


def new_trace(
    name: str,
    events: Sequence[TraceEvent],
    *,
    origin_platform: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    end_ms: Optional[float] = None,
) -> Trace:
    return Trace(
        name=name,
        events=list(events),
        created=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        origin_platform=origin_platform,
        metadata=dict(metadata or {}),
        end_ms=end_ms,
    )


def loads(text: str) -> Trace:
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise TraceError("trace: invalid JSON: %s" % exc) from None
    return Trace.from_dict(payload)


def dumps(trace: Trace, *, indent: Optional[int] = 1) -> str:
    return json.dumps(trace.to_dict(), indent=indent, sort_keys=False)


def load(path: str) -> Trace:
    with open(path, "r", encoding="utf-8") as handle:
        return loads(handle.read())


def save(trace: Trace, path: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(dumps(trace))
        handle.write("\n")
