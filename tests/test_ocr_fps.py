"""The shared OCR sampling loop, exercised with a fake capture backend.

This is the real loop the platform backends feed (``OcrSamplerFps``), so it covers
the parts the platform modules cannot: poll cadence, reading vetting, the notes
that end up in the report and the failure cutoff.
"""

from __future__ import annotations

import pytest

from fpsbench.backends._ocr_fps import OcrSamplerFps
from fpsbench.core.route import FpsConfig, Region
from fpsbench.core.samples import OCR
from tests import fakes

REGION = Region(x=0, y=0, w=40, h=20, space="pixels")


def make_sampler(texts, texts_before_failure=None, *, sample_hz=200.0, max_failures=None):
    ocr = fakes.FakeOcrBackend(texts, fail_after=texts_before_failure)
    return OcrSamplerFps(ocr, sleep=lambda _seconds: None, max_consecutive_failures=max_failures), ocr


def drain(sampler, *, want=2):
    sampler.start(FpsConfig(mode="ocr", region=REGION, sample_hz=200.0))
    collected = []
    for sample in sampler.samples():
        collected.append(sample)
        if len(collected) >= want:
            break
    sampler.stop()
    return collected


def test_samples_are_usable_readings_only():
    sampler, ocr = make_sampler(["60 FPS", "61 FPS", "garbage", "0 FPS", "62 FPS"])
    samples = drain(sampler, want=3)
    assert [sample.value for sample in samples] == [60.0, 61.0, 62.0]
    assert all(sample.source == OCR for sample in samples)
    assert ocr.prepared is True  # start() prepared the capture backend


def test_notes_name_the_recognised_text_and_the_rejections():
    sampler, _ocr = make_sampler(["143 FPS", "noise", "0", "144 FPS"])
    drain(sampler, want=2)
    notes = " | ".join(sampler.notes())
    assert "OCR read '144 FPS'" in notes
    assert "rejected while sampling" in notes
    rejected = [reading for reading in sampler.readings() if reading.suspicious]
    assert len(rejected) == 2  # "noise" and "0 FPS"
    assert "2 of 4 OCR polls" in notes


def test_final_samples_apply_jump_vetting():
    # 60, 60, a 900 spike that the next reading refutes, then 61.
    sampler, _ocr = make_sampler(["60 FPS", "60 FPS", "900 FPS", "61 FPS"])
    drain(sampler, want=4)
    sampler.stop()
    assert [sample.value for sample in sampler.final_samples()] == [60.0, 60.0, 61.0]
    notes = " | ".join(sampler.notes())
    assert "dropped after the run as unconfirmed jumps" in notes
    assert "3 of 4 polls usable" in notes  # the spike never reaches the statistics


def test_a_capture_error_is_recorded_and_stops_sampling():
    sampler, _ocr = make_sampler(["60 FPS"], texts_before_failure=1)
    samples = drain(sampler, want=5)
    assert len(samples) == 1  # the loop ended on the failure
    assert any("capture failed" in note for note in sampler.notes())


def test_sampling_gives_up_after_repeated_unusable_polls():
    sampler, _ocr = make_sampler(["nothing here"], max_failures=4)
    assert drain(sampler, want=1) == []
    notes = " | ".join(sampler.notes())
    assert "4 polls in a row" in notes
    assert "overlay counter visible" in notes


def test_missing_region_is_refused_before_any_capture():
    sampler, ocr = make_sampler(["60 FPS"])
    from fpsbench.backends.base import BackendUnavailable

    with pytest.raises(BackendUnavailable) as excinfo:
        sampler.start(FpsConfig(mode="ocr"))
    assert "pick-region" in (getattr(excinfo.value, "hint", "") or "")
    assert ocr.calls == 0
