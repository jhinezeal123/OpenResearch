import concurrent.futures
import json
from pathlib import Path
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'python'))
from pool import Pool

def request(project='project-a'):
    return {'runId':str(uuid.uuid4()),'projectId':project,'accelerator':'cpu','ttlSeconds':600,
            'competitionSources':[],'datasetSources':[],'runtime':{'accounts':['a','b']}}

class PoolTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.pool=Pool(self.temp.name)
    def tearDown(self): self.temp.cleanup()
    def test_failure_then_new_source_reuses_session_but_never_the_run_identity(self):
        first=request();a=self.pool.acquire(first,['a','b'])
        self.pool.ready(a['id'],123);self.pool.release(a['id'],first['runId'])
        next_run=request();next_run['digest']='a-new-commit'
        b=self.pool.acquire(next_run,[])
        self.assertEqual(a['id'],b['id']);self.assertEqual(b['active_run'],next_run['runId'])
        self.assertEqual(self.pool.assigned(first['runId'])['id'],a['id'])
    def test_parallel_admission_is_atomic_and_overflow_waits(self):
        jobs=[request() for _ in range(10)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            rows=list(executor.map(lambda r:self.pool.acquire(r,['a','b']),jobs))
        rows=[r for r in rows if r]
        self.assertEqual(len(rows),2)
        self.assertEqual({r['account'] for r in rows},{'a','b'})
        self.assertEqual(len({r['id'] for r in rows}),2)
    def test_failed_bootstrap_closes_with_a_reason_that_survives_later_closes(self):
        r=request();row=self.pool.acquire(r,['a'])
        self.pool.close(row['id'],'Kaggle bootstrap failed 5 times in a row: OSError')
        self.pool.close(row['id'])
        closed=self.pool.get(row['id'])
        self.assertEqual(closed['state'],'closing')
        self.assertEqual(closed['note'],'Kaggle bootstrap failed 5 times in a row: OSError')
    def test_reconnect_keeps_account_even_when_the_pool_order_changes(self):
        r=request();first=self.pool.acquire(r,['a','b'])
        r['runtime']['accounts']=['b']
        self.assertEqual(first,self.pool.acquire(r,['b']))
    def test_cancelled_run_does_not_close_notebook_or_release_another_run(self):
        r=request();row=self.pool.acquire(r,['a']);self.pool.ready(row['id'],123)
        self.pool.release(row['id'],r['runId'])
        second=request();assigned=self.pool.acquire(second,[])
        self.pool.release(row['id'],r['runId'])
        self.assertEqual(self.pool.get(row['id'])['active_run'],second['runId'])
        self.assertEqual(assigned['id'],row['id'])
    def test_unconfirmed_stop_reserves_account_and_confirmation_frees_it(self):
        r=request();r['runtime']['accounts']=['a'];row=self.pool.acquire(r,['a'])
        self.pool.close(row['id'])
        new=request();new['runtime']['accounts']=['a']
        self.assertIsNone(self.pool.acquire(new,['a']))
        self.pool.stopped(row['id'])
        self.assertNotEqual(self.pool.acquire(new,['a'])['id'],row['id'])
    def test_mounts_project_and_expiry_bound_reuse(self):
        r=request();row=self.pool.acquire(r,['a']);self.pool.ready(row['id'],123);self.pool.release(row['id'],r['runId'])
        other=request('project-b')
        self.assertIsNone(self.pool.acquire(other,[]))
        other=request();other['datasetSources']=['owner/new-data']
        self.assertIsNone(self.pool.acquire(other,[]))
        with self.pool.db() as db: db.execute('UPDATE sessions SET deadline=0')
        self.assertIsNone(self.pool.acquire(request(),[]))
    def test_worker_metadata_does_not_claim_the_native_bootstrap_directory(self):
        row=self.pool.acquire(request(),['a'])
        self.pool.worker_dir(row['id']).mkdir(parents=True)
        native=self.pool.home/'sessions'/row['id']
        self.assertFalse(native.exists())
        native.mkdir(parents=True)  # Native prepare must be able to claim this exclusively.
    def test_bad_run_identity_never_creates_a_lease(self):
        r=request();r['runId']='../../escape'
        with self.assertRaises(ValueError):self.pool.acquire(r,['a'])
        self.assertFalse(self.pool.list())

if __name__=='__main__':unittest.main()
