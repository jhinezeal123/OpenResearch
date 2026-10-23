import hashlib
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))
import remote


class RemoteRunTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        mocked = patch.object(remote, 'ROOT', self.root)
        mocked.start()
        self.addCleanup(mocked.stop)
        self.run_id = str(uuid.uuid4())
        self.directory = remote.run_dir(self.run_id)

    def test_unlaunched_lease_is_distinct_from_a_running_or_uncertain_command(self):
        self.assertEqual(remote.dispatch({'action':'inspect','runId':self.run_id})['stage'],'NOT_STARTED')
        self.directory.mkdir(parents=True)
        (self.directory/'launch-intent').write_text('uncertain')
        self.assertEqual(remote.dispatch({'action':'inspect','runId':self.run_id})['stage'],'UNKNOWN')
        (self.directory/'pid').write_text('123')
        self.assertEqual(remote.dispatch({'action':'inspect','runId':self.run_id})['stage'],'RUNNING')

    def test_cancel_before_launch_survives_a_lost_ack_and_blocks_delayed_launch(self):
        result = remote.dispatch({'action':'cancel','runId':self.run_id})
        self.assertEqual((result['stage'], result['exitCode']), ('ERROR', 143))
        with patch.object(remote.subprocess, 'Popen') as spawn:
            # No source is needed: the durable cancellation precedes extraction.
            replay = remote.dispatch({'action':'launch','runId':self.run_id})
            self.assertEqual(replay['exitCode'], 143)
            spawn.assert_not_called()

    def test_cancel_running_process_group_is_idempotent(self):
        self.directory.mkdir(parents=True)
        (self.directory/'launch-intent').write_text('launched')
        (self.directory/'pid').write_text('123')
        with patch.object(remote.os, 'killpg', create=True) as kill, \
                patch.object(remote.signal, 'SIGKILL', 9, create=True), \
                patch.object(remote.time, 'sleep'):
            result = remote.dispatch({'action':'cancel','runId':self.run_id})
            self.assertEqual(result['exitCode'], 143)
            self.assertEqual(kill.call_args_list[0].args, (123, signal.SIGTERM))
            self.assertEqual(kill.call_args_list[1].args, (123, remote.signal.SIGKILL))
            remote.dispatch({'action':'cancel','runId':self.run_id})
            self.assertEqual(kill.call_count, 2)

    def test_uncertain_launch_without_a_pid_never_fabricates_a_stopped_receipt(self):
        self.directory.mkdir(parents=True)
        (self.directory/'launch-intent').write_text('acknowledgement lost')
        result = remote.dispatch({'action':'cancel','runId':self.run_id})
        self.assertEqual(result['stage'], 'UNKNOWN')
        self.assertFalse((self.directory/'exit_code').exists())
        with patch.object(remote.subprocess, 'Popen') as spawn:
            self.assertEqual(remote.dispatch({'action':'launch','runId':self.run_id})['stage'], 'UNKNOWN')
            spawn.assert_not_called()

    def generated_command(self, command):
        cache = self.root/'source'
        cache.mkdir(exist_ok=True)
        archive = self.root/'empty.tar'
        with tarfile.open(archive, 'w'):
            pass
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        (cache/(digest+'.tar')).write_bytes(archive.read_bytes())
        with patch.object(remote.subprocess, 'Popen', return_value=SimpleNamespace(pid=123)):
            remote.dispatch({'action':'launch','runId':self.run_id,'digest':digest,
                             'seconds':10,'command':command,'env':{}})
        script = (self.directory/'run.sh').read_text()
        return next(line for line in script.splitlines() if line.startswith('timeout '))

    def test_generated_shell_preserves_failures_in_compound_commands_and_pipelines(self):
        env = os.environ.copy()
        if os.name == 'nt':
            git = Path(os.environ.get('ProgramFiles', 'C:/Program Files'))/'Git'
            bash = git/'bin/bash.exe'
            env['PATH'] = str(git/'usr/bin')+os.pathsep+env.get('PATH', '')
            if not bash.is_file():
                self.skipTest('Git Bash is required on Windows')
        else:
            bash = shutil.which('bash')
            if not bash or not shutil.which('timeout'):
                self.skipTest('Bash and timeout are required')
        for command, expected in [('false; echo SHOULD_NOT_RUN', 1),
                                  ('false | cat; echo SHOULD_NOT_RUN', 1),
                                  ('exit 7', 7), ('echo SUCCESS', 0)]:
            with self.subTest(command=command):
                self.run_id = str(uuid.uuid4())
                self.directory = remote.run_dir(self.run_id)
                generated = self.generated_command(command)
                result = subprocess.run([str(bash), '-c', generated], env=env,
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertNotIn('SHOULD_NOT_RUN', result.stdout)


if __name__ == '__main__':
    unittest.main()
