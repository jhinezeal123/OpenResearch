"""Short-lived Kaggle SSH bootstrap. Uses only the Python standard library."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile
import time
import urllib.request

RELEASE_URL = "https://github.com/tailscale/tailcat/releases/download/v0.7.0/tailcat_0.7.0_linux_amd64.tar.gz"
RELEASE_SHA256 = "23c0b1887a5ec422f0d18a9c52b4f5357815febdaae738a1eb54036d10bd9ee6"


def run(config):
    root = Path("/kaggle/working/ai-scientist") / config["run_id"]
    root.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + config["ttl_seconds"]
    print(json.dumps({"event": "bootstrap_start", "run_id": config["run_id"]}), flush=True)

    archive = root / "tailcat.tar.gz"
    with urllib.request.urlopen(RELEASE_URL, timeout=45) as response:
        data = response.read(20_000_001)
    if hashlib.sha256(data).hexdigest() != RELEASE_SHA256:
        raise RuntimeError("Tailcat Linux release checksum mismatch")
    archive.write_bytes(data)
    binary = root / "tailcat"
    with tarfile.open(archive) as package:
        member = next(m for m in package.getmembers() if Path(m.name).name == "tailcat" and m.isfile())
        with package.extractfile(member) as source:
            binary.write_bytes(source.read())
    binary.chmod(0o700)

    key = root / "server.private.json"
    key.write_text(json.dumps(config["server_key"]), encoding="utf-8")
    key.chmod(0o600)
    authorized = root / "authorized_keys"
    authorized.write_text(config["authorized_key"] + "\n", encoding="utf-8")
    authorized.chmod(0o600)
    endpoint = root / "endpoint.txt"
    env = {**os.environ, "TAILCAT_ADDR_FILE": str(endpoint)}
    with (root / "bootstrap.log").open("w", encoding="utf-8") as log:
        server = subprocess.Popen(
            [str(binary), "--key", str(key), "serve", "--ssh-authorized-keys", str(authorized), "ssh"],
            cwd="/kaggle/working", env=env, stdout=log, stderr=subprocess.STDOUT,
        )
        reason = "ttl_expired"
        ready = False
        try:
            while time.monotonic() < deadline:
                if server.poll() is not None:
                    raise RuntimeError("Tailcat server exited before stop; inspect bootstrap.log")
                if endpoint.exists() and not ready:
                    ready = True
                    print(json.dumps({"event": "ssh_ready", "run_id": config["run_id"]}), flush=True)
                if (root / "STOP").exists():
                    reason = "ssh_stop_requested"
                    break
                time.sleep(0.5)
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
            # These are per-execution tunnel secrets, not account credentials.
            key.unlink(missing_ok=True)
            endpoint.unlink(missing_ok=True)
        summary = {"run_id": config["run_id"], "ready": ready, "stop_reason": reason}
        (root / "lifecycle.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(json.dumps({"event": "bootstrap_finished", **summary}), flush=True)
