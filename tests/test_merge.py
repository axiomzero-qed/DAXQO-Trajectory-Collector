"""Tests for importing durable local receiver snapshots and ACK receipts."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from test_dataset import record
from trajectory.dataset import Dataset
from trajectory.merge import merge_dataset


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source_path = self.root / "source"
        self.destination = Dataset(self.root / "destination")

    def tearDown(self):
        self.destination.close()
        self.temp.cleanup()

    def make_source(self, rows, checkpoint=0):
        with Dataset(self.source_path) as source:
            source.ingest(rows)
            if rows and checkpoint:
                source.checkpoint(rows[0]["device"]["id"], "fixture-stream", checkpoint)

    def source_snapshot(self):
        path = self.source_path / "index.sqlite3"
        db = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
        try:
            records = db.execute("SELECT record_id, payload, raw_digest, ordinal FROM records ORDER BY ordinal").fetchall()
            checkpoints = db.execute("SELECT device_id, stream_id, sequence FROM checkpoints ORDER BY device_id, stream_id").fetchall()
        finally:
            db.close()
        jsonl = (self.source_path / "trajectories.jsonl").read_bytes()
        return records, checkpoints, jsonl

    def test_merge_success_is_idempotent_and_source_is_read_only(self):
        first = record()
        second = record(device_id=first["device"]["id"])
        self.make_source([first, second], checkpoint=640)
        before = self.source_snapshot()

        initial = merge_dataset(self.source_path, self.destination)
        repeated = merge_dataset(self.source_path, self.destination)

        self.assertEqual(initial["inserted"], 2)
        self.assertEqual(initial["adopted_checkpoints"][0]["sequence"], 640)
        self.assertEqual(repeated["inserted"], 0)
        self.assertEqual(repeated["duplicates"], 2)
        self.assertEqual(len(self.destination.records()), 2)
        self.assertEqual(self.destination.cursor(first["device"]["id"], "fixture-stream"), 640)
        self.assertEqual(self.source_snapshot(), before)
        receipts = list((self.destination.root / "imports").glob("*.json"))
        self.assertEqual(len(receipts), 2)

    def test_conflicting_record_rejects_merge_before_checkpoint_adoption(self):
        original = record()
        source_row = json.loads(json.dumps(original))
        source_row["action"]["parameters"]["x"] = 10
        self.make_source([source_row], checkpoint=640)
        self.destination.ingest([original])
        self.destination.checkpoint(original["device"]["id"], "fixture-stream", 5)
        before = self.source_snapshot()

        with self.assertRaisesRegex(ValueError, "Conflicting local receiver record"):
            merge_dataset(self.source_path, self.destination)

        self.assertEqual(len(self.destination.records()), 1)
        self.assertEqual(self.destination.cursor(original["device"]["id"], "fixture-stream"), 5)
        self.assertEqual(list((self.destination.root / "imports").glob("*.json")) if (self.destination.root / "imports").exists() else [], [])
        self.assertEqual(self.source_snapshot(), before)

    def test_invalid_source_record_writes_nothing_and_does_not_change_source(self):
        item = record()
        self.make_source([item], checkpoint=640)
        source_db = self.source_path / "index.sqlite3"
        db = sqlite3.connect(source_db)
        try:
            db.execute("UPDATE records SET payload=? WHERE record_id=?", ("{}", item["record_id"]))
            db.commit()
        finally:
            db.close()
        before = self.source_snapshot()

        with self.assertRaises(ValueError):
            merge_dataset(self.source_path, self.destination)

        self.assertEqual(self.destination.records(), [])
        self.assertEqual(self.destination.cursor(item["device"]["id"], "fixture-stream"), 0)
        self.assertEqual(self.source_snapshot(), before)


if __name__ == "__main__":
    unittest.main()
