import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class BatchQcTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.analyst = Actor("analyst-1", "analyst")
        self.authorizer = Actor("auth-1", "authorizer")
        self.instrument = self.service.create(
            self.admin, "instrument", {"name": "Analyzer", "serial": "A-1"}
        )
        self.service.transition(self.admin, self.instrument["id"], "send_calibration", {})
        self.service.transition(
            self.admin, self.instrument["id"], "calibrate",
            {"due_at": "2099-01-01", "passed": True},
        )
        self.method = self.service.create(
            self.admin, "method", {"name": "Assay-A", "version": "v1"}
        )
        self.service.transition(
            self.admin, self.method["id"], "validate_method",
            {"parameters": {"range": [0, 10]}, "instrument_ids": [self.instrument["id"]]},
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _batch(self, batch_no="B-001"):
        return self.service.create(
            self.analyst, "batch",
            {"batch_no": batch_no, "instrument_id": self.instrument["id"], "method_id": self.method["id"]},
        )

    def _qc(self, batch_id, measured, operator, allowed=0.5, standard=10.0):
        return self.service.create(
            self.analyst, "qc_check",
            {"batch_id": batch_id, "standard_value": standard, "measured_value": measured,
             "allowed_deviation": allowed, "operator": operator,
             "checked_at": "2026-09-27T08:00:00+00:00"},
        )

    def _result(self, batch_id, sample_id="S-1"):
        return self.service.create(
            self.analyst, "result",
            {"sample_id": sample_id, "measurement": "m", "batch_id": batch_id,
             "value": 4.2, "unit": "mg/L"},
        )

    def test_qc_failure_suspends_batch_and_returns_pending_results(self):
        batch = self._batch()
        first = self._result(batch["id"], "S-1")
        second = self._result(batch["id"], "S-2")
        released = self._result(batch["id"], "S-3")
        self.service.transition(self.analyst, released["id"], "release", {})

        qc = self._qc(batch["id"], 11.0, "op-1")
        self.assertEqual(qc["status"], "failed")
        self.assertEqual(qc["data"]["deviation"], 1.0)

        batch = self.service.get(batch["id"])
        self.assertEqual(batch["status"], "suspended")
        self.assertEqual(batch["data"]["qc_failed_operator"], "op-1")
        self.assertEqual(batch["data"]["suspended_reason"], "qc_limit_exceeded")

        for result_id in (first["id"], second["id"]):
            result = self.service.get(result_id)
            self.assertEqual(result["status"], "blocked")
            deviation = result["data"]["qc_deviation"]
            self.assertEqual(deviation["qc_check_id"], qc["id"])
            self.assertEqual(deviation["deviation"], 1.0)
            self.assertEqual(deviation["allowed_deviation"], 0.5)
            self.assertEqual(deviation["operator"], "op-1")

        self.assertEqual(self.service.get(released["id"])["status"], "released")

        with self.assertRaises(ValidationError):
            self.service.transition(self.analyst, first["id"], "release", {})
        with self.assertRaises(ValidationError):
            self._result(batch["id"], "S-4")

    def test_retest_requires_another_operator_and_two_consecutive_passes(self):
        batch = self._batch()
        result = self._result(batch["id"])
        self._qc(batch["id"], 11.0, "op-1")

        with self.assertRaises(ValidationError):
            self._qc(batch["id"], 10.1, "op-1")

        qc1 = self._qc(batch["id"], 10.1, "op-2")
        self.assertEqual(qc1["status"], "passed")
        batch = self.service.get(batch["id"])
        self.assertEqual(batch["status"], "suspended")
        self.assertEqual(batch["data"]["retest_passes"], 1)

        with self.assertRaises(ValidationError):
            self.service.transition(self.authorizer, batch["id"], "restore", {})

        self._qc(batch["id"], 10.1, "op-3")
        batch = self.service.get(batch["id"])
        self.assertEqual(batch["data"]["retest_passes"], 1)
        self.assertEqual(batch["data"]["retest_operator"], "op-3")

        self._qc(batch["id"], 10.2, "op-3")
        batch = self.service.get(batch["id"])
        self.assertEqual(batch["data"]["retest_passes"], 2)

        batch = self.service.transition(self.authorizer, batch["id"], "restore", {})
        self.assertEqual(batch["status"], "open")
        self.assertEqual(batch["data"]["reviewed_by"], "auth-1")

        released = self.service.transition(self.analyst, result["id"], "release", {})
        self.assertEqual(released["status"], "released")
        self.assertEqual(released["data"]["value"], 4.2)
        self.assertEqual(released["data"]["unit"], "mg/L")
        self.assertEqual(released["data"]["instrument_id"], self.instrument["id"])

    def test_retest_failure_resets_series_and_failed_operator(self):
        batch = self._batch()
        self._qc(batch["id"], 11.0, "op-1")
        self._qc(batch["id"], 10.1, "op-2")
        qc = self._qc(batch["id"], 12.0, "op-2")
        self.assertEqual(qc["status"], "failed")

        batch = self.service.get(batch["id"])
        self.assertEqual(batch["status"], "suspended")
        self.assertEqual(batch["data"]["retest_passes"], 0)
        self.assertEqual(batch["data"]["qc_failed_operator"], "op-2")

        with self.assertRaises(ValidationError):
            self._qc(batch["id"], 10.1, "op-2")

        self._qc(batch["id"], 10.1, "op-1")
        self._qc(batch["id"], 10.1, "op-1")
        batch = self.service.transition(self.authorizer, batch["id"], "restore", {})
        self.assertEqual(batch["status"], "open")

    def test_batch_number_and_instrument_combination_is_unique(self):
        self._batch("B-100")
        with self.assertRaises(ConflictError):
            self._batch("B-100")

        other = self.service.create(
            self.admin, "instrument", {"name": "Analyzer-2", "serial": "A-2"}
        )
        self.service.transition(self.admin, other["id"], "send_calibration", {})
        self.service.transition(
            self.admin, other["id"], "calibrate",
            {"due_at": "2099-01-01", "passed": True},
        )
        method2 = self.service.create(
            self.admin, "method", {"name": "Assay-B", "version": "v1"}
        )
        self.service.transition(
            self.admin, method2["id"], "validate_method",
            {"parameters": {"range": [0, 5]}, "instrument_ids": [other["id"]]},
        )
        batch2 = self.service.create(
            self.analyst, "batch",
            {"batch_no": "B-100", "instrument_id": other["id"], "method_id": method2["id"]},
        )
        self.assertEqual(batch2["status"], "open")

    def test_batch_requires_valid_instrument_and_method(self):
        raw = self.service.create(
            self.admin, "instrument", {"name": "New", "serial": "N-1"}
        )
        with self.assertRaises(ValidationError):
            self.service.create(
                self.analyst, "batch",
                {"batch_no": "B-200", "instrument_id": raw["id"], "method_id": self.method["id"]},
            )

        draft = self.service.create(
            self.admin, "method", {"name": "Assay-C", "version": "v1"}
        )
        with self.assertRaises(ValidationError):
            self.service.create(
                self.analyst, "batch",
                {"batch_no": "B-201", "instrument_id": self.instrument["id"], "method_id": draft["id"]},
            )


if __name__ == "__main__":
    unittest.main()
