"""One detached maintenance loop per private pool, independent of the dashboard."""
import os
from pathlib import Path
import subprocess
import sys
import time

from pool import Pool
from processes import alive, load, lock, owner, save, terminate

SWEEP_SECONDS = 10
HEARTBEAT_SECONDS = 180


def healthy(metadata, heartbeat):
    stamp = metadata.get('started', 0)
    if (heartbeat.get('pid'), heartbeat.get('birth')) == (metadata.get('pid'), metadata.get('birth')):
        stamp = heartbeat.get('at', stamp)
    try:
        return alive(metadata) and time.time() - float(stamp) < HEARTBEAT_SECONDS
    except (TypeError, ValueError):
        return False


def ensure_scheduler(pool):
    with lock(pool.home/'scheduler-start.lock', wait_seconds=2) as acquired:
        if not acquired:
            return
        metadata = load(pool.home/'scheduler-owner.json')
        if healthy(metadata, load(pool.home/'scheduler-heartbeat.json')):
            return
        if alive(metadata):
            terminate(metadata)
            if alive(metadata):
                return
        with lock(pool.home/'scheduler.lock') as unowned:
            if not unowned:
                return
        with (pool.home/'scheduler.log').open('ab') as log:
            child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), str(pool.home)],
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                creationflags=(getattr(subprocess,'DETACHED_PROCESS',0) |
                               getattr(subprocess,'CREATE_NEW_PROCESS_GROUP',0) |
                               getattr(subprocess,'CREATE_NO_WINDOW',0)),
                start_new_session=os.name != 'nt')
        save(pool.home/'scheduler-owner.json', owner(child.pid))


def sweep(pool):
    from bridge import ensure_worker
    rows = pool.reconcile()
    for row in rows:
        try:
            ensure_worker(None, pool, row, {'_scheduler': True})
        except Exception as error:
            # One corrupt/unavailable session must not starve the rest of the pool.
            print('Reconcile '+row['id']+': '+type(error).__name__, flush=True)
    return bool(rows)


def run(home):
    pool = Pool(home)
    # Held for the process lifetime. PID files are diagnostics, never the lock.
    with lock(pool.home/'scheduler.lock', wait_seconds=2) as acquired:
        if not acquired:
            return
        metadata = owner(os.getpid())
        while True:
            save(pool.home/'scheduler-heartbeat.json', {**metadata, 'at': time.time()})
            if not sweep(pool):
                # Exit under the launch lock. A new acquire sees no owner and
                # may start a replacement as this process releases its lock.
                with lock(pool.home/'scheduler-start.lock') as stopping:
                    if stopping and not pool.live():
                        save(pool.home/'scheduler-owner.json', {})
                        return
            time.sleep(SWEEP_SECONDS)


if __name__ == '__main__':
    run(sys.argv[1])
