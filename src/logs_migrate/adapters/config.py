"""Config REST boundary using the existing 8.18/9.0/9.1.1 authentication clients.

Routes and bodies follow docs/config-migration.md: GET wrappers per rule type, PUT by
destination id for updates (partitions: id in the body), the `/batch` wrapper for forwarder creates.
"""

from contextlib import nullcontext
from urllib.parse import quote

from ..config import RULE_TYPES, indexed, write_body
from ..errors import Code, Unrecognized, require, safe_boundary

ROUTES = {
    "partitions": ("/api/v1/partitioning", "partitions"),
    "filters": ("/api/v1/log-filter", "logFilters"),
    "masking": ("/api/v1/log-mask", "logMaskRules"),
    "forwarding": ("/api/v1/log-forwarder", None),
}


class RestConfigEnvironment:
    """A source reader and destination writer, with no delete or restart capability.

    `source` and `destination` are Logs90/Logs911 clients. The source uses Logs90's
    Bearer session adapter for 8.18. destination_version is supplied by the caller
    (the live environment reads it from the appliance first).
    """

    def __init__(self, source, destination, destination_version, destination_hosts=()):
        require(destination_version.startswith(("9.0.", "9.1.1")), Code.UNSUPPORTED)
        self.source = source
        self.destination = destination
        self.destination_version = destination_version
        self.destination_hosts = {h.lower().rstrip(".") for h in destination_hosts}
        self.mode = "live"

    def exclusive(self):
        # No distributed appliance lock is claimed. Recorded fixtures are single-caller.
        return nullcontext()

    @safe_boundary
    def config_rules(self, scope, kind):
        require(scope in {"source", "destination"} and kind in RULE_TYPES, Code.INVALID_INPUT)
        client = self.source if scope == "source" else self.destination
        path, wrapper = ROUTES[kind]
        response = client.call("GET", path)
        if wrapper:
            if not (isinstance(response, dict) and isinstance(response.get(wrapper), list)):
                raise Unrecognized(Code.SCHEMA)
            response = response[wrapper]
        return list(indexed(response).values())

    @safe_boundary
    def config_write(self, kind, rule, existing):
        require(kind in RULE_TYPES, Code.INVALID_INPUT)
        path, _ = ROUTES[kind]
        body = write_body(kind, rule, existing)
        if kind == "forwarding" and self.destination_version.startswith("9.0."):
            require(
                str(body.get("host", "")).lower().rstrip(".") not in self.destination_hosts,
                Code.INVALID_INPUT,
            )
        if existing is not None:
            require(bool(existing["id"]), Code.UNSUPPORTED)
            # Updates address the destination object, never a source-only ID.
            if kind == "partitions":
                # 9.0 serves the update at the collection route with the id in the body.
                self.destination.call("PUT", path, body)
            else:
                self.destination.call("PUT", path + "/" + quote(existing["id"], safe=""), body)
        elif kind == "forwarding":
            # GET is a bare array; the batch create endpoint requires this wrapper.
            self.destination.call("POST", path + "/batch", {"forwarders": [body]})
        else:
            self.destination.call("POST", path, body)
