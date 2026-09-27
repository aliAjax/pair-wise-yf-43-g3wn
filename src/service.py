from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import (
    RuleEngine,
    qc_deviation_detail,
    qc_failure_patch,
    qc_pass_patch,
    qc_retest_patch,
)


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind, payload)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        if kind == "qc_check":
            self._apply_qc_to_batch(actor, entity)
        return entity

    def _apply_qc_to_batch(self, actor, qc):
        batch = self.repository.get_entity(qc["data"].get("batch_id"))
        if not batch:
            return
        if batch["status"] == "open":
            if qc["status"] == "failed":
                patch = qc_failure_patch(batch["data"], qc)
                merged = dict(batch["data"])
                merged.update(patch)
                updated = self.repository.update_entity(batch["id"], None, "suspended", merged)
                self.audit.record(batch["id"], actor, "suspend", "open", "suspended", {"qc_check_id": qc["id"], "patch": patch})
                self._return_pending_results(actor, updated, qc)
            else:
                patch = qc_pass_patch(batch["data"], qc)
                merged = dict(batch["data"])
                merged.update(patch)
                self.repository.update_entity(batch["id"], None, "open", merged)
                self.audit.record(batch["id"], actor, "qc_pass", "open", "open", {"qc_check_id": qc["id"], "patch": patch})
        elif batch["status"] == "suspended":
            patch = qc_retest_patch(batch["data"], qc)
            merged = dict(batch["data"])
            merged.update(patch)
            self.repository.update_entity(batch["id"], None, "suspended", merged)
            action = "retest_pass" if qc["status"] == "passed" else "retest_fail"
            self.audit.record(batch["id"], actor, action, "suspended", "suspended", {"qc_check_id": qc["id"], "patch": patch})

    def _return_pending_results(self, actor, batch, qc):
        detail = qc_deviation_detail(qc)
        for result in self.repository.find_entities("result", "batch_id", batch["id"]):
            if result["status"] != "pending":
                continue
            patch = {"block_reason": "qc_limit_exceeded", "qc_deviation": detail}
            merged = dict(result["data"])
            merged.update(patch)
            self.repository.update_entity(result["id"], None, "blocked", merged)
            self.audit.record(result["id"], actor, "block", "pending", "blocked", {"qc_check_id": qc["id"], "patch": patch})

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

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
