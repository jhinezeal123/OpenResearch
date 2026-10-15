import importlib.util
import unittest
import tempfile
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location('kaggle_bridge', Path(__file__).with_name('kaggle_bridge.py'))
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)

class BridgeTests(unittest.TestCase):
    def test_run_identity_is_stable_and_rejects_path_traversal(self):
        self.assertEqual(bridge.session_id('9ef38277-9183-47b0-b869-62fe43701b61'), '9ef38277918347b0b86962fe43701b61')
        with self.assertRaises(ValueError):
            bridge.session_id('../../different-notebook')

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

    def test_unsubmitted_lease_is_safe_but_unknown_submission_stays_reserved(self):
        class Session:
            def control(self, *args):
                raise ConnectionError('outcome unverified')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertTrue(bridge.previous_stopped(Session(), root, 'qa-session', 'qa'))
            (root/'qa-session').mkdir()
            (root/'qa-session/submit-intent.json').write_text('{}')
            with self.assertRaises(ConnectionError):
                bridge.previous_stopped(Session(), root, 'qa-session', 'qa')

if __name__ == '__main__':
    unittest.main()
