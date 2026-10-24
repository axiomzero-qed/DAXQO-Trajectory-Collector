"""Rebuild the local inspection/export artifacts from one SQLite snapshot."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from trajectory.dataset import Dataset
from trajectory.cli import replay


def main():
    with Dataset(ROOT/'data') as dataset:
        dataset._db.execute('BEGIN')
        try:
            stats=dataset.stats();records=dataset.records()
            manifests={mode:dataset.export(ROOT/'exports'/('v0.1' if mode=='task' else 'by-'+mode),split_by=mode)
                       for mode in ('task','device','app')}
            replay(records,ROOT/'dist/replay.html')
            dataset._db.execute('COMMIT')
        except Exception:
            dataset._db.execute('ROLLBACK');raise
    summary={'generated_at':datetime.now(timezone.utc).isoformat(),'schema_version':'daxqo.trajectory/0.1',
             'dataset':stats,'exports':manifests,
             'scripted_acceptance':json.loads((ROOT/'dist/acceptance.json').read_text(encoding='utf-8'))['devices'],
             'deepseek_acceptance':json.loads((ROOT/'dist/deepseek-acceptance.json').read_text(encoding='utf-8')),
             'performance':json.loads((ROOT/'dist/performance.json').read_text(encoding='utf-8')),
             'receiver_policy':'Use one coordinated phone operator/ACK receiver at a time; shared data root is Collector/data.'}
    (ROOT/'dist/final-summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'records':stats['records'],'devices':stats['devices'],'scoring_samples':manifests['task']['counts'],
                      'scripted_passed':all(r['passed'] for r in summary['scripted_acceptance']),
                      'real_model_passed':all(r['passed'] for r in summary['deepseek_acceptance'])}))


if __name__=='__main__':main()
