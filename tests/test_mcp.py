import io
import json
from pathlib import Path
import tempfile
import unittest

from trajectory import Dataset, Recorder
from trajectory.mcp_server import CollectorMCP
from test_public_api import DEVICE, SESSION


class MCPTests(unittest.TestCase):
    def test_stdio_host_can_negotiate_record_retry_query_and_export(self):
        with tempfile.TemporaryDirectory() as temporary, Dataset(Path(temporary) / "data") as dataset:
            server = CollectorMCP(Recorder(dataset, device_id=DEVICE, session_id=SESSION), Path(temporary) / "exports")
            args = {"task_id": "foreign-task", "action_id": "foreign-action", "action": {"type": "click"},
                    "before": {"package_name": "demo.app", "nodes": []},
                    "after": {"package_name": "demo.app", "nodes": []}, "status": "succeeded",
                    "started_at": "2026-10-24T10:00:00Z", "finished_at": "2026-10-24T10:00:01Z"}
            requests = [
                {"method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
                {"method": "tools/list"},
                {"method": "tools/call", "params": {"name": "trajectory_record", "arguments": args}},
                {"method": "tools/call", "params": {"name": "trajectory_record", "arguments": args}},
                {"method": "tools/call", "params": {"name": "trajectory_stats"}},
                {"method": "tools/call", "params": {"name": "trajectory_export", "arguments": {"name": "sample"}}},
            ]
            source = io.BytesIO(b"\n".join(json.dumps({"jsonrpc": "2.0", "id": i + 1, **request}).encode()
                                         for i, request in enumerate(requests)) + b"\n")
            output = io.BytesIO()
            server.serve(source, output)
            responses = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(responses[0]["result"]["protocolVersion"], "2025-11-25")
            self.assertEqual(len(responses[1]["result"]["tools"]), 3)
            receipt = lambda i: json.loads(responses[i]["result"]["content"][0]["text"])
            self.assertEqual(receipt(2)["inserted"], 1)
            self.assertEqual(receipt(3)["duplicates"], 1)
            self.assertTrue((Path(temporary) / "exports/sample/manifest.json").is_file())

    def test_tools_cannot_claim_human_verification_or_escape_export_root(self):
        with tempfile.TemporaryDirectory() as temporary, Dataset(Path(temporary) / "data") as dataset:
            server = CollectorMCP(Recorder(dataset, device_id=DEVICE), Path(temporary) / "exports")
            for name, arguments in (("trajectory_export", {"name": "../escape"}),
                                    ("trajectory_record", {"verification": {"source": "human_review"}})):
                response = server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                            "params": {"name": name, "arguments": arguments}})
                self.assertTrue(response["result"]["isError"])
            self.assertFalse((Path(temporary) / "escape").exists())
            self.assertEqual(dataset.records(), [])


if __name__ == "__main__":
    unittest.main()
