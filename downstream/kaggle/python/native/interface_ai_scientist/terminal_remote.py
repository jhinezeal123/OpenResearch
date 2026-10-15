"""JSON-line transport around one persistent Bash shell inside Kaggle."""
import base64
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import uuid


def send(value):
    print(json.dumps(value, ensure_ascii=True), flush=True)


class Terminal:
    def __init__(self, root, *, shell='/bin/bash'):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        for name in ('source', 'output'):
            (self.root / name).mkdir(exist_ok=True)
        self.shell = subprocess.Popen([shell, '--noprofile', '--norc'], cwd=self.root,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        self.queue = queue.Queue()
        self.secrets = [v for k, v in os.environ.items() if len(v) >= 8
                        and any(part in k.upper() for part in ('TOKEN', 'SECRET', 'PASSWORD', 'COOKIE', 'API_KEY'))]
        self.command_count = 0
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        while data := os.read(self.shell.stdout.fileno(), 8192):
            self.queue.put(data)
        self.queue.put(None)

    def redact(self, text):
        for value in sorted(self.secrets, key=len, reverse=True):
            text = text.replace(value, '<redacted>')
        return re.sub(r'\btc[A-Za-z0-9_-]{40,}', '<tailcat-address>', text)

    def terminate(self):
        if self.shell.poll() is None:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(self.shell.pid), '/T', '/F'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            else:
                try:
                    os.killpg(self.shell.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            self.shell.wait(timeout=5)

    def execute(self, request):
        if self.shell.poll() is not None:
            raise RuntimeError('Shell is closed; commands cannot be replayed')
        command = request['command']
        if not isinstance(command, str) or not command or len(command.encode()) > 1_000_000:
            raise ValueError('Invalid terminal command')
        timeout = request.get('timeout', 120)
        if type(timeout) is not int or not 1 <= timeout <= 3600:
            raise ValueError('Invalid command timeout')
        marker = uuid.uuid4().hex.encode()
        marker_prefix = b'\x1e' + marker + b':'
        pattern = re.compile(b'\x1e' + marker + rb':(-?\d+)\x1f\r?\n')
        # Errexit from a previous request must not kill the persistent shell
        # before this request can report its nonzero result. Cwd and exports
        # still persist; a command can explicitly enable errexit for itself.
        script = "set +e\neval " + shlex.quote(command) + " </dev/null\n__working_status=$?\nprintf '\\036" + marker.decode() + ":%s\\037\\n' \"$__working_status\"\n"
        self.shell.stdin.write(script.encode())
        self.shell.stdin.flush()
        self.command_count += 1
        deadline = time.monotonic() + timeout
        pending = b''
        line_buffer = b''

        def emit(data, final=False):
            nonlocal line_buffer
            line_buffer += data
            parts = line_buffer.splitlines(keepends=True)
            if parts and not final and not parts[-1].endswith((b'\n', b'\r')):
                line_buffer = parts.pop()
            else:
                line_buffer = b''
            for line in parts:
                send({'type': 'output', 'id': request['id'], 'text': self.redact(line.decode('utf-8', 'replace'))})
            if len(line_buffer) > 1_000_000:
                raise ValueError('Terminal output line exceeds 1 MB')

        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.terminate()
                    emit(pending, final=True)
                    return {'returncode': -1, 'timed_out': True, 'command_count': self.command_count}
                try:
                    data = self.queue.get(timeout=min(remaining, .25))
                except queue.Empty:
                    continue
                if data is None:
                    emit(pending, final=True)
                    raise RuntimeError('Shell disconnected before the command result')
                pending += data
                match = pattern.search(pending)
                if match:
                    emit(pending[:match.start()], final=True)
                    return {'returncode': int(match.group(1)), 'timed_out': False, 'command_count': self.command_count}
                # Keep only a possible partial result marker. A fixed tail
                # buffer delays short progress lines until the command exits.
                start = pending.find(marker_prefix)
                if start >= 0:
                    emit(pending[:start])
                    pending = pending[start:]
                else:
                    keep = next((size for size in range(min(len(pending), len(marker_prefix) - 1), 0, -1)
                                 if pending.endswith(marker_prefix[:size])), 0)
                    emit(pending[:-keep] if keep else pending)
                    pending = pending[-keep:] if keep else b''
        except BaseException:
            self.terminate()
            raise

    def path(self, name):
        if not isinstance(name, str) or not name or '\\' in name:
            raise ValueError('Invalid relative file path')
        relative = Path(name)
        if relative.is_absolute() or any(part in {'.', '..'} for part in relative.parts):
            raise ValueError('File path escapes the run')
        path = self.root / relative
        if not path.resolve().is_relative_to(self.root) or any(parent.is_symlink() for parent in [path, *path.parents] if parent != self.root.parent):
            raise ValueError('Linked or escaping file path')
        return path

    def manifest(self, limit=None):
        if limit is not None and (type(limit) is not int or limit < 1):
            raise ValueError('Invalid output budget')
        files = []
        total = 0
        for directory in ('output', 'source'):
            for path in sorted((self.root / directory).rglob('*')):
                if path.is_symlink():
                    raise ValueError('Linked output refused')
                if not path.is_file():
                    continue
                relative = path.relative_to(self.root).as_posix()
                path = self.path(relative)
                size = path.stat().st_size
                total += size
                if (limit is not None and total > limit) or len(files) >= 200:
                    raise ValueError('Run files exceed the approved output budget')
                digest = hashlib.sha256()
                with path.open('rb') as content:
                    for chunk in iter(lambda: content.read(1_000_000), b''):
                        digest.update(chunk)
                files.append({'path': relative, 'bytes': size, 'sha256': digest.hexdigest()})
        return {'files': files, 'command_count': self.command_count, 'total_bytes': total}

    def dispatch(self, request):
        action = request['action']
        if action == 'exec':
            return self.execute(request)
        if action == 'manifest':
            return self.manifest(request['limit'])
        if action == 'read':
            path = self.path(request['path'])
            offset = request.get('offset', 0)
            if type(offset) is not int or offset < 0:
                raise ValueError('Invalid file offset')
            with path.open('rb') as source:
                source.seek(offset)
                return {'data': base64.b64encode(source.read(512_000)).decode('ascii')}
        if action == 'write':
            path = self.path(request['path'])
            data = base64.b64decode(request['data'], validate=True)
            if len(data) > 1_000_000:
                raise ValueError('File exceeds 1 MB')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            return {'bytes': len(data)}
        if action == 'stop':
            (self.root / 'STOP').touch()
            return {'stop_requested': True}
        raise ValueError('Unknown terminal action')


def serve(root, *, branch=None):
    if branch is not None:
        if not re.fullmatch(r'[a-f0-9]{32}', branch):
            raise ValueError('Invalid experiment branch')
        parent = Path(root).resolve()
        root = parent / 'branches' / branch
        if (not root.resolve().is_relative_to(parent)
                or any(p.is_symlink() for p in (root, root.parent))):
            raise ValueError('Refusing linked experiment branch')
    terminal = Terminal(root)
    send({'type': 'ready', 'protocol': 1})
    try:
        for line in sys.stdin:
            request = json.loads(line)
            try:
                result = terminal.dispatch(request)
                send({'type': 'result', 'id': request['id'], **result})
            except Exception as exc:
                send({'type': 'error', 'id': request['id'], 'error': type(exc).__name__})
            if request.get('action') == 'stop':
                break
    finally:
        terminal.terminate()
