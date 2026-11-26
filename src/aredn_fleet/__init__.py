"""AREDN Fleet Manager: safe, bulk updating of AREDN nodes."""

__version__ = "0.1.0a1"

# The exact build, e.g. "0.1.0-dev.aa17f60" (stamped by CI into _build.py; source runs
# show the package version).
try:
    from ._build import BUILD
except ImportError:  # pragma: no cover - not a CI build
    BUILD = __version__
