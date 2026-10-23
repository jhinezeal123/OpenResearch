"""Kaggle SSH bootstrap: SDK push through the account proxy, then SSH/SFTP.

Runtime state and generated notebook contain ephemeral keys. Keep state private;
the Kaggle notebook is private. No Codex credential goes to Kaggle.
"""

from contextlib import contextmanager
import base64
from datetime import datetime, timezone
import functools
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request
import urllib.parse
import uuid
import zipfile
import account_store
from proxy_control import ensure_proxy, PROXY_PORT
from processes import run_tree

BASE = Path(__file__).resolve().parents[1]
DEFAULT_STATE = Path(os.environ["ORX_KAGGLE_HOME"]) / "sessions"
WIN_URL = "https://github.com/tailscale/tailcat/releases/download/v0.7.0/tailcat_0.7.0_windows_amd64.zip"
WIN_SHA256 = "f04cac07e3bf6c700b0b543df0d3315a3b312cd5a05ee76714e1cc848e9b2dff"
ACCELERATORS = ('cpu', 'NvidiaTeslaT4', 'TpuV5E8', 'TpuV6E8')


@contextmanager
def request_lock(session_id, *, namespace='session'):
    """Serialize bootstrap admission and cancellation across CLI processes."""
    if not re.fullmatch('[0-9a-f]{32}', session_id) or namespace not in {'session', 'assets'}:
        raise ValueError('Expected a UUID request ID')
    locks = DEFAULT_STATE / 'locks'
    locks.mkdir(parents=True, exist_ok=True)
    name = session_id if namespace == 'session' else namespace + '-' + session_id
    with (locks / (name + '.lock')).open('a+b') as handle:
        handle.write(b'0')
        handle.flush()
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            deadline = time.monotonic() + 180
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Bootstrap operation still owns this session')
                    time.sleep(.1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def cancelled_descriptor(account, session_id, ttl_seconds=480):
    key, metadata = account_store.resolve(account)
    return {'session_id': session_id, 'account': key,
            'notebook_ref': metadata['username'] + '/ai-scientist-ssh-' + session_id[:12],
            'remote_directory': '/kaggle/working/ai-scientist/' + session_id,
            'ttl_seconds': ttl_seconds, 'submission_status': 'NOT_SUBMITTED', 'ssh_status': 'NOT_STARTED'}


def control(action, session_id, account=None):
    """A cancelled request with no submit intent is proven never submitted."""
    with request_lock(session_id):
        root = DEFAULT_STATE / session_id
        if action == 'stop':
            root.mkdir(parents=True, exist_ok=True)
            save(root / 'cancel-intent.json', {'session_id': session_id})
        state_path = root / 'state.json'
        if (root / 'cancel-intent.json').is_file() and not (root / 'submit-intent.json').exists():
            selected = load(state_path)['account'] if state_path.exists() else account
            receipt = cancelled_descriptor(selected, session_id)
            return {**receipt, 'status': 'not_submitted', 'stopped': True}
        state = state_for(session_id)
    if action == 'stop':
        stop(state)
        return {'session_id': session_id, 'stop_requested': True}
    return status(state)


def accelerator_name(value):
    """Resolve friendly labels to current Kaggle machine_shape identifiers."""
    aliases = {'gpu': 'NvidiaTeslaT4', 't4': 'NvidiaTeslaT4',
               'nvidiat4': 'NvidiaTeslaT4', 'tpu': 'TpuV5E8'}
    aliases.update({name.lower(): name for name in ACCELERATORS})
    if not isinstance(value, str) or value.strip().lower() not in aliases:
        raise ValueError('Choose accelerator: ' + ', '.join(ACCELERATORS))
    return aliases[value.strip().lower()]


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def tailcat(state_root):
    binary = state_root / "bin/tailcat.exe"
    if not binary.exists():
        binary.parent.mkdir(parents=True, exist_ok=True)
        archive = binary.parent / "tailcat_0.7.0_windows_amd64.zip"
        with urllib.request.urlopen(WIN_URL, timeout=60) as response:
            data = response.read(20_000_001)
        if hashlib.sha256(data).hexdigest() != WIN_SHA256:
            raise RuntimeError("Tailcat Windows release checksum mismatch")
        archive.write_bytes(data)
        with zipfile.ZipFile(archive) as package:
            binary.write_bytes(package.read("tailcat.exe"))
    return binary


def prepare(args):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    accelerator = accelerator_name(args.accelerator)
    if type(args.ttl) is not int or args.ttl < 60:
        raise ValueError('Choose a bootstrap TTL of at least 60 seconds')
    account = args.account
    if not account and args.config:
        account = load(args.config)['kaggle_account_alias']
    key, metadata = account_store.resolve(account)
    username = metadata.get('username')
    if not username:
        raise ValueError('Configure the selected account username before starting Kaggle SSH')
    account_store.read_token(key)
    config = {'kaggle_username': username}
    run_id = getattr(args, 'request_id', None) or uuid.uuid4().hex
    if not re.fullmatch('[0-9a-f]{32}', run_id):
        raise ValueError('Expected a UUID request ID')
    root = args.state_root / run_id
    # Callers hold request_lock and only prepare when state.json is absent, so a
    # leftover folder is an earlier attempt that failed before submission.
    if root.exists():
        if (root / 'submit-intent.json').exists() or (root / 'state.json').exists():
            raise RuntimeError('This request already has saved state; reconnect instead')
        shutil.rmtree(root)
    root.mkdir(parents=True)
    # Two first runs may need the same bundled binary. Serialize installation
    # across CLI processes without serializing their sessions or agent work.
    with request_lock(hashlib.sha256(str(args.state_root.resolve()).encode()).hexdigest()[:32], namespace='assets'):
        binary = tailcat(args.state_root)
    server_key = root / "server.private.json"
    generated = subprocess.run(
        [str(binary), "genkey", "--key", str(server_key), "--region=tok", "--embed-derp-map"],
        capture_output=True, text=True, timeout=45, check=True,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
    )
    addresses = re.findall(r"\btc[A-Za-z0-9_-]{40,}", generated.stdout + generated.stderr)
    if len(addresses) != 1:
        raise RuntimeError("Expected exactly one fixed Tailcat address")
    ssh_key = Ed25519PrivateKey.generate()
    private = root / "id_ed25519"
    private.write_bytes(ssh_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()))
    if os.name == "nt":
        owner = subprocess.run(["whoami"], capture_output=True, text=True, check=True, timeout=5,
                               creationflags=subprocess.CREATE_NO_WINDOW).stdout.strip()
        subprocess.run(["icacls", str(private), "/inheritance:r", "/grant:r", owner + ":(R,W)"],
                       capture_output=True, check=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        private.chmod(0o600)
    public = ssh_key.public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode("ascii")
    (root / "id_ed25519.pub").write_text(public + "\n", encoding="ascii")
    remote_config = {"run_id": run_id, "server_key": load(server_key),
                     "authorized_key": public, "ttl_seconds": args.ttl}
    source = (Path(__file__).with_name("bootstrap.py")).read_text(encoding="utf-8")
    source += "\nrun(" + repr(remote_config) + ")\n"
    compile(source, "bootstrap", "exec")
    bundle = root / "bundle"
    bundle.mkdir()
    notebook = {"nbformat": 4, "nbformat_minor": 5,
                "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"}},
                "cells": [{"cell_type": "code", "id": "bootstrap", "metadata": {},
                           "execution_count": None, "outputs": [], "source": source}]}
    save(bundle / "notebook.ipynb", notebook)
    kernel_ref = config["kaggle_username"] + "/ai-scientist-ssh-" + run_id[:12]
    meta = {"id": kernel_ref, "title": kernel_ref.split("/", 1)[1],
            "code_file": "notebook.ipynb", "language": "python", "kernel_type": "notebook",
            "is_private": True, "machine_shape": '' if accelerator == 'cpu' else accelerator,
            "enable_gpu": accelerator.startswith('Nvidia'), "enable_tpu": accelerator.startswith('Tpu'),
            "enable_internet": True,
            "competition_sources": args.competition_sources,
            "dataset_sources": args.dataset_sources, "kernel_sources": []}
    save(bundle / "kernel-metadata.json", meta)
    state = {"run_id": run_id, "root": str(root), "account": key, "username": username,
             "tailcat": str(binary), "address": addresses[0], "kernel_ref": kernel_ref,
             "remote_root": "/kaggle/working/ai-scientist/" + run_id, "ttl_seconds": args.ttl,
             "accelerator": accelerator}
    save(root / "state.json", state)
    save(args.state_root / "latest.json", {"state": str(root / "state.json")})
    write_ssh_config(state)
    return state


