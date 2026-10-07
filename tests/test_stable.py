"""Stable hashing: identity must survive a JSON round trip."""

from __future__ import annotations

import json

from fpsbench.core.route import Route, Step
from fpsbench.core.stable import canonicalise, stable_hash
from fpsbench.core.trace import Trace, TraceEvent


def test_hash_ignores_int_float_spelling():
    assert stable_hash({"t": 0}) == stable_hash({"t": 0.0})
    assert stable_hash([1, 2]) == stable_hash([1.0, 2.0])
    assert stable_hash({"a": 1, "b": 2}) == stable_hash({"b": 2, "a": 1})
    assert stable_hash([0.5]) != stable_hash([0])


def test_canonicalise_keeps_non_integral_floats_and_booleans():
    assert canonicalise(0.25) == 0.25
    assert canonicalise(True) is True
    assert canonicalise(None) is None
    assert canonicalise("x") == "x"


def test_trace_hash_survives_saving_and_loading(tmp_path):
    trace = Trace(
        name="h",
        events=[
            TraceEvent(kind="key", t_ms=0, key="w", down=True),
            TraceEvent(kind="look", t_ms=100, dx=30, dy=-2),
            TraceEvent(kind="key", t_ms=1000, key="w", down=False),
        ],
    )
    path = tmp_path / "t.trace.json"
    trace.save(str(path))
    assert Trace.load(str(path)).hash() == trace.hash()


def test_route_hash_survives_json_and_matches_hand_written_numbers(tmp_path):
    steps = [Step.key(["w"], 4000), Step.turn(600, -20, 300)]
    route = Route(name="h", steps=steps, warmup_ms=8000)
    path = tmp_path / "r.json"
    route.save(str(path))
    assert Route.load(str(path)).hash() == route.hash()

    # The same route written with integers or floats must hash the same.
    payload = json.loads(path.read_text())
    assert payload["steps"][0]["hold_ms"] == 4000.0
    payload["steps"][0]["hold_ms"] = 4000
    assert Route.from_dict(payload).hash() == route.hash()


def test_hashes_change_when_the_content_changes():
    a = Route(name="a", steps=[Step.key(["w"], 1000)])
    b = Route(name="a", steps=[Step.key(["w"], 1001)])
    c = Route(name="another name", steps=[Step.key(["w"], 1000)])
    assert a.hash() != b.hash()
    assert a.hash() == c.hash()  # the name is not playback content
