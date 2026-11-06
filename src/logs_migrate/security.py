"""Secret handles never stringify their contents. Credentials enter through prompts or streams."""

import getpass
import json
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


class Credentials:
    """Ephemeral only. No environment variable or argv password support."""

    def __init__(self, values: dict[str, str] | None = None, interactive: bool = True):
        self._values = {key: Secret(value) for key, value in (values or {}).items()}
        self.interactive = interactive

    def get(self, key: str) -> Secret:
        if key not in self._values:
            require(self.interactive and sys.stdin.isatty(), Code.AUTH)
            self._values[key] = Secret(getpass.getpass(f"Credential required for {key}: "))
        return self._values[key]

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

    def clear(self):
        # Python cannot guarantee cryptographic erasure of previously allocated strings.
        self._values.clear()
