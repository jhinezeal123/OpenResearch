"""Lifetime locks and owned process trees. Never identify an owner by PID alone."""
from contextlib import contextmanager
import ctypes
import errno
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.' + uuid.uuid4().hex + '.tmp')
    temporary.write_text(json.dumps(value), encoding='utf-8')
    # Windows refuses to replace a file another process is reading (WinError 5).
    for attempt in range(20):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if os.name != 'nt' or attempt == 19:
                temporary.unlink(missing_ok=True)
                raise
            time.sleep(.05)


def load(path):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def spawn_python(script, *args, **kwargs):
    """Start one background interpreter, preserving the current virtualenv.

    Windows venv python.exe is a launcher: its PID differs from the interpreter
    heartbeat, and its child may allocate a console after DETACHED_PROCESS.
    Use the base interpreter with the same venv hint as CPython's launcher.
    """
    executable = sys.executable
    if os.name == 'nt':
        executable = getattr(sys, '_base_executable', None) or executable
        kwargs['env'] = {**kwargs.get('env', os.environ), '__PYVENV_LAUNCHER__': sys.executable}
        kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs['start_new_session'] = True
    return subprocess.Popen([executable, str(script), *map(str, args)], **kwargs)


@contextmanager
def lock(path, wait_seconds=0):
    """Nonblocking OS lock; a crash releases it without stale-lock deletion."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b'0'); handle.flush()
        handle.seek(0)
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(.05)
        try:
            yield True
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


if os.name == 'nt':
    from ctypes import wintypes as w
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    # HANDLE arguments must have pointer width on 64-bit Windows.
    for name, result, args in (
        ('OpenProcess', w.HANDLE, [w.DWORD, w.BOOL, w.DWORD]),
        ('CloseHandle', w.BOOL, [w.HANDLE]),
        ('GetProcessTimes', w.BOOL, [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4),
        ('WaitForSingleObject', w.DWORD, [w.HANDLE, w.DWORD]),
        ('TerminateProcess', w.BOOL, [w.HANDLE, w.UINT]),
        ('CreateJobObjectW', w.HANDLE, [ctypes.c_void_p, w.LPCWSTR]),
        ('SetInformationJobObject', w.BOOL, [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]),
        ('AssignProcessToJobObject', w.BOOL, [w.HANDLE, w.HANDLE]),
        ('CreateToolhelp32Snapshot', w.HANDLE, [w.DWORD, w.DWORD]),
        ('OpenThread', w.HANDLE, [w.DWORD, w.BOOL, w.DWORD]),
        ('ResumeThread', w.DWORD, [w.HANDLE]),
    ):
        function = getattr(kernel, name)
        function.restype, function.argtypes = result, args

    def _birth(handle):
        times = [w.FILETIME() for _ in range(4)]
        if kernel.WaitForSingleObject(handle, 0) != 258:
            return None
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return None
        return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)


def identity(pid):
    if not isinstance(pid, int) or pid <= 0:
        return None
    if os.name == 'nt':
        handle = kernel.OpenProcess(0x1000 | 0x100000, False, pid)
        if not handle:
            return None
        try:
            return _birth(handle)
        finally:
            kernel.CloseHandle(handle)
    try:
        if Path('/proc').is_dir():
            fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
            boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            return boot+':'+fields[19] if fields[0] != 'Z' else None
        result = subprocess.run(['ps', '-p', str(pid), '-o', 'lstart='],
                                capture_output=True, text=True, timeout=5)
        return result.stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def owner(pid):
    return {'pid': pid, 'birth': identity(pid), 'started': time.time()}


def alive(metadata):
    return bool(metadata.get('birth')) and identity(metadata.get('pid')) == metadata['birth']


def terminate(metadata):
    """Signal only the recorded process instance; PID reuse cannot kill a stranger."""
    pid, birth = metadata.get('pid'), metadata.get('birth')
    if not birth or not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == 'nt':
        handle = kernel.OpenProcess(0x1000 | 0x100000 | 1, False, pid)
        if not handle:
            return False
        try:
            if _birth(handle) != birth:
                return False
            if not kernel.TerminateProcess(handle, 1):
                raise ctypes.WinError(ctypes.get_last_error())
            return kernel.WaitForSingleObject(handle, 5000) == 0
        finally:
            kernel.CloseHandle(handle)
    if hasattr(os, 'pidfd_open') and hasattr(signal, 'pidfd_send_signal'):
        try:
            descriptor = os.pidfd_open(pid)
        except ProcessLookupError:
            return False
        try:
            if identity(pid) != birth:
                return False
            signal.pidfd_send_signal(descriptor, signal.SIGKILL)
            return True
        finally:
            os.close(descriptor)
    # Other Unix platforms keep the lease and rely on its deadline rather than
    # use a racy kill(pid) after checking a process's creation time.
    return False


if os.name == 'nt':
    class _Limits(ctypes.Structure):
        _fields_ = [('user_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
                    ('flags', w.DWORD), ('min_working', ctypes.c_size_t),
                    ('max_working', ctypes.c_size_t), ('active', w.DWORD),
                    ('affinity', ctypes.c_size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [('basic', _Limits), ('io', ctypes.c_uint64 * 6),
                    ('memory', ctypes.c_size_t * 4)]

    class _Thread(ctypes.Structure):
        _fields_ = [('size', w.DWORD), ('usage', w.DWORD), ('tid', w.DWORD),
                    ('pid', w.DWORD), ('priority', w.LONG), ('delta', w.LONG), ('flags', w.DWORD)]

    for name in ('Thread32First', 'Thread32Next'):
        getattr(kernel, name).argtypes = [w.HANDLE, ctypes.POINTER(_Thread)]
        getattr(kernel, name).restype = w.BOOL

    class Job:
        def __init__(self):
            self.handle = kernel.CreateJobObjectW(None, None)
            if not self.handle:
                raise ctypes.WinError(ctypes.get_last_error())
            limits = _ExtendedLimits()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                self.close()
                raise ctypes.WinError(ctypes.get_last_error())

        def assign(self, handle):
            if not kernel.AssignProcessToJobObject(self.handle, handle):
                raise ctypes.WinError(ctypes.get_last_error())

        def close(self):
            if self.handle:
                kernel.CloseHandle(self.handle)
                self.handle = None

    def _resume(pid):
        snapshot = kernel.CreateToolhelp32Snapshot(4, 0)  # TH32CS_SNAPTHREAD
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = _Thread(); entry.size = ctypes.sizeof(entry)
            found = kernel.Thread32First(snapshot, ctypes.byref(entry))
            while found:
                if entry.pid == pid:
                    thread = kernel.OpenThread(2, False, entry.tid)
                    if not thread:
                        raise ctypes.WinError(ctypes.get_last_error())
                    try:
                        if kernel.ResumeThread(thread) == 0xffffffff:
                            raise ctypes.WinError(ctypes.get_last_error())
                        return
                    finally:
                        kernel.CloseHandle(thread)
                found = kernel.Thread32Next(snapshot, ctypes.byref(entry))
            raise RuntimeError('Suspended child thread was not found')
        finally:
            kernel.CloseHandle(snapshot)


def guard_worker():
    """Windows closes this handle on a worker crash, reaping SSH and Tailcat too."""
    global _worker_job
    if os.name == 'nt':
        _worker_job = Job()
        handle = kernel.OpenProcess(0x100 | 1, False, os.getpid())
        try:
            _worker_job.assign(handle)
        finally:
            kernel.CloseHandle(handle)


def spawn_tree(args, **kwargs):
    if os.name != 'nt':
        return subprocess.Popen(args, start_new_session=True, **kwargs)
    job = Job()
    process = None
    try:
        # Assign before any child code runs: even a fast ProxyCommand belongs
        # to this Job and cannot escape cleanup during reconnect.
        process = subprocess.Popen(args, creationflags=0x08000000 | 4, **kwargs)
        job.assign(int(process._handle))
        process._orx_job = job
        _resume(process.pid)
        return process
    except BaseException:
        if process is not None:
            process.kill(); process.wait(timeout=5)
        job.close()
        raise


def stop_tree(process):
    if process is None:
        return
    if os.name == 'nt':
        process._orx_job.close()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=5)
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream:
            stream.close()


def run_tree(args, *, input=None, timeout=None):
    process = spawn_tree(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        stdout, stderr = process.communicate(input=input, timeout=timeout)
        return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
    finally:
        stop_tree(process)
