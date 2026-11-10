"""Access the notices embedded in standalone builds."""

from pathlib import Path


def text() -> str:
    path = Path(__file__).with_name("THIRD-PARTY-NOTICES.txt")
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return "Third-party notices are generated when building a standalone download.\n"
