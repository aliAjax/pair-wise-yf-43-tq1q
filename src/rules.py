from datetime import datetime, timedelta, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _validate_calibration(actor, data, lookup):
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    if not instrument:
        raise ValidationError("instrument does not exist")


def _validate_perform(actor, entity, data, lookup):
    if data.get("result") not in ("passed", "failed"):
        raise ValidationError("calibration result must be passed or failed")
    if data.get("result") == "passed" and not data.get("due_at"):
        raise ValidationError("passed calibration requires due_at")


def calibration_current(due_at, as_of):
    return str(due_at) >= str(as_of)


SIGN_ROLES = ("analyst", "authorizer")


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _find_active_delegation(lookup, role, user_id, as_of):
    rows = lookup("delegation", "to_user_id", user_id) or []
    for row in rows:
        if row.get("status") != "active":
            continue
        data = row.get("data", {})
        if data.get("role") != role:
            continue
        valid_from = str(data.get("valid_from", ""))
        valid_to = str(data.get("valid_to", ""))
        if valid_from and valid_from > as_of:
            continue
        if valid_to and valid_to < as_of:
            continue
        return row
    return None


def _validate_delegation_create(actor, data, lookup):
    role = data.get("role")
    if role not in SIGN_ROLES:
        raise ValidationError("delegation role must be analyst or authorizer")
    for field in ("from_user_id", "to_user_id", "valid_from", "valid_to"):
        if not data.get(field):
            raise ValidationError("missing required field: " + field)
    if data["from_user_id"] == data["to_user_id"]:
        raise ValidationError("cannot delegate a post to yourself")
    if str(data["valid_from"]) >= str(data["valid_to"]):
        raise ValidationError("valid_from must be earlier than valid_to")
    return {}


def _make_sign_validator(sign_role):
    def _validate(actor, entity, data, lookup):
        as_of = _now_iso()
        on_behalf_of = None
        if actor.role != sign_role:
            delegation = _find_active_delegation(lookup, sign_role, actor.user_id, as_of)
            if delegation is None:
                raise PermissionDenied(
                    "user %s cannot sign as %s without the role or a valid delegation"
                    % (actor.user_id, sign_role)
                )
            on_behalf_of = delegation["data"]["from_user_id"]
        sigs = dict(entity.get("data", {}).get("signatures", {}))
        if sign_role in sigs:
            raise InvalidTransition("already signed as %s" % sign_role)
        other_role = "authorizer" if sign_role == "analyst" else "analyst"
        other = sigs.get(other_role)
        if other is not None and other.get("signer") == actor.user_id:
            raise ValidationError("the two signatures must be different persons")
        sigs[sign_role] = {
            "signer": actor.user_id,
            "on_behalf_of": on_behalf_of,
            "signed_at": as_of,
        }
        if other is not None:
            next_status = "released"
        elif sign_role == "analyst":
            next_status = "awaiting_authorizer"
        else:
            next_status = "awaiting_analyst"
        return {"signatures": sigs, "_next_status": next_status}

    return _validate


def _validate_withdraw_signature(actor, entity, data, lookup):
    sign_role = data.get("sign_role")
    if sign_role not in SIGN_ROLES:
        raise ValidationError("sign_role must be analyst or authorizer")
    sigs = dict(entity.get("data", {}).get("signatures", {}))
    target = sigs.get(sign_role)
    if target is None:
        raise InvalidTransition("no %s signature to withdraw" % sign_role)
    allowed = (
        actor.role == "admin"
        or target.get("signer") == actor.user_id
        or target.get("on_behalf_of") == actor.user_id
    )
    if not allowed:
        raise PermissionDenied(
            "only the signer, the delegator, or an admin can withdraw this signature"
        )
    remaining = {key: value for key, value in sigs.items() if key != sign_role}
    if not remaining:
        next_status = "pending"
    elif "authorizer" in remaining:
        next_status = "awaiting_analyst"
    else:
        next_status = "awaiting_authorizer"
    return {"signatures": remaining, "_next_status": next_status}


