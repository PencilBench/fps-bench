"""Declarative `route` format: the editable, hand-tweakable playback script.

A route is what ``--simplify`` emits from a recorded trace, and it is also the
format you write by hand when you would rather describe the run than perform it.

Route skeleton::

    {
      "format": "route",
      "schema_version": 1,
      "name": "Liurnia ride",
      "warmup_ms": 8000,
      "settle_ms": 3000,
      "repeat": 3,
      "fps": {"mode": "auto", "region": {...}},
      "steps": [
        {"type": "key",  "keys": ["w"], "hold_ms": 4000, "label": "straight"},
        {"type": "turn", "dx": 600, "dy": 0, "duration_ms": 300},
        {"type": "wait", "ms": 500}
      ]
    }

Like the trace codec this is strict: unknown keys, wrong types and unknown step
types are errors instead of being silently ignored.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .keycodes import KeycodeError, normalize
from .stable import stable_hash

__all__ = [
    "RouteError",
    "FpsConfig",
    "Region",
    "Step",
    "Route",
    "ROUTE_FORMAT",
    "SCHEMA_VERSION",
    "STEP_TYPES",
    "FPS_MODES",
    "BUTTONS",
    "loads_route",
    "dumps_route",
    "load_route",
    "save_route",
    "looks_like_route",
]

ROUTE_FORMAT = "route"
SCHEMA_VERSION = 1

STEP_TYPES = ("key", "turn", "wait", "scroll", "button", "label")
FPS_MODES = ("auto", "presentmon", "metalperftrace", "ocr")
BUTTONS = ("left", "right", "middle", "x1", "x2")
_BUTTONS = BUTTONS

_STEP_FIELDS: Dict[str, tuple] = {
    "key": (("keys", "hold_ms"), ("label",)),
    "turn": (("dx", "dy", "duration_ms"), ("label", "keys")),
    "wait": (("ms",), ("label",)),
    "scroll": (("dy",), ("duration_ms", "label", "keys")),
    "button": (("button", "hold_ms"), ("label", "keys")),
    "label": (("text",), ()),
}

_REGION_FIELDS = ("x", "y", "w", "h", "space")
_FPS_FIELDS = (
    "mode",
    "region",
    "sample_hz",
    "min_fps",
    "max_fps",
    "presentmon_path",
    "process_name",
)


class RouteError(ValueError):
    """Raised when a route payload violates the schema."""


def _check_keys(value: Any, where: str) -> List[str]:
    if not isinstance(value, list):
        raise RouteError("%s: keys must be an array" % where)
    keys: List[str] = []
    for item in value:
        if not isinstance(item, str):
            raise RouteError("%s: keys entries must be strings" % where)
        try:
            key = normalize(item)
        except KeycodeError as exc:
            raise RouteError("%s: %s" % (where, exc)) from None
        if key in keys:
            continue
        keys.append(key)
    if not keys:
        raise RouteError("%s: keys must not be empty" % where)
    return keys


def _num(
    value: Any,
    where: str,
    what: str,
    *,
    positive: bool = False,
    allow_zero: bool = False,
    signed: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RouteError("%s: %s must be a number" % (where, what))
    out = float(value)
    if signed:
        return out
    if positive and out <= 0 and not (allow_zero and out == 0):
        raise RouteError("%s: %s must be > 0" % (where, what))
    if out < 0 and not allow_zero:
        raise RouteError("%s: %s must not be negative" % (where, what))
    return out


def _str_or_none(value: Any, where: str, what: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RouteError("%s: %s must be a string or null" % (where, what))
    return value


def _opt_label(data: Dict[str, Any], where: str) -> Optional[str]:
    return _str_or_none(data.get("label"), where, "label")


@dataclass(frozen=True)
class Region:
    """Screen region holding the FPS counter.

    ``space`` records the coordinate space the numbers are in: macOS screen
    capture APIs speak display *points*, Win32 GDI speaks physical *pixels*.
    """

    x: float
    y: float
    w: float
    h: float
    space: str = "points"

    def __post_init__(self) -> None:
        if self.w <= 0 or self.h <= 0:
            raise RouteError("region: w and h must be > 0")
        if self.space not in ("points", "pixels"):
            raise RouteError("region: space must be 'points' or 'pixels'")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "x": round(self.x, 2),
            "y": round(self.y, 2),
            "w": round(self.w, 2),
            "h": round(self.h, 2),
            "space": self.space,
        }

    @classmethod
    def from_dict(cls, data: Any, where: str) -> "Region":
        if not isinstance(data, dict):
            raise RouteError("%s: region must be an object" % where)
        for field_name in sorted(set(data) - set(_REGION_FIELDS)):
            raise RouteError("%s: unknown region field %r" % (where, field_name))
        for field_name in ("x", "y", "w", "h"):
            if field_name not in data:
                raise RouteError("%s: region is missing %r" % (where, field_name))
        space = data.get("space", "points")
        if not isinstance(space, str):
            raise RouteError("%s: region space must be a string" % where)
        return cls(
            x=_num(data["x"], where, "region x"),
            y=_num(data["y"], where, "region y"),
            w=_num(data["w"], where, "region w", positive=True),
            h=_num(data["h"], where, "region h", positive=True),
            space=space,
        )


@dataclass(frozen=True)
class FpsConfig:
    """Everything the FPS layer needs, stored inside the route file."""

    mode: str = "auto"  # auto | presentmon | metalperftrace | ocr
    region: Optional[Region] = None
    sample_hz: float = 6.0
    min_fps: float = 0.5
    max_fps: float = 2000.0
    presentmon_path: Optional[str] = None
    process_name: Optional[str] = None

    def __post_init__(self) -> None:
        if self.mode not in FPS_MODES:
            raise RouteError("fps: mode must be one of %s" % (FPS_MODES,))
        if self.sample_hz <= 0:
            raise RouteError("fps: sample_hz must be > 0")
        if self.min_fps < 0 or self.max_fps <= self.min_fps:
            raise RouteError("fps: require 0 <= min_fps < max_fps")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "region": self.region.to_dict() if self.region else None,
            "sample_hz": self.sample_hz,
            "min_fps": self.min_fps,
            "max_fps": self.max_fps,
            "presentmon_path": self.presentmon_path,
            "process_name": self.process_name,
        }

    @classmethod
    def from_dict(cls, data: Any, where: str) -> "FpsConfig":
        if data is None:
            return cls()
        if not isinstance(data, dict):
            raise RouteError("%s: fps must be an object" % where)
        for field_name in sorted(set(data) - set(_FPS_FIELDS)):
            raise RouteError("%s: unknown fps field %r" % (where, field_name))
        region = data.get("region")
        return cls(
            mode=data.get("mode", "auto"),
            region=Region.from_dict(region, where) if region is not None else None,
            sample_hz=_num(data.get("sample_hz", 6.0), where, "fps sample_hz", positive=True),
            min_fps=_num(data.get("min_fps", 0.5), where, "fps min_fps"),
            max_fps=_num(data.get("max_fps", 2000.0), where, "fps max_fps", positive=True),
            presentmon_path=_str_or_none(data.get("presentmon_path"), where, "fps presentmon_path"),
            process_name=_str_or_none(data.get("process_name"), where, "fps process_name"),
        )

    def merged(self, **overrides: Any) -> "FpsConfig":
        """Copy with CLI overrides applied (``None`` values are ignored)."""
        data = self.to_dict()
        for key, value in overrides.items():
            if value is None:
                continue
            if key not in _FPS_FIELDS:
                raise RouteError("unknown fps override %r" % (key,))
            data[key] = value.to_dict() if isinstance(value, Region) else value
        return FpsConfig.from_dict(data, "fps")


@dataclass(frozen=True)
class Step:
    """One declarative playback step."""

    type: str
    keys: List[str] = field(default_factory=list)
    hold_ms: Optional[float] = None
    dx: Optional[float] = None
    dy: Optional[float] = None
    duration_ms: Optional[float] = None
    ms: Optional[float] = None
    text: Optional[str] = None
    button: Optional[str] = None
    label: Optional[str] = None

    def __post_init__(self) -> None:
        if self.type not in STEP_TYPES:
            raise RouteError("unknown step type %r (expected one of %s)" % (self.type, STEP_TYPES))

    @property
    def duration(self) -> float:
        """Wall-clock duration of the step in milliseconds (``label`` = 0)."""
        if self.type == "key":
            return float(self.hold_ms or 0.0)
        if self.type == "turn":
            return float(self.duration_ms or 0.0)
        if self.type == "wait":
            return float(self.ms or 0.0)
        if self.type == "scroll":
            return float(self.duration_ms if self.duration_ms is not None else 100.0)
        if self.type == "button":
            return float(self.hold_ms or 0.0)
        return 0.0

    # -- constructors ------------------------------------------------------ #
    @classmethod
    def key(cls, keys: Sequence[str], hold_ms: float, label: Optional[str] = None) -> "Step":
        return cls(type="key", keys=_check_keys(list(keys), "step"), hold_ms=hold_ms, label=label)

    @classmethod
    def turn(
        cls,
        dx: float,
        dy: float,
        duration_ms: float,
        label: Optional[str] = None,
        keys: Optional[Sequence[str]] = None,
    ) -> "Step":
        return cls(
            type="turn",
            dx=dx,
            dy=dy,
            duration_ms=duration_ms,
            label=label,
            keys=_check_keys(list(keys), "step") if keys else [],
        )

    @classmethod
    def wait(cls, ms: float, label: Optional[str] = None) -> "Step":
        return cls(type="wait", ms=ms, label=label)

    @classmethod
    def scroll(
        cls,
        dy: float,
        duration_ms: float = 100.0,
        label: Optional[str] = None,
        keys: Optional[Sequence[str]] = None,
    ) -> "Step":
        return cls(
            type="scroll",
            dy=dy,
            duration_ms=duration_ms,
            label=label,
            keys=_check_keys(list(keys), "step") if keys else [],
        )

    @classmethod
    def click(
        cls,
        button: str,
        hold_ms: float = 80.0,
        label: Optional[str] = None,
        keys: Optional[Sequence[str]] = None,
    ) -> "Step":
        """A mouse button press held for ``hold_ms``.

        Named ``click`` on purpose: a classmethod called ``button`` would shadow
        the dataclass field of the same name (the field default is read from the
        class body, so the method would become the default value).
        """
        return cls(
            type="button",
            button=button,
            hold_ms=hold_ms,
            label=label,
            keys=_check_keys(list(keys), "step") if keys else [],
        )

    @classmethod
    def marker(cls, text: str) -> "Step":
        return cls(type="label", text=text)

    # -- serialisation ----------------------------------------------------- #
    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"type": self.type}
        if self.type == "key":
            out.update({"keys": list(self.keys), "hold_ms": self.hold_ms})
        elif self.type == "turn":
            out.update({"dx": self.dx, "dy": self.dy, "duration_ms": self.duration_ms})
        elif self.type == "wait":
            out["ms"] = self.ms
        elif self.type == "scroll":
            out.update({"dy": self.dy, "duration_ms": self.duration})
        elif self.type == "button":
            out.update({"button": self.button, "hold_ms": self.hold_ms})
        else:
            out["text"] = self.text
        if self.keys and self.type != "key":
            out["keys"] = list(self.keys)
        if self.label is not None and self.type != "label":
            out["label"] = self.label
        return out

    @classmethod
    def from_dict(cls, data: Any, where: str) -> "Step":
        if not isinstance(data, dict):
            raise RouteError("%s: step must be an object" % where)
        if "type" not in data:
            raise RouteError("%s: step is missing 'type'" % where)
        step_type = data["type"]
        if not isinstance(step_type, str):
            raise RouteError("%s: step type must be a string" % where)
        if step_type not in STEP_TYPES:
            raise RouteError(
                "%s: unknown step type %r (expected one of %s)" % (where, step_type, STEP_TYPES)
            )
        required, optional = _STEP_FIELDS[step_type]
        allowed = set(required) | set(optional) | {"type"}
        for field_name in sorted(set(data) - allowed):
            raise RouteError("%s: unknown field %r on %s step" % (where, field_name, step_type))
        for field_name in required:
            if field_name not in data:
                raise RouteError("%s: %s step is missing %r" % (where, step_type, field_name))

        if step_type == "key":
            return cls(
                type="key",
                keys=_check_keys(data["keys"], where),
                hold_ms=_num(data["hold_ms"], where, "hold_ms", positive=True),
                label=_opt_label(data, where),
            )
        if step_type == "turn":
            return cls(
                type="turn",
                keys=_check_keys(data["keys"], where) if data.get("keys") else [],
                dx=_num(data["dx"], where, "dx", signed=True),
                dy=_num(data["dy"], where, "dy", signed=True),
                duration_ms=_num(data["duration_ms"], where, "duration_ms", positive=True),
                label=_opt_label(data, where),
            )
        if step_type == "wait":
            return cls(
                type="wait",
                ms=_num(data["ms"], where, "ms", allow_zero=True),
                label=_opt_label(data, where),
            )
        if step_type == "scroll":
            duration = data.get("duration_ms")
            return cls(
                type="scroll",
                keys=_check_keys(data["keys"], where) if data.get("keys") else [],
                dy=_num(data["dy"], where, "dy", signed=True),
                duration_ms=(
                    _num(duration, where, "duration_ms", positive=True) if duration is not None else 100.0
                ),
                label=_opt_label(data, where),
            )
        if step_type == "button":
            button_name = data["button"]
            if not isinstance(button_name, str) or button_name not in _BUTTONS:
                raise RouteError("%s: button must be one of %s" % (where, _BUTTONS))
            return cls(
                type="button",
                keys=_check_keys(data["keys"], where) if data.get("keys") else [],
                button=button_name,
                hold_ms=_num(data["hold_ms"], where, "hold_ms", positive=True),
                label=_opt_label(data, where),
            )
        text = data["text"]
        if not isinstance(text, str):
            raise RouteError("%s: label text must be a string" % where)
        return cls(type="label", text=text)


@dataclass
class Route:
    """A declarative playback script plus its FPS sampling configuration."""

    name: str
    steps: List[Step]
    warmup_ms: float = 0.0
    settle_ms: float = 0.0
    repeat: int = 1
    fps: FpsConfig = field(default_factory=FpsConfig)
    created: Optional[str] = None
    origin_platform: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    format: str = ROUTE_FORMAT
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.format != ROUTE_FORMAT:
            raise RouteError("format must be %r, got %r" % (ROUTE_FORMAT, self.format))
        if self.schema_version != SCHEMA_VERSION:
            raise RouteError(
                "unsupported schema_version %r (this build understands %d)"
                % (self.schema_version, SCHEMA_VERSION)
            )
        if self.repeat < 1:
            raise RouteError("repeat must be >= 1")
        if self.warmup_ms < 0 or self.settle_ms < 0:
            raise RouteError("warmup_ms and settle_ms must not be negative")
        self.steps = list(self.steps)
        for index, step in enumerate(self.steps):
            if not isinstance(step, Step):
                raise RouteError("steps[%d]: expected a Step, got %s" % (index, type(step).__name__))
        if not isinstance(self.fps, FpsConfig):
            raise RouteError(
                "fps must be an FpsConfig (use FpsConfig.from_dict for JSON input, got %s)"
                % type(self.fps).__name__
            )

    @property
    def duration_ms(self) -> float:
        return float(sum(step.duration for step in self.steps))

    def labels(self) -> List[Tuple[float, str]]:
        """``(t_ms, text)`` markers, walking the step timeline."""
        markers: List[Tuple[float, str]] = []
        cursor = 0.0
        for step in self.steps:
            if step.type == "label":
                markers.append((cursor, step.text or ""))
            elif step.label is not None:
                markers.append((cursor, step.label))
            cursor += step.duration
        return markers

    def hash(self) -> str:
        """Stable identity of the playback content (survives a JSON round trip)."""
        return stable_hash([step.to_dict() for step in self.steps])

    def to_dict(self) -> Dict[str, Any]:
        return {
            "format": self.format,
            "schema_version": self.schema_version,
            "name": self.name,
            "created": self.created,
            "origin_platform": self.origin_platform,
            "warmup_ms": self.warmup_ms,
            "settle_ms": self.settle_ms,
            "repeat": self.repeat,
            "fps": self.fps.to_dict(),
            "metadata": dict(self.metadata),
            "steps": [step.to_dict() for step in self.steps],
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Route":
        if not isinstance(data, dict):
            raise RouteError("route: root must be an object")
        allowed = {
            "format",
            "schema_version",
            "name",
            "created",
            "origin_platform",
            "warmup_ms",
            "settle_ms",
            "repeat",
            "fps",
            "metadata",
            "steps",
        }
        for field_name in sorted(set(data) - allowed):
            raise RouteError("route: unknown field %r" % (field_name,))
        if data.get("format") != ROUTE_FORMAT:
            raise RouteError(
                "route: format must be %r (a recorded trace is loaded with load_trace)" % (ROUTE_FORMAT,)
            )
        if data.get("schema_version") != SCHEMA_VERSION:
            raise RouteError(
                "route: unsupported schema_version %r (this build understands %d)"
                % (data.get("schema_version"), SCHEMA_VERSION)
            )
        name = data.get("name")
        if not isinstance(name, str):
            raise RouteError("route: name must be a string")
        origin = data.get("origin_platform")
        if origin is not None:
            if not isinstance(origin, str) or origin not in ("macos", "windows"):
                raise RouteError("route: unknown origin_platform %r" % (origin,))
        metadata = data.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise RouteError("route: metadata must be an object")
        steps_raw = data.get("steps")
        if not isinstance(steps_raw, list):
            raise RouteError("route: steps must be an array")
        repeat = data.get("repeat", 1)
        if isinstance(repeat, bool) or not isinstance(repeat, int):
            raise RouteError("route: repeat must be an integer")
        return cls(
            name=name,
            steps=[Step.from_dict(item, "steps[%d]" % index) for index, item in enumerate(steps_raw)],
            warmup_ms=_num(data.get("warmup_ms", 0.0), "route", "warmup_ms", allow_zero=True),
            settle_ms=_num(data.get("settle_ms", 0.0), "route", "settle_ms", allow_zero=True),
            repeat=repeat,
            fps=FpsConfig.from_dict(data.get("fps"), "route"),
            created=_str_or_none(data.get("created"), "route", "created"),
            origin_platform=origin,
            metadata=dict(metadata),
        )

    def save(self, path: str) -> None:
        save_route(self, path)

    @classmethod
    def load(cls, path: str) -> "Route":
        return load_route(path)


def looks_like_route(payload: Any) -> bool:
    return isinstance(payload, dict) and payload.get("format") == ROUTE_FORMAT


def loads_route(text: str) -> Route:
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise RouteError("route: invalid JSON: %s" % exc) from None
    return Route.from_dict(payload)


def dumps_route(route: Route, *, indent: Optional[int] = 1) -> str:
    return json.dumps(route.to_dict(), indent=indent, sort_keys=False)


def load_route(path: str) -> Route:
    with open(path, "r", encoding="utf-8") as handle:
        return loads_route(handle.read())


def save_route(route: Route, path: str) -> None:
    if route.created is None:
        route.created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(dumps_route(route))
        handle.write("\n")
