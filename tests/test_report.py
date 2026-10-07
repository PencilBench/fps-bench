"""Report rendering: tables, JSON, CSV and the ASCII timeline."""

from __future__ import annotations

import csv
import io
import json

import pytest

from fpsbench.core.report import (
    BenchRun,
    ascii_timeline,
    build_result,
    format_duration,
    render_terminal,
    result_to_json,
    samples_to_csv,
)
from fpsbench.core.samples import METALPERFTRACE, OCR, PRESENTMON, FpsSample, assign_labels

MS = 1_000_000


def frame_run(index=1, *, frames=200, dt_ms=10.0, labels=True):
    samples = []
    for position in range(frames):
        t_ns = position * int(dt_ms * MS)
        dt = int(dt_ms * MS) * (2 if position % 40 == 0 else 1)
        samples.append(FpsSample(source=PRESENTMON, t_ns=t_ns, dt_ns=dt))
    if labels:
        samples = assign_labels(samples, [(0, "start"), (1000 * MS, "boss")])
    return BenchRun(index=index, samples=samples, started_ns=0, ended_ns=frames * int(dt_ms * MS))


def ocr_run(index=1, values=(60.0, 61.0, 59.0, 144.0, 60.0)):
    samples = [
        FpsSample(source=OCR, t_ns=position * 1000 * MS, value=value, label="all")
        for position, value in enumerate(values)
    ]
    return BenchRun(index=index, samples=samples, started_ns=0, ended_ns=5000 * MS)


def presentmon_result():
    return build_result(
        "Liurnia ride",
        [frame_run(1), frame_run(2)],
        route_hash="abc123",
        route_source="trace",
        metadata={"os": "macOS", "fps_source": PRESENTMON},
    )


def metalperftrace_result():
    runs = []
    for index in (1, 2):
        run = frame_run(index)
        runs.append(
            BenchRun(
                index=run.index,
                samples=[
                    FpsSample(source=METALPERFTRACE, t_ns=sample.t_ns, dt_ns=sample.dt_ns, label=sample.label)
                    for sample in run.samples
                ],
                started_ns=run.started_ns,
                ended_ns=run.ended_ns,
            )
        )
    return build_result(
        "Liurnia ride",
        runs,
        route_hash="abc123",
        route_source="trace",
        metadata={"os": "macOS", "fps_source": METALPERFTRACE},
    )


# --------------------------------------------------------------------------- #
# terminal rendering
# --------------------------------------------------------------------------- #
def test_terminal_report_contains_the_essentials():
    text = render_terminal(presentmon_result(), color=False)
    assert "fpsbench - Liurnia ride" in text
    assert "FPS source: presentmon" in text
    assert "metadata" in text
    assert "overall" in text
    assert "per repeat" in text
    assert "per segment (all runs)" in text
    assert "frame time timeline" in text
    assert "1% low" in text
    assert "p99 frame" in text
    assert "median of runs" in text


def test_terminal_report_is_plain_text_when_colour_is_off():
    text = render_terminal(presentmon_result(), color=False)
    assert "\033[" not in text


def test_terminal_report_colours_when_asked():
    text = render_terminal(presentmon_result(), color=True)
    assert "\033[" in text


def test_ocr_report_states_the_precision_caveat():
    result = build_result("ocr run", [ocr_run()], route_source="route", metadata={"os": "macOS"})
    text = render_terminal(result, color=False)
    assert "NOT per-frame data" in text
    assert "overlay OCR counter readings" in text
    assert "readings" in text  # the OCR-specific column set
    assert "frame time timeline" in text


def test_metalperftrace_report_uses_the_frame_statistics():
    result = metalperftrace_result()
    text = render_terminal(result, color=False)
    assert "FPS source: metalperftrace" in text
    assert "NOT per-frame data" not in text
    assert "p99 frame" in text
    assert "frame time timeline" in text
    payload = json.loads(result_to_json(result))
    assert payload["source"] == METALPERFTRACE
    assert payload["overall"]["frame"]["frames"] > 0


def test_report_without_samples_is_not_a_crash():
    empty = BenchRun(index=1, samples=[])
    result = build_result("nothing", [empty], metadata={}, warnings=["repeat 1 produced no FPS samples"])
    text = render_terminal(result, color=False)
    assert "no usable samples" in text
    assert "warnings" in text


