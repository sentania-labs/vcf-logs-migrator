"""One version pairing decision, shared by every entry point."""

import re

from .errors import Code, MigrationError

MINIMUM_DESTINATION = "9.0.0"
EXERCISED = ((9, 0), (9, 1, 1))


def parts(value):
    if not re.fullmatch(r"\d+(?:\.\d+){1,3}", value):
        raise MigrationError(
            Code.UNSUPPORTED, f"Cannot read version {value!r}. Check the appliance and retry."
        )
    return tuple(int(p) for p in value.split(".")) + (0,) * (4 - len(value.split(".")))


def evaluate(source, destination, api, evidence):
    """Return a safe, serializable result even when the pairing is refused."""
    result = {
        "source": source,
        "destination": destination,
        "api": api,
        "evidence": evidence,
        "passed": False,
        "message": "",
        "warnings": [],
    }
    try:
        for side, versions in (("Source", source), ("Destination", destination)):
            if not versions:
                raise MigrationError(
                    Code.INCOMPLETE, f"{side} version could not be read. Check access and retry."
                )
            parsed = {parts(v) for v in versions.values()}
            if len(parsed) != 1:
                names = ", ".join(f"{node}: {v}" for node, v in versions.items())
                raise MigrationError(
                    Code.UNSUPPORTED,
                    f"{side} nodes have different versions: {names}. "
                    "Finish upgrading the cluster and retry.",
                )
        sv, dv = next(iter(source.values())), next(iter(destination.values()))
        source_parts, destination_parts = parts(sv), parts(dv)
        if "inferred" in evidence and len(dv.split(".")) == 2:
            source_parts, destination_parts = source_parts[:2], destination_parts[:2]
            result["warnings"].append("Only major and minor versions could be compared.")
        if source_parts > destination_parts:
            raise MigrationError(
                Code.UNSUPPORTED,
                f"Source version {sv} is newer than destination version {dv}. "
                "Upgrade the destination and retry.",
            )
        if api != "present":
            if api == "absent":
                raise MigrationError(
                    Code.UNSUPPORTED,
                    f"Destination version {dv} does not answer the migration API. "
                    f"The lowest destination version known to work is {MINIMUM_DESTINATION}. "
                    "Upgrade or check the destination and retry.",
                )
            raise MigrationError(
                Code.INCOMPLETE,
                f"The migration API on destination {dv} could not be checked. "
                "Check access and retry; the lowest destination version known to work "
                f"is {MINIMUM_DESTINATION}.",
            )
        if not any(parts(dv)[: len(prefix)] == prefix for prefix in EXERCISED):
            result["warnings"].append(
                f"Destination {dv} answered the migration API but has not been tested "
                "with real appliances."
            )
        result["passed"] = True
        result["message"] = f"Source {sv} and destination {dv} are compatible."
    except MigrationError as error:
        result["message"] = error.message
        result["code"] = error.code.value
    return result


def enforce(result):
    if not result["passed"]:
        error = MigrationError(Code(result["code"]), result["message"])
        error.version_check = result
        raise error
    return result
