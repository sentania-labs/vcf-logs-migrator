"""Secret handles never stringify their contents. Credentials enter through prompts or streams."""

import getpass
import json
import os
import sys

from .errors import Code, MigrationError, require


def harden_process():
    """Best effort dump suppression; release qualification must also cover OS crash reporters."""
    import faulthandler
    import logging

    faulthandler.disable()
    for name in ("paramiko", "urllib3", "http.client"):
        logging.getLogger(name).disabled = True
    if sys.platform.startswith("linux"):
        import ctypes
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        require(ctypes.CDLL(None).prctl(4, 0, 0, 0, 0) == 0, Code.UNSUPPORTED)


class Secret:
    __slots__ = ("_value",)

    def __init__(self, value: str):
        require(isinstance(value, str) and bool(value), Code.AUTH)
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self):
        return "Secret(<redacted>)"

    def __str__(self):
        return "<redacted>"


# Credential keys and the environment variables that may supply them. Never argv.
ENVIRONMENT = {
    "source_ssh": "LOGS_SOURCE_SSH_PASSWORD",
    "source_api": "LOGS_SOURCE_API_PASSWORD",
    "destination_api": "LOGS_DESTINATION_API_PASSWORD",
    "destination_ssh": "LOGS_DESTINATION_SSH_PASSWORD",
    "ops_token": "LOGS_OPS_TOKEN",
}
PROMPTS = {
    "source_ssh": "Source SSH password",
    "source_api": "Source Logs API password",
    "destination_api": "Destination Logs API password",
    "destination_ssh": "Destination SSH password",
    "ops_token": "VCF Operations token",
}


class Credentials:
    """Ephemeral. Values come from the GUI, environment variables, an operator-named file
    (mode 600) or an interactive prompt; never from command arguments."""

    def __init__(self, values: dict[str, str] | None = None, interactive: bool = True):
        self._values = {key: Secret(value) for key, value in (values or {}).items() if value}
        self.interactive = interactive

    def has(self, key: str) -> bool:
        return key in self._values or bool(os.environ.get(ENVIRONMENT.get(key, "")))

    def get(self, key: str) -> Secret:
        if key not in self._values:
            variable = ENVIRONMENT.get(key)
            if variable and os.environ.get(variable):
                self._values[key] = Secret(os.environ[variable])
                return self._values[key]
            require(self.interactive and sys.stdin.isatty(), Code.AUTH)
            self._values[key] = Secret(getpass.getpass(PROMPTS.get(key, key) + ": "))
        return self._values[key]

    @classmethod
    def from_file(cls, path, interactive: bool = False):
        """JSON object of credential key to value; the file must be private (mode 600)."""
        from pathlib import Path

        target = Path(path)
        try:
            require(target.is_file() and not target.is_symlink(), Code.AUTH)
            if os.name == "posix":
                require(target.stat().st_mode & 0o077 == 0, Code.AUTH)
            with open(target, encoding="utf-8") as handle:
                credentials = cls.from_stream(handle)
        except MigrationError:
            raise
        except Exception:
            raise MigrationError(Code.AUTH) from None
        credentials.interactive = interactive
        require(set(credentials._values) <= set(ENVIRONMENT), Code.AUTH)
        return credentials

    def destination_nodes(self, nodes: tuple[str, ...], same_password: bool):
        """UI/CLI asks 'same password on all N nodes?' before calling this method."""
        shared = self.get("destination_ssh") if same_password else None
        return {node: shared or self.get("destination_ssh:" + node) for node in nodes}

    @classmethod
    def from_stream(cls, stream):
        try:
            data = json.load(stream)
            require(
                isinstance(data, dict)
                and all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()),
                Code.AUTH,
            )
            return cls(data, interactive=False)
        except Exception:
            raise MigrationError(Code.AUTH) from None

    def snapshot(self) -> dict[str, str]:
        """Revealed copy for re-opening the same session; never persisted or logged."""
        return {key: secret.reveal() for key, secret in self._values.items()}

    def clear(self):
        # Python cannot guarantee cryptographic erasure of previously allocated strings.
        self._values.clear()
