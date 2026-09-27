from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


REFERENCE_DATE = "2026-09-24"
QC_TYPES = ("routine", "retest")
QC_FAILURE_FIELDS = (
    "failed_qc_id",
    "failed_performer",
    "standard_value",
    "measured_value",
    "allowed_deviation",
    "deviation",
    "abs_deviation",
    "failed_at",
)


def calibration_current(due_at, as_of):
    return str(due_at) >= str(as_of)


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _require_instrument_method(instrument, method):
    if not instrument or instrument["status"] != "active":
        raise ValidationError("requires an active instrument")
    if not calibration_current(instrument["data"].get("due_at", ""), REFERENCE_DATE):
        raise ValidationError("instrument calibration is not current")
    if not method or method["status"] != "validated":
        raise ValidationError("requires a validated method")
    if instrument["id"] not in method["data"].get("instrument_ids", []):
        raise ValidationError("method is not validated for this instrument")


def _validate_calibration(actor, data, lookup):
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    if not instrument:
        raise ValidationError("instrument does not exist")


def _validate_perform(actor, entity, data, lookup):
    if data.get("result") not in ("passed", "failed"):
        raise ValidationError("calibration result must be passed or failed")
    if data.get("result") == "passed" and not data.get("due_at"):
        raise ValidationError("passed calibration requires due_at")


def _validate_batch_create(actor, data, lookup):
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    method = _find_one(lookup, "method", "id", data.get("method_id"))
    _require_instrument_method(instrument, method)
    for row in lookup("batch", "batch_no", data.get("batch_no")) or []:
        if row["data"].get("instrument_id") == data.get("instrument_id"):
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
    if not data.get("performed_at"):
        raise ValidationError("missing required field: performed_at")
    if data.get("performed_by") != actor.user_id:
        raise ValidationError("performed_by must match the acting user")
    qc_type = data.get("qc_type", "routine")
    if qc_type not in QC_TYPES:
        raise ValidationError("qc_type must be routine or retest")
    if batch["status"] == "active" and qc_type != "routine":
        raise ValidationError("retest qc can only be registered on a suspended batch")
    if batch["status"] == "suspended" and qc_type != "retest":
        raise ValidationError("batch suspended: only retest qc can be registered")


def _validate_result_create(actor, data, lookup):
    batch = _find_one(lookup, "batch", "id", data.get("batch_id"))
    if not batch:
        raise ValidationError("result requires an existing batch_id")
    if batch["status"] != "active":
        raise ValidationError("batch is suspended; new results are not admitted")


def _validate_result_release(actor, entity, data, lookup):
    batch = _find_one(lookup, "batch", "id", entity["data"].get("batch_id"))
    if not batch:
        raise ValidationError("result is not tied to a detection batch")
    if batch["status"] != "active":
        raise ValidationError("batch is suspended; release is not admitted")
    instrument = _find_one(lookup, "instrument", "id", batch["data"].get("instrument_id"))
    method = _find_one(lookup, "method", "id", batch["data"].get("method_id"))
    _require_instrument_method(instrument, method)
    value = data.get("value", entity["data"].get("value"))
    unit = data.get("unit", entity["data"].get("unit"))
    if value is None or value == "" or not unit:
        raise ValidationError("value and unit are required to release")
    return {
        "batch_id": batch["id"],
        "value": value,
        "unit": unit,
        "released_by": actor.user_id,
    }


