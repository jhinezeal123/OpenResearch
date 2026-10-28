import concurrent.futures
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'python'))
from pool import Pool
import processes
import scheduler


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.pool=Pool(temporary.name)

    def test_parallel_requests_start_only_one_scheduler(self):
        with patch.object(scheduler.subprocess,'Popen',return_value=SimpleNamespace(pid=os.getpid())) as spawn:
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                list(executor.map(lambda _:scheduler.ensure_scheduler(self.pool),range(16)))
            self.assertEqual(spawn.call_count,1)

    def test_acquire_waits_for_a_scheduler_that_is_finishing_its_exit(self):
        started=threading.Event()
        def acquire():
            started.set();scheduler.ensure_scheduler(self.pool)
        with patch.object(scheduler.subprocess,'Popen',return_value=SimpleNamespace(pid=os.getpid())) as spawn, \
                concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            with processes.lock(self.pool.home/'scheduler-start.lock'):
                future=executor.submit(acquire)
                self.assertTrue(started.wait(1))
                time.sleep(.1)
                self.assertFalse(future.done())
            future.result(timeout=3)
            spawn.assert_called_once()

    def test_heartbeat_for_another_process_instance_cannot_keep_an_owner_healthy(self):
        metadata={**processes.owner(os.getpid()),'started':time.time()-181}
        heartbeat={**metadata,'birth':'reused-pid','at':time.time()}
        self.assertFalse(scheduler.healthy(metadata,heartbeat))
        self.assertTrue(scheduler.healthy(metadata,{**metadata,'at':time.time()}))

    def test_sweep_recovers_owners_and_marks_idle_expiry_without_freeing_accounts(self):
        import uuid
        request={'runId':str(uuid.uuid4()),'projectId':'p','accelerator':'cpu','ttlSeconds':3600,
                 'competitionSources':[],'datasetSources':[],'runtime':{'accounts':['a']}}
        row=self.pool.acquire(request,['a'])
        self.pool.ready(row['id'],123);self.pool.release(row['id'],row['active_run'])
        with self.pool.db() as db: db.execute('UPDATE sessions SET idle_since=0')
        with patch('bridge.ensure_worker') as ensure, patch.object(scheduler,'cleanup') as cleanup:
            self.assertTrue(scheduler.sweep(self.pool))
            ensure.assert_not_called()
            self.assertEqual(cleanup.call_args.args[1]['state'],'closing')
        self.assertEqual(self.pool.get(row['id'])['state'],'closing')
        self.pool.stopped(row['id'])
        with patch('bridge.ensure_worker') as ensure:
            self.assertFalse(scheduler.sweep(self.pool))
            ensure.assert_not_called()

    def test_many_closing_sessions_share_bounded_cleanup_and_backoff(self):
        import uuid
        rows=[]
        for account in ('a','b','c','d','e','f'):
            row=self.pool.acquire({'runId':str(uuid.uuid4()),'projectId':'p','accelerator':'cpu',
                                  'ttlSeconds':3600,'runtime':{'accounts':[account]}},[account])
            self.pool.close(row['id']);rows.append(row)
        with patch('bridge.ensure_worker') as ensure, patch('bridge.configure'), \
                patch('worker.shutdown',return_value=(False,False)) as shutdown:
            for _ in rows:
                self.assertTrue(scheduler.sweep(self.pool))
            ensure.assert_not_called()
            self.assertEqual(shutdown.call_count,6)
            self.assertEqual({c.args[1] for c in shutdown.call_args_list},{r['id'] for r in rows})
            scheduler.sweep(self.pool)
            self.assertEqual(shutdown.call_count,6,'backoff must survive repeated dashboard polls')
        self.assertTrue(all(r['state']=='closing' for r in self.pool.live()))

    def test_cleanup_persists_stop_request_and_releases_only_with_receipt(self):
        import uuid
        row=self.pool.acquire({'runId':str(uuid.uuid4()),'projectId':'p','accelerator':'cpu',
                               'ttlSeconds':3600,'runtime':{'accounts':['a']}},['a'])
        self.pool.close(row['id'])
        with patch('bridge.configure'), patch('worker.shutdown',side_effect=[(False,True),(True,True)]) as shutdown:
            scheduler.cleanup(self.pool,row)
            self.assertEqual(self.pool.get(row['id'])['state'],'closing')
            self.assertTrue(processes.load(self.pool.worker_dir(row['id'])/'stop-requested.json')['requested'])
            scheduler.cleanup(self.pool,row)
            self.assertTrue(shutdown.call_args.args[-1])
        self.assertEqual(self.pool.get(row['id'])['state'],'stopped')

    def test_cleanup_frees_a_quota_rejected_closing_session_and_its_run(self):
        import json, uuid
        run=str(uuid.uuid4())
        row=self.pool.acquire({'runId':run,'projectId':'p','accelerator':'NvidiaTeslaT4',
                               'ttlSeconds':3600,'runtime':{'accounts':['a']}},['a'])
        self.pool.close(row['id'],'Notebook provisioning exceeded 10 minutes')
        response=self.pool.home/'sessions'/row['id']/'push-response.json'
        response.parent.mkdir(parents=True)
        response.write_text(json.dumps({'error':'Maximum weekly GPU quota of 30.00 hours reached.'}))
        with patch('bridge.configure'), patch('bridge.control',side_effect=AssertionError('no provider call')):
            scheduler.cleanup(self.pool,self.pool.get(row['id']))
        current=self.pool.get(row['id'])
        self.assertEqual(current['state'],'stopped')
        self.assertIn('Maximum weekly GPU quota',current['note'])
        self.assertIsNone(self.pool.assigned(run))
        self.assertIn('a',self.pool.blocked('NvidiaTeslaT4'))

    def test_cleanup_waits_for_existing_worker_to_release_lifetime_lock(self):
        import uuid
        row=self.pool.acquire({'runId':str(uuid.uuid4()),'projectId':'p','accelerator':'cpu',
                               'ttlSeconds':3600,'runtime':{'accounts':['a']}},['a'])
        self.pool.close(row['id'])
        with processes.lock(self.pool.worker_dir(row['id'])/'worker.lock'), patch('worker.shutdown') as shutdown:
            scheduler.cleanup(self.pool,row)
            shutdown.assert_not_called()
        self.assertGreater(processes.load(self.pool.worker_dir(row['id'])/'cleanup.json')['retry_at'],time.time())


if __name__=='__main__': unittest.main()
