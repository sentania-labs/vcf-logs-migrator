"""SET ASIDE: existing 9.0 fixup code, not exposed by Engine or CLI.

This is not an enabled operation. Adapter qualification, source/destination typing,
remote restoration, artifact integrity and retirement checks are prerequisites to
reintroducing it. Retained for the later, separately reviewed fixup delivery.
"""

from enum import StrEnum

from .errors import Code, MigrationError, require, safe_boundary
from .models import Bucket, Cluster, Policy, Verification, Window, digest
from .planning import capacity, partition_diff
from .verification import stable_counts


class State(StrEnum):
    DRAFT = "DRAFT"
    DISCOVERED = "DISCOVERED"
    PLANNED = "PLANNED"
    PREFLIGHT_PASSED = "PREFLIGHT_PASSED"
    AUTHORIZED = "AUTHORIZED"
    RUNNING = "RUNNING"
    MONITOR_DETACHED = "MONITOR_DETACHED"
    VERIFYING = "VERIFYING"
    VERIFIED = "VERIFIED"
    BLOCKED = "BLOCKED"
    STOPPED = "STOPPED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


_TERMINAL = {"idle", "completed", "cancelled", "failed"}


class SetAsideFixup:
    def __init__(self, *args, **kwargs):
        raise NotImplementedError("Fixup is set aside pending migration qualification.")

    def _transition(self, run, state, reason=None):
        run["state"] = state.value
        run["reason"] = reason.value if reason else None
        self.journal.save(run, "fixup_state")

    def _execution_gate(self):
        # Deliberate release gate, not a user-overridable acknowledgment. Live lab qualification
        # of credentials, placement, cancellation and restoration must precede its removal.
        require(self.env.mode == "simulation", Code.UNSUPPORTED)

    def _save(self, run, kind, details=None):
        self.journal.save(run, kind, details)
        if self.on_event:
            # Subscribers cannot interrupt a service restoration or a journal commit.
            try:
                self.on_event(
                    {
                        "run_id": run["id"],
                        "kind": kind,
                        "at": run["updated_at"],
                        "details": details or {},
                    }
                )
            except Exception:
                pass

    def _counts(self, run, scope):
        plan = run["plan"]
        return stable_counts(
            self.env,
            self.clock,
            Policy(**plan["policy"]),
            scope,
            Window(**plan["coverage"]),
            tuple(run["bucket_outcomes"]),
        )

    @safe_boundary
    def fixup(
        self, run_id: str, *, consent_nodes: tuple[str, ...], backup_ack: bool, plan_hash: str
    ) -> dict:
        self._execution_gate()
        with self.journal.exclusive(), self.env.exclusive():
            run = self.journal.load(run_id)
            require(run["plan"] is not None and backup_ack, Code.ACK)
            require(plan_hash == run["plan_hash"] and digest(run["plan"]) == plan_hash, Code.ACK)
            require(not run["service_obligations"], Code.RECOVERY)
            require(run["state"] != State.RECOVERY_REQUIRED, Code.RECOVERY)
            require(
                run["plan"]["kind"] == "fixup"
                or run["verification"]["outcome"] == Verification.SUCCESS,
                Code.INCOMPLETE,
            )
            source, destination = self.env.discover()
            require(not self.journal.unresolved(destination.id, run_id), Code.RECOVERY)
            require(
                source.cohort_fingerprint(run["plan"]["api_selected"])
                == run["plan"]["source_fingerprint"],
                Code.INVENTORY_CHANGED,
            )
            require(destination.version.startswith("9.0."), Code.UNSUPPORTED)
            expected = Cluster.from_dict(run["plan"]["destination"])
            require(
                destination.identity_fingerprint == expected.identity_fingerprint, Code.TOPOLOGY
            )
            difference = partition_diff(source, destination)
            require(not difference["missing"] and not difference["different"], Code.PARTITIONS)
            require(
                self.env.transfer().status in _TERMINAL,
                Code.BUSY,
            )
            capacity(destination, (), Policy(**run["plan"]["policy"]))
            targets: dict[str, list[Bucket]] = {}
            for b in (Bucket(**b) for b in run["plan"]["selected"]):
                copies = [d for d in destination.buckets if d.id == b.id]
                require(
                    len(copies) == 1 and copies[0].complete and copies[0].manifest == b.manifest,
                    Code.INTEGRITY,
                )
                require(self.env.attest_transfer(b), Code.INTEGRITY)
                partition = next((p for p in destination.partitions if p.id == b.partition), None)
                require(partition is not None and b.end is not None, Code.PARTITIONS)
                from .models import parse_time

                margin = run["plan"]["policy"]["retention_margin_seconds"] * 1000
                require(
                    partition.retention_seconds == 0
                    or b.end + partition.retention_seconds * 1000
                    > parse_time(self.clock.now()) + margin,
                    Code.RETENTION,
                )
                node = copies[0].node
                already_fixed = run.get("fixup_nodes", {}).get(
                    node
                ) == "complete" and self.env.fixup_confirmed(node, b)
                if not already_fixed:
                    targets.setdefault(node, []).append(b)
            require(set(targets) <= set(consent_nodes), Code.ACK)
            # All credentials and initial states are tested before ANY node stops.
            for node in targets:
                self.env.test_node_credentials(node)
                require(all(self.env.readiness(node)), Code.SERVICE)
                require(self.env.fixup_artifact_verified(node), Code.INTEGRITY)
            before_counts = self._counts(run, "destination")
            run["backup_ack"] = True
            run["fixup_consent"] = {"plan_hash": plan_hash, "nodes": list(consent_nodes)}
            prior = run["verification"]["outcome"]
            run["verification"] = {"outcome": Verification.PENDING.value}
            run["fixup"] = {"outcome": "in_progress", "prior_verification": prior}
            self._save(run, "fixup_authorized")
            if run["plan"]["kind"] == "migration":
                self._transition(run.PARTIAL)
            primary = destination.nodes[0].id
            for node in sorted(targets, key=lambda n: (n == primary, n)):
                for other in destination.nodes:
                    require(all(self.env.readiness(other.id)), Code.SERVICE)
                require(
                    self.env.transfer().status in _TERMINAL,
                    Code.BUSY,
                )
                buckets = targets[node]
                self._fixup_node(run, node, buckets)
                run.setdefault("fixup_nodes", {})[node] = "complete"
                self._save(run, "fixup_node_complete", {"node": node})
            after_counts = self._counts(run, "destination")
            require(after_counts == before_counts, Code.INTEGRITY)
            run["fixup"] = {
                "outcome": "complete",
                "counts_unchanged": True,
                "count": after_counts.total,
                "at": self.clock.now(),
                "prior_verification": prior,
            }
            self._save(run, "fixup_complete")
            run["verification"] = {"outcome": Verification.PENDING.value}
            if run["plan"]["kind"] == "migration":
                self._transition(run.PARTIAL)
            return run

    def _fixup_node(self, run, node, buckets):
        run["service_obligations"][node] = {"initial": "running", "restart_required": True}
        self._save(run, "remote_recovery_arm_intent", {"node": node})
        self.env.arm_recovery(node, run["id"])
        require(self.env.owns_recovery(node, run["id"]), Code.RECOVERY)
        self._save(run, "service_stop_intent", {"node": node})
        failed = None
        catalog_safe = True
        before = None
        try:
            self.env.stop_service(node)
            require(self.env.service(node) == "stopped", Code.SERVICE)
            self._save(run, "service_stopped", {"node": node})
            self._save(run, "catalog_backup_intent", {"node": node})
            before = self.env.catalog_backup(node, run["id"])
            run["service_obligations"][node]["catalog_before"] = before
            self._save(run, "catalog_backup_observed", {"node": node})
            for b in buckets:
                require(
                    b.complete and b.status != "active" and self.env.attest_transfer(b),
                    Code.INTEGRITY,
                )
                self._save(
                    run,
                    "fixup_intent",
                    {
                        "node": node,
                        "bucket": b.id,
                        "partition": b.partition,
                        "creation_time": b.end,
                    },
                )
                self.env.fixup(node, b)
                require(self.env.fixup_confirmed(node, b), Code.INTEGRITY)
                self._save(run, "fixup_observed", {"node": node, "bucket": b.id})
        except Exception:
            failed = Code.SERVICE
        finally:
            try:
                if before is not None:
                    catalog_safe = self.env.catalog_safe(node, before, tuple(buckets))
                require(catalog_safe, Code.RECOVERY)
                journal_failure = False
                try:
                    self._save(run, "service_restart_intent", {"node": node})
                except MigrationError:
                    # Initial durable obligation requires restoration even if the disk fills.
                    journal_failure = True
                require(self.env.owns_recovery(node, run["id"]), Code.RECOVERY)
                self.env.start_service(node)
                require(all(self.env.readiness(node)), Code.RECOVERY)
                require(not journal_failure, Code.STORAGE)
                run["service_obligations"][node]["restored"] = True
                self._save(run, "service_restart_confirmed", {"node": node})
                self._save(run, "remote_recovery_clear_intent", {"node": node})
                self.env.clear_recovery(node, run["id"])
                del run["service_obligations"][node]
                self._save(run, "service_obligation_resolved", {"node": node})
            except Exception:
                self._transition(run.RECOVERY_REQUIRED, Code.RECOVERY)
                raise MigrationError(Code.RECOVERY) from None
        if failed:
            self._transition(run.PARTIAL, failed)
            raise MigrationError(failed)

    @safe_boundary
    def recover_services(self, run_id: str) -> dict:
        self._execution_gate()
        with self.journal.exclusive(), self.env.exclusive():
            run = self.journal.load(run_id)
            _, destination = self.env.discover()
            require(bool(run["service_obligations"]), Code.STATE)
            self._transition(run.RECOVERY_REQUIRED, Code.RECOVERY)
            expected = Cluster.from_dict(run["plan"]["destination"])
            require(
                destination.identity_fingerprint == expected.identity_fingerprint, Code.TOPOLOGY
            )
            for node, obligation in list(run["service_obligations"].items()):
                require(obligation["initial"] == "running", Code.RECOVERY)
                owns = self.env.owns_recovery(node, run["id"])
                require(owns or obligation.get("restored", False), Code.RECOVERY)
                self.env.test_node_credentials(node)
                if "catalog_before" in obligation:
                    require(
                        self.env.catalog_safe(
                            node,
                            obligation["catalog_before"],
                            tuple(Bucket(**b) for b in run["plan"]["selected"]),
                        ),
                        Code.RECOVERY,
                    )
                self._save(run, "service_recovery_intent", {"node": node})
                try:
                    if owns:
                        require(
                            self.env.transfer().status in _TERMINAL,
                            Code.BUSY,
                        )
                        self.env.start_service(node)
                    require(all(self.env.readiness(node)), Code.RECOVERY)
                except Exception:
                    self._transition(run.RECOVERY_REQUIRED, Code.RECOVERY)
                    raise MigrationError(Code.RECOVERY) from None
                obligation["restored"] = True
                self._save(run, "service_restart_confirmed", {"node": node})
                if owns:
                    self._save(run, "remote_recovery_clear_intent", {"node": node})
                    self.env.clear_recovery(node, run["id"])
                del run["service_obligations"][node]
                self._save(run, "service_obligation_resolved", {"node": node})
            self._transition(run.STOPPED)
            return run
