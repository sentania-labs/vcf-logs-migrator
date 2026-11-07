"""Desktop GUI: a presentation layer over the engine. Importing this package needs no Qt."""

from __future__ import annotations


def run(fixture: str | None = None, journal: str | None = None) -> int:
    """Launch the window; explain the optional dependency when PySide6 is absent."""
    try:
        from .app import run as _run
    except ImportError as error:  # pragma: no cover - depends on the environment
        if "PySide6" not in str(error) and "shiboken" not in str(error):
            raise
        print(
            "The GUI needs the optional PySide6 dependency: "
            "python -m pip install 'vcf-logs-migrator[gui]'",
            file=__import__("sys").stderr,
        )
        return 3
    return _run(fixture, journal)
