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
        with patch('bridge.ensure_worker') as ensure:
            self.assertTrue(scheduler.sweep(self.pool))
            self.assertEqual(ensure.call_args.args[2]['state'],'closing')
            self.assertTrue(ensure.call_args.args[3]['_scheduler'])
        self.assertEqual(self.pool.get(row['id'])['state'],'closing')
        self.pool.stopped(row['id'])
        with patch('bridge.ensure_worker') as ensure:
            self.assertFalse(scheduler.sweep(self.pool))
            ensure.assert_not_called()


if __name__=='__main__': unittest.main()
