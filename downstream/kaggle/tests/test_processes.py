"""Real local children exercise Windows Jobs and Unix process-group cleanup."""
import json
import importlib.util
import os
from pathlib import Path
import subprocess
import socket
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'python'))
import processes


class ProcessTests(unittest.TestCase):
    def test_bootstrap_proxy_cleanup_also_reaps_a_restarted_proxy(self):
        path=Path(processes.__file__).parent/'native/proxy_control.py'
        spec=importlib.util.spec_from_file_location('test_proxy_control',path)
        control=importlib.util.module_from_spec(spec);spec.loader.exec_module(control)
        with tempfile.TemporaryDirectory() as home, socket.socket() as available:
            available.bind(('127.0.0.1',0))
            port=available.getsockname()[1]
            available.close()
            with patch.dict(os.environ,{'ORX_KAGGLE_HOME':home,'PYTHONDONTWRITEBYTECODE':'1'}), \
                    patch.object(control,'PROXY_PORT',port):
                owners=[]
                try:
                    first=control.ensure_proxy();owners.append(processes.owner(first.pid))
                    self.assertIsNone(control.ensure_proxy())
                    processes.stop_tree(first)
                    second=control.ensure_proxy();owners.append(processes.owner(second.pid))
                    self.assertEqual(len(control._owned),2)
                    self.assertTrue(processes.alive(owners[1]))
                finally:
                    control.stop_proxies()
                self.assertTrue(all(not processes.alive(p) for p in owners))
                self.assertEqual(control._owned,[])

    def test_process_identity_rejects_a_reused_pid(self):
        metadata=processes.owner(os.getpid())
        self.assertTrue(processes.alive(metadata))
        stale={**metadata,'birth':'not-the-process-creation-time'}
        self.assertFalse(processes.alive(stale))
        self.assertFalse(processes.terminate(stale))
        self.assertFalse(processes.alive({'pid':os.getpid()}))

    def test_lock_is_exclusive_and_released(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'owner.lock'
            with processes.lock(path) as first:
                self.assertTrue(first)
                with processes.lock(path) as second: self.assertFalse(second)
            with processes.lock(path) as third: self.assertTrue(third)

    def test_crashed_owner_releases_its_os_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'owner.lock'
            code=('import sys,time;sys.path.insert(0,sys.argv[1]);from processes import lock;'
                  '\nwith lock(sys.argv[2]) as acquired:\n print(acquired,flush=True);time.sleep(30)')
            child=subprocess.Popen([sys.executable,'-u','-c',code,str(Path(processes.__file__).parent),str(path)],
                                   stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(),'True')
                with processes.lock(path) as acquired: self.assertFalse(acquired)
            finally:
                child.kill();child.communicate(timeout=5)
            with processes.lock(path) as acquired: self.assertTrue(acquired)

    def test_tree_cleanup_kills_descendants_even_after_the_parent_has_exited(self):
        code=('import subprocess,sys,json;'
              'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"]);'
              'print(p.pid,flush=True)')
        child=processes.spawn_tree([sys.executable,'-u','-c',code],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        metadata=None
        try:
            pid=int(child.stdout.readline())
            metadata=processes.owner(pid)
            child.wait(timeout=5)
            self.assertTrue(processes.alive(metadata))
        finally:
            processes.stop_tree(child)
        for _ in range(50):
            if not processes.alive(metadata): break
            time.sleep(.05)
        self.assertFalse(processes.alive(metadata),'grandchild survived process-tree cleanup')

    def test_run_timeout_cleans_up_the_process_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            receipt=Path(directory)/'child.json'
            code=('import sys,time;sys.path.insert(0,sys.argv[1]);from processes import owner,save;'
                  'import os;save(sys.argv[2],owner(os.getpid()));time.sleep(30)')
            with self.assertRaises(subprocess.TimeoutExpired):
                processes.run_tree([sys.executable,'-c',code,str(Path(processes.__file__).parent),str(receipt)],timeout=1)
            self.assertFalse(processes.alive(json.loads(receipt.read_text())))

    @unittest.skipUnless(os.name=='nt','Windows worker lifetime Job')
    def test_worker_crash_kills_owned_children(self):
        code=('import sys,subprocess,time;sys.path.insert(0,sys.argv[1]);'
              'from processes import guard_worker,spawn_tree,owner;import json;guard_worker();'
              'p=spawn_tree([sys.executable,"-c","import time;time.sleep(30)"]);'
              'print(json.dumps(owner(p.pid)),flush=True);time.sleep(30)')
        child=subprocess.Popen([sys.executable,'-u','-c',code,str(Path(processes.__file__).parent)],
                               stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            line=child.stdout.readline()
            if not line:
                self.fail(child.stderr.read())
            metadata=json.loads(line)
            self.assertTrue(processes.alive(metadata))
        finally:
            child.kill();child.communicate(timeout=5)
        for _ in range(50):
            if not processes.alive(metadata): break
            time.sleep(.05)
        self.assertFalse(processes.alive(metadata),'child survived worker crash')


if __name__=='__main__': unittest.main()
