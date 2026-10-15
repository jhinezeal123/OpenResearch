# Kaggle SSH compute

Kaggle is an OpenResearch `ComputeBackend`. It reuses the existing native
Kaggle bootstrap package from AI Scientist and OpenResearch's SSH source upload,
job execution, log tailing, cancellation and monitoring. No AI Scientist web
server, n8n instance or MCP server is required.

## Setup

1. Open **Compute → More compute options → Kaggle SSH**.
2. Under **Local Kaggle runtime**, select the existing AI Scientist Python
   executable and its `kaggle mcp` directory, then **Reload accounts**. On this
   machine those two paths are already filled in.
3. Select an account, accelerator and notebook lifetime. Add competition and
   dataset references if needed. Accept any competition rules on Kaggle first.
4. **Save Kaggle settings**, then **Check account readiness**. This check does
   not allocate compute. An expired account cookie must be renewed through the
   existing Kaggle account setup; this adapter never starts a password login.
5. Select Kaggle as the default destination if desired, or ask your agent to
   launch the experiment explicitly:

   ```text
   orx exp run <experiment-id> --backend kaggle --flavor cpu --timeout 5m
   ```

The configured default destination is machine-wide, following OpenResearch's
existing settings. Kaggle configuration alone does not change that default.

Accelerators come from the donor package: CPU, T4 × 2, TPU v5e-8 and TPU v6e-8.
`--flavor` selects the accelerator; `--timeout` selects the notebook lifetime.
Otherwise the saved Kaggle settings apply. Datasets are mounted by Kaggle under
`/kaggle/input`. The Kaggle notebook image owns its Python/CUDA environment.

## Ownership and recovery

- Credentials remain in AI Scientist's existing local account profiles. Only
  the Kaggle bootstrap child reads them. Agent credentials are not forwarded.
- OpenResearch keeps `kaggle.json`, launch snapshots, ephemeral SSH keys,
  notebook request journals and private diagnostics inside its data directory.
- Each run ID maps to exactly one Kaggle bootstrap request ID. Reconnects use
  that saved ID. A lost submission acknowledgement never creates a replacement
  notebook, and an uncertain experiment command is not replayed.
- One OpenResearch notebook per account is admitted at a time. Provider-side
  active sessions must also be verified idle before a fresh launch.
- A detached `orx supervise` process owns the notebook independently of the
  chat. Stopping chat does not stop an experiment; use its run cancellation.
- Logs drain before notebook shutdown. Shutdown is attempted on completion,
  cancellation and launch failure; a provider stop receipt is checked. If SSH
  cannot deliver STOP, the notebook still has its configured lifetime limit.
  An unconfirmed stop remains a visible monitoring warning, never a fabricated
  successful shutdown. Do not start another notebook until it is reconciled.
- A confirmed stop is saved with its account and session identity. The dashboard's
  background recovery loop reconciles earlier shutdown warnings against that
  receipt or the original notebook's provider status. Provider calls stay off
  run-list loads; temporary network loss does not force a new notebook or a
  repeated experiment.

Notebook storage is ephemeral. Logs remain in OpenResearch; save other outputs
through the project's external storage before the experiment exits. This
adapter does not copy Workbench's artifact publication workflow.

The runtime dependency is the bundled native Kaggle package, not the entire
Workbench workflow engine. After updating that package, recheck account
readiness and a short CPU run before using a larger budget.

## Verification on Windows

The integration was exercised through the dashboard against real Kaggle CPU
notebooks: source staging, command execution under Linux, persisted stdout,
successful completion, and the UI **Stop** action. The cancelled run reached
`Cancelled` and Kaggle confirmed its notebook was `complete`; no QA notebook
was left running. A completed smoke run printed `kaggle_ssh_ok: true` and
`metric: 285`. These are transport checks, not research benchmark scores.

GPU and TPU execution have not been exercised. Python tests cover confirmed
stop receipts, uncertain submission leases, stable request IDs and keeping
credentials out of the account catalog. Rust, UI and bridge checks run locally;
the upstream dev-slot lifecycle tests require macOS/Linux and the SQLite CLI,
so that helper's complete suite cannot run on this Windows host.
