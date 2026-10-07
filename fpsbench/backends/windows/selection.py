"""Interactive FPS region selection on Windows using tkinter.

A full-screen translucent ``Toplevel`` is floated above everything (``-topmost``)
while the user drags a box.  ``SetProcessDPIAware`` runs first: without it tkinter
reports virtualised coordinates while the GDI capture works in physical pixels, so
the selected region would be wrong on a scaled display.

Falls back to the CLI's ``--manual`` / ``--region`` options when there is no
usable display session.
"""

from __future__ import annotations

from typing import Optional

from ...core.route import Region
from ..base import BackendUnavailable

__all__ = ["select_region_tkinter"]


def select_region_tkinter(message: str = "Drag a box around the FPS counter") -> Optional[Region]:
    import ctypes

    try:  # must happen before tkinter starts, otherwise coordinates are virtualised
        from .._win_types import install_prototypes

        user32 = ctypes.windll.user32
        install_prototypes(user32)
        user32.SetProcessDPIAware()
    except Exception:
        pass
    try:
        import tkinter
    except ImportError as exc:  # pragma: no cover
        raise BackendUnavailable("tkinter is required for interactive selection: %s" % exc) from exc

    try:
        root = tkinter.Tk()
    except Exception as exc:  # pragma: no cover
        raise BackendUnavailable("no display available for interactive selection: %s" % exc) from exc

    state = {"start": None, "end": None, "canvas": None, "rect": None}
    root.attributes("-fullscreen", True)
    root.attributes("-topmost", True)
    try:
        # Dimmed but still readable: the alpha applies to the drawn rubber band too.
        root.attributes("-alpha", 0.25)
    except Exception:
        pass
    root.configure(cursor="crosshair")
    root.title(message)
    canvas = tkinter.Canvas(root, cursor="crosshair", highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    state["canvas"] = canvas
    canvas.create_text(20, 20, anchor="nw", text=message + "  (Esc cancels)", fill="red", font=("Helvetica", 18))

    def on_press(event):
        state["start"] = (event.x_root, event.y_root)
        state["end"] = state["start"]

    def on_drag(event):
        state["end"] = (event.x_root, event.y_root)
        if state["rect"] is not None:
            canvas.delete(state["rect"])
        start = state["start"]
        state["rect"] = canvas.create_rectangle(
            start[0], start[1], event.x_root, event.y_root, outline="red", width=2
        )

    def on_release(event):
        state["end"] = (event.x_root, event.y_root)
        root.quit()

    def on_escape(_event):
        state["start"] = None
        state["end"] = None
        root.quit()

    canvas.bind("<ButtonPress-1>", on_press)
    canvas.bind("<B1-Motion>", on_drag)
    canvas.bind("<ButtonRelease-1>", on_release)
    root.bind("<Escape>", on_escape)
    root.focus_force()
    root.mainloop()
    try:
        root.destroy()
    except Exception:
        pass

    start, end = state["start"], state["end"]
    if start is None or end is None:
        return None
    x = min(start[0], end[0])
    y = min(start[1], end[1])
    width = abs(end[0] - start[0])
    height = abs(end[1] - start[1])
    if width < 4 or height < 4:
        return None
    return Region(x=float(x), y=float(y), w=float(width), h=float(height), space="pixels")
