"""Full-screen rubber-band region selection using AppKit.

A translucent borderless window is floated above everything (including a running
game) and the user drags a box around the FPS counter.  The result is converted
from Cocoa's bottom-left view coordinates to the top-left display-point space
that the capture backends use.

If this cannot run (no GUI session, or the window never gets focus) the CLI
offers ``--manual`` and ``--region`` so the tool is never blocked on a dialog.
"""

from __future__ import annotations

from typing import Optional

from ...core.route import Region
from ..base import BackendUnavailable

__all__ = ["select_region_appkit", "quartz_point"]


def _import_appkit():
    try:
        import AppKit
        import objc

        return AppKit, objc
    except ImportError as exc:  # pragma: no cover
        raise BackendUnavailable(
            "pyobjc (AppKit) is required for interactive region selection: %s" % exc,
            hint='python3 -m pip install -e ".[macos]"',
        ) from exc


def quartz_point(box, screen_frame, primary_height: float) -> tuple:
    """Convert a Cocoa selection box into Quartz top-left display points.

    ``box`` is in the selected screen's own window coordinates (origin bottom-left
    of that screen), ``screen_frame`` is that screen's frame in Cocoa *global*
    coordinates - whose origin is the bottom-left of the **primary** screen - and
    ``primary_height`` is the primary screen's height.  Quartz measures y downwards
    from the top edge of the primary screen, so the mirror must use the primary
    height and subtract the screen's global origin; using the selected screen's own
    height (and adding its origin) is only correct on a single display.
    """
    width = float(box.size.width)
    height = float(box.size.height)
    x = float(screen_frame.origin.x) + float(box.origin.x)
    y = primary_height - (float(screen_frame.origin.y) + float(box.origin.y) + height)
    return x, y, width, height


def select_region_appkit(message: str = "Drag a box around the FPS counter") -> Optional[Region]:
    """Return the selected :class:`Region` or ``None`` if the user cancelled."""
    AppKit, objc = _import_appkit()

    screen = AppKit.NSScreen.mainScreen()
    if screen is None:
        raise BackendUnavailable("no screen available for interactive selection")
    screen_frame = screen.frame()
    screens = list(AppKit.NSScreen.screens() or [])
    primary_height = float((screens[0] if screens else screen).frame().size.height)

    state = {"start": None, "end": None, "done": False, "cancelled": False}

    class SelectionView(AppKit.NSView):
        def isFlipped(self):
            # Cocoa's default (bottom-left origin); the result is converted to
            # top-left display points before it leaves this module.
            return False

        def acceptsFirstResponder(self):
            return True

        def drawRect_(self, rect):
            AppKit.NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.25).set()
            AppKit.NSBezierPath.fillRect_(self.bounds())
            AppKit.NSColor.systemRedColor().set()
            path = AppKit.NSBezierPath.bezierPath()
            dictionary = {
                AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_(18.0),
                AppKit.NSForegroundColorAttributeName: AppKit.NSColor.whiteColor(),
            }
            AppKit.NSString.stringWithString_(message).drawAtPoint_withAttributes_(
                AppKit.NSPoint(24.0, self.bounds().size.height - 40.0), dictionary
            )
            path.setLineWidth_(2.0)
            if state["start"] and state["end"]:
                box = _box(state["start"], state["end"])
                path.appendBezierPathWithRect_(box)
                path.stroke()

        def mouseDown_(self, event):
            state["start"] = self.convertPoint_fromView_(event.locationInWindow(), None)
            state["end"] = state["start"]
            self.setNeedsDisplay_(True)

        def mouseDragged_(self, event):
            state["end"] = self.convertPoint_fromView_(event.locationInWindow(), None)
            self.setNeedsDisplay_(True)

        def mouseUp_(self, event):
            state["end"] = self.convertPoint_fromView_(event.locationInWindow(), None)
            state["done"] = True
            AppKit.NSApplication.sharedApplication().stop_(None)

        def keyDown_(self, event):
            if event.keyCode() == 53:  # Escape cancels
                state["cancelled"] = True
                state["done"] = True
                AppKit.NSApplication.sharedApplication().stop_(None)

    def _box(start, end):
        x = min(start.x, end.x)
        y = min(start.y, end.y)
        width = abs(start.x - end.x)
        height = abs(start.y - end.y)
        return AppKit.NSMakeRect(x, y, width, height)

    application = AppKit.NSApplication.sharedApplication()
    application.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        screen_frame,
        AppKit.NSWindowStyleMaskBorderless,
        AppKit.NSBackingStoreBuffered,
        False,
    )
    window.setLevel_(AppKit.NSScreenSaverWindowLevel)
    window.setOpaque_(False)
    window.setBackgroundColor_(AppKit.NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.01))
    window.setIgnoresMouseEvents_(False)
    view = SelectionView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, screen_frame.size.width, screen_frame.size.height))
    window.setContentView_(view)
    window.makeKeyAndOrderFront_(None)
    window.makeFirstResponder_(view)
    application.activateIgnoringOtherApps_(True)
    application.run()

    if state["cancelled"] or not state["done"] or state["start"] is None or state["end"] is None:
        return None
    box = _box(state["start"], state["end"])
    if float(box.size.width) < 4 or float(box.size.height) < 4:
        return None
    x, y, width, height = quartz_point(box, screen_frame, primary_height)
    return Region(x=round(x, 1), y=round(y, 1), w=round(width, 1), h=round(height, 1), space="points")
