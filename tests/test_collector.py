"""Fault-injection tests for receive -> durable commit -> checkpoint -> ACK."""
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs

from trajectory.collector import sync_device
from trajectory.dataset import Dataset, SCHEMA_VERSION


def uid():
    return str(uuid.uuid4())


def action_record(device_id):
    return {
        "schema_version": SCHEMA_VERSION, "record_type": "action", "record_id": uid(),
        "device": {"id": device_id}, "session_id": uid(),
        "task": {"id": uid(), "user_task": "Open fixture product", "subgoal": "Open details",
                 "stage": "navigation", "source": "fixture"},
        "action_id": uid(), "started_at": "2026-10-24T10:00:00Z", "finished_at": "2026-10-24T10:00:01Z",
        "before": {"fingerprint": "fixture-before", "package_name": "io.daxqo.trajectoryfixture",
                   "nodes": [], "width": 400, "height": 800, "training_allowed": True, "redacted": True},
        "after": {"fingerprint": "fixture-after", "package_name": "io.daxqo.trajectoryfixture",
                  "nodes": [], "width": 400, "height": 800, "training_allowed": True, "redacted": True},
        "action": {"type": "tap", "target": {}, "parameters": {}},
        "decision": {"source": "agent", "model": None, "tool_call_id": None,
                     "candidate_actions": [], "confidence": None},
        "result": {"status": "succeeded", "error_code": None, "verification": {"status": "unverified"}},
        "navigation": {}, "events": [], "metrics": {},
        "privacy": {"redacted": True, "excluded": False, "reasons": []},
    }


class FakePhone:
    def __init__(self, device_id=None, stream_id=None, rows=None):
        self.device_id = device_id or uid()
        self.stream_id = stream_id or uid()
        self.rows = list(rows or [])  # (sequence, record)
        self.serial = "fixture-serial"
        self.ack_calls = []
        self.fail_ack_after_apply = 0
        self.fail_batch = 0
        self.batch_identity = None
        self.batch_fault = None

    def request(self, path, body=None):
        if path == "/api/trajectory/status":
            return {"device_id": self.device_id, "stream_id": self.stream_id}
        if path.startswith("/api/trajectory/batch?"):
            if self.fail_batch:
                self.fail_batch -= 1
                raise ConnectionError("simulated disconnect")
            after = int(parse_qs(urlsplit(path).query)["after"][0])
            rows = [(seq, row) for seq, row in self.rows if seq > after][:20]
            end = rows[-1][0] if rows else after
            sequences = [seq for seq, _ in rows]
            echoed_after = after
            if self.batch_fault == "through_too_large":
                end += 1
            elif self.batch_fault == "unordered_sequences":
                rows = [(2, action_record(self.device_id)), (1, action_record(self.device_id))]
                sequences = [2, 1]
                end = 1
            elif self.batch_fault == "after_mismatch":
                echoed_after += 1
            device, stream = self.batch_identity or (self.device_id, self.stream_id)
            return {"device_id": device, "stream_id": stream, "after": echoed_after,
                    "sequences": sequences, "records": [row for _, row in rows], "through": end}
        if path == "/api/trajectory/ack":
            self.ack_calls.append(dict(body))
            if body.get("stream_id") != self.stream_id:
                raise ValueError("wrong stream ACK")
            through = body["through"]
            self.rows = [(seq, row) for seq, row in self.rows if seq > through]
            if self.fail_ack_after_apply:
                self.fail_ack_after_apply -= 1
                raise ConnectionError("ACK response lost after device accepted it")
            return {"acknowledged": through}
        raise AssertionError(f"Unexpected protocol request: {path}")


class CollectorProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dataset = Dataset(Path(self.temp.name) / "data")

    def tearDown(self):
        self.dataset.close()
        self.temp.cleanup()

    def test_local_commit_failure_never_acknowledges(self):
        phone = FakePhone(rows=[(1, action_record(""))])
        phone.rows[0][1]["device"]["id"] = phone.device_id
        with patch.object(self.dataset, "ingest", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                sync_device(phone, self.dataset)
        self.assertEqual(phone.ack_calls, [])
        self.assertEqual(self.dataset.cursor(phone.device_id, phone.stream_id), 0)

    def test_checkpoint_failure_never_acks_and_retry_commits_once(self):
        phone = FakePhone()
        phone.rows = [(1, action_record(phone.device_id))]
        real_checkpoint = self.dataset.checkpoint
        with patch.object(self.dataset, "checkpoint", side_effect=OSError("checkpoint disk error")):
            with self.assertRaisesRegex(OSError, "checkpoint"):
                sync_device(phone, self.dataset)
        self.assertEqual(phone.ack_calls, [])
        self.assertEqual(self.dataset.cursor(phone.device_id, phone.stream_id), 0)
        self.assertEqual(self.dataset.stats()["records"], 1)  # Durable commit happened before checkpoint.
        with patch.object(self.dataset, "checkpoint", wraps=real_checkpoint):
            result = sync_device(phone, self.dataset)
        self.assertEqual(result["inserted"], 0)
        self.assertEqual(result["duplicates"], 1)
        self.assertEqual(len(phone.ack_calls), 2)  # one ACK for each accepted batch attempt
        self.assertEqual(self.dataset.stats()["records"], 1)

    def test_ack_failure_after_device_acceptance_retries_without_duplicate(self):
        phone = FakePhone()
        phone.rows = [(1, action_record(phone.device_id))]
        phone.fail_ack_after_apply = 1
        with self.assertRaisesRegex(ConnectionError, "ACK response lost"):
            sync_device(phone, self.dataset)
        self.assertEqual(self.dataset.stats()["records"], 1)
        self.assertEqual(self.dataset.cursor(phone.device_id, phone.stream_id), 1)
        result = sync_device(phone, self.dataset)
        self.assertEqual(result["inserted"], 0)
        self.assertEqual(self.dataset.stats()["records"], 1)
        self.assertEqual(len(phone.ack_calls), 2)

    def test_cursor_is_scoped_to_both_device_and_stream(self):
        device_a, stream_a = uid(), uid()
        device_b, stream_b = uid(), uid()
        self.dataset.checkpoint(device_a, stream_a, 40)
        self.assertEqual(self.dataset.cursor(device_a, stream_a), 40)
        self.assertEqual(self.dataset.cursor(device_a, stream_b), 0)
        self.assertEqual(self.dataset.cursor(device_b, stream_a), 0)
        self.assertEqual(self.dataset.cursor(device_b, stream_b), 0)
        phone = FakePhone(device_b, stream_b, [(1, action_record(device_b))])
        result = sync_device(phone, self.dataset)
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(phone.ack_calls[0]["through"], 1)

    def test_batch_device_or_stream_mismatch_is_rejected_without_commit_or_ack(self):
        for mismatch in ("device", "stream"):
            phone = FakePhone(rows=[])
            phone.batch_identity = (uid(), phone.stream_id) if mismatch == "device" else (phone.device_id, uid())
            with self.subTest(mismatch=mismatch), self.assertRaisesRegex(ValueError, "refusing to acknowledge"):
                sync_device(phone, self.dataset)
            self.assertEqual(self.dataset.stats()["records"], 0)
            self.assertEqual(phone.ack_calls, [])

    def test_invalid_sequence_boundaries_and_after_echo_never_commit_or_ack(self):
        for fault in ("through_too_large", "unordered_sequences", "after_mismatch"):
            phone = FakePhone(rows=[(1, action_record("temporary"))])
            phone.rows[0][1]["device"]["id"] = phone.device_id
            phone.batch_fault = fault
            with self.subTest(fault=fault), self.assertRaises(ValueError):
                sync_device(phone, self.dataset)
            self.assertEqual(self.dataset.stats()["records"], 0)
            self.assertEqual(phone.ack_calls, [])

    def test_internal_sequence_gap_is_rejected_without_commit_or_ack(self):
        phone = FakePhone()
        phone.rows = [(1, action_record(phone.device_id)), (3, action_record(phone.device_id))]
        with self.assertRaisesRegex(ValueError, "Non-contiguous"):
            sync_device(phone, self.dataset)
        self.assertEqual(self.dataset.stats()["records"], 0)
        self.assertEqual(phone.ack_calls, [])

    def test_nonzero_cursor_requires_immediate_next_sequence(self):
        phone = FakePhone()
        self.dataset.checkpoint(phone.device_id, phone.stream_id, 5)
        phone.rows = [(7, action_record(phone.device_id))]
        with self.assertRaisesRegex(ValueError, "Non-contiguous"):
            sync_device(phone, self.dataset)
        self.assertEqual(self.dataset.stats()["records"], 0)
        self.assertEqual(phone.ack_calls, [])

    def test_new_dataset_records_existing_spool_start_sequence(self):
        phone = FakePhone()
        phone.rows = [(9, action_record(phone.device_id)), (10, action_record(phone.device_id))]
        result = sync_device(phone, self.dataset)
        self.assertEqual(result["starting_sequence"], 9)
        self.assertEqual(result["through"], 10)
        self.assertEqual(result["inserted"], 2)
        self.assertEqual([ack["through"] for ack in phone.ack_calls], [10, 10])

    def test_disconnect_then_new_connection_resumes_from_local_cursor(self):
        phone = FakePhone()
        phone.rows = [(1, action_record(phone.device_id))]
        phone.fail_batch = 1
        with self.assertRaisesRegex(ConnectionError, "disconnect"):
            sync_device(phone, self.dataset)
        self.assertEqual(self.dataset.cursor(phone.device_id, phone.stream_id), 0)
        self.assertEqual(phone.ack_calls, [])
        # A newly opened client sees the same durable phone queue and resumes safely.
        reconnected = FakePhone(device_id=phone.device_id, stream_id=phone.stream_id, rows=phone.rows)
        result = sync_device(reconnected, self.dataset)
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(self.dataset.cursor(phone.device_id, phone.stream_id), 1)
        self.assertEqual([ack["through"] for ack in reconnected.ack_calls], [1, 1])


if __name__ == "__main__":
    unittest.main()
