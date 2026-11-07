"""SQLite FULL durability plus a process lock; persist every intent before its side effect."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .errors import Code, MigrationError, require
from .models import canonical
from .ports import SystemClock


@contextmanager
def file_lock(lock_path: Path):
    require(not lock_path.is_symlink(), Code.STORAGE)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    handle = os.fdopen(fd, "r+b")
    locked = False
    try:
        try:
            if os.name == "nt":
                import msvcrt

                handle.write(b"0")
                handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError:
            raise MigrationError(Code.BUSY) from None
        yield
    finally:
        if locked:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


class Journal:
    def __init__(self, path: Path, clock=None):
        self.path = Path(path)
        self.clock = clock or SystemClock()
        require(not self.path.is_symlink(), Code.STORAGE)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        if os.name == "posix":
            require(self.path.stat().st_mode & 0o077 == 0, Code.STORAGE)
        self.db = sqlite3.connect(self.path, timeout=0, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS metadata (version INTEGER NOT NULL);
          INSERT INTO metadata SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM metadata);
          CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, body TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT, run TEXT NOT NULL,
            at TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS transfers (
            run TEXT PRIMARY KEY, source TEXT NOT NULL, destination TEXT NOT NULL,
            body TEXT NOT NULL);
        """)
        require(self.db.execute("SELECT version FROM metadata").fetchall() == [(1,)], Code.STORAGE)

    def exclusive(self):
        return file_lock(self.path.with_suffix(self.path.suffix + ".lock"))

    def close(self):
        self.db.close()

    def load(self, run_id: str) -> dict:
        row = self.db.execute("SELECT body FROM runs WHERE id=?", (run_id,)).fetchone()
        require(row is not None, Code.INVALID_INPUT)
        return json.loads(row[0])

    def runs(self) -> list[dict]:
        """Every recorded run in creation order, for choosing one after a restart."""
        return [
            json.loads(body) for (body,) in self.db.execute("SELECT body FROM runs ORDER BY rowid")
        ]

    def save(self, run: dict, kind: str, details: dict | None = None):
        """Run and its event are committed together before returning to the caller."""
        run["updated_at"] = self.clock.now()
        try:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                "INSERT INTO runs VALUES (?,?) ON CONFLICT(id) DO UPDATE SET body=?",
                (run["id"], canonical(run), canonical(run)),
            )
            self.db.execute(
                "INSERT INTO events(run,at,kind,body) VALUES (?,?,?,?)",
                (run["id"], run["updated_at"], kind, canonical(details or {})),
            )
            self.db.execute("COMMIT")
        except sqlite3.Error:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise MigrationError(Code.STORAGE) from None

    def record_transfer(self, run: dict):
        """Ledger row written before the POST: the selection is in flight until settled."""
        plan = run["plan"]
        keys = [b["node"] + "/" + b["id"] for b in plan["selected"]]
        self.db.execute(
            "INSERT INTO transfers VALUES (?,?,?,?)",
            (
                run["id"],
                plan["source"]["id"],
                plan["destination"]["id"],
                canonical(
                    {"keys": keys, "closed": False, "received": [], "window": plan["window"]}
                ),
            ),
        )

    def note_transfer_task(self, run_id: str, task_id: str):
        """Remember the API task id the start response carried, for matching later polls."""
        try:
            body = {**self._transfer_body(run_id), "task_id": task_id}
            self.db.execute("UPDATE transfers SET body=? WHERE run=?", (canonical(body), run_id))
        except sqlite3.Error:
            raise MigrationError(Code.STORAGE) from None

    def settle_transfer(self, run_id: str, received: list[str]):
        """Replace the in-flight selection with the bucket keys the API reported as transferred."""
        try:
            self.db.execute(
                "UPDATE transfers SET body=? WHERE run=?",
                (
                    canonical(
                        {
                            **self._transfer_body(run_id),
                            "closed": True,
                            "received": sorted(received),
                        }
                    ),
                    run_id,
                ),
            )
        except sqlite3.Error:
            raise MigrationError(Code.STORAGE) from None

    def release_transfer(self, run_id: str):
        """Drop an open row for a start the appliance never accepted."""
        try:
            self.db.execute(
                "DELETE FROM transfers WHERE run=? AND json_extract(body, '$.closed')=0",
                (run_id,),
            )
        except sqlite3.Error:
            raise MigrationError(Code.STORAGE) from None

    def _transfer_body(self, run_id: str) -> dict:
        row = self.db.execute("SELECT body FROM transfers WHERE run=?", (run_id,)).fetchone()
        require(row is not None, Code.INVALID_INPUT)
        return json.loads(row[0])

    def transfers(self, destination: str) -> list[dict]:
        return [
            {"run": r, "source": s, **json.loads(body)}
            for r, s, body in self.db.execute(
                "SELECT run,source,body FROM transfers WHERE destination=? ORDER BY rowid",
                (destination,),
            )
        ]

    def events(self, run_id: str) -> list[dict]:
        return [
            {"sequence": seq, "at": at, "kind": kind, "details": json.loads(body)}
            for seq, at, kind, body in self.db.execute(
                "SELECT sequence,at,kind,body FROM events WHERE run=? ORDER BY sequence", (run_id,)
            )
        ]
