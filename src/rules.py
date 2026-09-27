from datetime import date, datetime, timedelta

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


def _validate_batch_create(actor, data, lookup):
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    method = _find_one(lookup, "method", "id", data.get("method_id"))
    if not instrument or instrument["status"] != "active":
        raise ValidationError("batch requires an active instrument")
    if not calibration_current(instrument["data"].get("due_at", ""), date.today().isoformat()):
        raise ValidationError("instrument calibration is not current")
    if not method or method["status"] != "validated":
        raise ValidationError("batch requires a validated method")
    if data.get("instrument_id") not in method["data"].get("instrument_ids", []):
        raise ValidationError("method is not validated for this instrument")
    existing = lookup("batch", "batch_no", data.get("batch_no")) if lookup else []
    for other in existing or []:
        if other["data"].get("instrument_id") == data.get("instrument_id"):
            raise ConflictError("batch number already registered for this instrument")


def _validate_qc_create(actor, data, lookup):
    batch = _find_one(lookup, "batch", "id", data.get("batch_id"))
    if not batch:
        raise ValidationError("batch does not exist")
    for field in ("standard_value", "measured_value", "allowed_deviation"):
        value = data.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError(field + " must be a number")
    if data["allowed_deviation"] < 0:
        raise ValidationError("allowed_deviation must be >= 0")
    if batch["status"] == "suspended":
        failed_operator = batch["data"].get("qc_failed_operator")
        if failed_operator and data.get("operator") == failed_operator:
            raise ValidationError("retest must be performed by another operator")
    data["deviation"] = round(float(data["measured_value"]) - float(data["standard_value"]), 6)


def _qc_initial_status(data):
    if abs(float(data.get("deviation", 0))) <= float(data["allowed_deviation"]):
        return "passed"
    return "failed"


def _validate_result_create(actor, data, lookup):
    batch = _find_one(lookup, "batch", "id", data.get("batch_id"))
    if not batch:
        raise ValidationError("batch does not exist")
    if batch["status"] != "open":
        raise ValidationError("batch is not admitting new results")
    value = data.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError("value must be a number")
    data["instrument_id"] = batch["data"].get("instrument_id")
    data["method_id"] = batch["data"].get("method_id")


def _validate_result_release(actor, entity, data, lookup):
    batch = _find_one(lookup, "batch", "id", entity["data"].get("batch_id"))
    if not batch:
        raise ValidationError("result requires a batch")
    if batch["status"] != "open":
        raise ValidationError("batch is suspended; release is not allowed")
    return {"released_by": actor.user_id}


def _validate_batch_restore(actor, entity, data, lookup):
    if int(entity["data"].get("retest_passes") or 0) < 2:
        raise ValidationError("restore requires two consecutive passing retests")
    return {"reviewed_by": actor.user_id}


def qc_deviation_detail(qc):
    data = qc["data"]
    return {
        "qc_check_id": qc["id"],
        "operator": data.get("operator"),
        "checked_at": data.get("checked_at"),
        "standard_value": data.get("standard_value"),
        "measured_value": data.get("measured_value"),
        "allowed_deviation": data.get("allowed_deviation"),
        "deviation": data.get("deviation"),
        "status": qc["status"],
    }


def qc_failure_patch(batch_data, qc):
    return {
        "last_qc": qc_deviation_detail(qc),
        "qc_failed_operator": qc["data"].get("operator"),
        "retest_passes": 0,
        "retest_operator": None,
        "suspended_reason": "qc_limit_exceeded",
    }


def qc_pass_patch(batch_data, qc):
    return {"last_qc": qc_deviation_detail(qc)}


def qc_retest_patch(batch_data, qc):
    patch = qc_pass_patch(batch_data, qc)
    operator = qc["data"].get("operator")
    if qc["status"] == "passed":
        if operator and operator == batch_data.get("retest_operator"):
            patch["retest_passes"] = int(batch_data.get("retest_passes") or 0) + 1
        else:
            patch["retest_passes"] = 1
        patch["retest_operator"] = operator
    else:
        patch["retest_passes"] = 0
        patch["retest_operator"] = None
        patch["qc_failed_operator"] = operator
    return patch


