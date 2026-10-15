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

from pool import Pool

class Channel:
    def __init__(self, session, state, log):
        self.session,self.state,self.log=session,state,log
        self.process=None
        self.lock=threading.Lock()
        self.connections=0
    def connect(self):
        source=Path(__file__).with_name('remote.py').read_bytes()
        code='import base64;exec(base64.b64decode('+repr(base64.b64encode(source).decode())+'))'
        command=self.session.notebook_environment_command('python3 -u -c '+shlex.quote(code))
        ssh=str(Path(os.environ.get('SYSTEMROOT','C:/Windows'))/'System32/OpenSSH/ssh.exe') if os.name=='nt' else 'ssh'
        self.process=subprocess.Popen([ssh,*self.session.ssh_options(self.state),'-T',self.state['address'],command],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.lines=queue.Queue()
        process=self.process
        lines=self.lines
        def reader():
            for line in process.stdout: lines.put(line)
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
            except (OSError,queue.Empty,ValueError):
                self.process.kill();self.process.wait(timeout=5);self.process=None
                # Never replay a command whose acknowledgement was lost.
                raise ConnectionError('SSH interrupted; inspect the same Run ID before retrying') from None
    def close(self):
        if self.process and self.process.poll() is None:
            self.process.kill();self.process.wait(timeout=5)

def shutdown(session,sid,account,root,requested):
    from bridge import control
    # Read provider state even when SSH has already disappeared after STOP.
    for action in (('status',) if requested else ('status','stop')):
        try:
            result=control(session,action,sid,account,root)
            if action=='stop': requested=True
            if result.get('stopped'): return True,requested
        except Exception as error:
            print('Shutdown '+action+': '+type(error).__name__,file=sys.stderr,flush=True)
    return False,requested

def serve(config):
    from bridge import configure, control
    session=configure(config)
    pool=Pool(config['home'])
    sid=config['sessionId']
    row=pool.get(sid)
    folder=pool.worker_dir(sid)
    folder.mkdir(parents=True,exist_ok=True)
    token=secrets.token_urlsafe(32)
    state={'receipt':None,'channel':None,'error':None}
    from proxy_control import ensure_proxy
    proxy=ensure_proxy()
    log=(folder/'ssh.log').open('ab')
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_POST(self):
            if self.headers.get('Authorization')!='Bearer '+token:
                self.send_error(403);return
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=2*1024*1024: raise ValueError('Invalid packet size')
                r=json.loads(self.rfile.read(size))
                if r['action']=='health': result={'ready':state['channel'] is not None,'error':state['error'],'receipt':state['receipt'],'connections':state['channel'].connections if state['channel'] else 0}
                elif r['action']=='close': pool.close(sid);result={'closing':True}
                elif r['action']=='launch' and pool.get(sid)['state']=='closing': result={'pending':True,'closing':True}
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
            self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    server=HTTPServer(('127.0.0.1',0),Handler);server.timeout=1
    session.save(folder/'worker.json',{'pid':os.getpid(),'port':server.server_port,'token':token})
    from auto_login.web_session import protect_acl
    protect_acl(str(folder/'worker.json'))
    def provision():
        from interface_ai_scientist.bootstrap_service import start
        while pool.get(sid)['state']!='closing' and time.time()<row['deadline']:
            try:
                r=start(account=row['account'],request_id=sid,ttl_seconds=config['ttlSeconds'],accelerator=config['accelerator'],
                    competition_sources=config.get('competitionSources',[]),dataset_sources=config.get('datasetSources',[]),wait_seconds=5)
                state['receipt']=r
                if r.get('ssh_status')=='READY':
                    saved=session.state_for(sid)
                    state['channel']=Channel(session,saved,log)
                    state['channel'].rpc({'action':'ping'})
                    state['error']=None
                    pool.ready(sid,os.getpid());return
                if r.get('submission_status')=='NOT_SUBMITTED': state['error']='Notebook was not submitted';return
            except Exception as e:
                state['error']=type(e).__name__
                print('Provision: '+str(e),file=sys.stderr,flush=True)
            time.sleep(2)
    threading.Thread(target=provision,daemon=True).start()
    last_poll=0
    stop_requested=False
    try:
        while True:
            server.handle_request()
            current=pool.get(sid)
            if current['state']=='closing' or time.time()>=current['deadline']:
                # Keep ownership until the provider confirms stop, including offline periods.
                stopped,stop_requested=shutdown(session,sid,row['account'],Path(config['home'])/'sessions',stop_requested)
                if stopped: pool.stopped(sid);break
                time.sleep(3)
            elif state['channel'] and current['active_run'] and time.monotonic()-last_poll>5:
                last_poll=time.monotonic()
                try:
                    r=state['channel'].rpc({'action':'inspect','runId':current['active_run']})
                    if r['stage'] in ('COMPLETE','ERROR'): pool.release(sid,current['active_run'])
                except Exception: pass
    finally:
        server.server_close()
        if state['channel']: state['channel'].close()
        if proxy is not None and proxy.poll() is None: proxy.terminate();proxy.wait(timeout=5)
        log.close()

if __name__=='__main__':
    serve(json.loads(Path(sys.argv[1]).read_text('utf-8')))
