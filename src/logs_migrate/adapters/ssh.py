"""Read-only source SSH boundary with strict enrolled host identities.

No shell password, subprocess, source service controller, or arbitrary command method.
Destination mutation remains release-gated pending remote-supervisor qualification.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum

from ..errors import Code, MigrationError, require
from ..security import Secret


class ReadOperation(StrEnum):
    INVENTORY = "inventory"
    SERVICE = "service"
    SPACE = "space"


_READS = {
    ReadOperation.INVENTORY: "/usr/lib/loginsight/application/sbin/bucket-index show",
    ReadOperation.SERVICE: "systemctl is-active loginsight",
    ReadOperation.SPACE: "df -B1 /storage/core",
}


class SourceSSH:
    def __init__(
        self,
        host: str,
        username: str,
        password: Secret,
        host_key_sha256: str,
        known_hosts: str,
        *,
        port: int = 22,
        timeout: float = 30,
    ):
        self.host = host
        self.username = username
        self.password = password
        self.host_key_sha256 = host_key_sha256
        self.known_hosts = known_hosts
        self.port = port
        self.timeout = timeout

    def read(self, operation: ReadOperation) -> str:
        require(operation in _READS, Code.INVALID_INPUT)
        client = None
        try:
            import paramiko

            client = paramiko.SSHClient()
            client.load_host_keys(self.known_hosts)
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
            # Paramiko verifies known_hosts before attempting password auth. The explicit
            # enrolled digest is checked against that same key before opening the connection.
            name = self.host if self.port == 22 else f"[{self.host}]:{self.port}"
            enrolled = client.get_host_keys().lookup(name)
            require(enrolled is not None and len(enrolled) == 1, Code.AUTH)
            key = next(iter(enrolled.values()))
            require(hashlib.sha256(key.asbytes()).hexdigest() == self.host_key_sha256, Code.AUTH)
            client.connect(
                self.host,
                port=self.port,
                username=self.username,
                password=self.password.reveal(),
                look_for_keys=False,
                allow_agent=False,
                timeout=self.timeout,
                auth_timeout=self.timeout,
                banner_timeout=self.timeout,
            )
            _, stdout, stderr = client.exec_command(_READS[operation], timeout=self.timeout)
            # Never print stderr; remote error text is untrusted and may contain credentials.
            output = stdout.read(16 * 1024 * 1024 + 1)
            require(len(output) <= 16 * 1024 * 1024, Code.SCHEMA)
            require(stdout.channel.recv_exit_status() == 0, Code.CONNECTION)
            return output.decode("utf-8", errors="strict")
        except MigrationError:
            raise
        except Exception:
            raise MigrationError(Code.CONNECTION) from None
        finally:
            if client is not None:
                client.close()


class RecordedSSH:
    def __init__(self, replies: list[tuple[ReadOperation, str]]):
        self.replies = list(replies)
        self.commands: list[str] = []

    def read(self, operation: ReadOperation) -> str:
        require(operation in _READS and bool(self.replies), Code.SCHEMA)
        expected, response = self.replies.pop(0)
        require(operation == expected, Code.SCHEMA)
        self.commands.append(_READS[operation])
        return response
