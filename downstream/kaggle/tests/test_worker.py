from pathlib import Path
import concurrent.futures
import json
import os
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'python'))
import worker
import bridge
from pool import Pool
import processes

class WorkerShutdownTests(unittest.TestCase):
    def test_after_stop_the_next_poll_checks_provider_without_reopening_ssh(self):
        actions=[]
        def control(session,action,*args):
            actions.append(action)
            if actions==['status','stop','stop']: raise ConnectionError('SSH already closed')
            return {'stopped':len(actions)==3}
        with patch('bridge.control',control):
            stopped,requested=worker.shutdown(None,'session','account',Path('.'),False)
            self.assertFalse(stopped);self.assertTrue(requested)
            stopped,requested=worker.shutdown(None,'session','account',Path('.'),requested)
            self.assertTrue(stopped)
        self.assertEqual(actions,['status','stop','status'])
    def test_already_stopped_provider_does_not_require_ssh(self):
        with patch('bridge.control',return_value={'stopped':True}) as control:
            stopped,_=worker.shutdown(None,'session','account',Path('.'),False)
            self.assertTrue(stopped)
            self.assertEqual(control.call_args.args[1],'status')
            self.assertEqual(control.call_count,1)
    def test_unconfirmed_outcomes_do_not_free_the_account(self):
        with patch('bridge.control',side_effect=ConnectionError('offline')):
            self.assertEqual(worker.shutdown(None,'session','account',Path('.'),False),(False,False))
    def test_never_submitted_request_can_be_closed(self):
        with patch('bridge.control',side_effect=[FileNotFoundError(),{'stopped':True}]):
            self.assertEqual(worker.shutdown(None,'session','account',Path('.'),False),(True,True))


class ChannelTests(unittest.TestCase):
    def test_lost_or_invalid_ack_discards_transport_without_replaying_the_request(self):
        session=SimpleNamespace(notebook_environment_command=lambda c:c,ssh_options=lambda s:[])
        channel=worker.Channel(session,{'address':'test'},None)
        bad=Mock(stdout=iter([b'not json\n']),stdin=Mock())
        bad.poll.return_value=None
        good=Mock(stdout=iter([b'{"ok":true,"result":{"stage":"RUNNING"}}\n']),stdin=Mock())
        good.poll.return_value=None
        with patch.object(worker,'spawn_tree',side_effect=[bad,good]) as spawn, \
                patch.object(worker,'stop_tree') as stop:
            with self.assertRaises(ConnectionError): channel.rpc({'action':'launch','runId':'same-run'})
            self.assertIsNone(channel.process)
            bad.stdin.write.assert_called_once()
            self.assertEqual(channel.rpc({'action':'inspect','runId':'same-run'})['stage'],'RUNNING')
            self.assertEqual(spawn.call_count,2)
            self.assertIn(unittest.mock.call(bad),stop.call_args_list)
            channel.close();channel.close()
            self.assertIsNone(channel.process)


