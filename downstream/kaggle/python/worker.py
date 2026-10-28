"""One detached owner per notebook. Private loopback RPC, one persistent SSH pipe."""
import base64
import json
import os
from pathlib import Path
import queue
import secrets
import shlex
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from pool import Pool, LAUNCH_SECONDS

# Kaggle ends every session within 12 hours of starting, whatever its timeout.
MAX_NOTEBOOK_SECONDS = 12 * 60 * 60
# Push requests time out after 100 seconds; two absences must be this far apart.
PUSH_SETTLE_SECONDS = 2 * 60
ABSENCE_CONFIRM_SECONDS = 2 * 60
from processes import guard_worker, load, lock, owner, save, spawn_tree, stop_tree

class Channel:
    def __init__(self, session, state, log):
        self.session,self.state,self.log=session,state,log
        self.process=None
        self.lock=threading.Lock()
        self.connections=0
    def connect(self):
        self._disconnect()
        source=Path(__file__).with_name('remote.py').read_bytes()
        code='import base64;exec(base64.b64decode('+repr(base64.b64encode(source).decode())+'))'
        command=self.session.notebook_environment_command('python3 -u -c '+shlex.quote(code))
        ssh=str(Path(os.environ.get('SYSTEMROOT','C:/Windows'))/'System32/OpenSSH/ssh.exe') if os.name=='nt' else 'ssh'
        self.process=spawn_tree([ssh,*self.session.ssh_options(self.state),'-T',self.state['address'],command],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.log)
        self.lines=queue.Queue()
        process=self.process
        lines=self.lines
        def reader():
            try:
                for line in process.stdout: lines.put(line)
            except (OSError, ValueError):
                pass
            finally:
                lines.put(None)
        threading.Thread(target=reader,daemon=True).start()
        self.connections+=1
    def rpc(self, request):
        with self.lock:
            if self.process is None or self.process.poll() is not None: self.connect()
            try:
                self.process.stdin.write(json.dumps(request).encode()+b'\n');self.process.stdin.flush()
                line=self.lines.get(timeout=50)
                if line is None: raise ConnectionError('SSH disconnected')
                value=json.loads(line)
                if not value['ok']: raise RuntimeError(value.get('error','Remote operation failed'))
                return value['result']
            except (OSError,queue.Empty,ValueError,KeyError,TypeError):
                self._disconnect()
                # Never replay a command whose acknowledgement was lost.
                raise ConnectionError('SSH interrupted; inspect the same Run ID before retrying') from None
    def _disconnect(self):
        process,self.process=self.process,None
        stop_tree(process)
    def close(self):
        with self.lock:
            self._disconnect()

def rejected(root,sid):
    """Kaggle's error answer to the push, e.g. an exhausted GPU quota.

    The push was answered, so no notebook exists and nothing needs stopping.
    """
    response=load(Path(root)/sid/'push-response.json')
    error=response.get('error')
    return str(error) if error and not response.get('url') else None

def unsent(root,sid):
    """The bundled proxy's proof that the push never reached Kaggle.

    It could not open a connection, so no notebook exists and nothing needs stopping.
    """
    folder=Path(root)/sid
    failure=load(folder/'push-error.json')
    if not failure.get('unsent') or (folder/'push-response.json').exists(): return None
    return failure.get('summary') or 'no connection to Kaggle'

def lost_push(session,sid,row,root):
    """Whether Kaggle proves that a push whose response was lost never landed.

    As for shutdown, the status endpoint must not know the notebook (403/404)
    and verified session lists minutes apart must omit it.
    """
    folder=Path(root)/sid
    if not (folder/'submit-intent.json').exists() or (folder/'push-response.json').exists(): return False
    if load(folder/'absent.json').get('confirmed'): return True
    from bridge import control
    try:
        control(session,'status',sid,row['account'],Path(root))
        return False
    except Exception as error:
        if getattr(getattr(error,'response',None),'status_code',None) not in (403,404): return False
    return not_running(sid,row['account'],root,row) is True

