"""One detached maintenance loop per private pool, independent of the dashboard."""
import os
from pathlib import Path
import subprocess
import sys
import time

from pool import Pool
from processes import alive, load, lock, owner, save, spawn_python, terminate

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


def stale(home):
    """Whether a newer package was installed since this scheduler started."""
    try:
        current = (Path(home)/'package'/'current').read_text(encoding='utf-8').strip()
    except OSError:
        return False
    return bool(current) and current != Path(__file__).resolve().parent.name


def spawn(home, script):
    with (Path(home)/'scheduler.log').open('ab') as log:
        child = spawn_python(script, home, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
    save(Path(home)/'scheduler-owner.json', owner(child.pid))


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
        spawn(pool.home, Path(__file__).resolve())


def sweep(pool):
    from bridge import ensure_worker
    rows = pool.reconcile()
    for row in rows:
        if row['state'] == 'closing':
            continue
        try:
            ensure_worker(None, pool, row, {'_scheduler': True})
        except Exception as error:
            # One corrupt/unavailable session must not starve the rest of the pool.
            print('Reconcile '+row['id']+': '+type(error).__name__, flush=True)
    # At most one blocking shutdown per sweep. Round robin by persisted retry
    # time keeps an offline/unauthorized account from starving other sessions.
    closing = [(load(pool.worker_dir(r['id'])/'cleanup.json').get('retry_at', 0), r)
               for r in rows if r['state'] == 'closing']
    if closing:
        retry_at, row = min(closing, key=lambda item: item[0])
        if retry_at <= time.time():
            try:
                cleanup(pool, row)
            except Exception as error:
                print('Cleanup '+row['id']+': '+type(error).__name__, flush=True)
                save(pool.worker_dir(row['id'])/'cleanup.json', {'retry_at':time.time()+300})
    return bool(rows)


def cleanup(pool, row):
    from bridge import configure, contact
    from worker import settle_unsubmitted, shutdown
    folder = pool.worker_dir(row['id'])
    def wait_for_owner():
        save(folder/'cleanup.json', {**load(folder/'cleanup.json'), 'retry_at':time.time()+SWEEP_SECONDS})
    # An existing worker finishes any bootstrap/transport cleanup and hands
    # off by releasing its lifetime lock. Never race its provider stop call.
    with lock(folder/'worker.lock') as unowned:
        if not unowned:
            wait_for_owner()
            return
        legacy = load(folder/'worker.json')
        metadata = load(folder/'owner.json')
        if metadata.get('legacy') and alive(metadata):
            wait_for_owner()
            return
        if legacy.get('pid') and alive(owner(legacy['pid'])):
            try:
                contact(folder, {'action':'health'}, timeout=3)
                wait_for_owner()
                return
            except (OSError, ValueError, RuntimeError):
                pass
        previous = load(folder/'cleanup.json')
        requested = load(folder/'stop-requested.json').get('requested') is True
        try:
            session = configure({'home':str(pool.home), 'stateRoot':str(pool.home/'sessions')})
            # Sessions closed before Kaggle proved them absent still free their Run.
            settle_unsubmitted(session, pool, row['id'], pool.home/'sessions', row)
            stopped, requested = shutdown(session, row['id'], row['account'], pool.home/'sessions', requested, row=row)
            if requested:
                save(folder/'stop-requested.json', {'requested': True})
            if stopped:
                pool.stopped(row['id'])
                return
        except Exception as error:
            print('Cleanup '+row['id']+': '+type(error).__name__, flush=True)
        failures = min(7, previous.get('failures', 0)+1)
        save(folder/'cleanup.json', {'failures':failures, 'retry_at':time.time()+min(300, 5*2**(failures-1))})


def run(home):
    pool = Pool(home)
    # Held for the process lifetime. PID files are diagnostics, never the lock.
    with lock(pool.home/'scheduler.lock', wait_seconds=2) as acquired:
        if not acquired:
            return
        metadata = owner(os.getpid())
        while True:
            if stale(pool.home):
                break
            save(pool.home/'scheduler-heartbeat.json', {**metadata, 'at': time.time()})
            if not sweep(pool):
                # Exit under the launch lock. A new acquire sees no owner and
                # may start a replacement as this process releases its lock.
                with lock(pool.home/'scheduler-start.lock') as stopping:
                    if stopping and not pool.live():
                        save(pool.home/'scheduler-owner.json', {})
                        return
            time.sleep(SWEEP_SECONDS)
    # Hand the pool to the installed package's scheduler; it waits for this lock.
    with lock(pool.home/'scheduler-start.lock', wait_seconds=10) as acquired:
        if acquired:
            current = (pool.home/'package'/'current').read_text(encoding='utf-8').strip()
            spawn(pool.home, pool.home/'package'/current/'scheduler.py')


if __name__ == '__main__':
    run(sys.argv[1])