def client(state):
    from kagglesdk.kaggle_client import KaggleClient
    from kagglesdk.kaggle_env import KaggleEnv
    from sdk_version import _assert_pinned_sdk
    _assert_pinned_sdk()
    ensure_proxy()
    api = KaggleClient(env=KaggleEnv.LOCAL, api_token=account_store.read_token(state['account']))
    http = api.http_client()
    http._endpoint = f"http://127.0.0.1:{PROXY_PORT}"
    http._init_session()
    http._session.send = functools.partial(http._session.send, timeout=(10, 90))
    return api


def push(state):
    from kagglesdk.kernels.types.kernels_api_service import ApiSaveKernelRequest
    from kagglesdk.kernels.types.kernels_enums import KernelExecutionType

    root = Path(state["root"])
    if (root / 'cancel-intent.json').exists():
        raise RuntimeError('This bootstrap request was cancelled; do not submit it')
    intent_path = root / "submit-intent.json"
    if intent_path.exists():
        raise RuntimeError("This execution already has a submit intent; do not push it again")
    meta = load(root / "bundle/kernel-metadata.json")
    source = (root / "bundle/notebook.ipynb").read_text(encoding="utf-8")
    request = ApiSaveKernelRequest()
    for field, value in {"slug": meta["id"], "new_title": meta["title"], "text": source,
                         "language": "python", "kernel_type": "notebook", "is_private": True,
                         "enable_gpu": meta["enable_gpu"], "enable_tpu": meta.get("enable_tpu", False),
                         "machine_shape": meta.get("machine_shape", "NvidiaTeslaT4" if meta["enable_gpu"] else ""),
                         "enable_internet": True,
                         "competition_data_sources": meta["competition_sources"],
                         "dataset_data_sources": meta["dataset_sources"],
                         "session_timeout_seconds": state["ttl_seconds"] + 90,
                         "kernel_execution_type": KernelExecutionType.SAVE_AND_RUN_ALL}.items():
        setattr(request, field, value)
    api = client(state)
    save(intent_path, {"kernel_ref": state["kernel_ref"], "status": "SUBMITTING",
                       "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
                       "created_at": datetime.now(timezone.utc).isoformat()})
    try:
        with api:
            response = api.kernels.kernels_api_client.save_kernel(request)
        result = json.loads(response.to_json())
        save(root / "push-response.json", result)
        if response.error:
            raise RuntimeError("Kaggle rejected bootstrap: " + response.error)
        resolved_ref = urllib.parse.urlsplit(response.url).path.removeprefix("/code/").strip("/")
        if resolved_ref.split("/", 1)[0] != state["kernel_ref"].split("/", 1)[0]:
            raise RuntimeError("Kaggle response owner mismatch")
        state["kernel_ref"] = resolved_ref
        state["submission_status"] = "SUBMITTED"
        save(root / "state.json", state)
        return result
    except Exception:
        # A lost response does not prove Kaggle rejected the request.
        raise


