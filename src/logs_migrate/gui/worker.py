"""Every engine call runs on one worker thread; the UI thread only reads results."""

from __future__ import annotations

from PySide6.QtCore import QObject, QThread, Signal, Slot

from ..adapters.ssh_errors import SSHFailure
from ..errors import MigrationError
from ..trust import TrustRequired
from .session import LocalError, Session


class EngineWorker(QObject):
    """Serializes Session calls on a private QThread.

    `submit(job, method, **kwargs)` queues one call; `done(job, result)` or
    `failed(job, code)` fires on the UI thread. The session and its journal live on the
    worker thread, which keeps SQLite's same-thread rule intact.
    """

    done = Signal(str, object)
    failed = Signal(str, str)
    trust_required = Signal(str, list)
    _queued = Signal(str, str, dict)

    def __init__(self):
        super().__init__()
        self.session = Session()
        self.thread = QThread()
        self.thread.setObjectName("engine")
        self.moveToThread(self.thread)
        self._queued.connect(self._run)
        self.pending: set[str] = set()

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self._queued.emit("disconnect", "disconnect", {})
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

    @Slot(str, str, dict)
    def _run(self, job: str, method: str, kwargs: dict) -> None:
        try:
            result = getattr(self.session, method)(**kwargs)
        except TrustRequired as pending:
            self.trust_required.emit(
                job,
                [
                    {
                        "kind": i.kind,
                        "name": i.name,
                        "fingerprint": i.fingerprint,
                        "display": i.display,
                    }
                    for i in pending.identities
                ],
            )
        except SSHFailure as error:
            self.failed.emit(job, f"{error.code.value}: {error.message}")
        except MigrationError as error:
            self.failed.emit(job, error.code.value)
        except LocalError as error:
            self.failed.emit(job, f"gui:{error}")
        except Exception:
            self.failed.emit(job, "operation_failed")
        else:
            self.done.emit(job, result)