CUSTOM_CREATE = {'calibration': _validate_calibration, 'batch': _validate_batch_create, 'qc_check': _validate_qc_create, 'result': _validate_result_create}
CUSTOM_TRANSITIONS = {('calibration', 'perform'): _validate_perform, ('result', 'release'): _validate_result_release, ('batch', 'restore'): _validate_batch_restore}
INITIAL_STATUS_HOOKS = {'qc_check': _qc_initial_status}


class RuleEngine:
    ALIASES = {'instruments': 'instrument', 'calibrations': 'calibration', 'methods': 'method', 'results': 'result', 'batches': 'batch', 'qc_checks': 'qc_check'}
    INITIAL_STATUS = {'instrument': 'active', 'calibration': 'requested', 'method': 'draft', 'result': 'pending', 'batch': 'open', 'qc_check': 'passed'}
    TRANSITIONS = {'instrument': {'send_calibration': (('active',), 'calibrating'), 'calibrate': (('calibrating',), 'active'), 'quarantine': (('active',), 'quarantined'), 'restore': (('quarantined',), 'active')}, 'calibration': {'perform': (('requested', 'failed'), 'passed'), 'approve': (('passed',), 'approved'), 'reject': (('failed',), 'rejected')}, 'method': {'validate_method': (('draft',), 'validated'), 'revoke_method': (('validated',), 'revoked')}, 'result': {'release': (('pending', 'blocked'), 'released'), 'block': (('pending',), 'blocked'), 'reanalyze': (('blocked',), 'pending')}, 'batch': {'restore': (('suspended',), 'open')}, 'qc_check': {}}
    CREATE_REQUIRED = {'instrument': ('name', 'serial'), 'calibration': ('instrument_id', 'requested_at'), 'method': ('name', 'version'), 'result': ('sample_id', 'measurement', 'batch_id', 'value', 'unit'), 'batch': ('batch_no', 'instrument_id', 'method_id'), 'qc_check': ('batch_id', 'standard_value', 'measured_value', 'allowed_deviation', 'operator', 'checked_at')}
    ACTION_REQUIRED = {('instrument', 'calibrate'): ('due_at', 'passed'), ('instrument', 'quarantine'): ('reason',), ('calibration', 'perform'): ('result', 'performed_at', 'uncertainty'), ('calibration', 'approve'): ('authorized_by',), ('calibration', 'reject'): ('reason',), ('method', 'validate_method'): ('parameters', 'instrument_ids'), ('method', 'revoke_method'): ('reason',), ('result', 'block'): ('reason',), ('result', 'reanalyze'): ('reason',)}
    CREATE_ROLES = {'instrument': ('admin', 'technician'), 'calibration': ('admin', 'metrology'), 'method': ('admin', 'authorizer'), 'result': ('admin', 'analyst'), 'batch': ('admin', 'analyst'), 'qc_check': ('admin', 'analyst')}
    ROLE_ACTIONS = {'send_calibration': ('admin', 'technician'), 'calibrate': ('admin', 'metrology'), 'quarantine': ('admin', 'metrology'), 'restore': ('admin', 'metrology'), 'perform': ('admin', 'metrology'), 'approve': ('admin', 'authorizer'), 'reject': ('admin', 'authorizer'), 'validate_method': ('admin', 'authorizer'), 'revoke_method': ('admin', 'authorizer'), 'release': ('admin', 'analyst'), 'block': ('admin', 'analyst'), 'reanalyze': ('admin', 'analyst'), ('batch', 'restore'): ('admin', 'authorizer')}

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind, data=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        hook = INITIAL_STATUS_HOOKS.get(kind)
        if hook and data is not None:
            return hook(data)
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
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
