"""OCR text handling: parsing, step-hold and outlier rejection."""

from __future__ import annotations

import pytest

from fpsbench.core.ocrparse import (
    OcrReading,
    hold_spans,
    make_readings,
    parse_fps,
    parse_fps_detail,
    parse_readings,
    readings_to_samples,
    vet_readings,
)
from fpsbench.core.samples import OCR

MS = 1_000_000


# --------------------------------------------------------------------------- #
# text -> value
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text,expected",
    [
        ("143 FPS", 143.0),
        ("FPS: 143.2", 143.2),
        ("143", 143.0),
        ("fps 60", 60.0),
        ("59.9 FPS\n12.4 ms", 59.9),
        ("144 fps", 144.0),
        ("Frame rate: 87", 87.0),
        ("59,9 FPS", 59.9),
        ("1,234 FPS", 1234.0),
        ("FPS\n120", 120.0),
        ("  61  ", 61.0),
    ],
)
def test_parse_fps_accepts_the_common_overlay_shapes(text, expected):
    assert parse_fps(text) == pytest.approx(expected)


@pytest.mark.parametrize(
    "text",
    ["", "no counter here", "FPS", "0 FPS", "99999 FPS", "---"],
)
def test_parse_fps_rejects_unusable_text(text):
    assert parse_fps(text) is None


def test_parse_fps_prefers_the_number_next_to_the_keyword():
    # 1920 and 1080 are plausible FPS numbers, so the keyword has to win.
    detail = parse_fps_detail("FPS 143\n1920x1080")
    assert detail.value == 143
    assert 1920.0 in detail.candidates and 1080.0 in detail.candidates

    # A frame time next to the counter must not be mistaken for the counter.
    assert parse_fps("FPS 143\n14.3 ms") == 143


def test_resolution_strings_are_not_mistaken_for_a_counter():
    detail = parse_fps_detail("1920x1080")
    assert detail.value is None
    assert "resolution" in detail.reason or "range" in detail.reason


def test_parse_fps_respects_custom_bounds():
    assert parse_fps("20 FPS", min_fps=30.0) is None
    assert parse_fps("20 FPS", min_fps=30.0, max_fps=100.0) is None
    assert parse_fps("45 FPS", min_fps=30.0, max_fps=100.0) == 45


# --------------------------------------------------------------------------- #
# reading vetting
# --------------------------------------------------------------------------- #
def test_readings_reject_unparseable_and_out_of_range_polls():
    readings = parse_readings([(0, "60 FPS"), (1000, "noise"), (2000, "0 FPS"), (3000, "61 FPS")])
    assert [reading.usable for reading in readings] == [True, False, False, True]
    assert "no numeric token" in readings[1].reason


def test_unconfirmed_spike_is_rejected_but_a_sustained_change_is_kept():
    values = ["60 FPS", "60 FPS", "900 FPS", "60 FPS"]  # isolated spike -> misread
    readings = make_readings([(index * MS, text) for index, text in enumerate(values)])
    assert [reading.usable for reading in readings] == [True, True, False, True]
    assert "not confirmed" in readings[2].reason

    sustained = ["60 FPS", "60 FPS", "140 FPS", "141 FPS"]  # real settings change
    readings = make_readings([(index * MS, text) for index, text in enumerate(sustained)])
    assert all(reading.usable for reading in readings)


def test_jump_detection_only_marks_unconfirmed_spikes():
    # A 5x spike followed by a return to the old level is a misread.
    with_more = parse_readings([(0, "60 FPS"), (MS, "300 FPS"), (2 * MS, "61 FPS")])
    vetted = vet_readings(with_more)
    assert [reading.usable for reading in vetted] == [True, False, True]
    assert "not confirmed" in vetted[1].reason

    # A trailing spike can never be confirmed, so it is dropped as well.
    trailing = parse_readings([(0, "60 FPS"), (MS, "300 FPS")])
    assert [reading.usable for reading in vet_readings(trailing)] == [True, False]

    # confirm_jumps=False rejects any jump without looking ahead.
    rejected = vet_readings(parse_readings([(0, "60 FPS"), (MS, "61 FPS")]), confirm_jumps=False)
    assert all(reading.usable for reading in rejected)


def test_readings_to_samples_emits_one_sample_per_accepted_reading():
    readings = make_readings(
        [(0, "60 FPS"), (MS, "61 FPS"), (2 * MS, "garbage"), (3 * MS, "62 FPS")]
    )
    samples = readings_to_samples(readings, label="a")
    assert [sample.value for sample in samples] == [60.0, 61.0, 62.0]
    assert all(sample.source == OCR and sample.label == "a" for sample in samples)
    assert [sample.t_ns for sample in samples] == [0, MS, 3 * MS]


def test_hold_spans_step_hold_and_stop_at_the_next_poll():
    readings = [OcrReading(0, 60.0), OcrReading(MS, 61.0), OcrReading(2 * MS, None, suspicious=True)]
    spans = hold_spans(readings, end_ns=3 * MS)
    assert spans == [(0, MS, 60.0), (MS, 2 * MS, 61.0)]


def test_hold_spans_extends_the_last_reading_to_the_end():
    readings = [OcrReading(0, 60.0)]
    assert hold_spans(readings, end_ns=5 * MS) == [(0, 5 * MS, 60.0)]
    # Without an end time the last reading is held for the default 1 s.
    assert hold_spans(readings) == [(0, 1000 * MS, 60.0)]
    assert hold_spans(readings, hold_last_ms=200.0) == [(0, 200 * MS, 60.0)]
