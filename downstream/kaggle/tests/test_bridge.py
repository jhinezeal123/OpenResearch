import importlib.util
import unittest
import tempfile
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))
from pool import Pool

spec = importlib.util.spec_from_file_location('kaggle_bridge', Path(__file__).resolve().parents[1] / 'python/bridge.py')
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)

class BridgeTests(unittest.TestCase):
    def test_provider_status_identifies_client_and_recognizes_completed_notebook(self):
        session_path=Path(__file__).resolve().parents[1]/'python/native/interface_ai_scientist/session.py'
        store=SimpleNamespace(resolve=lambda a:('qa',{'username':'qa-user'}),read_token=lambda a:'KGAT_FIXTURE')
        response=SimpleNamespace(content=b'{"status":"complete"}',status_code=200,
                                 raise_for_status=lambda:None,json=lambda:{'status':'complete'})
        def get(url, *, params, headers, timeout):
            # The provider rejected the runtime library's default User-Agent.
            self.assertEqual(headers['User-Agent'],'OpenResearch-Kaggle')
            self.assertEqual(headers['Authorization'],'Bearer KGAT_FIXTURE')
            self.assertEqual(params,{'user_name':'qa-user','kernel_slug':'notebook'})
            return response
        with patch.dict(os.environ,{'ORX_KAGGLE_HOME':'unused-fixture'}), patch.dict(sys.modules,{
            'account_store':store,
            'proxy_control':SimpleNamespace(ensure_proxy=lambda:None,PROXY_PORT=0),
            'provider_metrics':SimpleNamespace(record=Mock()),
            'requests':SimpleNamespace(get=get),
        }):
            spec=importlib.util.spec_from_file_location('test_provider_session',session_path)
            session=importlib.util.module_from_spec(spec);spec.loader.exec_module(session)
            result=session.notebook_status('qa','qa-user/notebook','session-id')
        self.assertTrue(result['stopped'])
        self.assertEqual(result['session_id'],'session-id')

    def test_foreign_sessions_exclude_owned_notebooks_and_count_unidentified_ones(self):
        sid=uuid.uuid4().hex
        live=[{'id':sid,'account':'a'},{'id':uuid.uuid4().hex,'account':'b'}]
        observation={'readiness':'busy','active_session_count':3,'active_sessions':[
            {'ref':'user/ai-scientist-ssh-'+sid[:12]},{'ref':'user/other'},{'ref':''}]}
        self.assertEqual(bridge.foreign_sessions(observation,'a',live),2)
        self.assertEqual(bridge.foreign_sessions({**observation,'active_session_count':4},'a',live),3)
        self.assertEqual(bridge.foreign_sessions(observation,'b',live),3)
        self.assertIsNone(bridge.foreign_sessions({'readiness':'needs_login'},'a',live))

    def test_catalog_returns_metadata_without_credentials(self):
        class Store:
            def load_registry(self):
                return {'accounts': {'qa.txt': {'alias': 'qa', 'username': 'qa-user', 'password': 'PRIVATE_PASSWORD'}}}
            def read_token(self, key):
                return 'KGAT_PRIVATE_TOKEN'
        value = bridge.accounts(Store())
        self.assertEqual(value, [{'id': 'qa.txt', 'alias': 'qa', 'username': 'qa-user', 'configured': True}])
        self.assertNotIn('PRIVATE', str(value))

    def test_missing_token_does_not_advertise_a_configured_account(self):
        class Store:
            def load_registry(self):
                return {'accounts': {'qa.txt': {}}}
            def read_token(self, key):
                raise FileNotFoundError('private token path')
        self.assertFalse(bridge.accounts(Store())[0]['configured'])

    def test_stop_requires_confirmation_and_then_survives_network_loss(self):
        class Session:
            load = staticmethod(lambda p: json.loads(p.read_text()))
            @staticmethod
            def save(p, value):
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps(value))
            def control(self, action, sid, account):
                return {'session_id': sid, 'stopped': action == 'status'}
        with tempfile.TemporaryDirectory() as directory:
            root, session = Path(directory), Session()
            self.assertFalse(bridge.control(session, 'stop', 'qa-session', 'qa', root)['stopped'])
            self.assertFalse((root/'qa-session/stop-confirmed.json').exists())
            self.assertTrue(bridge.control(session, 'status', 'qa-session', 'qa', root)['stopped'])
            session.control = lambda *a: self.fail('Confirmed immutable session must not need the network')
            self.assertTrue(bridge.control(session, 'stop', 'qa-session', 'qa', root)['stopped'])

class BridgeLeaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.pool = Pool(self.home)
        self.request = {'home':str(self.home),'runId':str(uuid.uuid4()),
                        'projectId':'review-project','accelerator':'cpu','ttlSeconds':3600,
                        'competitionSources':[],'datasetSources':[],
                        'runtime':{'accounts':['account-a']}}
        for mocked in (patch.object(bridge, 'configure', return_value=None),
                       patch.object(bridge, 'ensure_worker'),
                       patch.dict(sys.modules, {'account_store':SimpleNamespace()})):
            mocked.start()
            self.addCleanup(mocked.stop)
        mocked = patch.object(bridge, 'contact')
        self.contact = mocked.start()
        self.addCleanup(mocked.stop)

    def reserve(self):
        self.row = self.pool.acquire(self.request, ['account-a'])
        self.pool.ready(self.row['id'], 123)

    def test_stop_after_restart_contacts_the_assigned_run_before_releasing(self):
        self.reserve()
        self.contact.return_value = {'stage':'ERROR','exitCode':143}
        result = bridge.dispatch({**self.request,'action':'cancel'})
        self.assertEqual(result['exitCode'], 143)
        self.assertEqual(self.contact.call_args.args[1]['action'], 'cancel')
        self.assertEqual(self.contact.call_args.args[1]['runId'], self.request['runId'])
        # The supervisor's terminal release must still check the remote receipt.
        self.assertEqual(self.pool.get(self.row['id'])['active_run'], self.request['runId'])
        self.assertTrue(bridge.dispatch({**self.request,'action':'release'})['released'])
        self.assertEqual(self.contact.call_args.args[1]['action'], 'inspect')

    def test_unconfirmed_remote_state_never_releases_a_live_lease(self):
        self.reserve()
        for observed in ({'stage':'RUNNING'}, {'stage':'UNKNOWN'}, {'pending':True}):
            with self.subTest(observed=observed):
                self.contact.return_value = observed
                self.assertFalse(bridge.dispatch({**self.request,'action':'release'})['released'])
                self.assertEqual(self.pool.get(self.row['id'])['active_run'], self.request['runId'])
                self.assertIsNone(self.pool.acquire({**self.request,'runId':str(uuid.uuid4())}, []))

    def test_launch_upload_waits_when_the_notebook_channel_is_not_ready(self):
        self.reserve()
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'source.tar';source.write_bytes(b'x'*10)
            self.contact.return_value={'pending':True}
            result=bridge.dispatch({**self.request,'action':'launch','sourcePath':str(source),'digest':'0'*64})
        self.assertTrue(result['pending'])
        self.assertEqual(self.contact.call_count,1)
        self.assertEqual(self.contact.call_args.args[1]['action'],'upload')

    def test_ssh_failure_keeps_the_lease_until_the_owner_confirms_completion(self):
        self.reserve()
        self.contact.side_effect = ConnectionError('offline')
        with self.assertRaises(ConnectionError):
            bridge.dispatch({**self.request,'action':'release'})
        self.assertEqual(self.pool.get(self.row['id'])['state'], 'busy')
        self.contact.side_effect = None
        self.contact.return_value = {'stage':'COMPLETE','exitCode':0}
        self.assertTrue(bridge.dispatch({**self.request,'action':'release'})['released'])
        replacement = self.pool.acquire({**self.request,'runId':str(uuid.uuid4())}, [])
        self.assertEqual(replacement['id'], self.row['id'])

    def test_stop_during_provisioning_waits_without_releasing(self):
        self.reserve()
        self.contact.return_value = {'pending':True}
        self.assertTrue(bridge.dispatch({**self.request,'action':'cancel'})['pending'])
        self.assertEqual(self.pool.get(self.row['id'])['state'], 'busy')

    def test_queued_stop_does_not_allocate_a_notebook_or_contact_a_worker(self):
        result = bridge.dispatch({**self.request,'action':'cancel'})
        self.assertEqual((result['stage'], result['exitCode']), ('ERROR', 143))
        self.assertTrue(bridge.dispatch({**self.request,'action':'release'})['released'])
        self.assertEqual(self.pool.list(), [])
        self.contact.assert_not_called()


if __name__ == '__main__':
    unittest.main()
