"""Statistics: the two source semantics, kept strictly apart."""

from __future__ import annotations

import math

import pytest

from fpsbench.core.samples import METALPERFTRACE, OCR, PRESENTMON, FpsSample, assign_labels
from fpsbench.core.stats import (
    OCR_CAVEAT,
    StatsError,
    low_fps,
    percentile,
    segments_by_label,
    summarize,
    summarize_many,
    trim,
)

MS = 1_000_000


def frame_samples(frame_times_ms):
    samples = []
    t_ns = 0
    for dt_ms in frame_times_ms:
        t_ns += int(dt_ms * MS)
        samples.append(FpsSample(source=PRESENTMON, t_ns=t_ns, dt_ns=int(dt_ms * MS)))
    return samples


def reading_samples(values, *, t0=0, step_ms=1000):
    return [
        FpsSample(source=OCR, t_ns=t0 + index * int(step_ms * MS), value=float(value))
        for index, value in enumerate(values)
    ]


# --------------------------------------------------------------------------- #
# presentmon: true frame intervals
# --------------------------------------------------------------------------- #
def test_presentmon_summary_matches_hand_computed_values():
    samples = frame_samples([10.0] * 9 + [50.0])
    summary = summarize(samples)
    frame = summary.frame
    assert frame is not None
    assert frame.frames == 10
    assert summary.duration_ns == 140 * MS
    assert summary.avg_fps == pytest.approx(10 / 0.140, rel=1e-9)
    assert summary.min_fps == pytest.approx(20.0)
    assert summary.max_fps == pytest.approx(100.0)
    assert summary.median_fps == pytest.approx(100.0)
    # 1% of 10 frames is one frame: the 50 ms outlier.
    assert summary.low_1pct == pytest.approx(20.0)
    assert summary.low_01pct == pytest.approx(20.0)
    assert frame.frame_time_ms_p50 == pytest.approx(10.0)
    assert frame.frame_time_ms_p99 == pytest.approx(46.4)
    # A stutter is a frame longer than twice the median frame time.
    assert frame.stutters == 1
    assert frame.stutter_ratio == pytest.approx(0.1)


def test_presentmon_marks_itself_as_per_frame_data():
    summary = summarize(frame_samples([16.0] * 4))
    assert summary.is_per_frame
    assert summary.frame is not None
    assert summary.readings is None
    assert "PresentMon" in " ".join(summary.notes)


def test_metalperftrace_uses_the_same_per_frame_semantics_with_its_own_caveat():
    samples = [
        FpsSample(source=METALPERFTRACE, t_ns=index * int(dt_ms * MS), dt_ns=int(dt_ms * MS))
        for index, dt_ms in enumerate([16.0, 16.0, 16.0, 16.0])
    ]
    summary = summarize(samples)
    assert summary.source == METALPERFTRACE
    assert summary.is_per_frame
    assert summary.avg_fps == pytest.approx(62.5)
    assert summary.frame is not None
    note = " ".join(summary.notes)
    assert "metalperftrace" in note
    assert "1 s" in note


def test_sources_are_never_mixed():
    mixed = frame_samples([16.0]) + [
        FpsSample(source=METALPERFTRACE, t_ns=1, dt_ns=16 * MS)
    ]
    with pytest.raises(StatsError) as excinfo:
        summarize(mixed)
    assert "presentmon" in str(excinfo.value)


def test_low_fps_averages_the_slowest_frames():
    frame_times = [10 * MS] * 990 + [100 * MS] * 10
    # 1% of 1000 frames = the 10 slowest -> their mean frame time is 100 ms.
    assert low_fps(frame_times, 0.01) == pytest.approx(10.0)
    # fraction 1.0 averages every frame: 1090 ms / (1000 / 1000) ... = 91.7 FPS.
    assert low_fps(frame_times, 1.0) == pytest.approx(1000 * 1e9 / (990 * 10 * MS + 10 * 100 * MS))
    with pytest.raises(StatsError):
        low_fps(frame_times, 0.0)
    with pytest.raises(StatsError):
        low_fps([], 0.01)


