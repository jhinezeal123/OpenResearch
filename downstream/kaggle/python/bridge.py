"""Local JSON boundary to AI Scientist's existing Kaggle bootstrap package.

No Workbench server or MCP transport is involved. Credentials remain in the
donor's account profiles; OpenResearch owns only its ephemeral session state.
"""
import contextlib
import hashlib
import json
from pathlib import Path
import sys


def session_id(run_id):
    import uuid
    return uuid.UUID(run_id).hex


def accounts(account_store):
    result = []
    for key, entry in account_store.load_registry()["accounts"].items():
        try:
            account_store.read_token(key)
            configured = True
        except (OSError, ValueError):
            configured = False
        result.append({"id": key, "alias": entry.get("alias", key),
                       "username": entry.get("username"), "configured": configured})
    return result


def control(session, action, sid, account, state_root):
    receipt = state_root / sid / "stop-confirmed.json"
    if receipt.is_file():
        previous = session.load(receipt)
        if previous.get("session_id") == sid and previous.get("account") == account and previous.get("stopped") is True:
            return previous
    result = session.control(action, sid, account)
    if result.get("stopped") is True:
        result["account"] = account
        session.save(receipt, result)
    return result


def previous_stopped(session, state_root, sid, account):
    # The account lease can survive a child exiting before prepare/submit. The
    # donor always journals submit intent BEFORE making a provider request.
    if not (state_root / sid / "submit-intent.json").exists():
        return True
    return control(session, "status", sid, account, state_root).get("stopped") is True


def configure(request):
    import os,socket
    home=Path(request.get('home') or Path(request['stateRoot']).parent).resolve()
    home.mkdir(parents=True,exist_ok=True)
    os.environ['ORX_KAGGLE_HOME']=str(home)
    if os.name=='nt':
        native_ssh=Path(os.environ.get('SYSTEMROOT','C:/Windows'))/'System32/OpenSSH'
        os.environ['PATH']=str(native_ssh)+os.pathsep+os.environ.get('PATH','')
    native=Path(__file__).resolve().parent/'native'
    sys.path.insert(0,str(native))
    os.environ['PYTHONPATH']=str(native)
    # Each owner gets its own local token proxy; never borrow another checkout's service.
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    os.environ['AI_SCIENTIST_KAGGLE_PROXY_PORT']=str(port)
    from interface_ai_scientist import session
    session.PROXY_PORT=port
    session.DEFAULT_STATE=home/'sessions'
    return session


def alive(pid):
    import os
    if not pid: return False
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes
        k=ctypes.WinDLL('kernel32',use_last_error=True)
        k.OpenProcess.restype=wintypes.HANDLE
        handle=k.OpenProcess(0x1000,False,int(pid))
        if not handle: return False
        try:
            status=wintypes.DWORD()
            return bool(k.GetExitCodeProcess(handle,ctypes.byref(status))) and status.value==259
        finally: k.CloseHandle(handle)
    try: os.kill(pid,0);return True
    except ProcessLookupError: return False


