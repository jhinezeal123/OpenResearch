from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))
import bridge
from pool import Pool
import worker
from test_bridge import load_session


class QueueStatusTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.pool = Pool(self.home)
        self.request = {'runId': str(uuid.uuid4()), 'projectId': 'queue-test',
                        'accelerator': 'cpu', 'ttlSeconds': 3600,
                        'runtime': {'accounts': ['qa.txt']}}
        self.row = self.pool.acquire(self.request, ['qa.txt'])
        self.sid = self.row['id']
        self.session = load_session()
        self.session.DEFAULT_STATE = self.home / 'sessions'
        self.session.save(self.session.DEFAULT_STATE / self.sid / 'state.json',
                          {'run_id': self.sid, 'account': 'qa.txt', 'kernel_ref': 'qa-user/notebook'})

    def observe(self, payload=None, failure=None):
        response = SimpleNamespace(content=b'{}', status_code=200,
                                   raise_for_status=lambda: None, json=lambda: payload)
        get = Mock(side_effect=failure, return_value=response)
        with patch.dict(sys.modules, {'requests': SimpleNamespace(get=get),
                                      'provider_metrics': SimpleNamespace(record=Mock())}):
            worker.observe_provision(self.session, self.pool, self.sid, self.row,
                                     self.session.DEFAULT_STATE)
        return bridge.progress(self.pool.get(self.sid))

    def test_provider_queue_survives_restart_then_becomes_ssh_wait_and_ready(self):
        queued = self.observe({'status': 'queued'})
        self.assertIn('Queued on Kaggle', queued['detail'])
        self.assertNotIn('error', queued)
        reopened = Pool(self.home)
        self.assertEqual(bridge.progress(reopened.get(self.sid)), queued)
        self.assertEqual(reopened.assigned(self.request['runId'])['id'], self.sid)
        started = self.observe({'status': 'running'})
        self.assertIn('SSH', started['detail'])
        self.assertNotIn('Queued', started['detail'])
        reopened.ready(self.sid, 123)
        self.assertEqual(bridge.progress(reopened.get(self.sid)), {})

    def test_provider_error_invalidates_queue_and_recovers_on_the_same_notebook(self):
        self.observe({'status': 'queued'})
        unknown = self.observe(failure=ConnectionError('network unavailable'))
        self.assertIn('network unavailable', unknown['error'])
        self.assertNotIn('detail', unknown)
        self.assertIsNone(self.pool.get(self.sid)['provider_status'])
        self.assertEqual(self.pool.get(self.sid)['state'], 'provisioning')
        recovered = self.observe({'status': 'queued'})
        self.assertNotIn('error', recovered)
        self.assertEqual(self.pool.assigned(self.request['runId'])['id'], self.sid)

    def test_stale_queue_is_unknown_and_missing_status_is_reported(self):
        self.observe({'status': 'queued'})
        with self.pool.db() as db:
            db.execute('UPDATE sessions SET provider_checked_at=? WHERE id=?', (time.time()-91, self.sid))
        self.assertIn('error', bridge.progress(self.pool.get(self.sid)))
        unknown = self.observe({})
        self.assertIn('no bootstrap status', unknown['error'])
        self.assertNotIn('detail', unknown)

    def test_provider_terminal_receipt_ends_provisioning_and_keeps_the_reason(self):
        self.observe({'status': 'error'})
        row = self.pool.get(self.sid)
        self.assertEqual(row['state'], 'stopped')
        self.assertIn('status: error', row['note'])
        self.assertTrue((self.session.DEFAULT_STATE / self.sid / 'stop-confirmed.json').is_file())

    def test_old_pool_migrates_and_old_writers_keep_new_observations(self):
        with self.pool.db() as db:
            for name in ('provider_status', 'provider_error', 'provider_checked_at'):
                db.execute('ALTER TABLE sessions DROP COLUMN '+name)
        migrated = Pool(self.home)
        self.assertNotIn('Queued', bridge.progress(migrated.get(self.sid))['detail'])
        self.observe({'status': 'queued'})
        with sqlite3.connect(self.home / 'pool.sqlite3') as old:
            old.execute('UPDATE sessions SET worker_pid=? WHERE id=?', (456, self.sid))
        old.close()
        self.assertEqual(Pool(self.home).get(self.sid)['provider_status'], 'queued')


if __name__ == '__main__':
    unittest.main()