def _validate_batch_recovery(actor, entity, data, lookup):
    # Only retests logged during the current suspension count.
    since = entity["data"].get("suspended_at", "")
    retests = [
        row
        for row in (lookup("qc_check", "batch_id", entity["id"]) or [])
        if row["data"].get("qc_type") == "retest"
        and (not since or row["created_at"] > since)
    ]
    last_two = retests[-2:]
    if len(last_two) < 2 or any(row["status"] != "passed" for row in last_two):
        raise ValidationError("recovery requires two consecutive passed retests")
    performers = {row["data"].get("performed_by") for row in last_two}
    if len(performers) != 1:
        raise ValidationError("two retests must be performed by the same executor")
    retester = performers.pop()
    if retester == entity["data"].get("failed_performer"):
        raise ValidationError("retest must be performed by another executor")
    if actor.user_id == retester:
        raise PermissionDenied("reviewer must differ from the retest executor")
    return {
        "reviewed_by": actor.user_id,
        "retested_by": retester,
        "recovery_qc_ids": [row["id"] for row in last_two],
    }


CUSTOM_CREATE = {
    "calibration": _validate_calibration,
    "batch": _validate_batch_create,
    "qc_check": _validate_qc_create,
    "result": _validate_result_create,
}
CUSTOM_TRANSITIONS = {
    ("calibration", "perform"): _validate_perform,
    ("result", "release"): _validate_result_release,
    ("batch", "review_recovery"): _validate_batch_recovery,
}


class RuleEngine:
    ALIASES = {
        "instruments": "instrument",
        "calibrations": "calibration",
        "methods": "method",
        "results": "result",
        "batches": "batch",
        "qc_checks": "qc_check",
    }
    INITIAL_STATUS = {
        "instrument": "active",
        "calibration": "requested",
        "method": "draft",
        "result": "pending",
        "batch": "active",
        "qc_check": "registered",
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
        "result": {
            "release": (("pending", "returned"), "released"),
            "block": (("pending",), "blocked"),
            "reanalyze": (("blocked",), "pending"),
            "return_for_qc": (("pending",), "returned"),
        },
        "batch": {
            "suspend": (("active",), "suspended"),
            "review_recovery": (("suspended",), "active"),
        },
    }
    CREATE_REQUIRED = {
        "instrument": ("name", "serial"),
        "calibration": ("instrument_id", "requested_at"),
        "method": ("name", "version"),
        "result": ("sample_id", "measurement", "batch_id"),
        "batch": ("batch_no", "instrument_id", "method_id"),
        "qc_check": (
            "batch_id",
            "standard_value",
            "measured_value",
            "allowed_deviation",
            "performed_by",
            "performed_at",
        ),
    }
    ACTION_REQUIRED = {
        ("instrument", "calibrate"): ("due_at", "passed"),
        ("instrument", "quarantine"): ("reason",),
        ("calibration", "perform"): ("result", "performed_at", "uncertainty"),
        ("calibration", "approve"): ("authorized_by",),
        ("calibration", "reject"): ("reason",),
        ("method", "validate_method"): ("parameters", "instrument_ids"),
        ("method", "revoke_method"): ("reason",),
        ("result", "block"): ("reason",),
        ("result", "reanalyze"): ("reason",),
        ("result", "return_for_qc"): ("returned_reason",),
        ("batch", "suspend"): ("reason",),
    }
    CREATE_ROLES = {
        "instrument": ("admin", "technician"),
        "calibration": ("admin", "metrology"),
        "method": ("admin", "authorizer"),
        "result": ("admin", "analyst"),
        "batch": ("admin", "analyst"),
        "qc_check": ("admin", "analyst"),
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
        "release": ("admin", "analyst"),
        "block": ("admin", "analyst"),
        "reanalyze": ("admin", "analyst"),
        "return_for_qc": ("admin", "analyst"),
        "suspend": ("admin", "metrology", "authorizer", "analyst"),
        "review_recovery": ("admin", "authorizer"),
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def qc_evaluate(data):
        """Deviation of a QC reading: |measured - standard| must stay within tolerance."""
        deviation = float(data["measured_value"]) - float(data["standard_value"])
        within = abs(deviation) <= float(data["allowed_deviation"])
        return {
            "status": "passed" if within else "failed",
            "deviation": deviation,
            "abs_deviation": abs(deviation),
            "within_tolerance": within,
        }

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


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
