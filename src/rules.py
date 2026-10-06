from datetime import datetime, timezone

from .domain import (
    Actor,
    InvalidTransition,
    PermissionDenied,
    Role,
    ValidationError,
)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validate_calibration(actor, data, lookup):
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    if not instrument:
        raise ValidationError("instrument does not exist")


def _validate_perform(actor, entity, data, lookup):
    if data.get("result") not in ("passed", "failed"):
        raise ValidationError("calibration result must be passed or failed")
    if data.get("result") == "passed" and not data.get("due_at"):
        raise ValidationError("passed calibration requires due_at")
    return None, dict(data)


def calibration_current(due_at, as_of):
    return str(due_at) >= str(as_of)


def _validate_release_package(data, lookup):
    """Shared release preconditions checked with the analyst's first sign."""
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    method = _find_one(lookup, "method", "id", data.get("method_id"))
    if not instrument or instrument["status"] != "active":
        raise ValidationError("result requires an active instrument")
    if not calibration_current(instrument["data"].get("due_at", ""), "2026-09-24"):
        raise ValidationError("instrument calibration is not current")
    if not method or method["status"] != "validated":
        raise ValidationError("result requires a validated method")
    if data.get("instrument_id") not in method["data"].get("instrument_ids", []):
        raise ValidationError("method is not validated for this instrument")


def _signed_slot(slot, actor, signed_at):
    return {
        "slot": slot,
        "role": slot,
        "signed_by": actor.principal_id,
        "signed_by_role": actor.role,
        "physical_actor": actor.physical_id,
        "delegation_id": actor.delegation_id,
        "signed_at": signed_at,
    }


def _empty_slots():
    return {"analyst": None, "authorizer": None}


def _validate_result_sign(actor, entity, data, lookup):
    """Countersigning: analyst first, authorizer second; never the same person."""
    slots = dict(_empty_slots())
    slots.update(entity["data"].get("signatures") or {})
    signed_at = _now()
    slot = "analyst" if actor.role == "analyst" else "authorizer"

    if slot == "authorizer" and not slots.get("analyst"):
        raise InvalidTransition("analyst must sign before authorizer")
    if slots.get(slot):
        raise InvalidTransition("%s signature already recorded" % slot)

    other = slots.get("authorizer" if slot == "analyst" else "analyst")
    if other:
        # Two signatures cannot be the same person -- check both the post
        # holder the signature is recorded under and the physical signer.
        if other["signed_by"] == actor.principal_id:
            raise PermissionDenied("two signatures cannot be the same person")
        if other["physical_actor"] == actor.physical_id:
            raise PermissionDenied("two signatures cannot be the same person")

    record = _signed_slot(slot, actor, signed_at)
    need_release_package = slot == "analyst" and not any(slots.values())
    if need_release_package:
        _validate_release_package(data, lookup)

    signatures = dict(slots)
    signatures[slot] = record
    if all(signatures.values()):
        return "released", {"signatures": signatures}

    if need_release_package:
        release_fields = ("instrument_id", "method_id", "value", "unit")
        patch = {field: data[field] for field in release_fields}
        patch.update(released_by=actor.principal_id, signatures=signatures)
        return "countersigning", patch
    return "countersigning", {"signatures": signatures}


def _validate_result_withdraw(actor, entity, data, lookup):
    """Withdraw one signature: the other signature is retained."""
    slot = data.get("slot")
    if slot not in ("analyst", "authorizer"):
        raise ValidationError("slot must be analyst or authorizer")
    slots = dict(_empty_slots())
    slots.update(entity["data"].get("signatures") or {})
    existing = slots.get(slot)
    if not existing:
        raise InvalidTransition("no %s signature to withdraw" % slot)
    if actor.role != "admin" and existing["signed_by"] != actor.principal_id:
        raise PermissionDenied("only the original signer may withdraw this signature")

    remaining = [name for name, item in slots.items() if name != slot and item]
    next_status = "countersigning" if remaining else "pending"
    slots[slot] = None
    return next_status, {"slot": slot, "signatures": slots}


def _validate_delegation_create(actor, data, lookup):
    role = data.get("role")
    if role not in DELEGATABLE_ROLES:
        raise ValidationError("role %s cannot be delegated" % str(role))
    if not data.get("principal_id") or not data.get("agent_id"):
        raise ValidationError("principal_id and agent_id are required")
    if data["principal_id"] == data["agent_id"]:
        raise ValidationError("principal and agent cannot be the same person")
    expires_at = data.get("expires_at")
    if expires_at and str(expires_at) <= _now():
        raise ValidationError("delegation must not expire in the past")


CUSTOM_CREATE = {"calibration": _validate_calibration, "delegation": _validate_delegation_create}
CUSTOM_TRANSITIONS = {
    ("calibration", "perform"): _validate_perform,
    ("result", "sign"): _validate_result_sign,
    ("result", "withdraw"): _validate_result_withdraw,
}

DELEGATABLE_ROLES = frozenset(
    {Role.technician.value, Role.metrology.value, Role.authorizer.value, Role.analyst.value}
)


