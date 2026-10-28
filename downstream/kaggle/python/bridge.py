"""Local JSON boundary to AI Scientist's existing Kaggle bootstrap package.

No Workbench server or MCP transport is involved. Credentials remain in the
donor's account profiles; OpenResearch owns only its ephemeral session state.
"""
import contextlib
import json
from pathlib import Path
import sys
import time


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


def quotas(session, pool, account_store):
    """Weekly accelerator hours per configured account, read concurrently.

    One unreachable account reports its error without hiding the others.
    """
    from concurrent.futures import ThreadPoolExecutor
    keys=[a['id'] for a in accounts(account_store) if a['configured']]
    def read(key):
        try: return session.accelerator_quota(key)
        except Exception as error: return {'account':key,'error':type(error).__name__+': '+str(error)[:200]}
    with ThreadPoolExecutor(max_workers=max(1,min(8,len(keys)))) as executor:
        rows=list(executor.map(read,keys))
    blocks=pool.quota_blocks()
    for row in rows:
        row['blocks']=[b for b in blocks if b['account']==row['account']]
    return {'accounts':rows}


def foreign_sessions(observation, account, live):
    """Kaggle sessions on an account that no live pool reservation owns.

    Returns None when readiness is unverified. A session whose notebook could
    not be identified counts as foreign: never infer a free slot.
    """
    count = observation.get('active_session_count')
    if observation.get('readiness') not in ('verified_idle', 'busy') or type(count) is not int:
        return None
    sessions = observation.get('active_sessions') or []
    owned = {'ai-scientist-ssh-'+r['id'][:12] for r in live if r['account'] == account}
    return count - len(sessions) + sum((s.get('ref') or '').split('/', 1)[-1] not in owned for s in sessions)


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


def configure(request):
    import os,socket
    home=Path(request.get('home') or Path(request['stateRoot']).parent).resolve()
    home.mkdir(parents=True,exist_ok=True)
    os.environ['ORX_KAGGLE_HOME']=str(home)
    if os.name=='nt':
        native_ssh=Path(os.environ.get('SYSTEMROOT','C:/Windows'))/'System32/OpenSSH'
        if str(native_ssh) not in os.environ.get('PATH','').split(os.pathsep):
            os.environ['PATH']=str(native_ssh)+os.pathsep+os.environ.get('PATH','')
    native=Path(__file__).resolve().parent/'native'
    if str(native) not in sys.path: sys.path.insert(0,str(native))
    os.environ['PYTHONPATH']=str(native)
    # Each owner gets its own local token proxy; never borrow another checkout's service.
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    os.environ['AI_SCIENTIST_KAGGLE_PROXY_PORT']=str(port)
    from interface_ai_scientist import session
    session.PROXY_PORT=port
    session.DEFAULT_STATE=home/'sessions'
    return session