def ssh_options(state):
    root = Path(state["root"])
    return ["-i", str(root / "id_ed25519"), "-o", "IdentitiesOnly=yes",
            "-o", "BatchMode=yes", "-o", "ConnectTimeout=12", "-o", "ConnectionAttempts=1",
            "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=2",
            "-o", "StrictHostKeyChecking=accept-new", "-o", 'UserKnownHostsFile="' + (root / "known_hosts").as_posix() + '"',
            "-o", 'ProxyCommand="' + state["tailcat"].replace("\\", "/") + '" ' + state["address"] + " 22"]


def notebook_environment_command(command):
    """Tailcat shells omit the notebook environment; inherit it from their parent.

    Recover only from this SSH process's ancestry. Environment credentials stay
    inside the Kaggle VM and are never written to an output file or printed.
    python -c leaves stdin attached to the terminal, unlike a heredoc launcher.
    """
    source = '''import os
from pathlib import Path
environment = dict(os.environ)
pid = os.getppid()
seen = set()
while pid > 0 and pid not in seen:
    seen.add(pid)
    parent = Path('/proc') / str(pid)
    values = dict(item.split(b'=', 1) for item in (parent / 'environ').read_bytes().split(b'\\0') if b'=' in item)
    if b'KAGGLE_KERNEL_RUN_TYPE' in values:
        environment.update({os.fsdecode(key): os.fsdecode(value) for key, value in values.items()})
        break
    pid = int(next(line.split(':', 1)[1] for line in (parent / 'status').read_text().splitlines() if line.startswith('PPid:')))
else:
    raise RuntimeError('Kaggle notebook parent environment unavailable')
if Path('/opt/bin/nvidia-smi').is_file():
    environment['PATH'] = '/opt/bin:' + environment.get('PATH', '/usr/bin:/bin')
'''
    source += f"os.execve('/bin/bash', ['bash', '-c', {command!r}], environment)\n"
    encoded = base64.b64encode(source.encode('utf-8')).decode('ascii')
    return 'python3 -c ' + shlex.quote(f'import base64;exec(base64.b64decode("{encoded}"))')


