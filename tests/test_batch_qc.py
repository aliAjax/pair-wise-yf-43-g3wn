import tempfile
import unittest
from pathlib import Path

from src.domain import (
    Actor,
    ConflictError,
    PermissionDenied,
    ValidationError,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class BatchQCTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.analyst_a = Actor("analyst-a", "analyst")
        self.analyst_b = Actor("analyst-b", "analyst")
        self.authorizer = Actor("qa-1", "authorizer")
        self._setup_batch()

    def tearDown(self):
        self.tmp.cleanup()

    def _setup_batch(self, batch_no="B-001"):
        self.instrument = self.service.create(
            self.admin, "instrument", {"name": "HPLC", "serial": "H-1"}
        )
        self.service.transition(
            self.admin,
            self.instrument["id"],
            "send_calibration",
            {},
        )
        self.service.transition(
            self.admin,
            self.instrument["id"],
            "calibrate",
            {"due_at": "2099-01-01", "passed": True},
        )
        self.method = self.service.create(
            self.admin, "method", {"name": "Assay", "version": "v1"}
        )
        self.service.transition(
            self.admin,
            self.method["id"],
            "validate_method",
            {"parameters": {"range": [0, 100]}, "instrument_ids": [self.instrument["id"]]},
        )
        self.batch = self.service.create(
            self.admin,
            "batch",
            {
                "batch_no": batch_no,
                "instrument_id": self.instrument["id"],
                "method_id": self.method["id"],
            },
        )

    def _qc(self, actor, standard, measured, tolerance, qc_type="routine", at="2026-09-27T09:00:00"):
        return self.service.create(
            actor,
            "qc_check",
            {
                "batch_id": self.batch["id"],
                "standard_value": standard,
                "measured_value": measured,
                "allowed_deviation": tolerance,
                "performed_by": actor.user_id,
                "performed_at": at,
                "qc_type": qc_type,
            },
        )

    def _result(self, sample_id):
        return self.service.create(
            self.analyst_a,
            "result",
            {"sample_id": sample_id, "measurement": "raw", "batch_id": self.batch["id"]},
        )

    def test_batch_number_instrument_unique(self):
        with self.assertRaises(ConflictError):
            self.service.create(
                self.admin,
                "batch",
                {
                    "batch_no": "B-001",
                    "instrument_id": self.instrument["id"],
                    "method_id": self.method["id"],
                },
            )
        # Same batch number on another instrument is allowed.
        other = self.service.create(
            self.admin, "instrument", {"name": "GC", "serial": "G-1"}
        )
        self.service.transition(self.admin, other["id"], "send_calibration", {})
        self.service.transition(
            self.admin, other["id"], "calibrate", {"due_at": "2099-01-01", "passed": True}
        )
        shared_method = self.service.create(
            self.admin, "method", {"name": "Assay", "version": "v2"}
        )
        self.service.transition(
            self.admin,
            shared_method["id"],
            "validate_method",
            {"parameters": {"range": [0, 100]}, "instrument_ids": [self.instrument["id"], other["id"]]},
        )
        second = self.service.create(
            self.admin,
            "batch",
            {"batch_no": "B-001", "instrument_id": other["id"], "method_id": shared_method["id"]},
        )
        self.assertEqual(second["status"], "active")

    def test_passing_qc_keeps_batch_admitting(self):
        qc = self._qc(self.analyst_a, 10.0, 10.05, 0.1)
        self.assertEqual(qc["status"], "passed")
        self.assertAlmostEqual(qc["data"]["deviation"], 0.05)
        batch = self.service.get(self.batch["id"])
        self.assertEqual(batch["status"], "active")

    def test_failed_qc_suspends_batch_and_returns_pending_results(self):
        result = self._result("S-1")
        released = self._result("S-2")
        self.service.transition(
            self.analyst_a, released["id"], "release", {"value": 1.0, "unit": "mg/L"}
        )
        qc = self._qc(self.analyst_a, 10.0, 10.5, 0.1)
        self.assertEqual(qc["status"], "failed")

        batch = self.service.get(self.batch["id"])
        self.assertEqual(batch["status"], "suspended")
        self.assertEqual(batch["data"]["failed_performer"], "analyst-a")
        self.assertAlmostEqual(batch["data"]["deviation"], 0.5)

        returned = self.service.get(result["id"])
        self.assertEqual(returned["status"], "returned")
        self.assertAlmostEqual(returned["data"]["deviation"], 0.5)
        self.assertEqual(returned["data"]["failed_qc_id"], qc["id"])

        # An already released result keeps its status.
        self.assertEqual(self.service.get(released["id"])["status"], "released")

        # No new results are admitted.
        with self.assertRaises(ValidationError):
            self._result("S-3")
        # The returned result cannot be released while suspended.
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.analyst_b, returned["id"], "release", {}
            )

    def test_retest_must_be_another_executor(self):
        self._result("S-1")
        self._qc(self.analyst_a, 10.0, 10.5, 0.1)
        # Same executor retesting fails recovery review.
        self._qc(self.analyst_a, 10.0, 10.01, 0.1, qc_type="retest", at="2026-09-27T10:00:00")
        self._qc(self.analyst_a, 10.0, 10.02, 0.1, qc_type="retest", at="2026-09-27T11:00:00")
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.authorizer, self.batch["id"], "review_recovery", {}
            )

    def test_two_consecutive_passes_and_review_recovers_batch(self):
        result = self._result("S-1")
        self._qc(self.analyst_a, 10.0, 10.5, 0.1)
        # A failed retest breaks the streak.
        self._qc(self.analyst_b, 10.0, 10.9, 0.1, qc_type="retest", at="2026-09-27T10:00:00")
        self._qc(self.analyst_b, 10.0, 10.01, 0.1, qc_type="retest", at="2026-09-27T11:00:00")
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.authorizer, self.batch["id"], "review_recovery", {}
            )
        # Two consecutive passes by another executor.
        self._qc(self.analyst_b, 10.0, 10.02, 0.1, qc_type="retest", at="2026-09-27T12:00:00")
        # The retester cannot review their own work.
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.analyst_b, self.batch["id"], "review_recovery", {}
            )
        batch = self.service.transition(
            self.authorizer, self.batch["id"], "review_recovery", {}
        )
        self.assertEqual(batch["status"], "active")
        self.assertEqual(batch["data"]["reviewed_by"], "qa-1")
        self.assertEqual(batch["data"]["retested_by"], "analyst-b")
        self.assertEqual(len(batch["data"]["recovery_qc_ids"]), 2)

        # The original returned result is re-released under its original record.
        original = self.service.get(result["id"])
        self.service.transition(
            self.analyst_a, result["id"], "release", {"value": 4.2, "unit": "mg/L"}
        )
        re_released = self.service.get(result["id"])
        self.assertEqual(re_released["status"], "released")
        self.assertEqual(re_released["data"]["value"], 4.2)
        self.assertEqual(re_released["data"]["returned_reason"], original["data"]["returned_reason"])

    def test_recovery_requires_two_passes(self):
        self._result("S-1")
        self._qc(self.analyst_a, 10.0, 10.5, 0.1)
        self._qc(self.analyst_b, 10.0, 10.01, 0.1, qc_type="retest", at="2026-09-27T10:00:00")
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.authorizer, self.batch["id"], "review_recovery", {}
            )

    def test_only_retest_qc_while_suspended(self):
        self._result("S-1")
        self._qc(self.analyst_a, 10.0, 10.5, 0.1)
        with self.assertRaises(ValidationError):
            self._qc(self.analyst_b, 10.0, 10.01, 0.1, qc_type="routine", at="2026-09-27T10:00:00")

    def test_manual_suspend_blocks_release(self):
        result = self._result("S-1")
        self.service.transition(
            self.authorizer, self.batch["id"], "suspend", {"reason": "maintenance"}
        )
        returned = self.service.get(result["id"])
        self.assertEqual(returned["status"], "returned")
        with self.assertRaises(ValidationError):
            # No two consecutive passed retests yet.
            self.service.transition(
                self.authorizer, self.batch["id"], "review_recovery", {}
            )

    def test_performed_by_must_match_actor(self):
        with self.assertRaises(ValidationError):
            self.service.create(
                self.analyst_b,
                "qc_check",
                {
                    "batch_id": self.batch["id"],
                    "standard_value": 10.0,
                    "measured_value": 10.0,
                    "allowed_deviation": 0.1,
                    "performed_by": "someone-else",
                    "performed_at": "2026-09-27T09:00:00",
                },
            )

    def test_qc_history_lists_entries(self):
        self._qc(self.analyst_a, 10.0, 10.01, 0.1, at="2026-09-27T09:00:00")
        self._qc(self.analyst_a, 10.0, 10.02, 0.1, at="2026-09-27T09:30:00")
        history = [
            row
            for row in self.service.list("qc_check")
            if row["data"]["batch_id"] == self.batch["id"]
        ]
        self.assertEqual(len(history), 2)
        self.assertTrue(all(row["status"] == "passed" for row in history))


if __name__ == "__main__":
    unittest.main()
