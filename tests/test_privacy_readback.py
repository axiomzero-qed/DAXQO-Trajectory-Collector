"""Legacy audit payloads must not bypass a newer privacy policy on read/export."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from trajectory.dataset import Dataset
from trajectory.privacy import sanitize_nodes
from test_dataset import record


class PrivacyReadbackTests(unittest.TestCase):
    def test_body_node_helper_also_clears_readable_values(self):
        node = {'viewId': 'com.fixture:id/message_body', 'text': 'fixture-private-body',
                'description': 'fixture-private-body', 'label': 'fixture-private-body', 'editable': False}
        cleaned = sanitize_nodes([node])[0]
        self.assertEqual(cleaned['text'], '')
        self.assertEqual(cleaned['description'], '')
        self.assertEqual(cleaned['label'], '')

    def test_legacy_body_resource_is_hidden_on_read_and_export_without_mutating_audit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'data'
            with Dataset(root) as dataset:
                item = record(status='failed')
                dataset.ingest([item])
                # Simulate a valid old producer which did not know this resource.
                for side in ('before', 'after'):
                    item[side]['nodes'] = [{'viewId': 'com.fixture:id/message_body', 'text': 'fixture-private-body'}]
                    item[side]['training_allowed'] = True
                item['action']['target'] = {'text': 'fixture-private-body'}
                payload = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
                dataset._db.execute('UPDATE records SET payload=?,raw_digest=? WHERE record_id=?',
                    (payload, hashlib.sha256(payload.encode('utf-8')).hexdigest(), item['record_id']))
                dataset.jsonl_path.write_text(payload+'\n', encoding='utf-8')
                cleaned = dataset.records()[0]
                self.assertNotIn('fixture-private-body', json.dumps(cleaned))
                self.assertTrue(cleaned['privacy']['excluded'])
                manifest = dataset.export(Path(temp)/'export')
                self.assertEqual(manifest['counts']['eligible'], 0)
                self.assertEqual(dataset._db.execute('SELECT payload FROM records').fetchone()[0], payload)
                self.assertEqual(dataset.jsonl_path.read_text(encoding='utf-8'), payload+'\n')
