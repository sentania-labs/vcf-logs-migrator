"""Every engine call runs on one worker thread; the UI thread only reads results."""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime

from PySide6.QtCore import QObject, QThread, Signal, Slot

from ..errors import MigrationError, QueryTimeout
from .session import Failure, Session


class EngineWorker(QObject):
    """Serializes Session calls on a private QThread.

    `submit(job, method, **kwargs)` queues one call; `done(job, result)` or
    `failed(job, code, data)` fires on the UI thread. The session and its record live on
    the worker thread, which keeps SQLite's same-thread rule intact.
    """

    done = Signal(str, object)
    failed = Signal(str, str, dict)
    progress = Signal(str, str)
    _queued = Signal(str, str, dict)

    def __init__(self):
        super().__init__()
        self.session = Session()
        self.session.on_progress = self._progress
        self._cancel = threading.Event()
        self.session.cancelled = self._cancel.is_set
        self.thread = QThread()
        self.thread.setObjectName("engine")
        self.moveToThread(self.thread)
        self._queued.connect(self._run)
        self.pending: set[str] = set()
        self._job = ""

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self._queued.emit("close", "close", {})
        self.thread.quit()
        if not self.thread.wait(5000):
            self.thread.terminate()
            self.thread.wait(2000)

    def submit(self, job: str, method: str, **kwargs) -> bool:
        """Queue a call unless the same job is already in flight."""
        if job in self.pending:
            return False
        self.pending.add(job)
        self._queued.emit(job, method, dict(kwargs))
        return True

    def request_cancel(self) -> None:
        """Ask the job in flight to stop before it asks either system to change anything."""
        self._cancel.set()

    def _progress(self, message: str) -> None:
        self.progress.emit(self._job, message)

    @Slot(str, str, dict)
    def _run(self, job: str, method: str, kwargs: dict) -> None:
        self._job = job
        self._cancel.clear()
        try:
            result = getattr(self.session, method)(**kwargs)
        except Failure as failure:
            self.failed.emit(job, failure.code, failure.data)
        except QueryTimeout as error:
            self.failed.emit(job, f"gui:{error.message}", {})
        except MigrationError as error:
            logging.getLogger(__name__).exception("Job %s failed", job)
            message = getattr(error, "message", None)
            text = f"{error.code.value}: {message}" if message else error.code.value
            self.session.errors.append(f"{datetime.now(UTC).isoformat()} {job}: {text}")
            self.failed.emit(job, text, {})
        except Exception as error:
            logging.getLogger(__name__).exception("Job %s failed", job)
            self.session.errors.append(
                f"{datetime.now(UTC).isoformat()} {job}: {type(error).__name__}"
            )
            self.failed.emit(job, "operation_failed", {})
        else:
            self.done.emit(job, result)
