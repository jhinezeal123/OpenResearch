# Kaggle SSH compute

Kaggle implements OpenResearch's `ComputeBackend`. The agent stays in its local
chat/worktree and starts asynchronous experiment Runs using `orx exp run`.
The notebook is a separate, reusable Session. DSH and Kaggle are independent.

## Install on Windows

Install [uv](https://docs.astral.sh/uv/), then run from this checkout:

```powershell
./downstream/kaggle/setup.ps1 -DataDir D:/path/to/openresearch-data -ImportFrom 'D:/path/to/old/kaggle mcp'
```

Use the same directory as `ORX_DATA_DIR`. `-Uv` can specify the installed uv
executable. The installer creates a private CPython 3.12.13 interpreter,
virtual environment and pinned dependencies under `<data>/kaggle`. Installer
cache and temporary files are stored there too. `-ImportFrom` is optional and
copies account metadata, tokens, cookies and saved login credentials **once**;
it does not save a dependency on that directory. No credentials belong in Git.
Re-run import only when you intend to replace those private account files.

The executable embeds all Python runtime source, including `auto_login`, and
extracts a versioned package into its own data directory. It does not import
Python, modules or profiles from AI Scientist at runtime. Windows' native
OpenSSH and the selected installed browser remain OS dependencies. Tailcat is
downloaded from its official release and checked against pinned hashes.

## Configure

1. Open **Compute → More compute options → Kaggle SSH**.
2. Enable accounts in the account pool. Selection applies across projects;
   projects are not pinned to a single account.
3. Set default accelerator, notebook lifetime, competition and dataset mounts.
   Accept competition rules on Kaggle before using their datasets.
4. Save, then **Check accounts**. **Recover cookie** uses the packaged
   auto-login module and private profiles. If Kaggle requires manual
   verification, complete it in the login window and check again.
5. Choose Kaggle as the destination or launch explicitly:

```text
orx exp run <experiment-id> --backend kaggle --flavor cpu --timeout 10m
```

`--flavor` overrides the accelerator. `--timeout` bounds notebook lifetime;
otherwise defaults apply. Lifetime starts at reservation and is never reset
when a notebook is reused. A later Run is bounded by the remaining lifetime.

## Runs and Sessions

- An Experiment can have multiple immutable-source Runs. Every Run has its own
  working directory, process group, log and exit receipt inside its Session.
- A Session serves one Run at a time. Compatible idle Sessions in the same
  project are reused, including after a failed Run. Packages, caches and
  `/kaggle/working` survive. Source snapshots remain separate per Run.
- An enabled account holds up to two GPU notebooks, or one CPU or TPU
  notebook. Kaggle sessions this pool does not own, including notebooks it
  cannot identify, occupy those slots.
  Additional Runs wait in a durable FIFO queue for contended accounts. Runs
  using disjoint account sets can proceed independently.
- Mounts, accelerator, project and requested lifetime define compatibility.
  An idle notebook is retained for up to five minutes. When an incompatible
  waiting Run has no ready account, one idle notebook is closed to make room.
- Submission and Run identities are durable. Reconnecting uses the same
  notebook; a lost acknowledgement never causes a replacement submission.
  Remote launch intents prevent replaying an uncertain experiment command.
- A detached plugin worker owns the notebook independently of chat and app.
  It uses one SSH pipe for upload, launch, logs and monitoring, closes that
  pipe while idle, and reconnects on reuse. After a transport failure it
  reconnects to that same notebook and inspects Run IDs.
- **Stop Run** cancels only that Run's process group. The notebook is retained.
  **Close session** or TTL shuts down the notebook. An account is released only
  after a provider stop receipt. Unknown stop outcomes remain reserved.
- Stop also reconciles the remote Run after a supervisor restart. If monitoring
  fails for 15 minutes, the local Run fails but its session stays reserved until
  the owner confirms completion or the notebook reaches its deadline.
- Commands stop on failed steps and pipelines. Each Run receives `ORX_RUN_ID`
  alongside the synced environment so metric trackers can identify it.
- A detached pool scheduler restarts missing owners using saved notebook
  identities, even while the dashboard is closed. Remote bootstrap TTL bounds
  compute if the local machine goes offline.

Logs are persisted in OpenResearch. Notebook storage remains ephemeral;
export required artifacts before closing the Session. No Workbench workflow,
MCP server, n8n or external Python checkout is required.

## Scheduling and zombie cleanup

The pool scheduler sweeps every 10 seconds and exits when no live reservations
remain. Each worker also enforces the same time bounds if the scheduler is
temporarily unavailable. SQLite transactions serialize admission, reuse and
reclamation; OS locks are held for the entire scheduler/worker lifetime.
Repeated worker startup failures back off up to two minutes instead of
spawning another interpreter on every poll.

| Condition | Action |
| --- | --- |
| Idle for 5 minutes | Close the notebook and release its local processes. |
| Incompatible queued Run cannot obtain a ready account | Close one idle notebook; wait for its provider stop receipt before admitting a replacement. |
| Provisioning for 10 minutes, or notebook TTL reached | Begin closing without submitting another notebook. |
| Remote confirms `NOT_STARTED` and no acquisition/upload activity for 2 minutes | Close the abandoned lease. Large ongoing uploads refresh this grace, never the notebook TTL. |
| Worker heartbeat absent for 180 seconds | Verify PID **and creation time**, terminate that owner, and recover the same session. A running remote experiment is inspected, never replayed. |
| Provider reports the notebook already stopped | Persist the receipt, free the account, and exit the worker. |
| Stop outcome or remote state is unknown | Keep the account reserved; the shared scheduler retries shutdown with backoff up to 5 minutes. A lost connection alone never releases a lease. |
| Push response was lost, Kaggle has no such notebook, and deadline plus lifetime has passed | Free the account: the notebook was never created. |
| Stop still unconfirmed 12 hours past the deadline | Free the account: Kaggle has ended any session by then. |

`processes.py` owns local tree cleanup. On Windows, SSH starts suspended and is
assigned to a kill-on-close Job before being resumed, so its Tailcat
ProxyCommand cannot escape reconnect cleanup. A worker lifetime Job also reaps
its children if the worker crashes. Unix transports use dedicated process
groups; automatic stale-owner termination uses Linux pidfds where available.
Other Unix platforms retain an unverifiable owner until its deadline instead
of risking a signal to a reused PID. This follows the [Windows Job ownership
model](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects).

Token proxies run only during bootstrap. Idle notebooks hold no persistent
SSH/Tailcat pipe. One active notebook still needs a worker, SSH and Tailcat,
plus one scheduler shared by the pool; the Run supervisor and short bridge
calls are separate processes. These are background processes, and internal
Windows commands run without opening terminal windows. Windows workers and the
scheduler launch the underlying interpreter directly while preserving their
virtualenv, so launcher PIDs cannot cause false stale-heartbeat recovery or
extra console windows. Closing sessions hand off to the shared scheduler and
release their workers. The scheduler attempts at most one closing session per
sweep; six uncertain stop receipts therefore need one background scheduler,
rather than six resident workers. After the last notebook's stop is confirmed,
the pool scheduler exits.

Provider status requests identify the client as `OpenResearch-Kaggle` rather
than inheriting a requests-library User-Agent. This avoids misleading 404
responses observed with the bundled requests version, which otherwise prevent
confirmed completed notebooks from releasing their account reservations.

The upgrade migrates `pool.sqlite3` in place. Workers started by an older binary
are authenticated through their existing private endpoint and allowed to
retire through their original close/TTL path, without creating duplicate
owners. New workers use lifetime locks, heartbeats and process Jobs.

Lifecycle decisions appear in each session's `note` in the existing session
list. Diagnostics live under `<data>/kaggle/scheduler.log` and
`workers/<session-id>/worker.log` / `ssh.log`; owner and heartbeat files identify
the exact local process instance. Do not manually free a database reservation
or mass-kill every Python/terminal process to recover an uncertain stop.

## Ownership and maintenance

`compute.rs`, `runtime.rs`, `supervisor.rs`, `settings.rs`, `ui/`, `python/`,
setup and tests belong here. `downstream/bridge.rs` exposes the upstream
contracts. `VENDORED.json` records the native package origin and original
hashes; local changes redirect data, profile and cookie recovery paths.

Merge upstream into this fork and build it. Installing an official upstream
binary replaces the whole application, so it cannot retain custom plugins.
The local D: launcher disables automatic upstream binary replacement.

## Verification

```powershell
python -m unittest discover -s downstream/kaggle/tests -p 'test_*.py'
```

Tests cover concurrent pool admission, overflow, retry identity, reuse,
project/mount isolation, unconfirmed shutdown ownership, profile import and
independence from the donor checkout. Lifecycle regression tests also exercise
FIFO fairness, idle/pressure reclamation, schema migration, orphaned launch
leases, real Windows process trees, worker crashes and transport recovery.
The new cleanup policy is tested locally with a fake provider; live Kaggle
acceptance for this policy has not been run. Browser acceptance results below
describe the earlier lifecycle and are recorded
after real CPU verification; GPU/TPU require separate hardware validation.

Verified on Windows through the browser against real Kaggle CPU notebooks
(2026-10-15): two concurrent accounts, overflow queue, failed Run → committed
fix → Done on the same notebook and SSH pipe, reconnect after SSH loss, UI Stop
without notebook teardown, and provider-confirmed Close session. A fresh CPU
Run from the final embedded binary also reached Done; all QA sessions stopped.
The printed `metric: 285` is a transport fixture, not a research benchmark.

Local checks for that earlier acceptance passed: 21 Python tests, 1,035 Rust unit tests, 9 integration tests,
299 UI tests, UI production build, Rust formatting and Clippy. GPU/TPU and a
fresh interactive/password cookie renewal were not exercised; auto-login
packaging, script paths and private profile import are covered by tests.
