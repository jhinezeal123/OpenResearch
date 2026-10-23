"""Bundle-owned token proxy; never borrow the external donor service."""
from pathlib import Path
import json
import os
import subprocess
import sys
import time
import urllib.request
from processes import owner, spawn_tree, stop_tree

PROXY_PORT = int(os.environ.get('AI_SCIENTIST_KAGGLE_PROXY_PORT', '8013'))
_owned = []


def stop_proxies():
    # Includes a proxy restarted by session.client() during a failed bootstrap.
    while _owned:
        stop_tree(_owned.pop())


def ensure_proxy():
    root = Path(__file__).resolve().parent
    def ready():
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{PROXY_PORT}/_kaggle_proxy_health', timeout=.5) as response:
                data = json.load(response)
            return (data.get('service') == 'ai-scientist-kaggle-proxy'
                    and Path(data.get('root', '')).resolve() == root)
        except (OSError, ValueError):
            return False
    if ready():
        return None
    logs = Path(os.environ['ORX_KAGGLE_HOME']) / 'logs'
    logs.mkdir(exist_ok=True)
    with (logs / 'proxy.log').open('ab') as output:
        process = spawn_tree([sys.executable, str(root / 'proxy.py'), str(PROXY_PORT)], cwd=root,
            stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
            env={**os.environ, 'ORX_KAGGLE_PROXY_OWNER_ID': json.dumps(owner(os.getpid())),
                 'PYTHONPATH': str(root.parent)+os.pathsep+os.environ.get('PYTHONPATH','')})
    _owned.append(process)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if ready():
            return process
        if process.poll() is not None:
            break
        time.sleep(0.1)
    stop_tree(process)
    _owned.remove(process)
    raise RuntimeError('Bundle proxy failed; check kaggle mcp/logs/proxy.log or the configured port')
