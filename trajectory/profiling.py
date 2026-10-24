"""A small repeatable fixture workload, with Android frame and process metrics."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import re
import statistics
import time
import uuid
from .transport import DeviceClient
from .collector import sync_device

APP = 'io.daxqo.trajectoryfixture'


def profile_device(adb, serial, dataset, cycles=4):
    with DeviceClient(adb, serial) as client:
        status = client.service_request('agent', '/api/status')
        if status.get('activeTaskId') or status.get('activeTaskIds'):
            raise RuntimeError('An owner task is active; profiling was not started')
        if not client.action('wake').get('ok'):
            raise RuntimeError('Phone needs owner unlock/control permission')
        task = {'id': str(uuid.uuid4()), 'user_task': '独立测试App采集性能测量',
                'subgoal': '打开关闭弹窗', 'stage': 'profiling', 'source': 'fixture'}
        report = {'serial': serial, 'phases': {}}

        def observe():
            response = client.action('tree', {'expectedPackage': APP})
            if not response.get('ok'): raise RuntimeError('Fixture tree unavailable')
            return response['result']

        def tap(label, expected):
            before = observe()
            node = next(n for n in before['nodes'] if n.get('visible') and n.get('clickable') and n.get('text')==label)
            started = time.monotonic()
            response = client.action('tap_node', {'expectedPackage': APP, 'nodeId': node['id'], 'snapshotId': before['snapshotId']})
            if not response.get('ok'): raise RuntimeError('Fixture action failed')
            deadline = time.monotonic()+3
            while True:
                after = observe()
                if any(expected in n.get('text','') for n in after['nodes'] if n.get('visible')): break
                if time.monotonic()>deadline: raise RuntimeError('Fixture did not reach expected page')
                time.sleep(.05)
            return (time.monotonic()-started)*1000

        def pss():
            raw = adb.run(serial, 'shell', 'dumpsys', 'meminfo', client.package)
            match = re.search(r'TOTAL PSS:\s*(\d+)', raw) or re.search(r'^\s*TOTAL\s+(\d+)',raw,re.M)
            return int(match.group(1)) if match else None

        def frame_metrics():
            for _ in range(6):
                try:
                    raw = adb.run(serial, 'exec-out', 'run-as', APP, 'cat', 'files/fixture-frame-metrics.json')
                    value=json.loads(raw)
                    value['available'] = value.get('total_frames',0)>0
                    return value
                except Exception:
                    time.sleep(.05)
            return {'available': False, 'reason': 'frame_metrics_unavailable'}

        try:
            for enabled in (False, True):
                client.request('/api/trajectory/config', {'enabled': enabled, 'screenshots': False, 'task': task, 'allowed_packages':[APP]})
                adb.run(serial, 'shell', 'am', 'start', '-S', '-W', '-n', APP+'/.MainActivity')
                time.sleep(.25)
                timings=[]
                for _ in range(cycles):
                    timings.extend([tap('显示弹窗','提示'),tap('关闭','商品列表')])
                report['phases']['enabled' if enabled else 'disabled']={
                    'actions':len(timings), 'workload_action_average_ms':round(statistics.mean(timings),2),
                    'workload_action_max_ms':round(max(timings),2), 'process_pss_kb':pss(), 'frame_metrics':frame_metrics()}
            enabled,disabled=report['phases']['enabled'],report['phases']['disabled']
            report['workload_action_overhead_ms']=round(enabled['workload_action_average_ms']-disabled['workload_action_average_ms'],2)
            report['process_pss_delta_kb']=(enabled['process_pss_kb']-disabled['process_pss_kb']) if enabled['process_pss_kb'] is not None and disabled['process_pss_kb'] is not None else None
            report['limitations']=['PSS includes the existing DAXQO process; the difference is a short-run observation, not isolated collector memory.',
              'Frame deadline misses are frame overruns; drop_count counts lost metric callbacks, not physical dropped frames.']
            time.sleep(.3);report['transfer']=sync_device(client,dataset)
        finally:
            client.request('/api/trajectory/config', {'enabled':False})
        return report


def profile_all(adb,dataset,serials=None):
    selected=[d['serial'] for d in adb.devices() if d['connection']=='device' and (not serials or d['serial'] in serials)]
    reports=[]
    with ThreadPoolExecutor(max_workers=max(1,len(selected))) as pool:
        futures={pool.submit(profile_device,adb,s,dataset):s for s in selected}
        for future in as_completed(futures):
            try:reports.append(future.result())
            except Exception as error:reports.append({'serial':futures[future],'error':str(error)})
    return reports
