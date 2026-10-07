import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict
from src.repository import Repository


CASE_OFFICER = Actor("officer", "case_officer")
INTAKE = Actor("intake", "intake_officer")
LEGAL = Actor("legal", "legal_rep")


def case(reference, is_main=False, main_reference="", sponsor_id="S1", required=None):
    return {
        "reference": reference,
        "is_main": is_main,
        "main_reference": main_reference,
        "data": {
            "applicant_id": reference,
            "case_type": "family",
            "received_day": 1,
            "deadline_days": 30,
            "response_day": 1,
            "representation_active": True,
            "sponsor_id": sponsor_id,
            "required_documents": required or ["passport"],
        },
    }


def batch_body(cases, receipts, token, expected_version=0, quota_limit=10, quota_date="2026-10-07"):
    return {
        "save_token": token,
        "expected_version": expected_version,
        "quota_date": quota_date,
        "quota_limit": quota_limit,
        "cases": cases,
        "receipts": receipts,
    }


def receipt(number, docs, refs, issued_day=1):
    return {
        "receipt_number": number,
        "issued_day": issued_day,
        "documents": docs,
        "fields": {"channel": "counter"},
        "case_references": refs,
    }


class BatchWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "test.db")
        self.service = build_service(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def _create_family_batch(self):
        return self.service.create_batch(
            CASE_OFFICER,
            batch_body(
                [
                    case("MAIN", is_main=True),
                    case("DEP-1", main_reference="MAIN"),
                    case("DEP-2", main_reference="MAIN"),
                ],
                [receipt("R-1", ["passport", "sponsor_letter"], ["MAIN", "DEP-1", "DEP-2"])],
                "token-create",
            ),
        )

    def test_batch_version_shared_and_repeated_receipt_only_patches(self):
        created = self._create_family_batch()
        batch_no = created["batch_no"]
        self.assertEqual(created["version"], 1)
        self.assertEqual(len(created["snapshot"]["quota"]["receipt_numbers"]), 1)

        snapshot = self.service.get_batch(CASE_OFFICER, batch_no)
        result = self.service.save_batch(
            CASE_OFFICER,
            batch_no,
            batch_body(
                [
                    case("MAIN", is_main=True),
                    case("DEP-1", main_reference="MAIN"),
                    case("DEP-2", main_reference="MAIN"),
                ],
                [receipt("R-1", ["income_proof"], ["MAIN"], issued_day=2)],
                "token-patch",
                expected_version=snapshot["version"],
            ),
        )
        self.assertEqual(result["version"], 2)
        saved_receipt = next(item for item in result["snapshot"]["receipts"] if item["receipt_number"] == "R-1")
        self.assertEqual(saved_receipt["payload"]["documents"], ["passport", "sponsor_letter", "income_proof"])
        self.assertEqual(result["snapshot"]["quota"]["reserved"], 1)
        self.assertEqual(result["snapshot"]["quota"]["receipt_numbers"], ["R-1"])
        self.assertTrue(all(item["version"] == 2 for item in result["snapshot"]["cases"]))

    def test_main_fact_change_invalidates_pending_and_reviews_decided(self):
        created = self._create_family_batch()
        batch_no = created["batch_no"]

        batch_id = created["snapshot"]["id"]
        dep1 = self.service.repository.get_by_reference(batch_id, "DEP-1")
        dep2 = self.service.repository.get_by_reference(batch_id, "DEP-2")
        dep1 = self.service.act(LEGAL, dep1["id"], 1, "submit", {"documents": ["passport", "sponsor_letter"]})
        dep1 = self.service.act(CASE_OFFICER, dep1["id"], dep1["version"], "decide", {"decision": "granted", "decision_reason": "材料充分"})

        result = self.service.save_batch(
            CASE_OFFICER,
            batch_no,
            batch_body(
                [
                    case("MAIN", is_main=True, sponsor_id="S2"),
                    case("DEP-1", main_reference="MAIN", sponsor_id="S2"),
                    case("DEP-2", main_reference="MAIN", sponsor_id="S2"),
                ],
                [receipt("R-1", ["passport", "sponsor_letter"], ["MAIN", "DEP-1", "DEP-2"])],
                "token-sponsor-change",
                expected_version=dep1["version"],
            ),
        )
        cases = {item["reference"]: item for item in result["snapshot"]["cases"]}
        self.assertEqual(cases["DEP-2"]["state"], "draft")
        self.assertEqual(cases["DEP-2"]["basis_version"], result["version"])
        self.assertIn("sponsor_letter", cases["DEP-2"]["payload"]["stale_documents"])
        self.assertEqual(cases["DEP-2"]["payload"]["missing_documents"], ["sponsor_letter"])
        self.assertEqual(cases["DEP-1"]["state"], "decided")
        self.assertEqual(cases["DEP-1"]["review_status"], "review_pending")
        self.assertEqual(cases["DEP-1"]["payload"].get("sponsor_id"), "S1")
        actions = [event["action"] for event in self.service.batch_timeline(CASE_OFFICER, batch_no)]
        self.assertIn("todos_invalidated", actions)
        self.assertIn("review_required", actions)

    def test_concurrent_save_only_one_wins(self):
        created = self._create_family_batch()
        batch_no = created["batch_no"]

        def body(token):
            return batch_body(
                [case("MAIN", is_main=True), case("DEP-1", main_reference="MAIN"), case("DEP-2", main_reference="MAIN")],
                [],
                token,
                expected_version=1,
            )

        first = self.service.save_batch(CASE_OFFICER, batch_no, body("token-first"))
        self.assertEqual(first["version"], 2)
        with self.assertRaises(Conflict) as caught:
            self.service.save_batch(CASE_OFFICER, batch_no, body("token-second"))
        self.assertEqual(caught.exception.details["current_version"], 2)
        self.assertEqual(caught.exception.details["snapshot"]["version"], 2)

    def test_failed_full_batch_rollback_then_idempotent_retry(self):
        created = self._create_family_batch()
        batch_no = created["batch_no"]
        other_body = batch_body(
            [case("OTHER", is_main=True, required=["passport"])],
            [receipt("R-SHARED", ["passport"], ["OTHER"])],
            "token-other",
        )
        other = self.service.create_batch(CASE_OFFICER, other_body)
        replay = self.service.create_batch(CASE_OFFICER, other_body)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["batch_no"], other["batch_no"])

        failed_cases = [case("MAIN", is_main=True), case("DEP-1", main_reference="MAIN"), case("DEP-2", main_reference="MAIN")]
        failed_body = batch_body(
            failed_cases,
            [
                receipt("R-SHARED", ["passport"], ["MAIN"]),
                receipt("R-RETRY", ["passport"], ["MAIN"]),
            ],
            "token-failure-retry",
            expected_version=1,
        )
        with self.assertRaises(Conflict):
            self.service.save_batch(CASE_OFFICER, batch_no, failed_body)

        recovered_body = batch_body(failed_cases, [receipt("R-RETRY", ["passport"], ["MAIN"])], "token-failure-retry", expected_version=1)
        result = self.service.save_batch(CASE_OFFICER, batch_no, recovered_body)
        self.assertEqual(result["snapshot"]["quota"]["receipt_numbers"], ["R-1", "R-RETRY"])
        # Using the same full-batch token again must not reserve quota or write audit again.
        replay = self.service.save_batch(CASE_OFFICER, batch_no, recovered_body)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["snapshot"]["quota"]["receipt_numbers"], ["R-1", "R-RETRY"])
        timeline = self.service.batch_timeline(CASE_OFFICER, batch_no)
        self.assertEqual(len([event for event in timeline if event["action"] == "quota_reserved"]), 2)

    def test_legacy_backfill_owner_and_pending_verification(self):
        # 先模拟缺批次字段的历史结构。
        old_db = str(Path(self.temp.name) / "old.db")
        with sqlite3.connect(old_db) as connection:
            connection.executescript(
                """
                CREATE TABLE records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            connection.execute(
                """
                INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                ("OLD-OK", "draft", 1, "{}", "owner-a", "owner-a", "2026-09-01T10:00:00+00:00", "2026-09-01T10:00:00+00:00"),
            )
            connection.execute(
                """
                INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                ("OLD-MISSING", "draft", 1, "{}", "", "", "", ""),
            )
        # 构造仓储时迁移旧唯一索引，并回填历史批次。
        Repository(old_db)
        service = build_service(old_db)
        with sqlite3.connect(old_db) as connection:
            connection.row_factory = sqlite3.Row
            ok = connection.execute("SELECT * FROM records WHERE reference='OLD-OK'").fetchone()
            missing = connection.execute("SELECT * FROM records WHERE reference='OLD-MISSING'").fetchone()
            ok_batch = connection.execute("SELECT * FROM batches WHERE id=?", (ok["batch_id"],)).fetchone()
            missing_batch = connection.execute("SELECT * FROM batches WHERE id=?", (missing["batch_id"],)).fetchone()
        self.assertEqual(ok_batch["status"], "ready")
        self.assertEqual(ok_batch["created_by"], "owner-a")
        self.assertEqual(ok_batch["quota_date"], "2026-09-01")
        self.assertEqual(missing["state"], "pending_verification")
        self.assertEqual(missing_batch["status"], "pending_verification")

        # 旧 reference 全局唯一约束迁移后，不同批次允许相同引用。
        another = service.create_batch(
            CASE_OFFICER,
            batch_body([case("OLD-OK", is_main=True, required=["passport"])], [], "token-old-schema"),
        )
        self.assertEqual(another["snapshot"]["cases"][0]["reference"], "OLD-OK")
