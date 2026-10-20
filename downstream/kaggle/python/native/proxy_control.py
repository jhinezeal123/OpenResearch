"""Bundle-owned token proxy; never borrow the external donor service."""
from pathlib import Path
import json
import os
import subprocess
import sys
import time
import urllib.request

PROXY_PORT = int(os.environ.get('AI_SCIENTIST_KAGGLE_PROXY_PORT', '8013'))


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
        process = subprocess.Popen([sys.executable, str(root / 'proxy.py'), str(PROXY_PORT)], cwd=root,
            stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
            env={**os.environ, 'ORX_KAGGLE_PROXY_OWNER': str(os.getpid())},
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if ready():
            return process
        if process.poll() is not None:
            break
        time.sleep(0.1)
    if process.poll() is None:
        process.terminate()
        process.wait(timeout=5)
    raise RuntimeError('Bundle proxy failed; check kaggle mcp/logs/proxy.log or the configured port')
