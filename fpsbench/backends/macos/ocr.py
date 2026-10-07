"""macOS screen-region capture + FPS counter recognition.

Capture prefers ScreenCaptureKit (``SCScreenshotManager``, macOS 14+) and falls
back to the older ``CGWindowListCreateImage``.  Both are fed rects in *display
points*, which is what ``pick-region`` produces, so a region file is portable
between the two paths.

Recognition uses Vision's accurate text recogniser and hands the raw text to
``core.ocrparse`` (so the parsing rules are identical on both platforms and
unit tested without a screen).
"""

from __future__ import annotations

import threading
from typing import Any, Optional

from ...core.ocrparse import FpsParse, parse_fps_detail
from ...core.route import Region
from ..base import BackendUnavailable, OcrBackend

try:  # pragma: no cover - macOS + pyobjc only
    import Quartz
    import Vision
except ImportError as exc:  # pragma: no cover
    raise BackendUnavailable(
        "pyobjc (Quartz/Vision) is required for macOS OCR: %s" % exc,
        hint='python3 -m pip install -e ".[macos]"',
    ) from exc

try:  # pragma: no cover
    import ScreenCaptureKit as SCK
except ImportError:  # pragma: no cover - macOS < 12.3
    SCK = None

__all__ = ["MacOcrBackend", "BACKEND"]

_SCREEN_PERMISSION_HINT = (
    "grant Screen Recording permission to your terminal/IDE in System Settings > "
    "Privacy & Security > Screen Recording, then restart it"
)


#: ScreenCaptureKit talks to the window server over XPC.  When the session has
#: no access (denied permission, no GUI session) that call can block instead of
#: failing, so every SCK call runs on a worker thread with a hard deadline.  A
#: worker stuck past the deadline is a daemon thread and is abandoned.
_SCK_TIMEOUT_S = 5.0


class MacOcrBackend(OcrBackend):
    name = "ScreenCaptureKit+Vision"
    region_space = "points"

    def __init__(self) -> None:
        self._display = None
        self._scale = 1.0

    def prepare(self) -> None:
        self._scale = _backing_scale()

    # -- capture ----------------------------------------------------------- #
    def grab_region(self, region: Region) -> Any:
        rect = self._rect(region)
        image = self._capture_screen_capture_kit(rect)
        if image is None:
            image = self._capture_window_list(rect)
        if image is None:
            raise BackendUnavailable(
                "screen capture returned nothing", hint=_SCREEN_PERMISSION_HINT
            )
        return image

    def _rect(self, region: Region):
        scale = 1.0
        if region.space == "pixels":
            scale = self._scale or 1.0
        return Quartz.CGRectMake(
            region.x / scale, region.y / scale, region.w / scale, region.h / scale
        )

    def _capture_screen_capture_kit(self, rect) -> Optional[Any]:
        if SCK is None or not hasattr(SCK, "SCScreenshotManager"):
            return None
        try:
            display = self._display or _display_for_point(rect.origin.x, rect.origin.y)
            self._display = display
            if display is None:
                return None
            configuration = SCK.SCStreamConfiguration.alloc().init()
            configuration.setWidth_(int(max(1, round(rect.size.width * self._scale))))
            configuration.setHeight_(int(max(1, round(rect.size.height * self._scale))))
            frame = display.frame()
            local = Quartz.CGRectMake(
                rect.origin.x - frame.origin.x,
                rect.origin.y - frame.origin.y,
                rect.size.width,
                rect.size.height,
            )
            configuration.setSourceRect_(local)
            content_filter = SCK.SCContentFilter.alloc().initWithDisplay_excludingWindows_(
                display, []
            )
            holder = {"image": None, "error": None}
            done = threading.Event()

            def handler(image, error):
                holder["image"] = image
                holder["error"] = error
                done.set()

            def call():
                SCK.SCScreenshotManager.captureImageWithFilter_configuration_completionHandler_(
                    content_filter, configuration, handler
                )

            threading.Thread(target=_guarded, args=(call, done), daemon=True).start()
            if not done.wait(timeout=_SCK_TIMEOUT_S):
                return None
            return holder["image"]
        except Exception:
            return None

    def _capture_window_list(self, rect) -> Optional[Any]:
        """Synchronous CoreGraphics fallback (also used when SCK is unavailable).

        This call talks to the window server too, so it is bounded as well: a
        blocked capture has to surface as an error, not as a silent no-op.
        """
        try:
            return _call_bounded(
                lambda: Quartz.CGWindowListCreateImage(
                    rect,
                    Quartz.kCGWindowListOptionOnScreenOnly,
                    Quartz.kCGNullWindowID,
                    Quartz.kCGWindowImageBestResolution,
                ),
                timeout_s=_CG_TIMEOUT_S,
                what="CGWindowListCreateImage",
            )
        except BackendUnavailable:
            raise
        except Exception as exc:
            raise BackendUnavailable(
                "CGWindowListCreateImage failed: %s: %s" % (type(exc).__name__, exc),
                hint=_SCREEN_PERMISSION_HINT,
            ) from exc

    # -- recognition ------------------------------------------------------- #
    def recognise(self, image: Any) -> str:
        request = Vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        if hasattr(request, "setUsesLanguageCorrection_"):
            request.setUsesLanguageCorrection_(False)
        if hasattr(request, "setCustomWords_"):
            request.setCustomWords_(["FPS", "fps", "Frames Per Second"])
        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(image, None)
        success, error = handler.performRequests_error_([request], None)
        if not success:
            raise BackendUnavailable("Vision text recognition failed: %s" % (error,))
        lines = []
        for observation in request.results() or []:
            candidates = observation.topCandidates_(1)
            if candidates and len(candidates):
                lines.append(str(candidates[0].string()))
        return "\n".join(lines)

    def read_fps(
        self, region: Region, *, min_fps: float = 0.5, max_fps: float = 2000.0
    ) -> FpsParse:
        image = self.grab_region(region)
        text = self.recognise(image)
        return parse_fps_detail(text, min_fps=min_fps, max_fps=max_fps)

    def capture_text(self, region: Region) -> tuple:
        """``(text, image)`` - used by ``pick-region`` for a live preview."""
        image = self.grab_region(region)
        return self.recognise(image), image


