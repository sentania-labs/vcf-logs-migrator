"""Opt-in, bounded diagnostic output. Every destination uses the same redaction filter."""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
import sys
import weakref
from datetime import datetime
from pathlib import Path

_secrets: weakref.WeakSet = weakref.WeakSet()
_handlers: list[logging.Handler] = []
_log_path: Path | None = None
_SENSITIVE = re.compile(
    r"""(?i)(["']?(?:password|passwd|token|session[_-]?id|authorization|cookie|private[_-]?key|x-jwt-token)["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;}]+)"""
)
_PEM = re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", re.S)


def register_secret(secret) -> None:
    # Weak references let logging be enabled in an already-connected GUI session
    # without retaining credentials after their owners release them.
    _secrets.add(secret)


def unregister_secret(secret) -> None:
    _secrets.discard(secret)


def redact(text: str) -> str:
    values = [secret.reveal() for secret in list(_secrets)]
    for value in sorted(values, key=len, reverse=True):
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
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        if os.name == "nt" and Path(self.baseFilename).is_symlink():
            raise OSError("Refusing a symbolic link as a diagnostic log")
        fd = os.open(self.baseFilename, flags, 0o600)
        try:
            if os.name == "posix":
                if os.fstat(fd).st_uid != os.geteuid():
                    raise PermissionError("Diagnostic log must be owned by the current user")
                os.fchmod(fd, 0o600)
            return os.fdopen(fd, "a", encoding=self.encoding, errors=self.errors)
        except BaseException:
            os.close(fd)
            raise


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