# --------------------------------------------------------------------------- #
# ocr: counter readings
# --------------------------------------------------------------------------- #
def test_ocr_summary_uses_reading_statistics_and_mentions_the_caveat():
    samples = reading_samples([60, 61, 62, 59, 60, 144])
    summary = summarize(samples)
    assert summary.source == OCR
    assert summary.is_per_frame is False
    assert summary.samples == 6
    assert summary.avg_fps == pytest.approx(sum([60, 61, 62, 59, 60, 144]) / 6.0)
    assert summary.min_fps == pytest.approx(59.0)
    assert summary.max_fps == pytest.approx(144.0)
    assert summary.duration_ns == 5 * 1000 * MS
    assert summary.readings is not None
    assert summary.readings.readings == 6
    assert OCR_CAVEAT in summary.notes[0]
    assert "not per-frame" in OCR_CAVEAT


def test_ocr_summary_has_no_frame_time_fields():
    summary = summarize(reading_samples([60, 60]))
    assert summary.frame is None
    assert summary.readings is not None
    assert summary.low_1pct == pytest.approx(60.0)
    assert summary.low_01pct == pytest.approx(60.0)


def test_ocr_low_quantiles_interpolate():
    summary = summarize(reading_samples([10, 20, 30, 40, 50]))
    assert summary.readings is not None
    # p1 of [10..50] sits just above the minimum.
    assert 10.0 <= summary.low_1pct <= 20.0


# --------------------------------------------------------------------------- #
# invariants
# --------------------------------------------------------------------------- #
def test_mixing_sources_is_refused():
    mixed = frame_samples([16.0]) + reading_samples([60.0])
    with pytest.raises(StatsError) as excinfo:
        summarize(mixed)
    assert "refusing to mix" in str(excinfo.value)


def test_empty_input_is_refused():
    with pytest.raises(StatsError):
        summarize([])


def test_percentile_matches_numpy_convention():
    values = sorted([1.0, 2.0, 3.0, 4.0])
    assert percentile(values, 0) == 1.0
    assert percentile(values, 100) == 4.0
    assert percentile(values, 50) == pytest.approx(2.5)
    assert percentile(values, 25) == pytest.approx(1.75)
    with pytest.raises(StatsError):
        percentile(values, 101)
    with pytest.raises(StatsError):
        percentile([], 50)


def test_summarize_many_handles_both_sources():
    presentmon = summarize(frame_samples([16.0, 16.0]))
    ocr = summarize(reading_samples([60.0, 60.0]))
    combined = summarize_many([frame_samples([16.0, 16.0]), reading_samples([60.0])])
    assert [summary.source for summary in combined] == [PRESENTMON, OCR]
    assert presentmon.avg_fps == pytest.approx(62.5)
    assert ocr.avg_fps == pytest.approx(60.0)


# --------------------------------------------------------------------------- #
# segmentation
# --------------------------------------------------------------------------- #
def test_segments_group_by_label_and_keep_order():
    samples = assign_labels(
        reading_samples([60, 61, 62, 63]), [(0, "start"), (2 * 1000 * MS, "boss")]
    )
    segments = segments_by_label(samples)
    assert list(segments) == ["start", "boss"]
    assert len(segments["start"]) == 2
    assert len(segments["boss"]) == 2
    assert summarize(segments["boss"]).avg_fps == pytest.approx(62.5)


def test_assign_labels_uses_the_most_recent_marker():
    samples = reading_samples([60, 61, 62], step_ms=1000)
    labelled = assign_labels(samples, [(0, "a"), (1000 * MS, "b"), (2000 * MS, "")])
    assert [sample.label for sample in labelled] == ["a", "b", None]


def test_trim_drops_the_warmup_window():
    samples = reading_samples([1, 2, 3, 4, 5], step_ms=1000)
    kept = trim(samples, t0_ns=0, skip_ns=3000 * MS)
    assert [sample.value for sample in kept] == [4.0, 5.0]
    assert trim(samples, 0, 0) == samples


def test_frame_weighted_average_is_not_the_mean_of_instantaneous_fps():
    """A 1 s hitch must dominate the average - the reason percentiles need frames."""
    samples = frame_samples([8.0] * 125 + [1000.0])
    summary = summarize(samples)
    assert summary.avg_fps == pytest.approx(126 / 2.0, rel=1e-6)
    assert summary.median_fps == pytest.approx(125.0)
    assert not math.isclose(summary.avg_fps, summary.median_fps, rel_tol=0.1)
