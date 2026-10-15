from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'python'))
import worker

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

if __name__=='__main__': unittest.main()
