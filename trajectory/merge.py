"""Import a complete local receiver snapshot before adopting its ACK receipts."""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid
from .dataset import Dataset, validate_record
from .privacy import redact_record


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def merge_dataset(source: Path, destination: Dataset):
    source = Path(source).resolve()
    database = source / 'index.sqlite3'
    if database.resolve() == destination.db_path.resolve():
        raise ValueError('Source and destination must be different datasets')
    if not database.is_file(): raise ValueError('Source SQLite index is missing')
    # The source can remain in use: the read transaction is a consistent snapshot.
    connection = sqlite3.connect(database.as_uri()+'?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('BEGIN')
        rows = connection.execute('SELECT * FROM records ORDER BY ordinal').fetchall()
        checkpoints = [dict(r) for r in connection.execute('SELECT * FROM checkpoints')]
    finally:
        connection.close()
    existing = {r['record_id']: r for r in destination.records()}
    new, raw_digests, source_devices = [], {}, set()
    for row in rows:
        item = json.loads(row['payload'])
        safe = redact_record(item); validate_record(safe)
        if row['record_id'] != item['record_id'] or row['device_id'] != item['device']['id']:
            raise ValueError('Source index does not match its record payload')
        source_devices.add(item['device']['id'])
        previous = existing.get(item['record_id'])
        if previous is not None and canonical(redact_record(previous)) != canonical(safe):
            raise ValueError('Conflicting local receiver record: '+item['record_id'])
        if previous is None:
            new.append(item)
            if 'raw_digest' in row.keys() and row['raw_digest']:
                raw_digests[item['record_id']] = row['raw_digest']
    for checkpoint in checkpoints:
        if (type(checkpoint['sequence']) is not int or checkpoint['sequence'] < 0
                or not isinstance(checkpoint['stream_id'], str) or not checkpoint['stream_id']
                or checkpoint['device_id'] not in source_devices):
            raise ValueError('Source checkpoint has no valid corresponding records')
    result = destination.ingest(new)
    # Preserve source/native idempotency hashes for a later native retry.
    with destination._lock:
        destination._db.execute('BEGIN IMMEDIATE')
        try:
            for record_id, digest in raw_digests.items():
                destination._db.execute('UPDATE records SET raw_digest=? WHERE record_id=?', (digest, record_id))
            destination._db.execute('COMMIT')
        except Exception:
            destination._db.execute('ROLLBACK'); raise
    adopted = []
    # These are receipts from the complete, locally durable source dataset,
    # never a guessed mapping from record order to native sequence numbers.
    for checkpoint in checkpoints:
        if checkpoint['sequence'] > destination.cursor(checkpoint['device_id'], checkpoint['stream_id']):
            destination.checkpoint(checkpoint['device_id'], checkpoint['stream_id'], checkpoint['sequence'])
            adopted.append(checkpoint)
    receipt = {'import_id': str(uuid.uuid4()), 'timestamp': datetime.now(timezone.utc).isoformat(),
               'source': str(source), 'source_records': len(rows), 'inserted': result['inserted'],
               'duplicates': len(rows)-len(new), 'adopted_checkpoints': adopted,
               'snapshot_sha256': hashlib.sha256(('\n'.join(row['payload'] for row in rows)+canonical(checkpoints)).encode('utf-8')).hexdigest(),
               'phone_operations': 0}
    directory = destination.root / 'imports'; directory.mkdir(exist_ok=True)
    (directory/(receipt['import_id']+'.json')).write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
    return receipt