def test_build_result_refuses_mixed_sources():
    mixed = BenchRun(index=1, samples=frame_run().samples + ocr_run().samples)
    with pytest.raises(ValueError):
        build_result("mixed", [mixed])


# --------------------------------------------------------------------------- #
# machine readable exports
# --------------------------------------------------------------------------- #
def test_json_report_has_the_documented_shape():
    payload = json.loads(result_to_json(presentmon_result()))
    assert payload["name"] == "Liurnia ride"
    assert payload["source"] == PRESENTMON
    assert payload["route_hash"] == "abc123"
    assert len(payload["runs"]) == 2
    assert payload["runs"][0]["repeat"] == 1
    assert payload["runs"][0]["summary"]["frame"]["frames"] > 0
    assert payload["runs"][0]["segments"][0]["label"] == "start"
    assert payload["overall"]["avg_fps"] > 0
    assert payload["median_of_runs"] is not None
    assert payload["metadata"]["os"] == "macOS"


def test_json_report_of_an_empty_run_keeps_null_summaries():
    result = build_result("nothing", [BenchRun(index=1, samples=[])], metadata={})
    payload = json.loads(result_to_json(result))
    assert payload["runs"][0]["summary"] is None
    assert payload["overall"] is None


def test_csv_export_is_parseable_and_source_tagged():
    result = presentmon_result()
    text = samples_to_csv(result.all_samples)
    rows = list(csv.DictReader(io.StringIO(text)))
    assert len(rows) == len(result.all_samples)
    assert rows[0]["source"] == PRESENTMON
    assert rows[0]["frame_ms"] != ""
    assert float(rows[0]["t_s"]) == 0.0
    assert rows[-1]["label"] == "boss"


def test_csv_export_leaves_frame_ms_empty_for_ocr():
    rows = list(csv.DictReader(io.StringIO(samples_to_csv(ocr_run().samples))))
    assert rows[0]["source"] == OCR
    assert rows[0]["frame_ms"] == ""
    assert float(rows[0]["fps"]) == 60.0


# --------------------------------------------------------------------------- #
# ascii timeline
# --------------------------------------------------------------------------- #
def test_ascii_timeline_draws_bars_and_reports_the_range():
    text = ascii_timeline(frame_run().samples)
    assert "|" in text and "." in text
    assert "FPS" in text
    assert "samples" in text
    assert len(text.splitlines()) >= 5


def test_ascii_timeline_handles_ocr_and_extremes():
    assert "single sample" in ascii_timeline([FpsSample(source=OCR, t_ns=0, value=60.0)])
    assert "no samples" in ascii_timeline([])
    flat = [FpsSample(source=OCR, t_ns=index * MS, value=60.0) for index in range(10)]
    assert "no samples" not in ascii_timeline(flat)


def test_ascii_timeline_marks_labels():
    samples = assign_labels(
        [FpsSample(source=OCR, t_ns=index * MS, value=60.0) for index in range(20)],
        [(0, "alpha"), (10 * MS, "beta")],
    )
    assert "alpha" in ascii_timeline(samples)


def test_metadata_block_renders_origin_and_source():
    result = build_result(
        "named",
        [frame_run(1)],
        route_hash="deadbeef",
        route_source="trace",
        metadata={
            "os": "macOS",
            "fps_source": "presentmon",
            "origin_platform": "macos",
            "platform_info": {"gpu": "Apple M6", "refresh_hz": 120},
            "fps_config": {"region": {"x": 10, "y": 20, "w": 120, "h": 40, "space": "points"}},
            "backend_notes": ["PresentMon 2.2.0: PresentMon.exe"],
        },
    )
    text = render_terminal(result, color=False)
    assert "recorded on: macos" in text
    assert "gpu: Apple M6" in text
    assert "FPS region: 120x40 at (10,20) points" in text
    assert "note: PresentMon 2.2.0" in text


def test_format_duration_is_human_readable():
    assert format_duration(1500 * MS) == "1.5s"
    assert format_duration(65000 * MS) == "1m05.0s"
