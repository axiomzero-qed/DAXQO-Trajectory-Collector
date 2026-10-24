"""Durable receive -> local commit -> checkpoint -> device ACK protocol."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
from .dataset import Dataset
from .transport import Adb, DeviceClient
from .protocols import RecordingClient


def sync_device(client: RecordingClient, dataset: Dataset, max_batches=1000):
    status = client.request('/api/trajectory/status')
    device, stream = status['device_id'], status['stream_id']
    cursor = dataset.cursor(device, stream)
    starting_sequence = None
    inserted = duplicates = batches = 0
    for _ in range(max_batches):
        batch = client.request('/api/trajectory/batch?after=' + str(cursor) + '&limit=20')
        if batch.get('device_id') != device or batch.get('stream_id') != stream:
            raise ValueError('Device stream changed; refusing to acknowledge')
        records, end = batch.get('records'), batch.get('through')
        sequences = batch.get('sequences')
        if not isinstance(records, list) or type(end) is not int or end < cursor or (records and end <= cursor):
            raise ValueError('Invalid resume sequence')
        if (batch.get('after') != cursor or not isinstance(sequences, list) or len(sequences) != len(records)
                or any(type(n) is not int for n in sequences)
                or sequences != sorted(set(sequences)) or any(n <= cursor for n in sequences)
                or end != (sequences[-1] if sequences else cursor)):
            raise ValueError('Invalid batch sequence boundaries; refusing to acknowledge')
        if sequences:
            # A new local dataset may first encounter an older retained spool.
            # Make that gap explicit in the result, then require continuity.
            expected = cursor + 1 if cursor > 0 or starting_sequence is not None else sequences[0]
            if sequences != list(range(expected, expected + len(sequences))):
                raise ValueError('Non-contiguous batch sequences; refusing to acknowledge')
            if starting_sequence is None:
                starting_sequence = sequences[0]
        result = dataset.ingest(records, device_id=device)
        inserted += result['inserted']; duplicates += result['duplicates']; batches += 1
        # If either local durability step fails, never ACK a phone record.
        dataset.checkpoint(device, stream, end)
        client.request('/api/trajectory/ack', {'stream_id': stream, 'through': end})
        cursor = end
        if not records:
            break
    return {'device_id': device, 'serial': client.serial, 'inserted': inserted,
            'duplicates': duplicates, 'through': cursor, 'batches': batches,
            'starting_sequence': starting_sequence}


def sync_all(adb: Adb, dataset: Dataset, serials=None):
    devices = [d['serial'] for d in adb.devices() if d['connection'] == 'device']
    if serials:
        devices = [s for s in devices if s in serials]
    results = []
    def work(serial):
        try:
            with DeviceClient(adb, serial) as client:
                return sync_device(client, dataset)
        except Exception as error:
            return {'serial': serial, 'error': str(error)}
    with ThreadPoolExecutor(max_workers=max(1, min(4, len(devices)))) as executor:
        futures = [executor.submit(work, s) for s in devices]
        for future in as_completed(futures):
            results.append(future.result())
    return results
