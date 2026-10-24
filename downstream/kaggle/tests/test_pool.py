import concurrent.futures
import json
from pathlib import Path
import sys
import tempfile
import time
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
    def test_gpu_accounts_hold_two_notebooks_but_cpu_stays_exclusive(self):
        gpu=lambda:{**request(),'accelerator':'NvidiaTeslaT4'}
        jobs=[gpu() for _ in range(10)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            rows=[r for r in executor.map(lambda r:self.pool.acquire(r,['a','b']),jobs) if r]
        self.assertEqual(sorted(r['account'] for r in rows),['a','a','b','b'])
        self.assertFalse(self.pool.has_room('a','NvidiaTeslaT4'))
        self.assertFalse(self.pool.has_room('a','cpu'))
        self.pool.stopped(rows[0]['id'])
        self.assertTrue(self.pool.has_room(rows[0]['account'],'NvidiaTeslaT4'))
        self.assertFalse(self.pool.has_room(rows[0]['account'],'cpu'))
    def test_a_cpu_notebook_blocks_a_gpu_partner_and_foreign_sessions_take_slots(self):
        gpu={**request(),'accelerator':'NvidiaTeslaT4','runtime':{'accounts':['a']}}
        self.pool.acquire({**request(),'runtime':{'accounts':['a']}},['a'])
        self.assertIsNone(self.pool.acquire(gpu,['a'],reclaim=False))
        self.assertIsNone(self.pool.acquire({**gpu,'runtime':{'accounts':['b']}},{'b':2}))
        self.assertEqual(self.pool.acquire({**gpu,'runtime':{'accounts':['b']}},{'b':1})['account'],'b')
        self.assertFalse(self.pool.has_room('b','NvidiaTeslaT4',1))
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
    def test_idle_grace_expires_without_closing_a_running_experiment(self):
        idle=self.pool.acquire(request(),['a'])
        self.pool.ready(idle['id'],123);self.pool.release(idle['id'],idle['active_run'])
        busy=self.pool.acquire(request('different-project'),['b'])
        self.pool.ready(busy['id'],124)
        with self.pool.db() as db:
            db.execute('UPDATE sessions SET deadline=?,idle_since=?',(time.time()+3600,time.time()-301))
        self.pool.reconcile()
        self.assertEqual(self.pool.get(idle['id'])['state'],'closing')
        self.assertEqual(self.pool.get(busy['id'])['state'],'busy')
        self.assertEqual(self.pool.get(busy['id'])['active_run'],busy['active_run'])
    def test_pressure_reclaims_one_idle_incompatible_notebook_and_keeps_account_reserved(self):
        for account in ('a','b'):
            r=request();r['runtime']['accounts']=[account]
            row=self.pool.acquire(r,[account])
            self.pool.ready(row['id'],123);self.pool.release(row['id'],row['active_run'])
        waiting=request('another-project')
        for _ in range(3): self.assertIsNone(self.pool.acquire(waiting,['a','b']))
        closing=[r for r in self.pool.live() if r['state']=='closing']
        self.assertEqual(len(closing),1)
        self.pool.stopped(closing[0]['id'])
        self.assertEqual(self.pool.acquire(waiting,[closing[0]['account']])['account'],closing[0]['account'])
    def test_reuse_probe_does_not_evict_a_warm_notebook_before_account_admission(self):
        row=self.pool.acquire(request(),['a'])
        self.pool.ready(row['id'],123);self.pool.release(row['id'],row['active_run'])
        waiting=request('other-project')
        self.assertIsNone(self.pool.acquire(waiting,[],reclaim=False))
        self.assertEqual(self.pool.get(row['id'])['state'],'idle')
        self.assertEqual(self.pool.acquire(waiting,['b'])['account'],'b')
        self.assertEqual(self.pool.get(row['id'])['state'],'idle')
    def test_fifo_prevents_a_new_run_from_stealing_a_contended_account(self):
        first=request();first['runtime']['accounts']=['a']
        row=self.pool.acquire(first,['a'])
        second={**request(),'runtime':first['runtime']}
        third={**request(),'runtime':first['runtime']}
        self.assertIsNone(self.pool.acquire(second,[]))
        self.assertIsNone(self.pool.acquire(third,[]))
        self.pool.stopped(row['id'])
        self.assertIsNone(self.pool.acquire(third,['a']))
        self.assertIsNotNone(self.pool.acquire(second,['a']))
    def test_a_waiting_run_does_not_block_disjoint_accounts_and_cancel_removes_it(self):
        a={**request(),'runtime':{'accounts':['a']}}
        b={**request(),'runtime':{'accounts':['b']}}
        self.assertIsNone(self.pool.acquire(a,[]))
        self.assertIsNotNone(self.pool.acquire(b,['b']))
        self.pool.cancel_wait(a['runId'])
        self.assertIsNotNone(self.pool.acquire({**a,'runId':str(uuid.uuid4())},['a']))
    def test_abandoned_waiters_expire(self):
        a={**request(),'runtime':{'accounts':['a']}}
        self.pool.acquire(a,[])
        with self.pool.db() as db: db.execute('UPDATE pending_runs SET last_seen=0')
        self.assertIsNotNone(self.pool.acquire({**a,'runId':str(uuid.uuid4())},['a']))
    def test_provisioning_timeout_and_ttl_begin_closing_but_never_free_the_account(self):
        a=self.pool.acquire(request(),['a'])
        b=self.pool.acquire(request(),['b'])
        with self.pool.db() as db:
            db.execute('UPDATE sessions SET created=?,deadline=? WHERE id=?',(time.time()-601,time.time()+3600,a['id']))
            db.execute('UPDATE sessions SET deadline=0 WHERE id=?',(b['id'],))
        self.pool.reconcile()
        self.assertEqual(self.pool.get(a['id'])['note'],'Notebook provisioning exceeded 10 minutes')
        self.assertEqual(self.pool.get(b['id'])['note'],'Notebook lifetime expired')
        self.assertIsNone(self.pool.acquire(request(),['a','b']))
    def test_delayed_ready_or_release_cannot_resurrect_a_closed_session(self):
        row=self.pool.acquire(request(),['a'])
        self.pool.stopped(row['id'])
        self.pool.ready(row['id'],123)
        self.pool.release(row['id'],row['active_run'])
        self.assertEqual(self.pool.get(row['id'])['state'],'stopped')
    def test_upload_activity_extends_only_the_current_run_launch_grace(self):
        row=self.pool.acquire(request(),['a'])
        with self.pool.db() as db: db.execute('UPDATE sessions SET leased_at=0')
        self.pool.touch(row['id'],str(uuid.uuid4()))
        self.assertEqual(self.pool.get(row['id'])['leased_at'],0)
        self.pool.touch(row['id'],row['active_run'])
        self.assertGreater(self.pool.get(row['id'])['leased_at'],time.time()-10)
        self.assertEqual(self.pool.get(row['id'])['deadline'],row['deadline'])
    def test_maintenance_is_not_limited_to_the_last_hundred_history_rows(self):
        row=self.pool.acquire(request(),['a'])
        with self.pool.db() as db:
            for _ in range(101):
                db.execute('INSERT INTO sessions(id,account,spec,created,deadline,state) VALUES(?,?,?,?,?,?)',
                           (uuid.uuid4().hex,'old','{}',time.time()+1,0,'stopped'))
        self.assertNotIn(row['id'],[r['id'] for r in self.pool.list()])
        self.assertEqual([r['id'] for r in self.pool.live()],[row['id']])
    def test_old_schema_is_migrated_without_losing_reservations(self):
        import sqlite3
        from contextlib import closing
        with tempfile.TemporaryDirectory() as home:
            with closing(sqlite3.connect(Path(home)/'pool.sqlite3')) as db, db:
                db.execute('''CREATE TABLE sessions(id TEXT PRIMARY KEY,account TEXT NOT NULL,spec TEXT NOT NULL,
                    created REAL NOT NULL,deadline REAL NOT NULL,state TEXT NOT NULL,active_run TEXT,
                    worker_pid INTEGER,note TEXT)''')
                db.execute("INSERT INTO sessions VALUES('old','a','{}',1,9999999999,'idle',NULL,NULL,NULL)")
                db.execute("CREATE UNIQUE INDEX one_live_notebook_per_account ON sessions(account) WHERE state != 'stopped'")
            migrated=Pool(home)
            self.assertEqual(migrated.live()[0]['account'],'a')
            self.assertGreater(migrated.live()[0]['idle_since'],time.time()-10)
            Pool(home)  # Reopening is idempotent and does not reset idle time.
            gpu={**request(),'accelerator':'NvidiaTeslaT4','runtime':{'accounts':['c']}}
            for _ in range(2): self.assertIsNotNone(migrated.acquire({**gpu,'runId':str(uuid.uuid4())},['c']))

if __name__=='__main__':unittest.main()
