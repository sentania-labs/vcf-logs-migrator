"""SSH diagnostics with a closed vocabulary: never forward server-supplied text."""

from ..errors import Code, MigrationError

_MESSAGES = {
    "tcp": "SSH TCP connection failed or timed out. Check the address, port and network access.",
    "host": "SSH host key is unknown or changed. Verify its fingerprint and re-enroll it.",
    "negotiation": (
        "SSH negotiation failed. Check the client and server key exchange and cipher support."
    ),
    "auth": (
        "SSH authentication was rejected or timed out. Check the username, password "
        "and server password-authentication policy."
    ),
    "handshake": "SSH handshake failed. Check the SSH banner, server logs and protocol support.",
    "command": (
        "SSH remote command failed. Check the account's shell, permissions and appliance tools."
    ),
    "dependency": (
        "SSH support is unavailable. Install the application's SSH dependency (paramiko)."
    ),
}
# These entire strings are library-generated constants. Any other exception text may
# contain credentials, remote banners, paths or command output and must be suppressed.
_SAFE_TEXT = {
    "Authentication failed.",
    "Authentication timeout.",
    "No existing session",
    "EOF during negotiation",
    "Incompatible ssh peer (no acceptable kex algorithm)",
    "Incompatible ssh peer (no acceptable host key)",
    "Incompatible ssh peer (no acceptable ciphers)",
    "Incompatible ssh server (no acceptable ciphers)",
    "Incompatible ssh peer (can't match requested host key type)",
    "Incompatible ssh server (no acceptable macs)",
    "Incompatible ssh server (no acceptable compression)",
}
_SAFE_TYPES = {
    "SSHException",
    "IncompatiblePeer",
    "AuthenticationException",
    "BadAuthenticationType",
    "BadHostKeyException",
    "ChannelException",
    "NoValidConnectionsError",
    "TimeoutError",
    "ConnectionRefusedError",
    "ConnectionResetError",
    "EOFError",
    "FileNotFoundError",
    "ModuleNotFoundError",
    "ImportError",
    "OSError",
}


class SSHFailure(MigrationError):
    def __init__(self, stage: str, error: Exception | None = None, *, status: int | None = None):
        super().__init__(Code.AUTH if stage in {"host", "auth"} else Code.CONNECTION)
        self.message = _MESSAGES[stage]
        if error is not None:
            text = str(error)
            diagnostic = text if text in _SAFE_TEXT else type(error).__name__
            if diagnostic in _SAFE_TEXT or diagnostic in _SAFE_TYPES:
                self.message += f" Detail: {diagnostic.rstrip('.')}."
        if status is not None:
            self.message += f" Exit status: {status}."


def ssh_failure(error: Exception, stage: str) -> SSHFailure:
    if isinstance(error, ImportError):
        return SSHFailure("dependency", error)
    import paramiko

    if isinstance(error, paramiko.BadHostKeyException):
        stage = "host"
    elif isinstance(error, paramiko.AuthenticationException):
        stage = "auth"
    elif isinstance(error, paramiko.ssh_exception.IncompatiblePeer):
        stage = "negotiation"
    elif stage == "connect":
        stage = "tcp" if isinstance(error, OSError) else "handshake"
    return SSHFailure(stage, error)
