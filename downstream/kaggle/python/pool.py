"""Durable account admission. A Run lease and a notebook lifetime are separate."""
import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

class Pool:
    def __init__(self, home):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions(
              id TEXT PRIMARY KEY, account TEXT NOT NULL, spec TEXT NOT NULL,
              created REAL NOT NULL, deadline REAL NOT NULL, state TEXT NOT NULL,
              active_run TEXT, worker_pid INTEGER, note TEXT);
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, session_id TEXT NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS one_live_notebook_per_account
              ON sessions(account) WHERE state != 'stopped';
            """)
    @contextmanager
    def db(self):
        db = sqlite3.connect(self.home/'pool.sqlite3', timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db: yield db
        finally: db.close()
    @staticmethod
    def spec(request):
        # Environment and input mounts must match; never cross project boundaries.
        return json.dumps({k:request.get(k) for k in ('projectId','accelerator','ttlSeconds','competitionSources','datasetSources')}, sort_keys=True)
    def worker_dir(self, sid):
        return self.home/"workers"/uuid.UUID(sid).hex
    def assigned(self, run_id):
        with self.db() as db:
            row=db.execute('SELECT s.* FROM sessions s JOIN runs r ON s.id=r.session_id WHERE r.id=?',(run_id,)).fetchone()
            return dict(row) if row else None
    def acquire(self, request, available):
        run_id=str(uuid.UUID(request['runId']))
        spec=self.spec(request)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            prior=db.execute('SELECT s.* FROM sessions s JOIN runs r ON s.id=r.session_id WHERE r.id=?',(run_id,)).fetchone()
            if prior: return dict(prior)
            enabled=request['runtime']['accounts']
            rows=db.execute("SELECT * FROM sessions WHERE spec=? AND state='idle' AND deadline>? ORDER BY created DESC",(spec,time.time()+60)).fetchall()
            chosen=next((r for r in rows if r['account'] in enabled),None)
            if chosen:
                sid=chosen['id']
                db.execute("UPDATE sessions SET state='busy',active_run=? WHERE id=?",(run_id,sid))
            else:
                busy={r[0] for r in db.execute("SELECT account FROM sessions WHERE state!='stopped'")}
                account=next((a for a in enabled if a in available and a not in busy),None)
                if not account: return None
                sid=uuid.uuid4().hex
                now=time.time()
                db.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?,?,NULL,NULL)',(sid,account,spec,now,now+request['ttlSeconds'],'provisioning',run_id))
            db.execute('INSERT INTO runs VALUES(?,?)',(run_id,sid))
            return dict(db.execute('SELECT * FROM sessions WHERE id=?',(sid,)).fetchone())
    def list(self):
        with self.db() as db: return [dict(r) for r in db.execute('SELECT * FROM sessions ORDER BY created DESC LIMIT 100')]
    def get(self, sid):
        if uuid.UUID(sid).hex != sid: raise ValueError('Invalid session ID')
        with self.db() as db:
            row=db.execute('SELECT * FROM sessions WHERE id=?',(sid,)).fetchone()
            if not row: raise ValueError('Unknown session')
            return dict(row)
    def ready(self, sid, pid):
        with self.db() as db:
            db.execute("UPDATE sessions SET state=CASE WHEN active_run IS NULL THEN 'idle' ELSE 'busy' END,worker_pid=? WHERE id=? AND state!='closing'",(pid,sid))
    def release(self, sid, run_id):
        with self.db() as db:
            db.execute("UPDATE sessions SET state='idle',active_run=NULL WHERE id=? AND active_run=? AND state IN ('busy','provisioning')",(sid,run_id))
    def close(self, sid):
        self.get(sid)
        with self.db() as db: db.execute("UPDATE sessions SET state='closing' WHERE id=? AND state!='stopped'",(sid,))
    def stopped(self, sid):
        # Only a caller holding a provider stop receipt may call this.
        with self.db() as db: db.execute("UPDATE sessions SET state='stopped',active_run=NULL WHERE id=?",(sid,))
