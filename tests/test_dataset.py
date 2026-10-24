import json
import tempfile
import unittest
import uuid
from pathlib import Path

from trajectory.dataset import Dataset, SCHEMA_VERSION


def uid():
    return str(uuid.uuid4())


def record(*, task_id=None, device_id=None, action_id=None, status="succeeded", package="io.daxqo.trajectoryfixture",
           fingerprint="screen-a", next_fingerprint="screen-b", user_task="打开商品详情"):
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": "action",
        "record_id": uid(),
        "device": {"id": device_id or uid()},
        "session_id": uid(),
        "task": {"id": task_id or uid(), "user_task": user_task, "subgoal": "打开商品",
                 "stage": "navigation", "source": "test"},
        "action_id": action_id or uid(),
        "started_at": "2026-10-24T10:00:00Z",
        "finished_at": "2026-10-24T10:00:01+00:00",
        "before": {"fingerprint": fingerprint, "package_name": package, "nodes": [], "width": 400,
                   "height": 800, "training_allowed": True, "redacted": True},
        "after": {"fingerprint": next_fingerprint, "package_name": package, "nodes": [], "width": 400,
                  "height": 800, "training_allowed": True, "redacted": True},
        "action": {"type": "tap", "target": "商品 01", "parameters": {"x": 40, "y": 100}},
        "decision": {"source": "deepseek", "model": "test-model", "tool_call_id": "tc-1",
                     "candidate_actions": [{"type": "tap"}], "confidence": 0.8},
        "result": {"status": status, "error_code": None, "verification": {"status": "unverified"}},
        "navigation": {}, "events": [], "metrics": {},
        "privacy": {"redacted": True, "excluded": False, "reasons": []},
    }


def feedback(action, source="fixture_assertion", device_id=None):
    return {"schema_version": SCHEMA_VERSION, "record_type": "agent_feedback", "record_id": uid(),
            "device": {"id": device_id or action["device"]["id"]}, "session_id": action["session_id"],
            "task": dict(action["task"]), "action_id": action["action_id"],
            "started_at": action["started_at"], "finished_at": action["finished_at"],
            "decision": {}, "verification": {"status": "verified", "source": source}}


def unverified_feedback(action, *, include_decision=True, device_id=None):
    item = feedback(action, device_id=device_id)
    item["record_id"] = uid()
    item["verification"] = {"status": "unverified"}
    if not include_decision:
        item.pop("decision")
    return item


class DatasetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "data"
        self.dataset = Dataset(self.root)

    def tearDown(self):
        self.dataset.close()
        self.temp.cleanup()

    def test_ingest_is_idempotent_and_conflict_is_rejected(self):
        item = record()
        self.assertEqual(self.dataset.ingest([item]), {"inserted": 1, "duplicates": 0})
        self.assertEqual(self.dataset.ingest([item]), {"inserted": 0, "duplicates": 1})
        conflict = dict(item, action=dict(item["action"], target="different"))
        with self.assertRaises(ValueError):
            self.dataset.ingest([conflict])
        self.assertEqual(len(self.dataset.records()), 1)
        self.assertEqual(len((self.root / "trajectories.jsonl").read_text(encoding="utf-8").splitlines()), 1)

    def test_raw_conflict_is_rejected_even_when_privacy_sanitization_matches(self):
        first = record()
        first["action"]["parameters"]["text"] = "654321"
        self.dataset.ingest([first])
        conflict = dict(first, action=dict(first["action"], parameters={**first["action"]["parameters"], "text": "123456"}))
        with self.assertRaisesRegex(ValueError, "raw content"):
            self.dataset.ingest([conflict])

    def test_structured_and_empty_targets_are_valid(self):
        structured, empty = record(), record()
        structured["action"]["target"] = {"nodeId": "button-1", "role": "button"}
        empty["action"]["target"] = {}
        self.assertEqual(self.dataset.ingest([structured, empty])["inserted"], 2)

    def test_batch_validation_precedes_any_write(self):
        first, invalid = record(), record()
        invalid["before"]["width"] = True
        with self.assertRaises(ValueError):
            self.dataset.ingest([first, invalid])
        self.assertEqual(self.dataset.records(), [])
        self.assertFalse((self.root / "trajectories.jsonl").exists())

    def test_restart_rebuilds_missing_jsonl_from_committed_index(self):
        item = record()
        self.dataset.ingest([item])
        self.dataset.close()
        (self.root / "trajectories.jsonl").unlink()
        self.dataset = Dataset(self.root)
        line = json.loads((self.root / "trajectories.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(line["record_id"], item["record_id"])

    def test_restart_repairs_partial_jsonl_tail_without_duplicate_rows(self):
        item = record()
        self.dataset.ingest([item])
        self.dataset.close()
        path = self.root / "trajectories.jsonl"
        with path.open("ab") as stream:
            stream.write(b'{"record_id":"torn')
        self.dataset = Dataset(self.root)
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["record_id"], item["record_id"])

    def test_multiple_dataset_instances_serialize_jsonl_and_idempotency(self):
        second = Dataset(self.root)
        item = record()
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda ds: ds.ingest([item]), (self.dataset, second)))
        self.assertEqual(sum(result["inserted"] for result in results), 1)
        self.assertEqual(sum(result["duplicates"] for result in results), 1)
        lines = (self.root / "trajectories.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        second.close()

    def test_cursor_checkpoint_is_monotonic_and_scoped(self):
        device, stream = uid(), "android-main"
        self.assertEqual(self.dataset.cursor(device, stream), 0)
        self.dataset.checkpoint(device, stream, 12)
        self.assertEqual(self.dataset.cursor(device, stream), 12)
        self.assertEqual(self.dataset.cursor(device, "other"), 0)
        with self.assertRaises(ValueError):
            self.dataset.checkpoint(device, stream, 11)

    def test_records_filters_and_stats(self):
        a, b = record(), record()
        self.dataset.ingest([a, b])
        self.assertEqual(len(self.dataset.records(task_id=a["task"]["id"])), 1)
        self.assertEqual(len(self.dataset.records(device_id=b["device"]["id"])), 1)
        self.assertEqual(self.dataset.stats()["records"], 2)

    def test_export_requires_independent_verification_and_keeps_failures_negative(self):
        verified = record()
        failed = record(status="failed", fingerprint="failed-before", next_fingerprint="failed-after")
        claimed = record(fingerprint="claimed-before", next_fingerprint="claimed-after")
        unknown = record(status="unknown", fingerprint="unknown-before", next_fingerprint="unknown-after")
        rows = [verified, failed, claimed, unknown,
                unverified_feedback(verified), feedback(verified), feedback(failed), feedback(claimed, "agent_claim")]
        self.dataset.ingest(rows)
        out = Path(self.temp.name) / "export"
        manifest = self.dataset.export(out, test_fraction=0.2)
        data = [json.loads(line) for line in (out / "train.jsonl").read_text(encoding="utf-8").splitlines()]
        data += [json.loads(line) for line in (out / "test.jsonl").read_text(encoding="utf-8").splitlines()]
        by_action = {x["action_id"]: x for x in data}
        self.assertEqual(by_action[verified["action_id"]]["label"], "positive")
        self.assertEqual(by_action[verified["action_id"]]["data_origin"], "fixture")
        self.assertEqual(by_action[failed["action_id"]]["label"], "negative")
        self.assertNotIn(claimed["action_id"], by_action)
        self.assertNotIn(unknown["action_id"], by_action)
        self.assertTrue((out / "manifest.json").exists())

    def test_same_action_id_on_different_devices_exports_independently(self):
        shared_id = uid()
        device_a, device_b = uid(), uid()
        a = record(action_id=shared_id, device_id=device_a, fingerprint="device-a-before", next_fingerprint="device-a-after")
        b = record(action_id=shared_id, device_id=device_b, fingerprint="device-b-before", next_fingerprint="device-b-after")
        self.dataset.ingest([a, feedback(a, "human_review"), b, feedback(b, "fixture_assertion")])
        out = Path(self.temp.name) / "same-action-id"
        self.dataset.export(out)
        rows = [json.loads(line) for name in ("train.jsonl", "test.jsonl")
                for line in (out / name).read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["device_id"] for row in rows}, {device_a, device_b})
        self.assertEqual({row["label"] for row in rows}, {"positive"})

    def test_agent_feedback_supplies_decision_and_task_without_cross_device_mix(self):
        action = record()
        action["task"] = {"id": uid(), "user_task": "[redacted]", "subgoal": "", "stage": "running",
                          "source": "unattributed"}
        action["decision"] = {"source": "unattributed", "model": None, "tool_call_id": None,
                              "candidate_actions": [], "confidence": None}
        own = unverified_feedback(action)
        own["task"] = {"id": uid(), "user_task": "open the fixture details", "subgoal": "select item",
                       "stage": "running", "source": "agent"}
        own["decision"] = {"source": "deepseek_agent", "model": "deepseek-test", "tool_call_id": "call-7",
                           "candidate_actions": [{"type": "tap", "target": "商品 01"}], "confidence": 0.7}
        other_device = unverified_feedback(action, device_id=uid())
        other_device["task"] = {"id": uid(), "user_task": "wrong private task", "subgoal": "", "stage": "",
                                 "source": "agent"}
        other_device["decision"] = {"source": "wrong", "model": "wrong-model", "candidate_actions": [{"type": "secret"}]}
        self.dataset.ingest([action, own, other_device])
        out = Path(self.temp.name) / "feedback-context"
        self.dataset.export(out, include_unverified=True)
        rows = [json.loads(line) for name in ("train.jsonl", "test.jsonl")
                for line in (out / name).read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 1)
        exported = rows[0]
        self.assertEqual(exported["label"], "unverified")
        self.assertIsNone(exported["reward"])
        self.assertEqual(exported["task"]["user_task"], "open the fixture details")
        self.assertEqual(exported["decision"]["model"], "deepseek-test")
        self.assertEqual(exported["candidate_actions"][0]["type"], "tap")

    def test_fixture_task_is_marked_as_fixture_for_negative_export(self):
        item = record(status="failed")
        item["task"]["source"] = "fixture"
        self.dataset.ingest([item])
        out = Path(self.temp.name) / "fixture-negative"
        self.dataset.export(out)
        row = json.loads((out / "train.jsonl").read_text(encoding="utf-8").strip())
        self.assertEqual(row["label"], "negative")
        self.assertEqual(row["data_origin"], "fixture")

    def test_stats_summarize_outcomes_privacy_verification_duplicates_and_timings(self):
        succeeded = record()
        succeeded["metrics"] = {"tree_ms": 10, "observer_ms": 20, "total_ms": 30}
        failed = record(status="failed", fingerprint="failed-before", next_fingerprint="failed-after")
        failed["privacy"]["excluded"] = True
        failed["metrics"] = {"tree_ms": 20, "observer_ms": 40, "total_ms": 50}
        duplicate = dict(succeeded, record_id=uid(), action_id=uid(), task=dict(succeeded["task"], id=uid()))
        duplicate["metrics"] = {}
        self.dataset.ingest([succeeded, feedback(succeeded), failed, duplicate])
        stats = self.dataset.stats()
        self.assertEqual(stats["outcomes"], {"failed": 1, "succeeded": 2, "unknown": 0})
        self.assertEqual(stats["verification"]["independently_verified"], 1)
        self.assertEqual(stats["verification"]["unverified"], 2)
        self.assertEqual(stats["privacy_excluded"], 1)
        self.assertEqual(stats["duplicate_transitions"], 1)
        self.assertEqual(stats["timings_ms"]["tree_ms"]["samples"], 2)
        self.assertEqual(stats["timings_ms"]["tree_ms"]["average"], 15)
        self.assertEqual(stats["timings_ms"]["tree_ms"]["p95"], 20)

    def test_unknown_is_unverified_never_positive_and_privacy_gates_win(self):
        unknown = record(status="unknown")
        verified_unknown = feedback(unknown)
        denied = record()
        denied["privacy"]["excluded"] = True
        disallowed = record()
        disallowed["after"]["training_allowed"] = False
        sensitive = record(package="com.tencent.mm")
        self.dataset.ingest([unknown, verified_unknown, denied, disallowed, feedback(disallowed), sensitive,
                             feedback(sensitive)])
        out = Path(self.temp.name) / "include-unverified"
        self.dataset.export(out, include_unverified=True)
        lines = [json.loads(line) for name in ("train.jsonl", "test.jsonl")
                 for line in (out / name).read_text(encoding="utf-8").splitlines()]
        found = {line["action_id"]: line for line in lines}
        self.assertEqual(found[unknown["action_id"]]["label"], "unverified")
        self.assertIsNone(found[unknown["action_id"]]["reward"])
        self.assertNotIn(denied["action_id"], found)
        self.assertNotIn(disallowed["action_id"], found)
        self.assertNotIn(sensitive["action_id"], found)

    def test_feedback_from_other_device_cannot_verify_action(self):
        action = record()
        self.dataset.ingest([action, feedback(action, device_id=uid())])
        out = Path(self.temp.name) / "wrong-device"
        self.dataset.export(out)
        self.assertEqual(sum(1 for name in ("train.jsonl", "test.jsonl")
                             for line in (out / name).read_text(encoding="utf-8").splitlines()), 0)

    def test_unverified_feedback_defaults_and_does_not_override_verified_or_context(self):
        action = record()
        pending = unverified_feedback(action, include_decision=False)
        pending["task"] = dict(pending["task"], user_task="a private alternate context")
        model_decision = unverified_feedback(action)
        model_decision["decision"] = {"model": "DeepSeek", "candidate_actions": [{"text": "sensitive"}]}
        model_decision.pop("verification")
        self.dataset.ingest([action, pending, model_decision])
        stored = self.dataset.records()
        saved_feedback = next(row for row in stored if row["record_type"] == "agent_feedback"
                              and row["task"]["user_task"] == "a private alternate context")
        self.assertEqual(saved_feedback["verification"], {"status": "unverified"})
        self.assertEqual(saved_feedback["decision"], {})
        self.assertEqual(saved_feedback["task"]["user_task"], "a private alternate context")
        saved_model_decision = next(row for row in stored if row["record_id"] == model_decision["record_id"])
        self.assertEqual(saved_model_decision["verification"], {"status": "unverified"})
        out = Path(self.temp.name) / "pending"
        self.dataset.export(out)
        self.assertEqual(sum(1 for name in ("train.jsonl", "test.jsonl")
                             for line in (out / name).read_text(encoding="utf-8").splitlines()), 0)

    def test_same_task_template_and_task_id_cannot_leak_across_splits(self):
        a = record(user_task="打开 商品 列表")
        b = record(user_task="  打开   商品 列表 ")
        # Keep both eligible transitions distinct but link through normalized task template.
        b["before"]["fingerprint"] = "another-screen"
        b["after"]["fingerprint"] = "another-result"
        self.dataset.ingest([a, b, feedback(a), feedback(b)])
        out = Path(self.temp.name) / "grouped"
        manifest = self.dataset.export(out, test_fraction=0.5)
        train = {json.loads(x)["action_id"] for x in (out / "train.jsonl").read_text(encoding="utf-8").splitlines()}
        test = {json.loads(x)["action_id"] for x in (out / "test.jsonl").read_text(encoding="utf-8").splitlines()}
        self.assertFalse(train & test)
        self.assertTrue(train == {a["action_id"], b["action_id"]} or test == {a["action_id"], b["action_id"]})
        self.assertEqual(manifest["groups"], 1)
        self.assertEqual(len(test), 0)
        self.assertTrue(manifest["limitations"])

    def test_identical_transitions_are_export_deduplicated(self):
        a = record()
        b = dict(a, record_id=uid(), action_id=uid(), task=dict(a["task"], id=uid()))
        b["action"] = dict(a["action"], parameters=dict(a["action"]["parameters"]))
        self.dataset.ingest([a, feedback(a), b, feedback(b)])
        out = Path(self.temp.name) / "dedup"
        manifest = self.dataset.export(out)
        self.assertEqual(manifest["counts"]["eligible"], 1)
        self.assertEqual(manifest["excluded"]["transition_duplicate"], 1)

    def test_duplicate_transition_preserves_all_split_aliases(self):
        task_a, task_b = uid(), uid()
        x_a = record(task_id=task_a, user_task="Open product list")
        x_b = dict(x_a, record_id=uid(), action_id=uid(), task=dict(x_a["task"], id=task_b,
                                                                       user_task="Inspect featured product"))
        y = record(task_id=task_a, user_task="Open product list", fingerprint="y-before",
                   next_fingerprint="y-after")
        z = record(task_id=task_b, user_task="Inspect featured product", fingerprint="z-before",
                   next_fingerprint="z-after")
        self.dataset.ingest([x_a, feedback(x_a), y, feedback(y), x_b, feedback(x_b), z, feedback(z)])
        out = Path(self.temp.name) / "duplicate-aliases"
        manifest = self.dataset.export(out, test_fraction=0.5)
        train = [json.loads(line) for line in (out / "train.jsonl").read_text(encoding="utf-8").splitlines()]
        test = (out / "test.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(manifest["groups"], 1)
        self.assertEqual(manifest["counts"]["test"], 0)
        self.assertEqual(test, [])
        self.assertEqual(len(train), 3)
        x_rows = [row for row in train if row["state"]["fingerprint"] == x_a["before"]["fingerprint"]]
        self.assertEqual(len(x_rows), 1)
        self.assertEqual(x_rows[0]["label"], "positive")

    def test_snapshot_request_ids_remain_auditable_but_do_not_split_transition(self):
        a = record()
        b = dict(a, record_id=uid(), action_id=uid(), task=dict(a["task"], id=uid()))
        b["action"] = dict(a["action"], parameters=dict(a["action"]["parameters"]))
        a["action"]["parameters"].update({"nodeId": "row-1", "snapshotId": "snapshot-a"})
        b["action"]["parameters"] = dict(a["action"]["parameters"], snapshotId="snapshot-b")
        b["action"]["parameters"]["request_id"] = "request-b"
        a["action"]["parameters"]["request_id"] = "request-a"
        self.dataset.ingest([a, feedback(a), b, feedback(b)])
        audit = {row["action_id"]: row for row in self.dataset.records() if row["record_type"] == "action"}
        self.assertEqual(audit[a["action_id"]]["action"]["parameters"]["snapshotId"], "snapshot-a")
        self.assertEqual(audit[b["action_id"]]["action"]["parameters"]["snapshotId"], "snapshot-b")
        out = Path(self.temp.name) / "snapshot-dedup"
        manifest = self.dataset.export(out, test_fraction=0.5)
        rows = [json.loads(line) for name in ("train.jsonl", "test.jsonl")
                for line in (out / name).read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(manifest["excluded"]["transition_duplicate"], 1)
        train_ids = {json.loads(line)["action_id"] for line in (out / "train.jsonl").read_text(encoding="utf-8").splitlines()}
        test_ids = {json.loads(line)["action_id"] for line in (out / "test.jsonl").read_text(encoding="utf-8").splitlines()}
        self.assertFalse(train_ids & test_ids)

    def test_split_by_device_groups_devices(self):
        a, b = record(), record()
        self.dataset.ingest([a, feedback(a), b, feedback(b)])
        out = Path(self.temp.name) / "devices"
        self.dataset.export(out, split_by="device", test_fraction=0.5)
        train = {json.loads(x)["device_id"] for x in (out / "train.jsonl").read_text(encoding="utf-8").splitlines()}
        test = {json.loads(x)["device_id"] for x in (out / "test.jsonl").read_text(encoding="utf-8").splitlines()}
        self.assertFalse(train & test)

    def test_device_split_still_keeps_same_task_together_across_phones(self):
        shared_task = uid()
        a = record(task_id=shared_task, device_id=uid(), fingerprint="phone-a-before", next_fingerprint="phone-a-after")
        b = record(task_id=shared_task, device_id=uid(), fingerprint="phone-b-before", next_fingerprint="phone-b-after",
                   user_task="  OPEN   FIXTURE PRODUCT ")
        a["task"]["user_task"] = "open fixture product"
        self.dataset.ingest([a, feedback(a), b, feedback(b)])
        out = Path(self.temp.name) / "device-task-safe"
        manifest = self.dataset.export(out, split_by="device", test_fraction=0.5)
        train = {json.loads(x)["action_id"] for x in (out / "train.jsonl").read_text(encoding="utf-8").splitlines()}
        test = {json.loads(x)["action_id"] for x in (out / "test.jsonl").read_text(encoding="utf-8").splitlines()}
        self.assertFalse(train & test)
        self.assertTrue(train == {a["action_id"], b["action_id"]} or test == {a["action_id"], b["action_id"]})
        self.assertEqual(manifest["groups"], 1)

    def test_invalid_arguments_and_record_types_are_rejected(self):
        with self.assertRaises(ValueError):
            self.dataset.export(Path(self.temp.name) / "bad", split_by="prompt")
        bad = record()
        bad["session_id"] = "not-a-uuid"
        with self.assertRaises(ValueError):
            self.dataset.ingest([bad])


if __name__ == "__main__":
    unittest.main()
