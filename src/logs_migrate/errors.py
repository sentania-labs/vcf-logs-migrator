"""Only static reason codes cross the adapter boundary; never remote error text."""

from enum import StrEnum
from functools import wraps


class Code(StrEnum):
    INVALID_INPUT = "invalid_input"
    UNSUPPORTED = "unsupported_capability"
    INVENTORY_CHANGED = "inventory_changed"
    CAPACITY = "insufficient_capacity"
    RETENTION = "destination_retention_would_delete_history"
    OVERLAP = "overlapping_transfer"
    TOPOLOGY = "topology_changed"
    COLLISION = "bucket_identity_conflict"
    PARTITIONS = "partition_preparation_required"
    FIELDS = "extracted_fields_required"
    ACK = "acknowledgment_required"
    AUTH = "authentication_failed"
    CONNECTION = "connection_failed"
    SCHEMA = "unrecognized_response_schema"
    INCOMPLETE = "incomplete_evidence"
    UNCERTAIN = "remote_outcome_uncertain"
    BUSY = "destination_busy"
    SERVICE = "service_state_unconfirmed"
    RECOVERY = "service_recovery_required"
    INTEGRITY = "integrity_mismatch"
    STATE = "invalid_state_transition"
    STORAGE = "journal_unavailable"


class MigrationError(Exception):
    def __init__(self, code: Code):
        self.code = code
        super().__init__(code.value)


def require(condition: bool, code: Code) -> None:
    if not condition:
        raise MigrationError(code)


def safe_boundary(function):
    """Public adapters/services never propagate remote or library exception strings."""

    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except MigrationError:
            raise
        except Exception:
            raise MigrationError(Code.CONNECTION) from None

    return wrapped