class WorkerLifecycleTests(unittest.TestCase):
    """Exercise the real RPC loop/SQLite lifecycle with a local fake provider."""
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.pool=Pool(temporary.name)
        self.request={'runId':str(uuid.uuid4()),'projectId':'p','accelerator':'cpu','ttlSeconds':3600,
                      'competitionSources':[],'datasetSources':[],'runtime':{'accounts':['a']}}
        self.row=self.pool.acquire(self.request,['a'])
        self.config={**json.loads(self.row['spec']),'home':temporary.name,
                     'sessionId':self.row['id'],'account':'a'}
        self.channel=SimpleNamespace(connections=1,rpc=Mock(return_value={'stage':'RUNNING'}),close=Mock())
        self.start=Mock(return_value={'ssh_status':'READY'})
        self.proxy=Mock(return_value=None)
        session=SimpleNamespace(save=processes.save,state_for=lambda s:{})
        for mocked in (
            patch.object(bridge,'configure',return_value=session),
            patch.object(bridge,'control',return_value={'stopped':True}),
            patch('scheduler.ensure_scheduler'),
            patch('scheduler.healthy',return_value=False),
            patch.object(worker,'Channel',return_value=self.channel),
            patch.dict(sys.modules,{
                'interface_ai_scientist.bootstrap_service':SimpleNamespace(start=self.start),
                'proxy_control':SimpleNamespace(ensure_proxy=self.proxy,stop_proxies=Mock()),
                'auto_login.web_session':SimpleNamespace(protect_acl=lambda p:None),
            }),
        ):
            mocked.start();self.addCleanup(mocked.stop)
        self.executor=concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.future=None
        self.addCleanup(self.finish)

    def finish(self):
        if self.future:
            self.pool.close(self.row['id'])
            self.future.result(timeout=8)
        self.executor.shutdown(wait=True)

    def begin(self):
        self.future=self.executor.submit(worker.serve,self.config)

    def wait_for(self,predicate):
        deadline=time.monotonic()+5
        while not predicate():
            if self.future.done(): self.future.result();self.fail('worker exited before the expected state')
            if time.monotonic()>deadline: self.fail('worker did not reach the expected state')
            time.sleep(.02)

    def test_closing_owner_never_bootstraps_a_replacement_notebook(self):
        self.pool.close(self.row['id'])
        self.begin();self.future.result(timeout=5)
        self.start.assert_not_called();self.proxy.assert_not_called()
        self.assertEqual(self.pool.get(self.row['id'])['state'],'stopped')

    def test_closing_owner_hands_off_to_shared_scheduler_without_releasing_account(self):
        self.pool.close(self.row['id'])
        with patch('scheduler.healthy', return_value=True):
            self.begin();self.future.result(timeout=5)
        self.start.assert_not_called();self.proxy.assert_not_called()
        self.assertEqual(self.pool.get(self.row['id'])['state'],'closing')

    def test_external_stop_receipt_ends_the_owner_and_closes_its_channel(self):
        self.begin()
        self.wait_for(lambda:self.pool.get(self.row['id'])['state']=='busy')
        self.pool.stopped(self.row['id'])
        self.future.result(timeout=5)
        self.channel.close.assert_called()

    def test_unlaunched_abandoned_run_is_closed_but_a_running_run_is_preserved(self):
        self.channel.rpc.return_value={'stage':'NOT_STARTED'}
        with self.pool.db() as db: db.execute('UPDATE sessions SET leased_at=?',(time.time()-121,))
        self.begin();self.future.result(timeout=5)
        self.assertEqual(self.pool.get(self.row['id'])['state'],'stopped')
        self.assertIn('never launched',self.pool.get(self.row['id'])['note'])

    def test_long_running_remote_run_does_not_expire_with_the_client_launch_grace(self):
        with self.pool.db() as db: db.execute('UPDATE sessions SET leased_at=?',(time.time()-121,))
        self.begin()
        self.wait_for(lambda:self.channel.rpc.call_count>=2)
        self.assertEqual(self.pool.get(self.row['id'])['state'],'busy')
        self.assertEqual(self.pool.get(self.row['id'])['active_run'],self.request['runId'])

    def test_idle_notebook_drops_the_terminal_pipe_before_its_reuse_grace_expires(self):
        self.begin()
        self.wait_for(lambda:self.pool.get(self.row['id'])['state']=='busy')
        self.pool.release(self.row['id'],self.request['runId'])
        self.wait_for(lambda:self.channel.close.called)
        self.assertEqual(self.pool.get(self.row['id'])['state'],'idle')

    def test_failed_bootstrap_ping_closes_the_unpublished_channel_before_retry(self):
        failed=SimpleNamespace(rpc=Mock(side_effect=ConnectionError('no acknowledgement')),close=Mock())
        with patch.object(worker,'Channel',side_effect=[failed,self.channel]):
            self.begin()
            self.wait_for(lambda:self.pool.get(self.row['id'])['state']=='busy')
            failed.close.assert_called_once()


