# Kaggle experiments

Use the normal OpenResearch experiment flow and committed source:

```sh
orx exp run <experiment-id> --backend kaggle --flavor cpu --timeout 10m
orx exp status <experiment-id>
orx logs <run-id>
```

Accelerators: `cpu`, `NvidiaTeslaT4`, `TpuV5E8`, `TpuV6E8`.
The configured account pool selects an available account automatically.
Parallel Runs lease distinct notebooks/accounts; overflow waits. Do not pin a
project to an account or submit notebooks yourself.

Experiments and notebook Sessions have different lifetimes. A failed Run does
not close its notebook: fix code in the experiment worktree, commit it, and
launch another Run. Compatible Runs reuse the notebook and installed packages,
while each Run gets a fresh immutable source directory. Follow normal immutable
source rules; editing the local worktree does not alter an in-flight Run.

`orx exp cancel` cancels the Run, preserving its Session. Close notebooks in
Compute → Kaggle SSH → Sessions, or let their TTL expire. Lifetime starts at
reservation and does not reset on reuse; `--timeout` overrides this lifetime.
Do not repeat an uncertain command under a replacement Run/notebook ID.
If account login is required, use Recover cookie in Compute settings.
