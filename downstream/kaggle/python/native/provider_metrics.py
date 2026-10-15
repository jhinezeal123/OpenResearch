"""Payload measurements across bundled CLI processes; credentials are never stored."""
from pathlib import Path
import os
import sqlite3
import time
import uuid

PATH = Path(os.environ['ORX_KAGGLE_HOME']) / '.runtime' / 'provider-metrics.sqlite3'
FIELDS = ('requests', 'request_bytes', 'response_bytes', 'latency_ms', 'errors')


def record(account, request_bytes, response_bytes, started, failed=False):
    try:
        PATH.parent.mkdir(exist_ok=True)
        with sqlite3.connect(PATH, timeout=.2) as db:
            db.execute('CREATE TABLE IF NOT EXISTS metadata(epoch TEXT NOT NULL)')
            if not db.execute('SELECT epoch FROM metadata').fetchone():
                db.execute('INSERT INTO metadata VALUES(?)', (uuid.uuid4().hex,))
            db.execute('CREATE TABLE IF NOT EXISTS totals(account TEXT PRIMARY KEY, requests INTEGER, '
                       'request_bytes INTEGER,response_bytes INTEGER,latency_ms REAL,errors INTEGER)')
            db.execute('INSERT INTO totals VALUES(?,1,?,?,?,?) ON CONFLICT(account) DO UPDATE SET '
                       'requests=requests+1,request_bytes=totals.request_bytes+excluded.request_bytes,'
                       'response_bytes=totals.response_bytes+excluded.response_bytes,'
                       'latency_ms=totals.latency_ms+excluded.latency_ms,errors=totals.errors+excluded.errors',
                       (account, request_bytes, response_bytes, (time.perf_counter()-started)*1000, int(failed)))
    except (OSError, sqlite3.Error):
        pass  # Diagnostics must never alter submission/control outcomes.