def settle_unsubmitted(session,pool,sid,root,row):
    """Close a session Kaggle proves has no notebook; True when proven.

    Nothing ran, so its Run may be admitted again without replaying anything:
    on another account after a quota refusal, or anywhere after a lost push.
    Other refusals would repeat anywhere and fail the Run.
    """
    error=rejected(root,sid)
    if error:
        note='Kaggle rejected the notebook: '+error
        if 'quota' in error.lower(): pool.never_created(sid,note,quota=True)
        else: pool.close(sid,note)
    elif failure:=unsent(root,sid):
        pool.never_created(sid,'Kaggle push never left this machine: '+failure)
    elif lost_push(session,sid,row,root):
        pool.never_created(sid,'Kaggle never created the notebook; its push response was lost')
    else:
        return False
    return True

def observe_sessions(account):
    from account_runtime import account_readiness
    return account_readiness(account)

def not_running(sid,account,root,row):
    """Whether verified account session lists, minutes apart, omit this notebook.

    True once confirmed, False after the first absence, None when unverified.
    """
    folder=Path(root)/sid
    intent=folder/'submit-intent.json'
    # A push whose response was lost may still land until its request times out.
    if time.time()<(intent.stat().st_mtime if intent.exists() else row['created'])+PUSH_SETTLE_SECONDS: return None
    try: observation=observe_sessions(account)
    except Exception as error:
        print('Shutdown sessions: '+type(error).__name__,file=sys.stderr,flush=True)
        return None
    sessions=observation.get('active_sessions') or []
    slugs=[(s.get('ref') or '').split('/',1)[-1] for s in sessions]
    if 'ai-scientist-ssh-'+sid[:12] in slugs:
        (folder/'absent.json').unlink(missing_ok=True)
        return None
    # An unidentified session could be this notebook; never infer absence.
    if observation.get('readiness') not in ('verified_idle','busy') or observation.get('active_session_count')!=len(sessions) or not all(slugs):
        return None
    first=load(folder/'absent.json').get('at')
    if first is None:
        save(folder/'absent.json',{'at':time.time()})
        return False
    if time.time()-first<ABSENCE_CONFIRM_SECONDS: return False
    # Remembered so shutdown after a requeue needs no further provider calls.
    save(folder/'absent.json',{'at':first,'confirmed':True})
    return True

def shutdown(session,sid,account,root,requested,row=None):
    from bridge import control
    if rejected(root,sid) or unsent(root,sid) or load(Path(root)/sid/'absent.json').get('confirmed'):
        print('Shutdown confirmed: Kaggle never created the notebook',file=sys.stderr,flush=True)
        return True,requested
    missing,absent=False,None
    # Read provider state even when SSH has already disappeared after STOP.
    for action in (('status',) if requested else ('status','stop')):
        if action=='stop' and absent is False: break
        try:
            result=control(session,action,sid,account,root)
            if action=='stop': requested=True
            if result.get('stopped'): return True,requested
        except Exception as error:
            print('Shutdown '+action+': '+type(error).__name__,file=sys.stderr,flush=True)
            # Kaggle answers 403 (not 404) for a private slug that does not exist.
            missing|=action=='status' and getattr(getattr(error,'response',None),'status_code',None) in (403,404)
        if action=='status' and missing and row is not None and not (Path(root)/sid/'push-response.json').exists():
            # Prove the notebook is not running instead of waiting out its
            # lifetime. Nothing running needs an SSH STOP either.
            absent=not_running(sid,account,root,row)
            if absent:
                print('Shutdown confirmed: notebook absent from verified Kaggle sessions',file=sys.stderr,flush=True)
                return True,requested
    if row is not None:
        # Without a receipt the reservation would otherwise outlive any notebook
        # forever. A push that never got a response, has no notebook at Kaggle,
        # and is past deadline plus its own timeout cannot still be running.
        now=time.time()
        lifetime=row['deadline']-row['created']
        never_created=missing and not (Path(root)/sid/'push-response.json').exists() and now>row['deadline']+lifetime+90
        if never_created or now>row['deadline']+MAX_NOTEBOOK_SECONDS:
            print('Shutdown abandoned: '+('notebook never created' if never_created else 'past Kaggle maximum runtime'),file=sys.stderr,flush=True)
            return True,requested
    return False,requested