class RuleEngine:
    ALIASES = {
        "instruments": "instrument",
        "calibrations": "calibration",
        "methods": "method",
        "results": "result",
        "delegations": "delegation",
    }
    INITIAL_STATUS = {
        "instrument": "active",
        "calibration": "requested",
        "method": "draft",
        "result": "pending",
        "delegation": "active",
    }
    TRANSITIONS = {
        "instrument": {
            "send_calibration": (("active",), "calibrating"),
            "calibrate": (("calibrating",), "active"),
            "quarantine": (("active",), "quarantined"),
            "restore": (("quarantined",), "active"),
        },
        "calibration": {
            "perform": (("requested", "failed"), "passed"),
            "approve": (("passed",), "approved"),
            "reject": (("failed",), "rejected"),
        },
        "method": {
            "validate_method": (("draft",), "validated"),
            "revoke_method": (("validated",), "revoked"),
        },
        # sign/withdraw carry a dynamic target status resolved by the
        # validators: pending -> countersigning -> released and back.
        "result": {
            "sign": (("pending", "countersigning"), None),
            "withdraw": (("countersigning", "released"), None),
            "block": (("pending", "countersigning"), "blocked"),
            "reanalyze": (("blocked",), "pending"),
        },
        "delegation": {
            "revoke": (("active",), "revoked"),
        },
    }
    CREATE_REQUIRED = {
        "instrument": ("name", "serial"),
        "calibration": ("instrument_id", "requested_at"),
        "method": ("name", "version"),
        "result": ("sample_id", "measurement"),
        "delegation": ("principal_id", "agent_id", "role"),
    }
    ACTION_REQUIRED = {
        ("instrument", "calibrate"): ("due_at", "passed"),
        ("instrument", "quarantine"): ("reason",),
        ("calibration", "perform"): ("result", "performed_at", "uncertainty"),
        ("calibration", "approve"): ("authorized_by",),
        ("calibration", "reject"): ("reason",),
        ("method", "validate_method"): ("parameters", "instrument_ids"),
        ("method", "revoke_method"): ("reason",),
        ("result", "sign"): (),
        ("result", "withdraw"): ("slot",),
        ("result", "block"): ("reason",),
        ("result", "reanalyze"): ("reason",),
        ("delegation", "revoke"): (),
    }
    CREATE_ROLES = {
        "instrument": ("admin", "technician"),
        "calibration": ("admin", "metrology"),
        "method": ("admin", "authorizer"),
        "result": ("admin", "analyst"),
        "delegation": ("admin",),
    }
    ROLE_ACTIONS = {
        "send_calibration": ("admin", "technician"),
        "calibrate": ("admin", "metrology"),
        "quarantine": ("admin", "metrology"),
        "restore": ("admin", "metrology"),
        "perform": ("admin", "metrology"),
        "approve": ("admin", "authorizer"),
        "reject": ("admin", "authorizer"),
        "validate_method": ("admin", "authorizer"),
        "revoke_method": ("admin", "authorizer"),
        "sign": ("analyst", "authorizer"),
        "withdraw": ("analyst", "authorizer", "admin"),
        "block": ("admin", "analyst"),
        "reanalyze": ("admin", "analyst"),
        "revoke": ("admin",),
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def resolve_actor(self, actor, required_role, allowed_roles, lookup):
        """Resolve a delegated action against active delegations.

        ``required_role`` is the single post this particular invocation needs
        (e.g. the missing signature slot). An ``X-On-Behalf-Of`` header is
        honoured only when an active, unexpired delegation for exactly that
        post exists. Everything else is a hard permission refusal
        (越权代签直接拒绝).
        """
        principal = actor.on_behalf_of
        if not principal:
            return actor
        delegations = lookup("delegation", "principal_id", principal) if lookup else []
        matches = []
        for delegation in delegations:
            if delegation["status"] != "active":
                continue
            if delegation["data"].get("agent_id") != actor.user_id:
                continue
            if delegation["data"].get("role") != required_role:
                continue
            if required_role not in allowed_roles:
                continue
            expires_at = delegation["data"].get("expires_at")
            if expires_at and str(expires_at) <= _now():
                continue
            matches.append(delegation)
        if actor.delegation_id:
            matches = [item for item in matches if item["id"] == actor.delegation_id]
        if not matches:
            raise PermissionDenied(
                "no active delegation for %s acting as %s on this action"
                % (actor.user_id, principal)
            )
        if len(matches) > 1:
            raise PermissionDenied("ambiguous delegation for this action")
        delegation = matches[0]
        return Actor(
            user_id=principal,
            role=required_role,
            on_behalf_of=principal,
            delegation_id=delegation["id"],
            agent_id=actor.user_id,
        )

    def required_role_for(self, actor, kind, action, entity, data):
        """Pin down the single post this invocation targets."""
        slots = entity["data"].get("signatures") or {}
        if (kind, action) == ("result", "sign"):
            return "authorizer" if slots.get("analyst") else "analyst"
        if (kind, action) == ("result", "withdraw"):
            slot = data.get("slot")
            if slot in ("analyst", "authorizer"):
                return slot
        # Non slot-specific actions: the caller's own claimed role identifies
        # the post, provided that role may perform the action at all.
        action_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        if actor.on_behalf_of and actor.role in action_roles:
            return actor.role
        return actor.role

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, static_next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        required_role = self.required_role_for(actor, kind, action, entity, data)
        effective = self.resolve_actor(actor, required_role, allowed_roles, lookup)
        self._ensure_role(effective, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        next_status, patch = (
            custom(effective, entity, data, lookup) if custom else (None, dict(data))
        )
        return next_status or static_next_status, patch, effective


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
