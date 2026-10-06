from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import RuleEngine


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
        if kind == "delegation":
            self._supersede_delegations(actor, payload)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def _supersede_delegations(self, actor, payload):
        """A new post assignment invalidates every previous one (岗位一变即失效)."""
        for delegation in self._lookup(
            "delegation", "principal_id", payload["principal_id"]
        ):
            if delegation["status"] != "active":
                continue
            if delegation["data"].get("role") != payload["role"]:
                continue
            updated = self.repository.update_entity(
                delegation["id"],
                delegation["version"],
                "superseded",
                dict(delegation["data"], superseded_reason="post reassigned"),
            )
            self.audit.record(
                delegation["id"],
                actor,
                "supersede",
                "active",
                updated["status"],
                {"reason": "new delegation created for the same post"},
            )

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        if expected_version is not None and expected != entity["version"]:
            # The client wrote against a stale snapshot. Surface this before
            # the state-machine guard so the late second signer always sees a
            # version conflict rather than a generic transition error.
            raise ConflictError(
                "version conflict: expected %s, found %s"
                % (expected, entity["version"])
            )
        next_status, patch, effective = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        stored_patch = {key: value for key, value in patch.items() if key != "slot"}
        merged.update(stored_patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        # Business signature/release fields are recorded under the principal;
        # the audit timeline keeps both the physical person and the principal.
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            self._audit_detail(action, patch, effective, entity),
        )
        return updated

    @staticmethod
    def _audit_detail(action, patch, effective, entity):
        detail = {"patch": patch}
        if effective.is_delegated:
            detail["delegated"] = {
                "delegation_id": effective.delegation_id,
                "principal_id": effective.principal_id,
                "physical_actor": effective.physical_id,
            }
        if action == "withdraw":
            slot = patch.get("slot")
            previous = (entity["data"].get("signatures") or {}).get(slot)
            detail["withdrawn_slot"] = slot
            if previous:
                detail["recorded_under"] = previous.get("signed_by")
        return detail

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
