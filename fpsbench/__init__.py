"""fpsbench - external black-box "walk the map" benchmark tool for single-player games.

The package is split into two halves:

* ``fpsbench.core``     - platform independent pure logic (testable anywhere)
* ``fpsbench.backends`` - per platform backends behind four small interfaces

Nothing in ``fpsbench.core`` imports a platform backend module; backends are
loaded lazily through :func:`fpsbench.backends.base.load_backend`.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
