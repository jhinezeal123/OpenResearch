"""Short RPC operations over one SSH pipe; experiment process groups outlive it."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tarfile
import time
import uuid

ROOT = Path.home()/'.orx'

def run_dir(run_id):
    return ROOT/'runs'/str(uuid.UUID(run_id))

def inspect(directory, offset=0):
    log = directory/'log'
    data = b''
    if log.exists():
        with log.open('rb') as f:
            f.seek(offset)
            data=f.read(256*1024)
    result={'log':base64.b64encode(data).decode(), 'offset':offset+len(data), 'stage':'RUNNING','logSize':log.stat().st_size if log.exists() else 0}
    if (directory/'exit_code').exists():
        code=int((directory/'exit_code').read_text())
        result.update(stage='COMPLETE' if code==0 else 'ERROR', exitCode=code)
    elif (directory/'launch-intent').exists() and not (directory/'pid').exists():
        result.update(stage='UNKNOWN', message='Launch acknowledgement missing; command will not be replayed.')
    return result

def dispatch(r):
    action=r['action']
    if action=='ping': return {'ready':True}
    if action=='upload':
        digest=r['digest']
        if not re.fullmatch('[0-9a-f]{64}',digest): raise ValueError('Invalid source digest')
        cache=ROOT/'source';cache.mkdir(parents=True,exist_ok=True)
        target=cache/(digest+'.tar')
        if target.exists(): return {'present':True}
        temporary=cache/(digest+'.part')
        data=base64.b64decode(r.get('data',''),validate=True)
        offset=r['offset']
        if offset==0: temporary.write_bytes(b'')
        if temporary.stat().st_size!=offset: raise ValueError('Upload offset mismatch')
        with temporary.open('ab') as f: f.write(data)
        if r.get('final'):
            h=hashlib.sha256()
            with temporary.open('rb') as f:
                while chunk:=f.read(1024*1024): h.update(chunk)
            if h.hexdigest()!=digest: raise ValueError('Source digest mismatch')
            temporary.replace(target)
        return {'present':target.exists()}
    directory=run_dir(r['runId'])
    if action=='launch':
        directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        if (directory/'launch-intent').exists() or (directory/'cancel-intent').exists(): return inspect(directory)
        repo=directory/'repo';repo.mkdir(exist_ok=True)
        digest=r['digest']
        if not re.fullmatch('[0-9a-f]{64}',digest): raise ValueError('Invalid source digest')
        with tarfile.open(ROOT/'source'/(digest+'.tar')) as archive:
            archive.extractall(repo,filter='data')
        # Separate cwd/source for every run; notebook packages and mounted inputs persist.
        import shlex
        seconds=max(1,int(r['seconds']))
        script='''#!/bin/bash
cd "$(dirname "$0")"
trap 'code=$?; echo "$code" > exit_code; trap - TERM; kill -KILL 0' EXIT
cd repo || exit 97
'''+f'exec_env_placeholder\ntimeout --foreground --signal=TERM --kill-after=5s {seconds}s bash -eo pipefail -c '+shlex.quote(r['command'])+'\n'
        exports='\n'.join('export '+k+'='+shlex.quote(v) for k,v in r.get('env',{}).items() if re.fullmatch('[A-Za-z_][A-Za-z0-9_]*',k))
        # exit_code lives beside the run log, independent of command cwd. run.sh
        # leads its own session, so the EXIT trap's `kill 0` also reaps anything the
        # command left behind (timeout --foreground signals only its direct child),
        # keeping GPU memory free for the next Run on this reused notebook.
        script=script.replace("echo \"$code\" > exit_code",'echo "$code" > '+shlex.quote(str(directory/'exit_code'))).replace('exec_env_placeholder',exports)
        (directory/'run.sh').write_text(script)
        (directory/'launch-intent').write_text(str(time.time()))
        with (directory/'log').open('ab') as log:
            process=subprocess.Popen(['bash',str(directory/'run.sh')],stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        (directory/'pid').write_text(str(process.pid))
        return inspect(directory)
    if action=='cancel':
        directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        # A durable tombstone also blocks a delayed launch after cancellation.
        (directory/'cancel-intent').write_text(str(time.time()))
        if (directory/'pid').exists() and not (directory/'exit_code').exists():
            pid=int((directory/'pid').read_text())
            try: os.killpg(pid,signal.SIGTERM)
            except ProcessLookupError: pass
            # Kill descendants, then persist a cancellation receipt. Never touch another run.
            time.sleep(.3)
            try: os.killpg(pid,signal.SIGKILL)
            except ProcessLookupError: pass
            (directory/'exit_code').write_text('143')
        elif not (directory/'launch-intent').exists() and not (directory/'exit_code').exists():
            (directory/'exit_code').write_text('143')
        return inspect(directory,r.get('offset',0))
    if action=='inspect': return inspect(directory,r.get('offset',0))
    raise ValueError('Unknown remote action')

def main():
    os.umask(0o077)
    for line in sys.stdin:
        try: value={'ok':True,'result':dispatch(json.loads(line))}
        except Exception as e: value={'ok':False,'error':str(e)}
        print(json.dumps(value),flush=True)

if __name__=='__main__': main()
