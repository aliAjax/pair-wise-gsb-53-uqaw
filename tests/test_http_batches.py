import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.http_api import create_server


def body(**kwargs):
    return kwargs


class HttpBatchTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service = build_service(str(Path(self.temp.name) / "http.db"))
        self.server = create_server("127.0.0.1", 0, service, Path(__file__).resolve().parents[1] / "static")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.temp.cleanup()

    def request(self, method, path, payload=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        data = json.dumps(payload or {}).encode("utf-8")
        headers = {"X-User-Id": "officer", "X-Role": "case_officer", "Content-Type": "application/json"}
        connection.request(method, path, data, headers)
        response = connection.getresponse()
        parsed = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, parsed

    def test_create_and_save_batch(self):
        payload = body(
            save_token="http-token-1",
            quota_date="2026-10-07",
            quota_limit=5,
            cases=[
                {
                    "reference": "MAIN",
                    "is_main": True,
                    "data": {
                        "applicant_id": "A1",
                        "case_type": "family",
                        "received_day": 1,
                        "deadline_days": 30,
                        "response_day": 1,
                        "representation_active": True,
                        "sponsor_id": "S1",
                        "required_documents": ["passport"],
                    },
                }
            ],
            receipts=[
                {"receipt_number": "HTTP-R1", "issued_day": 1, "documents": ["passport", "sponsor_letter"], "case_references": ["MAIN"]}
            ],
        )
        status, created = self.request("POST", "/api/batches", payload)
        self.assertEqual(status, 201)
        self.assertEqual(created["version"], 1)

        payload["expected_version"] = 1
        payload["save_token"] = "http-token-2"
        payload["receipts"] = [
            {"receipt_number": "HTTP-R1", "issued_day": 2, "documents": ["income_proof"], "case_references": ["MAIN"]}
        ]
        status, saved = self.request("POST", "/api/batches/%s/save" % created["batch_no"], payload)
        self.assertEqual(status, 200)
        self.assertEqual(saved["version"], 2)
        receipt = saved["snapshot"]["receipts"][0]
        self.assertEqual(receipt["payload"]["documents"], ["passport", "sponsor_letter", "income_proof"])
        self.assertEqual(saved["snapshot"]["quota"]["reserved"], 1)
