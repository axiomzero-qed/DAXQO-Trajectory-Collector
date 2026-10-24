from __future__ import annotations
import argparse
from collections import Counter
from datetime import datetime, timezone
import html
import json
from pathlib import Path
import sys
import time
import uuid
from .collector import sync_all
from .dataset import Dataset
from .transport import Adb, DeviceClient

FIXTURE = 'io.daxqo.trajectoryfixture'


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def replay(records, output):
    """Inspection replay deliberately does not dispatch historical phone writes."""
    rows = []
    feedback = {}
    for item in records:
        if item['record_type'] == 'agent_feedback':
            feedback.setdefault((item['device']['id'], item['action_id']), []).append(item)
    for item in records:
        if item['record_type'] != 'action': continue
        def state(side):
            state = item.get(side, {})
            texts = [n.get('text') or n.get('description', '') for n in state.get('nodes', []) if n.get('visible', True)]
            return html.escape(state.get('package_name', '') + '\n' + '\n'.join(t for t in texts if t)[:4000])
        rows.append('<tr><td>' + html.escape(item['started_at']) + '<br>' + html.escape(item['device']['id'][:8]) +
                    '<br>' + html.escape(item['task']['id']) + '</td><td><details><summary>' +
                    html.escape(item['action']['type'] + ' · ' + item['action_id'][:8]) + '</summary><pre>' +
                    html.escape(json.dumps({'action': item['action'], 'result': item['result'],
                        'navigation': item.get('navigation'), 'metrics': item.get('metrics'),
                        'feedback': feedback.get((item['device']['id'], item['action_id']), [])}, ensure_ascii=False, indent=2)) +
                    '</pre></details></td><td><pre>' + state('before') + '</pre></td><td><pre>' + state('after') + '</pre></td></tr>')
    output = Path(output); output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text('<!doctype html><meta charset="utf-8"><title>DAXQO 轨迹检查</title><style>'
        'body{font:15px system-ui;margin:30px;background:#fafafa;color:#202126}table{border-collapse:collapse;width:100%}'
        'th,td{border:1px solid #ddd;padding:12px;text-align:left;vertical-align:top}pre{white-space:pre-wrap;max-width:520px}'
        '</style><h1>DAXQO 轨迹检查</h1><p>脱敏状态与实际结果；时间顺序检查，不会重新执行手机操作。</p>'
        '<table><tr><th>时间 / 设备 / 任务</th><th>动作 / 结果</th><th>操作前</th><th>操作后</th></tr>' + ''.join(rows) + '</table>', encoding='utf-8')
    return {'output': str(output.resolve()), 'actions': len(rows)}


