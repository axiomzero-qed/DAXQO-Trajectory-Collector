"""Run a real existing DeepSeek Harness task against the isolated fixture only."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from trajectory.transport import Adb, DeviceClient
from trajectory.collector import sync_device
from trajectory.dataset import Dataset

APP = 'io.daxqo.trajectoryfixture'
PROMPT = ('这是独立测试App的真实Agent采集验收。仅操作 io.daxqo.trajectoryfixture，它已打开。'
          '请使用安卓工具完成：点击商品01进入商品详情，点击选择规格，然后系统返回两次回商品列表；'
          '打开显示弹窗并关闭；向下滚动商品列表直到商品30，读出其价格。'
          '不要打开其他App，不要访问隐私测试，不要截图，不要写文件或使用终端代替手机操作。'
          '完成后用一句中文报告商品30价格。')


def contains(state, text):
    return any(text in n.get('text', '') for n in state.get('nodes', []) if n.get('visible', True))


def verified_fixture_transition(record):
    before, after, action = record['before'], record['after'], record['action']
    if record['result']['status'] != 'succeeded': return False
    if not before['training_allowed'] or not after['training_allowed']: return False
    if before['package_name'] != APP or after['package_name'] != APP: return False
    target = action.get('target') or {}
    label = target.get('text', '') if isinstance(target, dict) else str(target)
    if action['type'] == 'swipe':
        return before['fingerprint'] != after['fingerprint']
    if action['type'] in ('tap_node', 'tap'):
        rules = [('商品 01', '商品详情'), ('选择规格', '标准款'), ('显示弹窗', '这是用于轨迹验收'), ('关闭', '商品列表')]
        return any(match in label and contains(after, expected) for match, expected in rules)
    if action['type'] == 'back':
        return ((contains(before, '标准款') and contains(after, '商品详情')) or
                (contains(before, '商品详情') and contains(after, '商品列表')))
    return False


def run(serial, dataset, seconds):
    adb = Adb()
    report = {'serial': serial, 'passed': False, 'real_model': True}
    with DeviceClient(adb, serial) as c:
        status = c.service_request('agent', '/api/status')
        if status.get('activeTaskId') or status.get('activeTaskIds'):
            raise RuntimeError('An owner task is active; no test was started')
        native = c.request('/api/device/status')
        report['platform'] = native['platformId']; report['harness_version'] = status['app']['version']
        report['model'] = status.get('provider', {}).get('model')
        awake = c.action('wake')
        if not awake.get('ok'): raise RuntimeError('Phone needs owner unlock/control permission')
        adb.run(serial, 'shell', 'am', 'start', '-S', '-W', '-n', APP+'/.MainActivity')
        task = {'id': str(uuid.uuid4()), 'user_task': PROMPT, 'subgoal': '', 'stage': 'queued', 'source': 'agent'}
        c.request('/api/trajectory/config', {'enabled': True, 'screenshots': False, 'allowed_packages': [APP], 'task': task})
        request_id = 'trajectory-' + str(uuid.uuid4())
        created = c.service_request('agent', '/api/tasks', {'title': '轨迹采集真实模型验收', 'prompt': PROMPT, 'clientRequestId': request_id})
        task['id'] = created['task']['id']; task['stage'] = 'running'
        c.request('/api/trajectory/config', {'task': task})
        report['task_id'] = task['id']; started = time.monotonic(); last = None
        while time.monotonic() - started < seconds:
            detail = c.service_request('agent', '/api/tasks/' + task['id'])
            current = detail['task']['status']
            if current != last:
                print(json.dumps({'serial': serial, 'task_id': task['id'], 'status': current}), flush=True); last = current
            if current not in ('running', 'queued'): break
            time.sleep(1)
        else:
            c.service_request('agent', '/api/tasks/'+task['id']+'/stop', {})
            raise RuntimeError('Test task exceeded its time budget and was stopped')
        report['task_status'] = current; report['elapsed_seconds'] = round(time.monotonic()-started, 2)
        # Drain the bounded model-feedback worker before reading its durable spool.
        time.sleep(2)
        before = c.request('/api/trajectory/status'); report['device_id'] = before['device_id']
        report['transfer'] = sync_device(c, dataset)
        records = dataset.records(device_id=report['device_id'])
        actions = [r for r in records if r['record_type']=='action' and r['task']['id']==task['id']]
        ids = {r['action_id'] for r in actions}
        feedback = [r for r in records if r['record_type']=='agent_feedback' and r['action_id'] in ids
                    and r.get('decision',{}).get('source')=='deepseek_agent']
        report['actions'] = len(actions); report['model_feedback'] = len(feedback)
        report['distinct_correlated_actions'] = len({r['action_id'] for r in feedback})
        report['decision_task_ids_match'] = all(r['task']['id']==task['id'] for r in feedback)
        independent = 0
        for action in actions:
            if verified_fixture_transition(action):
                c.request('/api/trajectory/feedback', {'action_id': action['action_id'], 'task': task, 'decision': {},
                          'verification': {'status': 'verified', 'source': 'fixture_assertion'}})
                independent += 1
        report['independently_verified_actions'] = independent
        observed = c.action('tree', {'expectedPackage': APP})
        report['final_product_30_visible'] = observed.get('ok') and any('商品 30' in n.get('text','') and n.get('visible') for n in observed.get('result',{}).get('nodes',[]))
        report['no_screenshot_actions'] = all(r['action']['type']!='screenshot' for r in actions)
        time.sleep(.3); sync_device(c, dataset)
        c.request('/api/trajectory/config', {'enabled': False})
        report['passed'] = (current=='completed' and bool(actions) and bool(feedback) and report['decision_task_ids_match']
                            and report['final_product_30_visible'] and report['no_screenshot_actions'])
        return report


def main():
    p=argparse.ArgumentParser();p.add_argument('--serial',action='append',required=True);p.add_argument('--seconds',type=int,default=240)
    p.add_argument('--data',default=str(ROOT/'data'));args=p.parse_args();reports=[]
    with Dataset(Path(args.data)) as dataset:
        with ThreadPoolExecutor(max_workers=len(args.serial)) as pool:
            futures={pool.submit(run,s,dataset,args.seconds):s for s in args.serial}
            for f in as_completed(futures):
                try:r=f.result()
                except Exception as e:r={'serial':futures[f],'passed':False,'error':str(e),'error_type':type(e).__name__}
                reports.append(r);print(json.dumps(r,ensure_ascii=True),flush=True)
                single = ROOT / ('dist/deepseek-' + futures[f] + '.json')
                single.parent.mkdir(parents=True, exist_ok=True)
                single.write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding='utf-8')
        dest=ROOT/'dist/deepseek-acceptance.json';dest.parent.mkdir(parents=True,exist_ok=True)
        dest.write_text(json.dumps(reports,ensure_ascii=False,indent=2),encoding='utf-8')
    return 0 if all(r['passed'] for r in reports) else 1


if __name__=='__main__':raise SystemExit(main())