CUSTOM_CREATE = {'calibration': _validate_calibration, 'delegation': _validate_delegation_create}
CUSTOM_TRANSITIONS = {
    ('calibration', 'perform'): _validate_perform,
    ('result', 'sign_analyst'): _make_sign_validator('analyst'),
    ('result', 'sign_authorizer'): _make_sign_validator('authorizer'),
    ('result', 'withdraw_signature'): _validate_withdraw_signature,
}


class RuleEngine:
    ALIASES = {'instruments': 'instrument', 'calibrations': 'calibration', 'methods': 'method', 'results': 'result', 'delegations': 'delegation'}
    INITIAL_STATUS = {'instrument': 'active', 'calibration': 'requested', 'method': 'draft', 'result': 'pending', 'delegation': 'active'}
    TRANSITIONS = {'instrument': {'send_calibration': (('active',), 'calibrating'), 'calibrate': (('calibrating',), 'active'), 'quarantine': (('active',), 'quarantined'), 'restore': (('quarantined',), 'active')}, 'calibration': {'perform': (('requested', 'failed'), 'passed'), 'approve': (('passed',), 'approved'), 'reject': (('failed',), 'rejected')}, 'method': {'validate_method': (('draft',), 'validated'), 'revoke_method': (('validated',), 'revoked')}, 'result': {'sign_analyst': (('pending', 'awaiting_analyst'), None), 'sign_authorizer': (('pending', 'awaiting_authorizer'), None), 'withdraw_signature': (('awaiting_analyst', 'awaiting_authorizer', 'released'), None), 'block': (('pending',), 'blocked'), 'reanalyze': (('blocked',), 'pending')}, 'delegation': {'revoke': (('active',), 'revoked')}}
    CREATE_REQUIRED = {'instrument': ('name', 'serial'), 'calibration': ('instrument_id', 'requested_at'), 'method': ('name', 'version'), 'result': ('sample_id', 'measurement'), 'delegation': ('role', 'from_user_id', 'to_user_id', 'valid_from', 'valid_to')}
    ACTION_REQUIRED = {('instrument', 'calibrate'): ('due_at', 'passed'), ('instrument', 'quarantine'): ('reason',), ('calibration', 'perform'): ('result', 'performed_at', 'uncertainty'), ('calibration', 'approve'): ('authorized_by',), ('calibration', 'reject'): ('reason',), ('method', 'validate_method'): ('parameters', 'instrument_ids'), ('method', 'revoke_method'): ('reason',), ('result', 'sign_analyst'): (), ('result', 'sign_authorizer'): (), ('result', 'withdraw_signature'): ('sign_role',), ('result', 'block'): ('reason',), ('result', 'reanalyze'): ('reason',)}
    CREATE_ROLES = {'instrument': ('admin', 'technician'), 'calibration': ('admin', 'metrology'), 'method': ('admin', 'authorizer'), 'result': ('admin', 'analyst'), 'delegation': ('admin',)}
    ROLE_ACTIONS = {'send_calibration': ('admin', 'technician'), 'calibrate': ('admin', 'metrology'), 'quarantine': ('admin', 'metrology'), 'restore': ('admin', 'metrology'), 'perform': ('admin', 'metrology'), 'approve': ('admin', 'authorizer'), 'reject': ('admin', 'authorizer'), 'validate_method': ('admin', 'authorizer'), 'revoke_method': ('admin', 'authorizer'), 'sign_analyst': ('*',), 'sign_authorizer': ('*',), 'withdraw_signature': ('*',), 'block': ('admin', 'analyst'), 'reanalyze': ('admin', 'analyst'), ('delegation', 'revoke'): ('admin',)}

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

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        if extra is None:
            extra = {}
        patch = dict(data)
        override = extra.pop("_next_status", None)
        if extra:
            patch.update(extra)
        next_status = override or next_status
        if next_status is None:
            raise InvalidTransition("transition has no target status")
        return next_status, patch

    def delegation_supersede_candidates(self, data, lookup):
        role = data.get("role")
        from_user_id = data.get("from_user_id")
        rows = lookup("delegation", "from_user_id", from_user_id) or []
        return [
            row
            for row in rows
            if row.get("status") == "active" and row.get("data", {}).get("role") == role
        ]


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
