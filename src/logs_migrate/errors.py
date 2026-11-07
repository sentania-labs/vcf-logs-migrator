"""Only static reason codes cross the adapter boundary; never remote error text."""

from enum import StrEnum
from functools import wraps


class Code(StrEnum):
    INVALID_INPUT = "invalid_input"
    UNSUPPORTED = "unsupported_capability"
    INVENTORY_CHANGED = "inventory_changed"
    CAPACITY = "insufficient_capacity"
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
    VERIFICATION_PENDING = "verification_pending"
    UNCERTAIN = "remote_outcome_uncertain"
    BUSY = "destination_busy"
    SERVICE = "service_state_unconfirmed"
    RECOVERY = "service_recovery_required"
    INTEGRITY = "integrity_mismatch"
    STATE = "invalid_state_transition"
    STORAGE = "journal_unavailable"
    NOT_RECEIVED = "selected_buckets_not_received"


class MigrationError(Exception):
    def __init__(self, code: Code):
        self.code = code
        super().__init__(code.value)


class Rejection(MigrationError):
    """The appliance answered with an HTTP error status: the request was received and refused."""


class Accepted(MigrationError):
    """The appliance answered 2xx but the body could not be read: the request took effect."""


def require(condition: bool, code: Code) -> None:
    if not condition:
        raise MigrationError(code)


def safe_boundary(function):
    """Public adapters/services never propagate remote or library exception strings."""

    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except MigrationError as error:
            # Remote text may sit anywhere in the chain; only the static code crosses.
            error.__context__ = None
            error.__cause__ = None
            raise
        except Exception:
            pass
        raise MigrationError(Code.CONNECTION)

    return wrapped