def parser():
    p = argparse.ArgumentParser(description='DAXQO Trajectory Collector v0.1 · local only')
    p.add_argument('--data', default='data', help='Local dataset directory')
    p.add_argument('--adb', help='Path to adb')
    p.add_argument('--serial', action='append', help='Select device; repeat for multiple devices')
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('devices', help='Discover device and collector readiness')
    start = sub.add_parser('start', help='Enable local phone recording')
    start.add_argument('--task', required=True, help='User task (redacted before persistence)')
    start.add_argument('--task-id', help='Optional common task identifier; device IDs stay separate')
    start.add_argument('--allow-app', action='append', default=None, help='Explicit collection allowlist; default isolated fixture only')
    start.add_argument('--screenshots', action='store_true', help='Explicitly retain safe explicit Agent screenshots')
    start.add_argument('--source', default='manual', choices=['manual', 'agent', 'fixture'])
    sub.add_parser('stop', help='Disable recording without interrupting Agent')
    sub.add_parser('sync', help='Receive and acknowledge queued records from all selected phones')
    watch = sub.add_parser('watch', help='Reconnect and sync continuously')
    watch.add_argument('--interval', type=float, default=3)
    watch.add_argument('--seconds', type=float, default=0, help='Zero keeps running until Ctrl+C')
    sub.add_parser('stats', help='Dataset counts, errors and quality statistics')
    sub.add_parser('tasks', help='Recorded tasks by device')
    prof = sub.add_parser('profile', help='Measure fixture latency, process memory and Android frame metrics')
    prof.add_argument('--output', default='dist/performance.json')
    ex = sub.add_parser('export', help='Export scoring dataset with leakage-safe split')
    ex.add_argument('--output', default='exports/v0.1')
    ex.add_argument('--split-by', default='task', choices=['task', 'device', 'app'])
    ex.add_argument('--test-fraction', type=float, default=.2)
    ex.add_argument('--include-unverified', action='store_true')
    ex.add_argument('--seed', default='daxqo-v0.1')
    rp = sub.add_parser('replay', help='Produce local HTML inspection replay')
    rp.add_argument('--task-id'); rp.add_argument('--device-id'); rp.add_argument('--output', default='dist/replay.html')
    im = sub.add_parser('import', help='Import a local JSONL bundle through validation/privacy gates')
    im.add_argument('path')
    merge = sub.add_parser('merge', help='Merge a local receiver dataset and its durable ACK receipts')
    merge.add_argument('--source', required=True)
    review = sub.add_parser('review', help='Record an explicit human review after inspection')
    review.add_argument('--action-id', required=True); review.add_argument('--device-id', required=True)
    review.add_argument('--passed', action='store_true', required=True)
    return p


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'): sys.stdout.reconfigure(encoding='utf-8')
    args = parser().parse_args(argv)
    try:
        if args.command in ('devices', 'start', 'stop'):
            adb = Adb(args.adb); results = []
            discovered = adb.devices()
            if args.serial:
                discovered = [d for d in discovered if d['serial'] in args.serial]
                missing = set(args.serial) - {d['serial'] for d in discovered}
                results.extend({'serial': s, 'error': 'Device not connected'} for s in sorted(missing))
            for d in discovered:
                if d['connection'] != 'device': results.append(d); continue
                try:
                    with DeviceClient(adb, d['serial']) as client:
                        if args.command == 'start':
                            body = {'enabled': True, 'screenshots': args.screenshots,
                                'allowed_packages': args.allow_app or [FIXTURE],
                                'task': {'id': args.task_id or str(uuid.uuid4()), 'user_task': args.task,
                                         'subgoal': '', 'stage': 'running', 'source': args.source}}
                            d['collector'] = client.request('/api/trajectory/config', body)
                        elif args.command == 'stop':
                            d['collector'] = client.request('/api/trajectory/config', {'enabled': False})
                        else:
                            d['control'] = client.request('/api/device/status')
                            d['collector'] = client.request('/api/trajectory/status')
                except Exception as error:
                    d['error'] = str(error)
                results.append(d)
            emit(results)
            return 1 if any('error' in r for r in results) or not results else 0
        with Dataset(Path(args.data)) as dataset:
            if args.command == 'profile':
                from .profiling import profile_all
                result = profile_all(Adb(args.adb), dataset, args.serial)
                output = Path(args.output); output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
                emit(result)
                return 1 if not result or any('error' in r for r in result) else 0
            if args.command == 'sync':
                result = sync_all(Adb(args.adb), dataset, args.serial); emit(result)
                return 1 if not result or any('error' in r for r in result) else 0
            if args.command == 'watch':
                adb = Adb(args.adb); until = time.monotonic() + args.seconds if args.seconds else float('inf')
                last = None
                try:
                    while time.monotonic() < until:
                        result = sync_all(adb, dataset, args.serial)
                        status = json.dumps(result, sort_keys=True)
                        if status != last: emit(result); last = status
                        time.sleep(max(.2, min(30, args.interval)))
                except KeyboardInterrupt:
                    pass
                return 0
            if args.command == 'stats': emit(dataset.stats())
            elif args.command == 'tasks':
                grouped = {}
                for record in dataset.records():
                    key = (record['device']['id'], record['task']['id'])
                    row = grouped.setdefault(key, {'device_id': key[0], 'task': record['task'], 'actions': 0, 'failures': 0})
                    if record['record_type'] == 'action':
                        row['actions'] += 1; row['failures'] += record['result']['status'] != 'succeeded'
                emit(list(grouped.values()))
            elif args.command == 'export':
                emit(dataset.export(Path(args.output), split_by=args.split_by, test_fraction=args.test_fraction,
                                    seed=args.seed, include_unverified=args.include_unverified))
            elif args.command == 'replay':
                emit(replay(dataset.records(task_id=args.task_id, device_id=args.device_id), args.output))
            elif args.command == 'import':
                records = [json.loads(line) for line in Path(args.path).read_text(encoding='utf-8').splitlines() if line.strip()]
                emit(dataset.ingest(records))
            elif args.command == 'merge':
                from .merge import merge_dataset
                emit(merge_dataset(Path(args.source), dataset))
            elif args.command == 'review':
                records = [r for r in dataset.records(device_id=args.device_id) if r['record_type'] == 'action' and r['action_id'] == args.action_id]
                if len(records) != 1: raise ValueError('Select exactly one recorded action for this device')
                action = records[0]; stamp = datetime.now(timezone.utc).isoformat()
                record = {k: action[k] for k in ('schema_version', 'device', 'session_id', 'task', 'action_id')}
                record.update(record_id=str(uuid.uuid4()), record_type='agent_feedback', started_at=stamp, finished_at=stamp,
                        decision={}, verification={'status': 'verified', 'source': 'human_review'})
                emit(dataset.ingest([record]))
        return 0
    except Exception as error:
        emit({'error': str(error), 'error_type': type(error).__name__})
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