#: CoreGraphics capture is a synchronous window server round trip.
_CG_TIMEOUT_S = 3.0


def _call_bounded(function, *, timeout_s: float, what: str):
    """Run ``function`` on a worker thread; a blocked call becomes an error."""
    holder: dict = {}
    done = threading.Event()

    def worker():
        try:
            holder["result"] = function()
        except BaseException as exc:  # re-raised on the calling thread
            holder["error"] = exc
        finally:
            done.set()

    threading.Thread(target=worker, args=(), daemon=True).start()
    if not done.wait(timeout=timeout_s):
        raise BackendUnavailable(
            "%s did not answer within %.0fs - screen capture access is most likely denied"
            % (what, timeout_s),
            hint=_SCREEN_PERMISSION_HINT,
        )
    if "error" in holder:
        raise holder["error"]
    return holder.get("result")


def _guarded(call, done: threading.Event) -> None:
    """Run a blocking ObjC call on a worker thread; a stuck call is abandoned."""
    try:
        call()
    except BaseException:
        pass
    finally:
        done.set()


def _display_for_point(x: float, y: float, timeout_s: float = _SCK_TIMEOUT_S):
    """The display whose Quartz frame contains ``(x, y)``.

    ``SCShareableContent.displays()`` has no documented order, so taking the first
    one puts the capture on the wrong screen whenever the FPS counter is not on the
    primary display.  ``display.frame()`` is in the same global top-left space as
    the region, so the containing display can be picked directly.
    """
    try:
        content = _shareable_content(timeout_s)
    except Exception:
        return None
    displays = list(content.displays() or [])
    for display in displays:
        frame = display.frame()
        if (
            frame.origin.x <= x < frame.origin.x + frame.size.width
            and frame.origin.y <= y < frame.origin.y + frame.size.height
        ):
            return display
    return displays[0] if displays else None


def _shareable_content(timeout_s: float = _SCK_TIMEOUT_S):
    holder = {"content": None, "error": None}
    done = threading.Event()

    def handler(content, error):
        holder["content"] = content
        holder["error"] = error
        done.set()

    def call():
        SCK.SCShareableContent.getShareableContentWithCompletionHandler_(handler)

    threading.Thread(target=_guarded, args=(call, done), daemon=True).start()
    if not done.wait(timeout=timeout_s):
        raise BackendUnavailable(
            "ScreenCaptureKit did not answer within %.0fs" % timeout_s, hint=_SCREEN_PERMISSION_HINT
        )
    if holder["content"] is None:
        raise BackendUnavailable(
            "ScreenCaptureKit refused to list displays: %s" % (holder["error"],),
            hint=_SCREEN_PERMISSION_HINT,
        )
    return holder["content"]


def _backing_scale() -> float:
    try:
        from AppKit import NSScreen

        screen = NSScreen.mainScreen()
        if screen is not None:
            return float(screen.backingScaleFactor())
    except Exception:
        pass
    return 1.0


def capture_text(region: Region) -> tuple:
    backend = MacOcrBackend()
    backend.prepare()
    return backend.capture_text(region)


BACKEND = MacOcrBackend
