"""Shared FPS backend dispatch: ``auto`` -> the platform's per-frame source, else OCR.

Each platform has one *primary* source of real frame timing - PresentMon (ETW) on
Windows, ``metalperftrace`` on macOS 27+ - and one fallback, overlay OCR.  The
dispatcher picks between them from ``config.mode`` at ``start()`` time:

* ``auto`` tries the primary source and visibly falls back to OCR;
* the primary's own mode string (``presentmon`` / ``metalperftrace``) forces it and
  fails with a hint instead of falling back;
* ``ocr`` forces the counter.

A fallback is *always* reported through :meth:`notes` so a report can never look
like a per-frame measurement when it is not.  Overlay OCR needs a configured
region; if the primary source is unavailable *and* no region is set, the failure
names both problems instead of dying on the OCR one alone.
"""

from __future__ import annotations

from typing import Callable, Iterator, List, Optional

from ..core.route import FpsConfig
from ..core.samples import FpsSample
from .base import BackendUnavailable, FpsBackend

__all__ = ["DispatchingFpsBackend"]

FALLBACK_NOTE = (
    "the platform's per-frame FPS source is unavailable, so FPS came from overlay "
    "OCR: average FPS is usable, 1%/0.1% lows are approximations bounded by the "
    "counter refresh rate"
)


class DispatchingFpsBackend(FpsBackend):
    """Chooses a concrete FPS backend from ``config.mode`` at ``start()`` time."""

    def __init__(
        self,
        *,
        primary_mode: str,
        ocr_factory: Callable[[], FpsBackend],
        primary_factory: Optional[Callable[[Optional[str]], FpsBackend]] = None,
        primary_note: str = "the platform's per-frame FPS source is not available",
        primary_hint: str = "use --fps ocr with a visible overlay counter",
        unsupported_hint: str = "use --fps auto or --fps ocr",
    ) -> None:
        self._primary_mode = primary_mode
        self._primary_factory = primary_factory
        self._ocr_factory = ocr_factory
        self._primary_note = primary_note
        self._primary_hint = primary_hint
        self._unsupported_hint = unsupported_hint
        self._inner: Optional[FpsBackend] = None
        self._notes: List[str] = []
        self._primary_failure: Optional[str] = None
        self.source = ""

    @property
    def mode(self) -> str:
        """The mode string that forces the primary backend (``presentmon`` / ...)."""
        return self._primary_mode

    @property
    def inner(self) -> Optional[FpsBackend]:
        return self._inner

    def start(
        self,
        config: FpsConfig,
        *,
        process_name: Optional[str] = None,
        presentmon_path: Optional[str] = None,
    ) -> None:
        self._notes = []
        self._primary_failure = None
        if config.mode not in ("auto", "ocr", self._primary_mode):
            raise BackendUnavailable(
                "%s is not an FPS source on this platform" % config.mode,
                hint=self._unsupported_hint,
            )

        if config.mode in ("auto", self._primary_mode):
            candidate: Optional[FpsBackend] = None
            failure = self._primary_note
            primary_hint = self._primary_hint
            if self._primary_factory is not None:
                try:
                    candidate = self._primary_factory(presentmon_path)
                    candidate.start(
                        config, process_name=process_name, presentmon_path=presentmon_path
                    )
                except BackendUnavailable as exc:
                    candidate = None
                    failure = str(exc)
                    # The backend's own hint is the actionable one (it may name the
                    # processes it could have used); the generic one is the fallback.
                    primary_hint = exc.hint or self._primary_hint
                except Exception as exc:  # surface anything else as a fallback reason
                    candidate = None
                    failure = "%s: %s" % (type(exc).__name__, exc)
            if candidate is not None:
                self._inner = candidate
                self.source = candidate.source
                return
            if config.mode == self._primary_mode:
                raise BackendUnavailable(
                    "%s is unavailable: %s" % (self._primary_mode, failure),
                    hint=primary_hint,
                )
            self._primary_failure = failure
            self._notes.append("%s; %s" % (failure, FALLBACK_NOTE))

        ocr = self._ocr_factory()
        try:
            ocr.start(config, process_name=process_name, presentmon_path=presentmon_path)
        except BackendUnavailable as exc:
            if self._primary_failure is None:
                raise
            # Both halves failed: name both, so "pick-region" is not the only thing
            # the user learns (the per-frame source usually has its own fix).
            hint = primary_hint
            if exc.hint and exc.hint != hint:
                hint = "%s; to use the OCR fallback instead: %s" % (hint, exc.hint)
            raise BackendUnavailable(
                "no per-frame FPS source (%s) and the OCR fallback cannot run: %s"
                % (self._primary_failure, exc),
                hint=hint,
            ) from None
        self._inner = ocr
        self.source = ocr.source

    def samples(self) -> Iterator[FpsSample]:
        if self._inner is None:
            raise BackendUnavailable("FPS backend was not started")
        return self._inner.samples()

    def stop(self) -> None:
        if self._inner is not None:
            self._inner.stop()

    def final_samples(self) -> Optional[List[FpsSample]]:
        return None if self._inner is None else self._inner.final_samples()

    def notes(self) -> List[str]:
        notes = list(self._notes)
        if self._inner is not None:
            notes.extend(self._inner.notes())
        return notes
