"""Opt-in, bounded diagnostic output. Every destination uses the same redaction filter."""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
import sys
from datetime import datetime
from pathlib import Path

_secrets: set[str] = set()
_handlers: list[logging.Handler] = []
_log_path: Path | None = None
_SENSITIVE = re.compile(
    r"""(?i)(["']?(?:password|passwd|token|session[_-]?id|authorization|cookie|private[_-]?key|x-jwt-token)["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;}]+)"""
)
_PEM = re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", re.S)


def register_secret(value: str) -> None:
    if value:
        _secrets.add(value)


def redact(text: str) -> str:
    for value in sorted(_secrets, key=len, reverse=True):
        text = text.replace(value, "<redacted>")
    text = re.sub(
        r"(?im)\b(authorization|cookie|set-cookie|x-jwt-token)\s*:\s*[^\r\n]*",
        r"\1: <redacted>",
        text,
    )
    text = _PEM.sub("<redacted private key>", text)
    return _SENSITIVE.sub(r"\1<redacted>", text)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        formatter = logging.Formatter()
        record.msg = redact(formatter.format(record))
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


class PrivateRotatingFileHandler(logging.handlers.RotatingFileHandler):
    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        return os.fdopen(fd, "a", encoding=self.encoding, errors=self.errors)


def configure(
    path: Path | None = None, *, verbose: bool = False, enabled: bool = True
) -> Path | None:
    global _log_path
    loggers = [logging.getLogger("logs_migrate"), logging.getLogger("paramiko")]
    for handler in _handlers:
        for logger in loggers:
            logger.removeHandler(handler)
        handler.close()
    _handlers.clear()
    _log_path = None
    if not enabled:
        logging.getLogger("logs_migrate").setLevel(logging.WARNING)
        logging.getLogger("paramiko").setLevel(logging.CRITICAL + 1)
        logging.getLogger("paramiko").disabled = True
        return None
    if path is None:
        from .gui.settings import config_dir

        path = config_dir() / "logs" / f"debug-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.log"
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create private output before the handler opens it; never relax existing permissions.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.close(fd)
    handler = PrivateRotatingFileHandler(
        path, maxBytes=5 * 1024 * 1024, backupCount=2, encoding="utf-8"
    )
    _handlers.append(handler)
    if verbose:
        _handlers.append(logging.StreamHandler(sys.stderr))
    for item in _handlers:
        item.setLevel(logging.DEBUG)
        item.addFilter(RedactingFilter())
        item.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        for logger in loggers:
            logger.addHandler(item)
    for logger in loggers:
        logger.setLevel(logging.DEBUG)
    logging.getLogger("paramiko").propagate = False
    logging.getLogger("paramiko").disabled = False
    _log_path = path
    logging.getLogger(__name__).debug("Detailed logging enabled")
    return path


def log_path() -> Path | None:
    return _log_path
