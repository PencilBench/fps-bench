"""Platform backends: narrow interfaces plus a loader that picks the right
implementation for the running OS.

These interfaces are the only place where platform knowledge is allowed::

    InputBackend            injects keyboard/mouse input during playback
    RecorderBackend         records the user's own play-through into trace events
    GamepadRecorderBackend  polls a real controller and yields state changes
    GamepadBackend          publishes full controller state to a virtual device
    FpsBackend              produces FpsSample streams (frame intervals / ocr readings)
    OcrBackend              grabs a screen region and reads the FPS counter in it

``fpsbench.core`` never imports from here; the CLI composes the two halves.
"""
