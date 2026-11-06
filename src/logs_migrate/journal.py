"""SQLite FULL durability plus a process lock; persist every intent before its side effect."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .errors import Code, MigrationError, require
from .models import State, canonical
from .ports import SystemClock

_NORMAL = {
    State.DRAFT: {State.DISCOVERED},
    State.DISCOVERED: {State.PLANNED},
    State.PLANNED: {State.PREFLIGHT_PASSED},
    State.PREFLIGHT_PASSED: {State.AUTHORIZED},
    State.AUTHORIZED: {State.RUNNING},
    State.RUNNING: {State.VERIFYING, State.STOPPED, State.MONITOR_DETACHED},
    State.MONITOR_DETACHED: {State.RUNNING, State.VERIFYING, State.STOPPED},
    State.VERIFYING: {State.VERIFIED, State.PARTIAL},
    State.PARTIAL: {State.VERIFYING},
    State.STOPPED: {State.VERIFYING},
    State.BLOCKED: {State.PREFLIGHT_PASSED, State.VERIFYING},
    State.RECOVERY_REQUIRED: {State.STOPPED, State.VERIFYING, State.RUNNING},
    State.FAILED: {State.VERIFYING},
    State.VERIFIED: {State.VERIFYING},
}


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
            start INTEGER NOT NULL, end INTEGER NOT NULL, body TEXT NOT NULL);
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

    def transition(self, run: dict, state: State, reason: Code | None = None):
        old = State(run["state"])
        exceptional = {State.BLOCKED, State.PARTIAL, State.FAILED, State.RECOVERY_REQUIRED}
        require(
            state == old or state in _NORMAL.get(old, set()) or state in exceptional, Code.STATE
        )
        run["state"] = state.value
        run["reason"] = reason.value if reason else None
        self.save(run, "state", {"from": old.value, "to": state.value, "reason": run["reason"]})

    def record_transfer(self, run: dict):
        plan = run["plan"]
        self.db.execute(
            "INSERT INTO transfers VALUES (?,?,?,?,?,?)",
            (
                run["id"],
                plan["source"]["id"],
                plan["destination"]["id"],
                plan["window"]["start"],
                plan["window"]["end"],
                canonical(
                    {
                        "destination_nodes": [n["id"] for n in plan["destination"]["nodes"]],
                        "bucket_ids": [b["id"] for b in plan["selected"]],
                        "plan_hash": run["plan_hash"],
                    }
                ),
            ),
        )

    def transfers(self, destination: str) -> list[dict]:
        return [
            {"run": r, "source": s, "start": a, "end": b, **json.loads(body)}
            for r, s, a, b, body in self.db.execute(
                "SELECT run,source,start,end,body FROM transfers WHERE destination=?",
                (destination,),
            )
        ]

    def unresolved(self, destination: str, excluding: str | None = None) -> list[str]:
        pending = []
        for (body,) in self.db.execute("SELECT body FROM runs"):
            run = json.loads(body)
            if run["id"] == excluding:
                continue
            target = (
                run["plan"]["destination"]["id"]
                if run.get("plan")
                else run.get("preparation", {}).get("destination_id")
            )
            if target == destination and (
                run["service_obligations"]
                or run.get("preparation", {}).get("pending", False)
                or run["state"] in {State.RUNNING, State.RECOVERY_REQUIRED}
            ):
                pending.append(run["id"])
        return pending

    def awaiting_verification(self, destination: str) -> list[str]:
        """The current appliance task is needed until its transfer is fully accounted for."""
        return [
            transfer["run"]
            for transfer in self.transfers(destination)
            if self.load(transfer["run"])["verification"].get("complete") is not True
        ]

    def events(self, run_id: str) -> list[dict]:
        return [
            {"sequence": seq, "at": at, "kind": kind, "details": json.loads(body)}
            for seq, at, kind, body in self.db.execute(
                "SELECT sequence,at,kind,body FROM events WHERE run=? ORDER BY sequence", (run_id,)
            )
        ]