def serve(config):
    from bridge import configure, control
    session=configure(config)
    pool=Pool(config['home'])
    sid=config['sessionId']
    pool.reconcile()
    row=pool.get(sid)
    if row['state']=='stopped': return
    folder=pool.worker_dir(sid)
    folder.mkdir(parents=True,exist_ok=True)
    token=secrets.token_urlsafe(32)
    state={'receipt':None,'channel':None,'error':None,'fatal':False}
    stopping=threading.Event()
    log=(folder/'ssh.log').open('ab')
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def setup(self):
            super().setup()
            self.connection.settimeout(10)
        def do_POST(self):
            if self.headers.get('Authorization')!='Bearer '+token:
                self.send_error(403);return
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=2*1024*1024: raise ValueError('Invalid packet size')
                r=json.loads(self.rfile.read(size))
                if r['action']=='health': result={'ready':state['channel'] is not None,'error':state['error'],'fatal':state['fatal'],'receipt':state['receipt'],'connections':state['channel'].connections if state['channel'] else 0}
                elif r['action']=='close': pool.close(sid);result={'closing':True}
                elif pool.get(sid)['state'] in ('closing','stopped'): result={'pending':True,'closing':True}
                elif state['channel'] is None: result={'pending':True,'error':state['error']}
                else:
                    if 'runId' in r:
                        assignment=pool.assigned(r['runId'])
                        if not assignment or assignment['id']!=sid: raise ValueError('Run does not belong to this session')
                    result=state['channel'].rpc(r)
                    if r['action']=='inspect' and result.get('stage') in ('COMPLETE','ERROR'):
                        pool.release(sid,r['runId'])
                body=json.dumps({'ok':True,'result':result}).encode()
            except Exception as e:
                body=json.dumps({'ok':False,'errorType':type(e).__name__}).encode()
                print(type(e).__name__+': '+str(e),file=sys.stderr,flush=True)
            try:
                self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            except ConnectionError:
                pass  # The caller timed out first, e.g. a 3-second health probe.
    server=HTTPServer(('127.0.0.1',0),Handler);server.timeout=1
    session.save(folder/'worker.json',{'pid':os.getpid(),'port':server.server_port,'token':token})
    from auto_login.web_session import protect_acl
    protect_acl(str(folder/'worker.json'))
    def give_up(message):
        # Closing stops any submitted notebook and lets the supervisor fail the
        # Run with this reason instead of waiting for the session deadline.
        state['error']=message;state['fatal']=True
        pool.close(sid,message)
    def provision():
        from interface_ai_scientist.bootstrap_service import start
        from proxy_control import ensure_proxy, stop_proxies
        proxy=None
        failures=0
        last_probe=0
        reported=None
        sessions=Path(config['home'])/'sessions'
        try:
            while not stopping.is_set() and pool.get(sid)['state'] not in ('closing','stopped') and time.time()<row['deadline']:
                try:
                    if proxy is None or proxy.poll() is not None: proxy=ensure_proxy()
                    r=start(account=row['account'],request_id=sid,ttl_seconds=config['ttlSeconds'],accelerator=config['accelerator'],
                        competition_sources=config.get('competitionSources',[]),dataset_sources=config.get('datasetSources',[]),wait_seconds=5)
                    state['receipt']=r
                    if r.get('ssh_status')=='READY':
                        saved=session.state_for(sid)
                        channel=Channel(session,saved,log)
                        try:
                            channel.rpc({'action':'ping'})
                            if stopping.is_set() or pool.get(sid)['state'] in ('closing','stopped'): return
                            pool.ready(sid,os.getpid())
                            state['channel']=channel
                        finally:
                            if state['channel'] is not channel: channel.close()
                        state['error']=None
                        return
                    outcome=(r.get('submission_status'),r.get('ssh_status'),r.get('error_type'))
                    if outcome!=reported and r.get('error_type'):
                        detail=(' - '+r['error_detail']) if r.get('error_detail') else ''
                        print('Provision: submission %s, SSH %s, %s'%outcome+detail,file=sys.stderr,flush=True)
                    reported=outcome
                    # Bootstrap reports a rejected or lost push as an UNKNOWN
                    # submission and would wait for SSH from a notebook that
                    # never existed. Provider probes are rate limited.
                    if rejected(sessions,sid) or unsent(sessions,sid) or time.monotonic()-last_probe>=30:
                        last_probe=time.monotonic()
                        if settle_unsubmitted(session,pool,sid,sessions,row):
                            state['error']=pool.get(sid)['note'];state['fatal']=True;return
                    if r.get('submission_status')=='NOT_SUBMITTED':
                        give_up('Kaggle notebook was not submitted ('+str(r.get('error_type') or 'no detail')+')');return
                    failures=0
                except Exception as e:
                    failures+=1
                    state['error']=type(e).__name__+': '+str(e)[:300]
                    print('Provision: '+str(e),file=sys.stderr,flush=True)
                    if failures>=5:
                        give_up('Kaggle bootstrap failed 5 times in a row: '+state['error']);return
                stopping.wait(2)
        finally:
            # Token forwarding is only needed for bootstrap submission, not
            # throughout training or the notebook's idle grace period.
            stop_proxies()
    provision_thread=threading.Thread(target=provision,daemon=True)
    provision_thread.start()
    last_poll=0
    last_status=time.monotonic()
    last_heartbeat=0
    last_maintenance=0
    stop_requested=load(folder/'stop-requested.json').get('requested') is True
    next_stop=0
    stop_delay=3
    try:
        while True:
            if time.monotonic()-last_maintenance>=10:
                # Each owner enforces its bounds even if the pool scheduler
                # itself is temporarily unavailable.
                pool.reconcile()
                last_maintenance=time.monotonic()
            if time.monotonic()-last_heartbeat>=5:
                save(folder/'heartbeat.json',{**owner(os.getpid()),'at':time.time()})
                last_heartbeat=time.monotonic()
            server.handle_request()
            current=pool.get(sid)
            if current['state']=='stopped': break
            if current['state']=='closing' or time.time()>=current['deadline']:
                if current['state']!='closing': pool.close(sid,'Notebook lifetime expired')
                stopping.set()
                if state['channel']: state['channel'].close()
                # A shared scheduler retains uncertain stop reservations;
                # do not keep one local worker alive per expired notebook.
                from scheduler import ensure_scheduler, healthy
                try:
                    ensure_scheduler(pool)
                    if healthy(load(pool.home/'scheduler-owner.json'), load(pool.home/'scheduler-heartbeat.json')):
                        break
                except OSError:
                    pass
                if time.monotonic()<next_stop: continue
                settle_unsubmitted(session,pool,sid,Path(config['home'])/'sessions',row)
                # Keep ownership until the provider confirms stop, including offline periods.
                stopped,stop_requested=shutdown(session,sid,row['account'],Path(config['home'])/'sessions',stop_requested,row=row)
                if stopped: pool.stopped(sid);break
                if stop_requested:
                    save(folder/'stop-requested.json',{'requested':True})
                next_stop=time.monotonic()+stop_delay
                stop_delay=min(60,stop_delay*2)
            elif state['channel'] and current['active_run'] and time.monotonic()-last_poll>5:
                last_poll=time.monotonic()
                try:
                    r=state['channel'].rpc({'action':'inspect','runId':current['active_run']})
                    if r['stage'] in ('COMPLETE','ERROR'): pool.release(sid,current['active_run'])
                    elif r['stage']=='NOT_STARTED' and time.time()-(current['leased_at'] or current['created'])>LAUNCH_SECONDS:
                        pool.close(sid,'Run lease was never launched within 2 minutes')
                except Exception: pass
            elif current['state']=='idle' and state['channel']:
                # Preserve the notebook's packages, not an unused terminal pipe.
                state['channel'].close()
            if current['state']!='closing' and time.monotonic()-last_status>=60:
                last_status=time.monotonic()
                try:
                    if control(session,'status',sid,row['account'],Path(config['home'])/'sessions').get('stopped'):
                        pool.stopped(sid);break
                except Exception:
                    pass  # A network failure does not prove a live Run is dead.
    finally:
        stopping.set()
        server.server_close()
        if state['channel']: state['channel'].close()
        # The lifetime Job reaps any bootstrap child still blocked in a network
        # call when this worker exits. Never wait indefinitely on that thread.
        provision_thread.join(timeout=1)
        log.close()

if __name__=='__main__':
    config=json.loads(Path(sys.argv[1]).read_text('utf-8'))
    pool=Pool(config['home'])
    with lock(pool.worker_dir(config['sessionId'])/'worker.lock') as acquired:
        if acquired:
            guard_worker()
            serve(config)