def remote(state, command, *, wait=30, data=None, record=None):
    started = time.monotonic()
    result = run_tree([shutil.which("ssh") or "ssh", *ssh_options(state), "-T",
                       state["address"], notebook_environment_command(command)],
                      input=data, timeout=wait)
    elapsed = time.monotonic() - started
    if record:
        save(Path(state["root"]) / record, {"returncode": result.returncode, "elapsed_sec": elapsed,
            "stdout": result.stdout.decode("utf-8", "replace"),
            "stderr": result.stderr.decode("utf-8", "replace").replace(state["address"], "<tailcat-address>")})
    return result, elapsed


def connect(state, wait_seconds=240):
    if type(wait_seconds) is not int or wait_seconds < 1:
        raise ValueError('SSH wait must be a positive number of seconds')
    deadline = time.monotonic() + wait_seconds
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            result, elapsed = remote(state, "python3 -c 'import json,os,platform; print(json.dumps({\"platform\":platform.system(),\"cwd\":os.getcwd(),\"uid\":os.getuid()}))'",
                                     wait=min(30, remaining), record="connect.json")
            if result.returncode == 0:
                return {'ssh_status': 'READY', 'command_elapsed_sec': elapsed,
                        'connection_checked_at': datetime.now(timezone.utc).isoformat()}
        except subprocess.TimeoutExpired:
            pass
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(3, remaining))
    raise TimeoutError("SSH bootstrap not ready; reconnect to this session without submitting again")


def stop(state):
    result, elapsed = remote(state, "touch " + shlex.quote(state["remote_root"] + "/STOP"), record="stop.json")
    if result.returncode:
        raise RuntimeError("SSH stop failed; notebook still has bounded TTL")
    print(json.dumps({"stop_requested_over_ssh": True, "elapsed_sec": elapsed}))


