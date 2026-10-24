"""Durable account admission. A Run lease and a notebook lifetime are separate."""
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

# Warm notebooks are useful between Runs, but must not occupy an account all
# day. These bounds are independent of a running experiment's lifetime.
IDLE_SECONDS = 5 * 60
PROVISION_SECONDS = 10 * 60
QUEUE_SECONDS = 2 * 60
LAUNCH_SECONDS = 2 * 60
# Kaggle runs at most two GPU sessions per account; CPU and TPU stay exclusive.
GPU_ACCELERATORS = frozenset({'NvidiaTeslaT4'})
GPU_SESSIONS_PER_ACCOUNT = 2

def capacity(accelerator, live):
    """Notebook slots for a new session beside an account's live accelerators."""
    if accelerator in GPU_ACCELERATORS and all(a in GPU_ACCELERATORS for a in live):
        return GPU_SESSIONS_PER_ACCOUNT
    return 1

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
            -- Admission counts slots per accelerator inside BEGIN IMMEDIATE.
            DROP INDEX IF EXISTS one_live_notebook_per_account;
            CREATE TABLE IF NOT EXISTS pending_runs(
              id TEXT PRIMARY KEY, accounts TEXT NOT NULL, created REAL NOT NULL,
              last_seen REAL NOT NULL);
            """)
            db.execute('BEGIN IMMEDIATE')
            columns = {r['name'] for r in db.execute('PRAGMA table_info(sessions)')}
            for name in ('idle_since', 'leased_at'):
                if name not in columns:
                    db.execute(f'ALTER TABLE sessions ADD COLUMN {name} REAL')
            # Give existing idle notebooks one grace period after upgrading.
            db.execute("UPDATE sessions SET idle_since=? WHERE state='idle' AND idle_since IS NULL", (time.time(),))
            db.execute('UPDATE sessions SET leased_at=created WHERE active_run IS NOT NULL AND leased_at IS NULL')
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
    @staticmethod
    def _live_accelerators(db, account):
        return [json.loads(r[0]).get('accelerator') for r in
                db.execute("SELECT spec FROM sessions WHERE account=? AND state!='stopped'", (account,))]
    def has_room(self, account, accelerator, external=0):
        """Whether local reservations plus ``external`` provider sessions leave a slot."""
        with self.db() as db:
            live = self._live_accelerators(db, account)
        return len(live) + external < capacity(accelerator, live)
    def acquire(self, request, available, *, reclaim=True):
        """``available`` lists ready accounts, or maps each to its count of
        Kaggle sessions this pool does not own (they occupy slots too)."""
        run_id=str(uuid.UUID(request['runId']))
        spec=self.spec(request)
        external=available if isinstance(available, dict) else {}
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            prior=db.execute('SELECT s.* FROM sessions s JOIN runs r ON s.id=r.session_id WHERE r.id=?',(run_id,)).fetchone()
            if prior: return dict(prior)
            now = time.time()
            db.execute('DELETE FROM pending_runs WHERE last_seen<?', (now-QUEUE_SECONDS,))
            enabled=request['runtime']['accounts']
            db.execute('''INSERT INTO pending_runs VALUES(?,?,?,?) ON CONFLICT(id)
                          DO UPDATE SET accounts=excluded.accounts,last_seen=excluded.last_seen''',
                       (run_id,json.dumps(enabled),now,now))
            # FIFO for contended accounts, without blocking disjoint accounts.
            older = set()
            for waiting in db.execute('SELECT id,accounts FROM pending_runs ORDER BY created,rowid'):
                if waiting['id'] == run_id: break
                older.update(json.loads(waiting['accounts']))
            eligible = [a for a in enabled if a not in older]
            rows=db.execute("SELECT * FROM sessions WHERE spec=? AND state='idle' AND deadline>? AND idle_since>? ORDER BY created DESC",(spec,now+60,now-IDLE_SECONDS)).fetchall()
            chosen=next((r for r in rows if r['account'] in eligible),None)
            if chosen:
                sid=chosen['id']
                db.execute("UPDATE sessions SET state='busy',active_run=?,leased_at=?,idle_since=NULL WHERE id=?",(run_id,now,sid))
            else:
                def room(a):
                    live=self._live_accelerators(db, a)
                    return len(live)+external.get(a,0) < capacity(request.get('accelerator'), live)
                account=next((a for a in eligible if a in available and room(a)),None)
                if not account:
                    # Reclaim only one idle notebook under pressure. A closing
                    # notebook still reserves its account until a stop receipt.
                    closing={r[0] for r in db.execute("SELECT account FROM sessions WHERE state='closing'")}
                    if reclaim and not closing.intersection(eligible):
                        idle=db.execute("SELECT * FROM sessions WHERE state='idle' ORDER BY idle_since,created").fetchall()
                        victim=next((r for r in idle if r['account'] in eligible),None)
                        if victim:
                            db.execute("UPDATE sessions SET state='closing',note=? WHERE id=?",('Idle notebook reclaimed for a waiting Run',victim['id']))
                    return None
                sid=uuid.uuid4().hex
                db.execute('''INSERT INTO sessions(id,account,spec,created,deadline,state,active_run,leased_at)
                              VALUES(?,?,?,?,?,?,?,?)''',(sid,account,spec,now,now+request['ttlSeconds'],'provisioning',run_id,now))
            db.execute('INSERT INTO runs VALUES(?,?)',(run_id,sid))
            db.execute('DELETE FROM pending_runs WHERE id=?',(run_id,))
            return dict(db.execute('SELECT * FROM sessions WHERE id=?',(sid,)).fetchone())
    def list(self):
        with self.db() as db: return [dict(r) for r in db.execute('SELECT * FROM sessions ORDER BY created DESC LIMIT 100')]
    def live(self):
        # Maintenance must not lose old live rows behind the UI's history cap.
        with self.db() as db: return [dict(r) for r in db.execute("SELECT * FROM sessions WHERE state!='stopped' ORDER BY created")]
    def cancel_wait(self, run_id):
        with self.db() as db: db.execute('DELETE FROM pending_runs WHERE id=?',(run_id,))
    def reconcile(self, now=None):
        now = time.time() if now is None else now
        with self.db() as db:
            db.execute('DELETE FROM pending_runs WHERE last_seen<?',(now-QUEUE_SECONDS,))
            for condition, parameters, note in (
                ('deadline<=?', (now,), 'Notebook lifetime expired'),
                ("state='idle' AND idle_since<=?", (now-IDLE_SECONDS,), 'Notebook idle for 5 minutes'),
                ("state='provisioning' AND created<=?", (now-PROVISION_SECONDS,), 'Notebook provisioning exceeded 10 minutes'),
            ):
                db.execute("UPDATE sessions SET state='closing',note=? WHERE state NOT IN ('stopped','closing') AND "+condition,(note,*parameters))
        return self.live()
    def get(self, sid):
        if uuid.UUID(sid).hex != sid: raise ValueError('Invalid session ID')
        with self.db() as db:
            row=db.execute('SELECT * FROM sessions WHERE id=?',(sid,)).fetchone()
            if not row: raise ValueError('Unknown session')
            return dict(row)
    def ready(self, sid, pid):
        with self.db() as db:
            db.execute("UPDATE sessions SET state=CASE WHEN active_run IS NULL THEN 'idle' ELSE 'busy' END,worker_pid=?,idle_since=CASE WHEN active_run IS NULL THEN COALESCE(idle_since,?) ELSE NULL END WHERE id=? AND state IN ('provisioning','busy','idle')",(pid,time.time(),sid))
    def touch(self, sid, run_id):
        # Activity before launch may include a large source upload. Only the
        # current lease holder can extend this grace, never the notebook TTL.
        with self.db() as db:
            db.execute("UPDATE sessions SET leased_at=? WHERE id=? AND active_run=? AND state IN ('busy','provisioning')",(time.time(),sid,run_id))
    def release(self, sid, run_id):
        with self.db() as db:
            db.execute("UPDATE sessions SET state='idle',active_run=NULL,idle_since=?,leased_at=NULL WHERE id=? AND active_run=? AND state IN ('busy','provisioning')",(time.time(),sid,run_id))
    def close(self, sid, note=None):
        self.get(sid)
        with self.db() as db: db.execute("UPDATE sessions SET state='closing',note=COALESCE(?,note) WHERE id=? AND state!='stopped'",(note,sid))
    def stopped(self, sid):
        # Only a caller holding a provider stop receipt may call this.
        with self.db() as db: db.execute("UPDATE sessions SET state='stopped',active_run=NULL WHERE id=?",(sid,))