def contact(folder,request,timeout=55):
    import urllib.request
    owner=json.loads((folder/'worker.json').read_text('utf-8'))
    req=urllib.request.Request('http://127.0.0.1:'+str(owner['port']),data=json.dumps(request).encode(),
        headers={'Authorization':'Bearer '+owner['token'],'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=timeout) as response: result=json.load(response)
    if not result.get('ok'): raise RuntimeError(result.get('errorType','Session operation failed'))
    return result['result']


def ensure_worker(session,pool,row,request):
    import os,subprocess
    folder=pool.worker_dir(row['id'])
    with session.request_lock(row['id'],namespace='assets'):
        owner=folder/'owner.json'
        if owner.exists() and alive(json.loads(owner.read_text())['pid']): return
        folder.mkdir(parents=True,exist_ok=True)
        config={**json.loads(row['spec']), 'home':str(pool.home),'sessionId':row['id'],'account':row['account']}
        session.save(folder/'worker-config.json',config)
        with (folder/'worker.log').open('ab') as log:
            child=subprocess.Popen([sys.executable,str(Path(__file__).with_name('worker.py')),str(folder/'worker-config.json')],
                stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,cwd=Path(__file__).parent,
                creationflags=(getattr(subprocess,'DETACHED_PROCESS',0)|getattr(subprocess,'CREATE_NEW_PROCESS_GROUP',0)|getattr(subprocess,'CREATE_NO_WINDOW',0)),start_new_session=os.name!='nt')
        session.save(owner,{'pid':child.pid})


def dispatch(request):
    session=configure(request)
    import account_store
    from pool import Pool
    home=Path(request.get('home') or Path(request['stateRoot']).parent)
    pool=Pool(home)
    action=request['action']
    if action=='accounts':
        return {'accounts':accounts(account_store),'accelerators':list(session.ACCELERATORS)}
    if action=='recover-cookie':
        from auto_login.recovery import recover_cookie_core
        return recover_cookie_core(request['account'])
    if action=='readiness':
        from account_runtime import account_readiness
        return account_readiness(request['account'])
    if action=='sessions':
        rows=pool.list()
        for row in rows:
            if row['state']!='stopped': ensure_worker(session,pool,row,request)
        return {'sessions':[{k:v for k,v in r.items() if k not in ('spec',)} for r in rows]}
    if action=='close-session':
        row=pool.get(request['sessionId']);pool.close(row['id']);ensure_worker(session,pool,row,request)
        return {'closing':True}
    # Historical one-Run notebooks remain inspectable after the migration.
    if action in ('stop','status'):
        return control(session,action,session_id(request['runId']),request['account'],home/'sessions')
    row=pool.assigned(request['runId'])
    if action=='acquire' and row is None:
        from account_runtime import account_readiness
        # Prefer a compatible idle notebook before probing provider account admission.
        row=pool.acquire(request,[])
        if row is None:
            free=[]
            reserved={r['account'] for r in pool.list() if r['state']!='stopped'}
            for account in request['runtime']['accounts']:
                if account in reserved: continue
                try:
                    if account_readiness(account).get('readiness')=='verified_idle': free.append(account)
                except Exception: pass
            row=pool.acquire(request,free)
        if row is None: return {'queued':True,'detail':'Waiting for a compatible idle notebook or a ready account from the pool.'}
    if row is None: raise ValueError('Run has no session assignment')
    if row['state']=='stopped': return {'stopped':True,'sessionId':row['id']}
    if row['state']=='closing' and action in ('acquire','launch'): return {'pending':True,'closing':True,'sessionId':row['id']}
    ensure_worker(session,pool,row,request)
    folder=pool.worker_dir(row['id'])
    if action=='acquire':
        try: health=contact(folder,{'action':'health'},timeout=3)
        except (OSError,ValueError): return {'pending':True,'sessionId':row['id'],'account':row['account']}
        return {**health,'sessionId':row['id'],'account':row['account'],'deadline':row['deadline']}
    if action=='release': pool.release(row['id'],request['runId']);return {'released':True}
    if action=='launch':
        source=Path(request['sourcePath'])
        import base64
        offset=0
        with source.open('rb') as f:
            while chunk:=f.read(256*1024):
                r=contact(folder,{'action':'upload','digest':request['digest'],'offset':offset,'data':base64.b64encode(chunk).decode(),'final':offset+len(chunk)==source.stat().st_size})
                offset+=len(chunk)
                if r['present']: break
        request['seconds']=max(1,min(request['ttlSeconds'],int(row['deadline']-__import__('time').time())))
    return contact(folder,request)


def main():
    request=json.load(sys.stdin)
    try:
        with contextlib.redirect_stdout(sys.stderr): result=dispatch(request)
        print(json.dumps({'ok':True,'result':result}),flush=True)
    except Exception as error:
        print(f"Kaggle {request.get('action')}: {type(error).__name__}: {error}",file=sys.stderr)
        print(json.dumps({'ok':False,'errorType':type(error).__name__}),flush=True)
        return 1
    return 0

if __name__=='__main__': raise SystemExit(main())
