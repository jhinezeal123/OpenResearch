# Kaggle SSH (`--backend kaggle`)

Use Kaggle only when it is the configured compute default or the user explicitly
chooses it. The local Kaggle settings already select the account, accelerator,
notebook lifetime and dataset/competition sources. Credentials stay in that
account's local profile. Never ask for tokens in chat or copy them to Kaggle.

```sh
orx exp run <expId> --backend kaggle
orx exp run <expId> --backend kaggle --flavor cpu --timeout 10m
```

`--flavor` overrides the saved accelerator: `cpu`, `NvidiaTeslaT4` (T4 × 2),
`TpuV5E8` or `TpuV6E8`. `--timeout` overrides the notebook lifetime, including
startup, and must be between 60 seconds and 12 hours. Do not exceed the user's
budget. Do not pass `--host`, `--image`, or container flags.

Commit code first. The backend transfers the immutable source snapshot and
executes the experiment's run command under the notebook's Python/CUDA
environment. Mounted datasets are under `/kaggle/input`. Prepare dependencies
in committed scripts. Do not depend on local Windows paths on the remote VM.

Use `orx logs <runId>` and `orx exp wait <expId>` as with other compute backends.
Print metrics with their names to stdout so they remain in the local run log.
The notebook is ephemeral and is stopped after log collection; do not rely on
its SSH directory remaining available after completion. Save desired output
through the project's existing external storage before the command exits.

Only one OpenResearch notebook per selected account is admitted at a time.
Readiness must also verify that no other Kaggle notebook is using that account.
If the account needs login, let the user renew its existing local Kaggle profile.
Do not automatically choose a different account or backend.

Cancel with `orx exp cancel <expId>`. The supervisor cancels the experiment and
stops its own notebook. Chat cancellation is independent. During a network
outage, monitoring retries using the same notebook ID; do not launch replacement
notebooks or repeat an uncertain experiment. Unconfirmed teardown is shown as
a warning and the configured notebook lifetime remains the final bound.
