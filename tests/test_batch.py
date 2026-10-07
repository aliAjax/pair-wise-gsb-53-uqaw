import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


def member(reference, role, applicant, **over):
    data = {
        "reference": reference, "role": role, "applicant_id": applicant,
        "case_type": "family", "received_day": 100, "deadline_days": 30,
        "response_day": 100, "representation_active": True,
        "required_documents": ["passport", "sponsor_letter"],
    }
    data.update(over)
    return data


class BatchTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "batch.db")
        self.service = build_service(self.db)
        self.officer = Actor("officer", "case_officer")
        self.intake = Actor("intake", "intake_officer")
        self.service.create_batch(self.intake, "J-1", {"application_id": "APP-1", "quota_per_day": 2})

    def tearDown(self):
        self.temp.cleanup()

    def _ids(self, batch_no="J-1"):
        return {m["reference"]: m for m in self.service.get_batch(self.officer, batch_no)["members"]}

    def test_first_save_shares_one_batch_version_for_case_receipt_quota_audit(self):
        result = self.service.save_batch(self.officer, "J-1", 0, "s1", {
            "members": [member("R-P", "primary", "A-1"), member("R-D", "dependent", "A-2")],
            "receipts": [{"receipt_no": "RC-1", "documents": ["passport"]}],
            "quota_day": 100,
        })
        self.assertEqual(result["version"], 1)
        batch = self.service.get_batch(self.officer, "J-1")
        self.assertTrue(all(m["batch_version"] == 1 for m in batch["members"]))
        self.assertEqual(batch["receipts"][0]["batch_version"], 1)
        self.assertEqual(batch["quota"], [{"day": 100, "amount": 1}])
        primary = self._ids()["R-P"]
        timeline = self.service.timeline(self.officer, primary["id"])
        self.assertEqual({e["batch_no"] for e in timeline}, {"J-1"})
        self.assertTrue(all(e["batch_version"] == 1 for e in timeline))

    def test_duplicate_receipt_appends_diff_only_and_keeps_quota(self):
        self.service.save_batch(self.officer, "J-1", 0, "s1", {
            "members": [member("R-P", "primary", "A-1")],
            "receipts": [{"receipt_no": "RC-1", "documents": ["passport"]}],
            "quota_day": 100,
        })
        result = self.service.save_batch(self.officer, "J-1", 1, "s2", {
            "receipts": [{"receipt_no": "RC-1", "documents": ["sponsor_letter"]}],
        })
        self.assertEqual(result["receipts"][0]["action"], "receipt_diff_append")
        self.assertEqual(result["quota"], [])
        receipt = self.service.get_batch(self.officer, "J-1")["receipts"][0]
        self.assertEqual(receipt["documents"], ["passport", "sponsor_letter"])
        self.assertEqual(self.service.quota_status(self.officer, 100)["used"], 1)

        # 完全重复：只留“重复无新增”痕，不新增名额
        again = self.service.save_batch(self.officer, "J-1", 2, "s3", {
            "receipts": [{"receipt_no": "RC-1", "documents": ["passport", "sponsor_letter"]}],
        })
        self.assertEqual(again["receipts"][0]["action"], "receipt_duplicate_noop")
        self.assertEqual(self.service.quota_status(self.officer, 100)["used"], 1)

    def test_primary_fact_change_invalidates_pending_and_reviews_decided(self):
        self.service.save_batch(self.officer, "J-1", 0, "s1", {
            "members": [member("R-P", "primary", "A-1"),
                        member("R-PEND", "dependent", "A-2"),
                        member("R-DEC", "dependent", "A-3"),
                        member("R-CLOSE", "dependent", "A-4")],
        })
        ids = self._ids()
        self.service.act(Actor("rep", "legal_rep"), ids["R-PEND"]["id"], 1, "submit",
                         {"documents": ["passport", "sponsor_letter"]})
        dec = ids["R-DEC"]
        self.service.act(Actor("rep", "legal_rep"), dec["id"], 1, "submit",
                         {"documents": ["passport", "sponsor_letter"]})
        self.service.act(Actor("co", "case_officer"), dec["id"], 2, "decide",
                         {"decision": "granted", "decision_reason": "ok"})
        close = ids["R-CLOSE"]
        self.service.act(Actor("rep", "legal_rep"), close["id"], 1, "submit",
                         {"documents": ["passport", "sponsor_letter"]})
        self.service.act(Actor("co", "case_officer"), close["id"], 2, "decide",
                         {"decision": "granted", "decision_reason": "ok"})
        self.service.act(Actor("sup", "supervisor"), close["id"], 3, "close", {"closure_note": "done"})

        current = self.service.get_batch(self.officer, "J-1")["version"]
        upd = member("R-P", "primary", "A-1", guarantor_id="G-NEW", deadline_days=45)
        result = self.service.save_batch(self.officer, "J-1", current, "s-fact", {"members": [upd]})
        actions = {c["reference"]: c["action"] for c in result["cascades"]}
        self.assertEqual(actions["R-PEND"], "recompute")
        self.assertEqual(actions["R-DEC"], "enter_review")
        self.assertNotIn("R-CLOSE", actions)

        ids = self._ids()
        pending = ids["R-PEND"]
        decided = ids["R-DEC"]
        closed = ids["R-CLOSE"]
        self.assertEqual(pending["state"], "submitted")
        self.assertEqual(pending["payload"]["deadline_day"], 145)  # 100+45 重算
        self.assertEqual(pending["payload"]["guarantor_id"], "G-NEW")
        self.assertEqual(decided["state"], "review")
        self.assertTrue(decided["payload"]["basis_snapshot"])          # 原依据保留
        self.assertEqual(decided["payload"]["basis_snapshot"]["state"], "decided")
        self.assertEqual(closed["state"], "closed")                    # 归档冻结

        timeline = {e["action"] for e in self.service.timeline(self.officer, pending["id"])}
        self.assertIn("fact_invalidated", timeline)
        self.assertIn("fact_recalculated", timeline)

    def test_concurrent_saves_only_one_version_lands(self):
        self.service.save_batch(self.officer, "J-1", 0, "s1", {
            "members": [member("R-P", "primary", "A-1")]})

        errors, winners = [], []

        def worker(save_id):
            try:
                winners.append(self.service.save_batch(
                    Actor("op-" + save_id, "case_officer"),
                    "J-1", 1, save_id, {"members": []})["version"])
            except Conflict as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=("t1",)),
                   threading.Thread(target=worker, args=("t2",))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(errors), 1)
        extra = errors[0].extra
        self.assertEqual(extra["code"], "batch_version_conflict")
        self.assertEqual(extra["current_version"], 2)
        self.assertIn("batch", extra)  # 失败者拿到完整批次用于合并保留填写内容

    def test_failed_save_rolls_back_and_retry_does_not_double_occupy(self):
        self.service.save_batch(self.officer, "J-1", 0, "s1", {
            "members": [member("R-P", "primary", "A-1")],
            "receipts": [{"receipt_no": "RC-1", "documents": ["passport"]}],
            "quota_day": 100,
        })
        before = self.service.get_batch(self.officer, "J-1")["version"]
        with self.assertRaises(ValidationError):
            self.service.save_batch(self.officer, "J-1", 1, "s-over", {
                "receipts": [{"receipt_no": "RC-X1", "documents": ["passport"]},
                             {"receipt_no": "RC-X2", "documents": ["passport"]}],
                "quota_day": 100,
            })
        self.assertEqual(self.service.get_batch(self.officer, "J-1")["version"], before)
        self.assertEqual(self.service.quota_status(self.officer, 100)["used"], 1)

        # 从完整批次恢复后重试：改用合法名额日，同一份业务可再次提交
        recovered = self.service.get_batch(self.officer, "J-1")
        retry = self.service.save_batch(self.officer, "J-1", before, "s-over", {
            "receipts": [{"receipt_no": "RC-X1", "documents": ["passport"]}],
            "quota_day": 101,
        })
        self.assertEqual(retry["version"], before + 1)
        self.assertEqual(self.service.quota_status(self.officer, 100)["used"], 1)
        self.assertEqual(self.service.quota_status(self.officer, 101)["used"], 1)

        # save_id 重试：回放首结果，不重复占名额/留痕
        replay = self.service.save_batch(self.officer, "J-1", before, "s-over", {
            "receipts": [{"receipt_no": "RC-X1", "documents": ["passport"]}],
            "quota_day": 101,
        })
        self.assertTrue(replay["replayed"])
        self.assertEqual(self.service.quota_status(self.officer, 101)["used"], 1)

    def test_legacy_rows_backfilled_else_pending_verification(self):
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at)"
            " VALUES('OLD-1','submitted',2,?,?,?,?,?)",
            (json.dumps({"applicant_id": "Z"}), "zhao", "zhao",
             "2026-02-01T00:00:00+00:00", "2026-02-02T00:00:00+00:00"))
        conn.execute(
            "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at)"
            " VALUES('OLD-2','draft',1,?,'','','','')",
            (json.dumps({"applicant_id": "W"}),))
        conn.commit()
        conn.close()

        service = build_service(self.db)  # 启动即回填
        actor = Actor("admin", "admin")
        good = service.get_record(actor, 100) if False else None
        records = {r["reference"]: r for r in service.list_records(actor, limit=500)}
        self.assertEqual(records["OLD-1"]["batch_no"], "LEGACY-zhao")
        self.assertEqual(records["OLD-1"]["batch_version"], 1)
        self.assertEqual(records["OLD-2"]["state"], "pending_verification")
        self.assertEqual(records["OLD-2"]["batch_no"], "LEGACY-UNVERIFIED")

        # 再次启动：回填幂等，不重复写留痕
        service2 = build_service(self.db)
        old1 = next(r for r in service2.list_records(actor, limit=500) if r["reference"] == "OLD-1")
        actions = [e["action"] for e in service2.timeline(actor, old1["id"])]
        self.assertEqual(actions.count("legacy_backfill"), 1)

    def test_role_permission_for_batch(self):
        with self.assertRaises(PermissionDenied):
            self.service.create_batch(Actor("x", "outsider"), "J-X", {"application_id": "a"})
