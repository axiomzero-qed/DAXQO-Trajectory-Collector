"""Local-only tests for correlating optional MCP feedback to native action IDs."""
import importlib.util
import json
import queue
import sys
import types
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch


HOOK_PATH = Path(__file__).resolve().parents[1] / "integration/backend/trajectory_hooks.py"
SPEC = importlib.util.spec_from_file_location("trajectory_hooks_test_module", HOOK_PATH)
hooks = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hooks)


def uid():
    return str(uuid.uuid4())


class CaptureQueue:
    def __init__(self):
        self.items = []

    def put_nowait(self, item):
        self.items.append(item)


class OnePassQueue:
    def __init__(self, items):
        self.items = list(items)
        self.done = 0

    def get(self):
        if not self.items:
            raise StopIteration
        return self.items.pop(0)

    def task_done(self):
        self.done += 1


class MockThread:
    def __init__(self, *args, **kwargs):
        self.started = False

    def is_alive(self):
        return False

    def start(self):
        self.started = True


class TrajectoryHookTests(unittest.TestCase):
    def test_concurrent_mcp_results_queue_only_their_corresponding_action_id(self):
        action_a, action_b = uid(), uid()
        captured = CaptureQueue()
        hooks._thread = None
        args = {"action": "tap", "text": "654321", "password": "secret", "image": {"data": "ZmFrZS1iYXNlNjQ="}}
        error_tree_content = [
            {"type": "text", "text": json.dumps({"tree": {"trajectory_action_id": action_a, "nodes": []}})},
            {"type": "text", "text": json.dumps({"ok": False, "error": {"code": "timeout", "trajectory_action_id": action_a}})},
            {"type": "image", "mimeType": "image/png", "data": "ZmFrZS1iYXNlNjQ="},
        ]
        normal_content = [{"type": "text", "text": json.dumps({"ok": True, "trajectory_action_id": action_b})}]
        with patch.object(hooks, "_queue", captured), patch.object(hooks.threading, "Thread", MockThread):
            hooks.observe_engine_event(Path("local-state"),
                {"id": "task-a", "model": "deepseek-test", "prompt": "first task", "status": "running"},
                {"item": {"id": "call-a", "tool": "android_action", "arguments": args,
                           "result": {"content": error_tree_content}}})
            hooks.observe_engine_event(Path("local-state"),
                {"id": "task-b", "model": "deepseek-test", "prompt": "second task", "status": "running"},
                {"item": {"id": "call-b", "tool": "android_action", "arguments": {"action": "tap"},
                           "result": {"content": normal_content}}})

        by_action = {body["action_id"]: body for _state, body in captured.items}
        self.assertEqual(set(by_action), {action_a, action_b})
        self.assertEqual(by_action[action_a]["task"]["id"], "task-a")
        self.assertEqual(by_action[action_b]["task"]["id"], "task-b")
        self.assertEqual(by_action[action_a]["decision"]["tool_call_id"], "call-a")
        encoded = json.dumps([body for _, body in captured.items])
        self.assertNotIn("654321", encoded)
        self.assertNotIn("secret", encoded)
        self.assertNotIn("ZmFrZS1iYXNlNjQ=", encoded)
        self.assertEqual(by_action[action_a]["decision"]["selected_action"]["text"], "[REDACTED]")

    def test_mocked_queue_worker_posts_feedback_without_network_or_sensitive_payload(self):
        action_id = uid()
        capture = CaptureQueue()
        hooks._thread = None
        with patch.object(hooks, "_queue", capture), patch.object(hooks.threading, "Thread", MockThread):
            hooks.observe_engine_event(Path("state-fixture"),
                {"id": "task-fixture", "model": "deepseek-test", "prompt": "fixture task", "status": "running"},
                {"item": {"id": "tool-call-fixture", "tool": "android_action",
                           "arguments": {"password": "do-not-store"},
                           "result": {"content": [{"type": "text", "text": json.dumps({
                               "trajectory_action_id": action_id, "error": {"code": "action_failed"}})}]}}})

        requests = []

        class FakeBridge:
            def __init__(self, state, timeout):
                self.state, self.timeout = state, timeout

            def request(self, method, path, body):
                requests.append((str(self.state), self.timeout, method, path, body))

        worker_queue = OnePassQueue(capture.items)
        with patch.object(hooks, "_queue", worker_queue), patch.dict(
                sys.modules, {"android_mcp_server": types.SimpleNamespace(AndroidBridge=FakeBridge)}):
            with self.assertRaises(StopIteration):
                hooks._work()

        self.assertEqual(worker_queue.done, 1)
        self.assertEqual(len(requests), 1)
        state, timeout, method, path, body = requests[0]
        self.assertEqual((state, timeout, method, path),
                         ("state-fixture", 2, "POST", "/api/trajectory/feedback"))
        self.assertEqual(body["action_id"], action_id)
        self.assertNotIn("do-not-store", json.dumps(body))


if __name__ == "__main__":
    unittest.main()
