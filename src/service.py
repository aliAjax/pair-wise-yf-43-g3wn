import threading
from uuid import uuid4

from .audit import AuditTrail
from .domain import Actor, ConflictError, NotFoundError
from .repository import utcnow
from .rules import QC_FAILURE_FIELDS, RuleEngine


SYSTEM_ACTOR = Actor("system", "admin")


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)
        # Cascade suspension touches the batch plus every pending result.
        self._lock = threading.RLock()

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        with self._lock:
            if idempotency_key:
                existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
                if existing:
                    entity = self.repository.get_entity(existing)
                    if entity:
                        return entity
            self.rules.validate_create(actor, kind, payload, self._lookup)
            if kind == "qc_check":
                payload.setdefault("qc_type", "routine")
                evaluation = self.rules.qc_evaluate(payload)
                qc_status = evaluation.pop("status")
                payload.update(evaluation)
            else:
                qc_status = None
            entity_id = str(payload.pop("id", "") or uuid4())
            if self.repository.get_entity(entity_id):
                raise ConflictError("entity already exists: " + entity_id)
            status = qc_status or self.rules.initial_status(kind)
            entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
            self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
            if idempotency_key:
                self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
            if kind == "qc_check" and entity["status"] == "failed":
                self._handle_failed_qc(actor, entity)
                entity = self.repository.get_entity(entity_id)
            return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        with self._lock:
            return self._transition(actor, entity_id, action, data, expected_version)

    def _transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        patch = dict(data or {})
        if entity["kind"] == "batch" and action == "suspend":
            patch.setdefault("suspended_at", utcnow())
        if entity["kind"] == "batch" and action == "review_recovery":
            patch["recovered_at"] = utcnow()
        next_status, validated_patch = self.rules.validate_transition(
            actor, entity, action, patch, self._lookup
        )
        merged = dict(entity["data"])
        merged.update(validated_patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": validated_patch},
        )
        if entity["kind"] == "batch" and updated["status"] == "suspended":
            self._return_pending_results(actor, updated, validated_patch)
        return updated

    def _handle_failed_qc(self, actor, qc):
        """Out-of-tolerance QC: the batch stops admitting releases.

        On an active batch the batch is suspended and pending results are
        returned with the deviation. A failed retest while already suspended
        only refreshes the suspension time, restarting the two-pass counter;
        the original out-of-tolerance marker is kept for the "another
        executor" check.
        """
        batch = self.repository.get_entity(qc["data"]["batch_id"])
        if batch["status"] == "active":
            failure = {
                "failed_qc_id": qc["id"],
                "failed_performer": qc["data"].get("performed_by"),
                "standard_value": qc["data"].get("standard_value"),
                "measured_value": qc["data"].get("measured_value"),
                "allowed_deviation": qc["data"].get("allowed_deviation"),
                "deviation": qc["data"].get("deviation"),
                "abs_deviation": qc["data"].get("abs_deviation"),
                "failed_at": qc["data"].get("performed_at"),
            }
            self._transition(
                SYSTEM_ACTOR,
                batch["id"],
                "suspend",
                dict(failure, reason="qc out of tolerance"),
            )
        else:
            data = {
                key: value for key, value in batch["data"].items()
                if key not in ("reviewed_by", "retested_by", "recovery_qc_ids", "recovered_at")
            }
            # Restart the consecutive-pass window; keep the original failure marker.
            data["suspended_at"] = utcnow()
            data["last_failed_retest_qc_id"] = qc["id"]
            self.repository.update_entity(batch["id"], batch["version"], "suspended", data)
            self.audit.record(
                batch["id"],
                actor,
                "retest_failed",
                "suspended",
                "suspended",
                {"failed_qc_id": qc["id"]},
            )

    def _return_pending_results(self, actor, batch, failure):
        marker = {
            field: failure.get(field, batch["data"].get(field))
            for field in QC_FAILURE_FIELDS
        }
        pending = self.repository.find_entities("result", "batch_id", batch["id"])
        for result in pending:
            if result["status"] != "pending":
                continue
            patch = dict(marker)
            patch["returned_reason"] = "batch suspended: qc out of tolerance"
            self._transition(SYSTEM_ACTOR, result["id"], "return_for_qc", patch)

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