def contact(folder,request,timeout=55):
    import urllib.request
    owner=json.loads((folder/'worker.json').read_text('utf-8'))
    req=urllib.request.Request('http://127.0.0.1:'+str(owner['port']),data=json.dumps(request).encode(),
        headers={'Authorization':'Bearer '+owner['token'],'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=timeout) as response: result=json.load(response)
    if not result.get('ok'): raise RuntimeError(result.get('errorType','Session operation failed'))
    return result['result']


def ensure_worker(session,pool,row,request):
    import subprocess
    from processes import alive, load, lock, owner, save, spawn_python, terminate
    from scheduler import ensure_scheduler, healthy
    if not request.get('_scheduler'):
        ensure_scheduler(pool)
    row=pool.get(row['id'])
    # Closing reservations need a retry record, not a resident interpreter.
    # The shared scheduler performs bounded cleanup one session at a time.
    if row['state'] in ('closing','stopped'): return
    folder=pool.worker_dir(row['id'])
    with lock(folder/'launch.lock') as acquired:
        if not acquired: return
        metadata=load(folder/'owner.json')
        if metadata.get('legacy') and alive(metadata): return
        if healthy(metadata,load(folder/'heartbeat.json')): return
        if alive(metadata):
            terminate(metadata)
            if alive(metadata): return
        # A worker also holds this lock throughout its lifetime, including
        # cleanup. A missing/corrupt PID file cannot create a second owner.
        with lock(folder/'worker.lock') as unowned:
            if not unowned: return
        if time.time()<metadata.get('restart_after',0): return
        # Pre-upgrade workers have no lifetime lock. Their authenticated endpoint
        # is also checked when owner.json is lost, before creating any new owner.
        from processes import identity
        previous=load(folder/'worker.json')
        birth=identity(previous.get('pid'))
        known_reused=(previous.get('pid')==metadata.get('pid') and metadata.get('birth') and metadata['birth']!=birth)
        if birth and not known_reused:
            try:
                contact(folder,{'action':'health'},timeout=3)
            except (OSError,ValueError,RuntimeError):
                return  # Unverified legacy owner: neither kill nor duplicate it.
            if identity(previous['pid'])!=birth: return
            save(folder/'owner.json',{**owner(previous['pid']),'legacy':True})
            return
        if metadata.get('pid') and not metadata.get('birth') and identity(metadata['pid']):
            return
        row=pool.get(row['id'])
        if row['state']=='stopped': return
        folder.mkdir(parents=True,exist_ok=True)
        config={**json.loads(row['spec']), 'home':str(pool.home),'sessionId':row['id'],'account':row['account']}
        save(folder/'worker-config.json',config)
        with (folder/'worker.log').open('ab') as log:
            child=spawn_python(Path(__file__).with_name('worker.py'),folder/'worker-config.json',
                stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,cwd=Path(__file__).parent)
        failures=min(6,metadata.get('failures',0)+1)
        save(folder/'owner.json',{**owner(child.pid),'failures':failures,
             'restart_after':time.time()+min(120,5*2**(failures-1))})


def dispatch(request):
    session=configure(request)
    import account_store
    from pool import Pool
    home=Path(request.get('home') or Path(request['stateRoot']).parent)
    pool=Pool(home)
    action=request['action']
    if action=='accounts':
        return {'accounts':accounts(account_store),'accelerators':list(session.ACCELERATORS)}
    if action=='quota':
        return quotas(session,pool,account_store)
    if action=='recover-cookie':
        from auto_login.recovery import recover_cookie_core
        return recover_cookie_core(request['account'])
    if action=='readiness':
        from account_runtime import account_readiness
        return account_readiness(request['account'])
    if action=='sessions':
        for row in pool.reconcile(): ensure_worker(session,pool,row,request)
        rows=pool.list()
        return {'sessions':[{k:v for k,v in r.items() if k not in ('spec',)} for r in rows]}
    if action=='close-session':
        row=pool.get(request['sessionId']);pool.close(row['id']);ensure_worker(session,pool,row,request)
        return {'closing':True}
    row=pool.assigned(request['runId'])
    if action=='acquire' and row is None:
        from account_runtime import account_readiness
        # Prefer a compatible idle notebook before probing provider account admission.
        row=pool.acquire(request,[],reclaim=False)
        blocked=pool.blocked(request['accelerator']) if row is None else {}
        if row is None and blocked and all(a in blocked for a in request['runtime']['accounts']):
            # Waiting cannot help before the weekly reset; fail with Kaggle's reason.
            pool.cancel_wait(request['runId'])
            return {'stopped':True,'error':'Every enabled Kaggle account is out of quota for '+request['accelerator']+'. '+next(iter(blocked.values()))}
        if row is None:
            for existing in pool.reconcile(): ensure_worker(session,pool,existing,request)
            free={}
            for account in request['runtime']['accounts']:
                if account in blocked or not pool.has_room(account,request['accelerator']): continue
                try: external=foreign_sessions(account_readiness(account),account,pool.live())
                except Exception: continue
                if external is not None and pool.has_room(account,request['accelerator'],external): free[account]=external
            row=pool.acquire(request,free)
            if row is None:
                # Reclamation may have changed idle -> closing just now. Wake
                # maintenance even if the scheduler had no owners before this call.
                for existing in pool.live(): ensure_worker(session,pool,existing,request)
        if row is None: return {'queued':True,'detail':'Waiting for a compatible idle notebook or a ready account from the pool.'}
    if row is None and action=='cancel':
        # A queued Run owns no notebook and must not acquire one just to cancel.
        pool.cancel_wait(request['runId'])
        return {'stage':'ERROR','exitCode':143,'log':'','offset':request.get('offset',0),'logSize':0}
    if row is None and action=='release':
        pool.cancel_wait(request['runId'])
        return {'released':True}
    if row is None: raise ValueError('Run has no session assignment')
    if row['state']=='stopped': return {'stopped':True,'sessionId':row['id'],'error':row.get('note')}
    if row['state']=='closing':
        # Only the shared scheduler finishes a closing session. Keep it running
        # while any Run still waits on one, even after a scheduler crash.
        ensure_worker(session,pool,row,request)
    if row['state']=='closing' and action in ('acquire','launch'): return {'pending':True,'closing':True,'sessionId':row['id'],'error':row.get('note')}
    if row['state']=='closing' and action=='cancel':
        # Shutting down the notebook ends the Run; its worker no longer relays
        # requests. The account stays reserved until the provider stop receipt.
        return {'stopped':True,'closing':True,'sessionId':row['id'],'error':row.get('note')}
    ensure_worker(session,pool,row,request)
    folder=pool.worker_dir(row['id'])
    if action=='acquire':
        pool.touch(row['id'],request['runId'])
        try: health=contact(folder,{'action':'health'},timeout=3)
        except (OSError,ValueError): return {'pending':True,'sessionId':row['id'],'account':row['account']}
        return {**health,'sessionId':row['id'],'account':row['account'],'deadline':row['deadline']}
    if action=='release':
        # A supervisor can time out or restart while its remote process lives.
        # Never make the notebook reusable on the strength of local state alone.
        observed=contact(folder,{**request,'action':'inspect'})
        terminal=observed.get('stage') in ('COMPLETE','ERROR')
        if terminal: pool.release(row['id'],request['runId'])
        return {'released':terminal}
    if action=='launch':
        pool.touch(row['id'],request['runId'])
        source=Path(request['sourcePath'])
        import base64
        offset=0
        with source.open('rb') as f:
            while chunk:=f.read(256*1024):
                pool.touch(row['id'],request['runId'])
                r=contact(folder,{'action':'upload','digest':request['digest'],'offset':offset,'data':base64.b64encode(chunk).decode(),'final':offset+len(chunk)==source.stat().st_size})
                # A channel that is not ready, or a closing session, answers pending.
                if r.get('pending'): return r
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
