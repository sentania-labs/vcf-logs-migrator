"""Every engine call runs on one worker thread; the UI thread only reads results."""

from __future__ import annotations

import logging
from threading import Event

from PySide6.QtCore import QObject, QThread, Signal, Slot

from ..adapters.ssh_errors import SSHFailure
from ..errors import Code, MigrationError, QueryTimeout
from ..trust import TrustRequired, shared_ssh_keys
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
    progress = Signal(str)
    _queued = Signal(str, str, dict)

    def __init__(self):
        super().__init__()
        self.session = Session()
        self.cancel_requested = Event()
        self.session.on_progress = self._progress
        self.thread = QThread()
        self.thread.setObjectName("engine")
        self.moveToThread(self.thread)
        self._queued.connect(self._run)
        self.pending: set[str] = set()
        self.affected: dict[str, list[str]] = {}

    def _progress(self, message: str) -> None:
        if self.cancel_requested.is_set():
            raise MigrationError(Code.STATE, "Cancelled. You can try again when ready.")
        self.progress.emit(message)

    def cancel(self) -> None:
        self.cancel_requested.set()

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
        if job.split(":")[0] in {
            "probe",
            "connect",
            "selftest",
            "plan",
            "verify",
            "discover",
            "resume",
        }:
            self.cancel_requested.clear()
        self.pending.add(job)
        self._queued.emit(job, method, dict(kwargs))
        return True

    @Slot(str, str, dict)
    def _run(self, job: str, method: str, kwargs: dict) -> None:
        self.affected.pop(job, None)
        try:
            result = getattr(self.session, method)(**kwargs)
            if self.cancel_requested.is_set() and job.split(":")[0] in {
                "probe",
                "connect",
                "selftest",
                "plan",
                "verify",
                "discover",
                "resume",
            }:
                raise MigrationError(Code.STATE, "Cancelled. You can try again when ready.")
        except TrustRequired as pending:
            shared = dict(shared_ssh_keys(getattr(pending, "existing", []) + pending.identities))
            self.trust_required.emit(
                job,
                [
                    {
                        "kind": i.kind,
                        "name": i.name,
                        "fingerprint": i.fingerprint,
                        "display": i.display,
                        "shared_with": shared.get(i.name),
                    }
                    for i in pending.identities
                ],
            )
        except SSHFailure as error:
            self.failed.emit(job, f"{error.code.value}: {error.message}")
        except QueryTimeout as error:
            self.failed.emit(job, f"gui:{error.message}")
        except MigrationError as error:
            logging.getLogger(__name__).exception("Job %s failed", job)
            self.affected[job] = sorted(getattr(error, "affected_ids", ()))
            self.failed.emit(
                job,
                f"{error.code.value}: {error.message}"
                if hasattr(error, "message")
                else error.code.value,
            )
        except LocalError as error:
            logging.getLogger(__name__).exception("Job %s failed", job)
            self.failed.emit(job, f"gui:{error}")
        except Exception:
            logging.getLogger(__name__).exception("Job %s failed", job)
            self.failed.emit(job, "operation_failed")
        else:
            self.done.emit(job, result)
