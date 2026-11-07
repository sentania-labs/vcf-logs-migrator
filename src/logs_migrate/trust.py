"""Appliance identities: TLS certificate digests and SSH host keys, trusted on first use.

Nothing is trusted silently. An unknown identity is returned to the caller, who shows the
fingerprint and asks the operator (CLI prompt or `--trust-new`, GUI dialog). A changed
identity is refused until the operator removes the stale entry from the trust store.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .errors import Code, MigrationError, require

APP_DIR = "vcf-logs-migrator"


def config_dir() -> Path:
    if os.environ.get("LOGS_MIGRATE_CONFIG_DIR"):
        return Path(os.environ["LOGS_MIGRATE_CONFIG_DIR"])
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif os.environ.get("XDG_CONFIG_HOME"):
        base = Path(os.environ["XDG_CONFIG_HOME"])
    else:
        base = Path.home() / ".config"
    return base / APP_DIR


@dataclass(frozen=True)
class Identity:
    kind: str  # "tls" or "ssh"
    name: str  # host:port
    fingerprint: str  # SHA-256 hex digest
    detail: str = ""  # Key type for SSH

    @property
    def display(self) -> str:
        pairs = ":".join(self.fingerprint[i : i + 2] for i in range(0, 64, 2))
        label = f"{self.kind.upper()} {self.name}"
        if self.detail:
            label += f" ({self.detail})"
        return f"{label} SHA256 {pairs}"


class TrustRequired(MigrationError):
    """Identities the operator has not enrolled yet. Nothing was sent to them."""

    def __init__(self, identities: list[Identity]):
        super().__init__(Code.AUTH)
        self.identities = identities


class TrustStore:
    """`trusted-identities.json` (mode 600) plus a Paramiko known_hosts file beside it."""

    def __init__(self, directory: Path | None = None):
        self.directory = directory or config_dir()
        self.path = self.directory / "trusted-identities.json"
        self.known_hosts = self.directory / "known_hosts"
        self._data = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            return {"tls": {}, "ssh": {}}
        require(not self.path.is_symlink(), Code.STORAGE)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            require(isinstance(data, dict), Code.STORAGE)
            return {"tls": dict(data.get("tls", {})), "ssh": dict(data.get("ssh", {}))}
        except MigrationError:
            raise
        except Exception:
            raise MigrationError(Code.STORAGE) from None

    def _save(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp = self.path.with_suffix(".tmp")
        fd = os.open(
            temp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(self._data, handle, indent=1, sort_keys=True)
        os.replace(temp, self.path)
        if not self.known_hosts.exists():
            fd = os.open(self.known_hosts, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)

    def known(self, identity: Identity) -> str | None:
        return self._data[identity.kind].get(identity.name)

    def check(self, identity: Identity) -> str:
        """`trusted`, `unknown` or `changed`."""
        enrolled = self.known(identity)
        if enrolled is None:
            return "unknown"
        return "trusted" if enrolled == identity.fingerprint else "changed"

    def trust(self, identity: Identity, key_line: str | None = None) -> None:
        require(self.check(identity) != "changed", Code.AUTH)
        self._data[identity.kind][identity.name] = identity.fingerprint
        self._save()
        if identity.kind == "ssh" and key_line:
            with open(self.known_hosts, "a", encoding="utf-8") as handle:
                handle.write(key_line.rstrip("\n") + "\n")

    def forget(self, kind: str, name: str) -> bool:
        if name not in self._data.get(kind, {}):
            return False
        del self._data[kind][name]
        if kind == "ssh" and self.known_hosts.exists():
            host, _, port = name.rpartition(":")
            entry = host if port == "22" else f"[{host}]:{port}"
            lines = self.known_hosts.read_text(encoding="utf-8").splitlines()
            kept = [line for line in lines if not line.startswith(entry + " ")]
            self.known_hosts.write_text("".join(line + "\n" for line in kept), encoding="utf-8")
        self._save()
        return True

    def entries(self) -> list[tuple[str, str, str]]:
        return sorted(
            (kind, name, digest)
            for kind, names in self._data.items()
            for name, digest in names.items()
        )


def probe_tls(host: str, port: int) -> Identity:
    from .adapters.rest import peer_certificate_sha256

    return Identity("tls", f"{host}:{port}", peer_certificate_sha256(host, port))


def probe_ssh(host: str, port: int = 22, timeout: float = 15) -> tuple[Identity, str]:
    """Identity plus the known_hosts line to enroll once the operator accepts it."""
    import base64
    import hashlib

    try:
        import paramiko

        transport = paramiko.Transport((host, port))
        try:
            transport.banner_timeout = timeout
            transport.start_client(timeout=timeout)
            key = transport.get_remote_server_key()
        finally:
            transport.close()
    except Exception:
        raise MigrationError(Code.CONNECTION) from None
    key_type = key.get_name()
    digest = hashlib.sha256(key.asbytes()).hexdigest()
    entry = host if port == 22 else f"[{host}]:{port}"
    line = f"{entry} {key_type} {base64.b64encode(key.asbytes()).decode()}"
    return Identity("ssh", f"{host}:{port}", digest, key_type), line