def status(state):
    """Read bootstrap status by its unique slug without browser cookies."""
    return notebook_status(state['account'], state['kernel_ref'], state['run_id'])


def notebook_status(account, kernel_ref, session_id=None):
    import requests
    key, metadata = account_store.resolve(account)
    owner, slug = kernel_ref.split('/', 1)
    if owner != metadata.get('username') or not re.fullmatch('[a-zA-Z0-9_-]+', slug):
        raise ValueError('Bootstrap owner mismatch')
    from provider_metrics import record as record_metrics
    started, response = time.perf_counter(), None
    try:
        response = requests.get('https://www.kaggle.com/api/v1/kernels/status',
            params={'user_name': owner, 'kernel_slug': slug},
            headers={'Authorization': 'Bearer ' + account_store.read_token(key)}, timeout=(10, 20))
    finally:
        record_metrics(key, 0, len(response.content) if response is not None else 0, started,
                       response is None or response.status_code >= 400)
    response.raise_for_status()
    payload = response.json()
    current = str(payload.get('status', '')).lower()
    if not current:
        raise RuntimeError('Kaggle returned no bootstrap status')
    return {'session_id': session_id, 'notebook_ref': kernel_ref,
            'status': current, 'stopped': current in {'complete', 'completed', 'error', 'cancelled', 'canceled'},
            'observed_at': datetime.now(timezone.utc).isoformat()}


def write_ssh_config(state):
    root = Path(state['root'])
    alias = 'kaggle-' + state['run_id'][:12]
    path = root / 'ssh_config'
    shell_command = 'cd ' + shlex.quote(state['remote_root']) + ' && exec bash --noprofile --norc'
    config = [f'Host {alias}', f'    HostName {state["address"]}', '    User root',
              f'    IdentityFile "{(root / "id_ed25519").as_posix()}"',
              '    IdentitiesOnly yes', '    BatchMode yes', '    ConnectTimeout 12',
              '    ServerAliveInterval 10', '    ServerAliveCountMax 2',
              '    RequestTTY auto',
              '    RemoteCommand ' + notebook_environment_command(shell_command),
              '    StrictHostKeyChecking accept-new',
              f'    UserKnownHostsFile "{(root / "known_hosts").as_posix()}"',
              f'    ProxyCommand "{Path(state["tailcat"]).as_posix()}" {state["address"]} 22']
    path.write_text('\n'.join(config) + '\n', encoding='utf-8')
    from auto_login.web_session import protect_acl
    for secret in [path, root / 'state.json', root / 'server.private.json']:
        protect_acl(str(secret))


def descriptor(state):
    config = Path(state['root']) / 'ssh_config'
    return {'session_id': state['run_id'], 'account': state['account'],
            'notebook_ref': state['kernel_ref'], 'relay': 'tok',
            'ssh_config': str(config), 'ssh_alias': 'kaggle-' + state['run_id'][:12],
            'shell_command': f'ssh -F "{config}" kaggle-{state["run_id"][:12]}',
            'remote_directory': state['remote_root'],
            'stop_command': 'touch ' + shlex.quote(state['remote_root'] + '/STOP'),
            'ttl_seconds': state['ttl_seconds'],
            'requested_accelerator': state.get('accelerator', 'unknown'),
            'reconnect_command': ('& ' if os.name == 'nt' else '') +
                                 f'"{Path(sys.executable)}" -m interface_ai_scientist connect '
                                 f'--state "{Path(state["root"]) / "state.json"}"'}


def state_for(session_id, state_root=None):
    state_root = state_root or DEFAULT_STATE
    if not re.fullmatch('[0-9a-f]{32}', session_id):
        raise ValueError('Expected a Kaggle SSH session ID')
    state = load(state_root / session_id / 'state.json')
    if state['run_id'] != session_id:
        raise ValueError('Saved SSH session identity mismatch')
    return state
