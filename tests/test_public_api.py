"""A foreign harness can record and retry observations without a phone runtime."""
import json
import tempfile
import unittest
from pathlib import Path

from trajectory import Dataset, Recorder, snapshot

DEVICE = "b683d45a-e298-40b3-90c2-3bcd8f59cb84"
SESSION = "7468c2e4-dae4-4d3b-b959-b6e671f6f951"


def action():
    return dict(task_id="outside-task", action_id="outside-action", action={"type": "click"},
                before=snapshot("demo.app", [{"text": "Details"}]),
                after=snapshot("demo.app", [{"text": "Demo details"}]), status="succeeded",
                started_at="2026-10-24T10:00:00Z", finished_at="2026-10-24T10:00:01Z")


class PublicAPITests(unittest.TestCase):
    def test_independent_recording_is_durable_and_retryable_across_sessions(self):
        with tempfile.TemporaryDirectory() as temporary:
            with Dataset(Path(temporary)) as dataset:
                writer = Recorder(dataset, device_id=DEVICE, session_id=SESSION, source="another_harness")
                first = writer.record_action(**action())
                self.assertEqual(first["inserted"], 1)
            with Dataset(Path(temporary)) as dataset:
                writer = Recorder(dataset, device_id=DEVICE, session_id=SESSION, source="another_harness")
                self.assertEqual(writer.record_action(**action())["duplicates"], 1)
                records = dataset.records()
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["task"]["source"], "another_harness")
                self.assertEqual(records[0]["result"]["verification"]["source"], "agent_claim")
                self.assertEqual(records[0]["result"]["verification"]["status"], "unverified")

    def test_public_ingress_keeps_existing_privacy_and_hidden_reasoning_rules(self):
        with tempfile.TemporaryDirectory() as temporary, Dataset(Path(temporary)) as dataset:
            writer = Recorder(dataset, device_id=DEVICE, source="another_harness")
            request = action()
            request["action"]["parameters"] = {"text": "private-input"}
            request["candidate_actions"] = [{"type": "click", "hidden_reasoning": "PRIVATE_REASONING"}]
            request["before"] = snapshot("demo.app", [{"editable": True, "text": "private-input"}])
            writer.record_action(**request)
            serialized = json.dumps(dataset.records())
            self.assertNotIn("private-input", serialized)
            self.assertNotIn("PRIVATE_REASONING", serialized)
            self.assertNotIn("hidden_reasoning", serialized)


if __name__ == "__main__":
    unittest.main()
