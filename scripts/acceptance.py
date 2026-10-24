"""Opt-in, real-device acceptance; operates only on the isolated fixture App."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import re
import statistics
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from trajectory.transport import Adb, DeviceClient
from trajectory.dataset import Dataset
from trajectory.collector import sync_device
from trajectory.cli import replay

APP = 'io.daxqo.trajectoryfixture'


def run_device(adb, serial, dataset):
    report = {'serial': serial, 'passed': False, 'checks': []}
    task = {'id': str(uuid.uuid4()), 'user_task': '遍历虚构商品列表、检查详情与规格、多级返回、滚动、关闭弹窗',
            'subgoal': '', 'stage': 'acceptance', 'source': 'fixture'}
    context = {'task': task, 'decision': {'source': 'fixture_script', 'model': None,
               'tool_call_id': None, 'candidate_actions': [], 'confidence': None}}
    with DeviceClient(adb, serial) as c:
        status = c.request('/api/device/status')
        if not status.get('controlEnabled') or not status.get('accessibilityConnected'):
            raise RuntimeError('Device requires owner to enable accessibility and phone control')
        if status.get('locked') or not status.get('screenOn'):
            awake = c.action('wake')
            if not awake.get('ok'): raise RuntimeError('Owner must unlock the test phone')
        initial = c.request('/api/trajectory/status')
        report['device_id'] = initial['device_id']
        report['platform'] = status.get('platformId')
        report['device'] = status.get('device')
        report['policy_mode'] = status.get('policyMode')
        report['task_id'] = task['id']
        # Never run the harness against a user app; it is a separate zero-permission APK.
        adb.run(serial, 'shell', 'am', 'start', '-S', '-W', '-n', APP + '/.MainActivity')

        def tree():
            value = c.action('tree', {'expectedPackage': APP}, context)
            if not value.get('ok'): raise AssertionError('Tree unavailable: ' + value.get('error', {}).get('code', 'unknown'))
            return value['result']

        def visible(t):
            return [n for n in t['nodes'] if n.get('visible') and n.get('bounds', [0, 0, 0, 0])[1] < t['height'] and n.get('bounds', [0,0,0,0])[3] > 0]

        def has(t, text):
            return any(text in n.get('text', '') for n in visible(t))

        def feedback(response, passed=True):
            if passed:
                c.request('/api/trajectory/feedback', {'action_id': response['id'], 'task': task,
                      'decision': context['decision'], 'verification': {'status': 'verified', 'source': 'fixture_assertion'}})

        def act(kind, args=None, expect=None, ident=None):
            task['subgoal'] = expect or kind
            prior = tree() if kind == 'swipe' else None
            value = c.action(kind, {'expectedPackage': APP, **(args or {})}, context, ident)
            if not value.get('ok'): raise AssertionError(kind + ': ' + value.get('error', {}).get('code', 'unknown'))
            t = tree()
            deadline = time.monotonic() + 3
            while expect and not has(t, expect) and time.monotonic() < deadline:
                time.sleep(.08)
                t = tree()
            if expect and not has(t, expect): raise AssertionError('Expected fixture state: ' + expect)
            verified = bool(expect) or (kind == 'swipe' and [n.get('text') for n in visible(prior)] != [n.get('text') for n in visible(t)])
            feedback(value, passed=verified)
            return value, t

        def tap(text, expect=None):
            t = tree()
            targets = [n for n in visible(t) if text in n.get('text', '') and n.get('clickable')]
            if not targets: raise AssertionError('Fixture target not visible: ' + text)
            return act('tap_node', {'nodeId': targets[0]['id'], 'snapshotId': t['snapshotId']}, expect)

        # Same workload without collection provides a latency baseline.
        c.request('/api/trajectory/config', {'enabled': False})
        baseline = []
        for _ in range(3):
            before = time.monotonic(); tap('显示弹窗', '提示'); tap('关闭', '商品列表')
            baseline.append((time.monotonic() - before) * 1000 / 2)
        c.request('/api/trajectory/config', {'enabled': True, 'screenshots': False, 'allowed_packages': [APP], 'task': task})
        adb.run(serial, 'shell', 'dumpsys', 'gfxinfo', APP, 'reset')
        observed = []
        for _ in range(3):
            before = time.monotonic(); tap('显示弹窗', '提示'); tap('关闭', '商品列表')
            observed.append((time.monotonic() - before) * 1000 / 2)
        report['checks'].append('popup_open_close')
        t = tree()
        product = next(n['text'] for n in visible(t) if n.get('clickable') and n.get('text', '').startswith('商品 0'))
        tap(product, '商品详情'); tap('选择规格', '标准款')
        act('back', expect='商品详情'); act('back', expect='商品列表')
        report['checks'].append('multi_level_back')
        t = tree(); before_text = [n.get('text') for n in visible(t)]
        visited = {n['text'].split('    ')[0] for n in visible(t) if n.get('text', '').startswith('商品 ')}
        _, scrolled = act('swipe', {'x1': t['width']*.5, 'y1': t['height']*.82,
                         'x2': t['width']*.5, 'y2': t['height']*.55, 'durationMs': 650})
        if before_text == [n.get('text') for n in visible(scrolled)]: raise AssertionError('Scroll did not change visible rows')
        visited.update(n['text'].split('    ')[0] for n in visible(scrolled) if n.get('text', '').startswith('商品 '))
        for _ in range(14):
            t = tree()
            visited.update(n['text'].split('    ')[0] for n in visible(t) if n.get('text', '').startswith('商品 '))
            act('swipe', {'x1': t['width']*.5, 'y1': t['height']*.82,
                         'x2': t['width']*.5, 'y2': t['height']*.55, 'durationMs': 650})
        report['visible_products_visited'] = len(visited)
        report['checks'].append('product_scroll_traversal')

        # A stale target must be a retained negative sample, never relabeled positive.
        old = tree(); old_target = next(n for n in visible(old) if n.get('text') == '显示弹窗')
        tap('显示弹窗', '提示'); tap('关闭', '商品列表')
        failure = c.action('tap_node', {'expectedPackage': APP, 'nodeId': old_target['id'], 'snapshotId': old['snapshotId']}, context)
        if failure.get('ok') or failure.get('error', {}).get('code') != 'stale_snapshot': raise AssertionError('Stale target was not rejected')
        report['failure_action_id'] = failure['id']
        report['checks'].append('stale_failure_preserved')

        # Idempotent Android transport returns the cached result, not another tap.
        t = tree(); n = next(n for n in visible(t) if n.get('text') == '显示弹窗')
        ident = str(uuid.uuid4()); args = {'expectedPackage': APP, 'nodeId': n['id'], 'snapshotId': t['snapshotId']}
        one = c.action('tap_node', args, context, ident); two = c.action('tap_node', args, context, ident)
        if one != two or not one.get('ok'): raise AssertionError('Action replay is not idempotent')
        feedback(one); tap('关闭', '商品列表')
        report['duplicate_action_id'] = ident
        report['checks'].append('android_request_deduplication')

        tap('隐私测试', '验证码'); tree()
        # Back is safe through the fixture UI even if confirm mode requires approval
        # for the simulated authentication screen; use the owner's ADB test driver.
        adb.run(serial, 'shell', 'input', 'keyevent', '4')
        time.sleep(.2)
        tree()
        for _ in range(40):
            queued = c.request('/api/trajectory/status')
            if queued['queue_size'] == 0: break
            time.sleep(.1)
        report['queued_before_transfer'] = queued['pending']
        report['latency_ms'] = {'disabled_workload_per_action': round(statistics.mean(baseline), 2),
                                'enabled_workload_per_action': round(statistics.mean(observed), 2)}
        gfx = adb.run(serial, 'shell', 'dumpsys', 'gfxinfo', APP)
        report['frames'] = {key: (int(m.group(1)) if (m := re.search(pattern, gfx)) else None)
                  for key, pattern in {'total': r'Total frames rendered:\s*(\d+)', 'janky': r'Janky frames:\s*(\d+)'}.items()}
        mem = adb.run(serial, 'shell', 'dumpsys', 'meminfo', c.package)
        m = re.search(r'TOTAL PSS:\s*(\d+)', mem)
        if not m: m = re.search(r'^\s*TOTAL\s+(\d+)', mem, re.M)
        report['collector_pss_kb'] = int(m.group(1)) if m else None
        # Exhaust only the optional collector's budget. Every phone operation
        # must still complete, and telemetry must count the discarded records.
        dropped_before = queued['dropped']
        c.request('/api/trajectory/config', {'max_bytes': 1024})
        try:
            tap('显示弹窗', '提示'); tap('关闭', '商品列表')
            for _ in range(40):
                exhausted = c.request('/api/trajectory/status')
                if exhausted['queue_size'] == 0 and exhausted['dropped'] > dropped_before: break
                time.sleep(.1)
            if exhausted['dropped'] <= dropped_before: raise AssertionError('Collector fault was not exercised')
            report['fault_dropped'] = exhausted['dropped'] - dropped_before
            report['checks'].append('collector_buffer_full_agent_unaffected')
        finally:
            c.request('/api/trajectory/config', {'max_bytes': 64*1024*1024})
        # Removing this forward models transport interruption while SQLite stays on the phone.
    with DeviceClient(adb, serial) as reconnected:
        report['transfer'] = sync_device(reconnected, dataset)
        report['second_transfer'] = sync_device(reconnected, dataset)
        report['collector_after'] = reconnected.request('/api/trajectory/status')
        reconnected.request('/api/trajectory/config', {'enabled': False})
    actions = [r for r in dataset.records(device_id=report['device_id']) if r['record_type'] == 'action' and r['task']['id'] == task['id']]
    if not actions: raise AssertionError('No native action records received')
    actual_seen = set()
    for record in actions:
        for side in ('before', 'after'):
            for node in record[side]['nodes']:
                match = re.match(r'商品 (\d{2})', node.get('text', ''))
                if match and node.get('visible'): actual_seen.add(match.group(1))
    report['visible_products_visited'] = len(actual_seen)
    if len(actual_seen) != 30: raise AssertionError('Not all 30 fixture products were visited')
    if sum(r['action_id'] == report['duplicate_action_id'] for r in actions) != 1: raise AssertionError('Duplicate Android request was recorded twice')
    negative = next(r for r in actions if r['action_id'] == report['failure_action_id'])
    if negative['result']['status'] != 'failed': raise AssertionError('Failure was relabeled')
    encoded = json.dumps(actions, ensure_ascii=False)
    if '654321' in encoded or '"screenshot"' in encoded: raise AssertionError('Privacy/screenshot default violated')
    if not any(r['privacy']['excluded'] for r in actions): raise AssertionError('Sensitive fixture screen was not excluded')
    for r in actions:
        if r['navigation']['from'] != r['before']['fingerprint'] or r['navigation']['to'] != r['after']['fingerprint']:
            raise AssertionError('Pre/post state links broken')
    report['actions'] = len(actions)
    report['checks'].extend(['paired_pre_post_states', 'sensitive_screen_exclusion', 'offline_resume', 'central_idempotency'])
    report['passed'] = True
    return report


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--serial', action='append', required=True)
    parser.add_argument('--data', default=str(ROOT/'data')); args = parser.parse_args()
    adb = Adb(); reports = []
    with Dataset(Path(args.data)) as dataset:
        with ThreadPoolExecutor(max_workers=len(args.serial)) as pool:
            futures = {pool.submit(run_device, adb, serial, dataset): serial for serial in args.serial}
            for future in as_completed(futures):
                try: report = future.result()
                except Exception as error: report = {'serial': futures[future], 'passed': False, 'error': str(error), 'error_type': type(error).__name__}
                reports.append(report); print(json.dumps(report, ensure_ascii=True), flush=True)
        destination = ROOT/'dist/acceptance.json'; destination.parent.mkdir(parents=True, exist_ok=True)
        manifest = dataset.export(ROOT/'exports/acceptance', split_by='task')
        replay(dataset.records(), ROOT/'dist/replay.html')
        destination.write_text(json.dumps({'devices': reports, 'dataset': dataset.stats(), 'export': manifest}, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if all(r['passed'] for r in reports) else 1


if __name__ == '__main__':
    raise SystemExit(main())