class OwnerRecoveryTests(unittest.TestCase):
    def test_closing_sessions_never_spawn_an_individual_worker(self):
        with tempfile.TemporaryDirectory() as home:
            pool=Pool(home)
            request={'runId':str(uuid.uuid4()),'projectId':'p','accelerator':'cpu','ttlSeconds':600,
                     'runtime':{'accounts':['a']}}
            row=pool.acquire(request,['a'])
            pool.close(row['id'])
            with patch('scheduler.ensure_scheduler') as ensure, patch('subprocess.Popen') as spawn:
                bridge.ensure_worker(None,pool,row,{})
                ensure.assert_called_once();spawn.assert_not_called()

    def test_repeated_startup_failures_back_off_instead_of_spawning_on_every_request(self):
        with tempfile.TemporaryDirectory() as home:
            pool=Pool(home)
            request={'runId':str(uuid.uuid4()),'projectId':'p','accelerator':'cpu','ttlSeconds':600,
                     'runtime':{'accounts':['a']}}
            row=pool.acquire(request,['a'])
            with patch('scheduler.ensure_scheduler'), \
                    patch('subprocess.Popen',return_value=SimpleNamespace(pid=0)) as spawn:
                bridge.ensure_worker(None,pool,row,{})
                bridge.ensure_worker(None,pool,row,{})
                self.assertEqual(spawn.call_count,1)
                path=pool.worker_dir(row['id'])/'owner.json'
                metadata=processes.load(path)
                processes.save(path,{**metadata,'restart_after':0})
                bridge.ensure_worker(None,pool,row,{})
                self.assertEqual(spawn.call_count,2)
                self.assertEqual(processes.load(path)['failures'],2)

    def test_missing_legacy_pid_file_authenticates_the_existing_owner_before_spawning(self):
        with tempfile.TemporaryDirectory() as home:
            pool=Pool(home)
            request={'runId':str(uuid.uuid4()),'projectId':'p','accelerator':'cpu','ttlSeconds':600,
                     'runtime':{'accounts':['a']}}
            row=pool.acquire(request,['a'])
            folder=pool.worker_dir(row['id'])
            processes.save(folder/'worker.json',{'pid':os.getpid(),'port':1,'token':'fixture'})
            with patch('scheduler.ensure_scheduler'),patch.object(bridge,'contact',return_value={'ready':True}) as contact, \
                    patch('subprocess.Popen') as spawn:
                bridge.ensure_worker(None,pool,row,{})
                spawn.assert_not_called();contact.assert_called_once()
            self.assertTrue(processes.load(folder/'owner.json')['legacy'])

    def test_lifetime_lock_prevents_duplicates_even_with_a_missing_pid_file(self):
        with tempfile.TemporaryDirectory() as home:
            pool=Pool(home)
            request={'runId':str(uuid.uuid4()),'projectId':'p','accelerator':'cpu','ttlSeconds':600,
                     'runtime':{'accounts':['a']}}
            row=pool.acquire(request,['a'])
            with processes.lock(pool.worker_dir(row['id'])/'worker.lock'), \
                    patch('scheduler.ensure_scheduler'),patch('subprocess.Popen') as spawn:
                bridge.ensure_worker(None,pool,row,{})
                spawn.assert_not_called()

    def test_stale_heartbeat_restarts_only_the_recorded_owner_and_keeps_session_identity(self):
        with tempfile.TemporaryDirectory() as home:
            pool=Pool(home)
            request={'runId':str(uuid.uuid4()),'projectId':'p','accelerator':'cpu','ttlSeconds':600,
                     'runtime':{'accounts':['a']}}
            row=pool.acquire(request,['a'])
            import subprocess
            child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
            try:
                metadata={**processes.owner(child.pid),'started':time.time()-181}
                processes.save(pool.worker_dir(row['id'])/'owner.json',metadata)
                with patch('scheduler.ensure_scheduler'), \
                        patch('subprocess.Popen',return_value=SimpleNamespace(pid=os.getpid())) as spawn:
                    bridge.ensure_worker(None,pool,row,{})
                    self.assertEqual(spawn.call_count,1)
                    self.assertFalse(processes.alive(metadata))
                self.assertEqual(pool.assigned(request['runId'])['id'],row['id'])
            finally:
                if child.poll() is None: child.kill()
                child.wait(timeout=5)

if __name__=='__main__': unittest.main()
