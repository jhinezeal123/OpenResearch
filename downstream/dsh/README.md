# DSH harness

The `dsh` adapter implements OpenResearch's existing `Harness` contract over
ACP v1 stdio. It does not introduce a second research coordinator or model API
client. The installed DSH runtime owns model routing, credentials, tools and
reasoning semantics; OpenResearch owns project worktrees, chat history and
permission cards.

## Configure

Install DSH with a version supporting ACP `configOptions`, model/effort
selection and `session/resume` (verified with `0.2.1-alpha.2`). Configure your
providers in DSH Web first. Then, from this checkout:

```powershell
node downstream/dsh/setup.mjs D:/path/to/node_modules/@deepseek-ai/dsh/lib/bin.js D:/path/to/openresearch-data
```

An optional third argument selects the source DSH home (default `~/.dsh`).
Use the same data directory as `ORX_DATA_DIR`. Restart OpenResearch or refresh
DSH in Settings → Harnesses.

The script imports only provider/default-model entries and their credential
references into `<data>/agents/dsh`, and writes `<data>/dsh.json`. No secrets
belong in Git. Existing DSH profiles are not changed. Re-run the command after
changing providers in the source profile. No runtime is downloaded by detection
or by a chat turn.

## Model and reasoning selection

Discovery initializes ACP and creates a temporary session without inference.
The adapter reads the advertised model catalog and selects each model to read
its exact reasoning options. The configured default model is listed first.
Models with no advertised effort have no effort picker. Agent default leaves
the provider's default, except the imported default model also retains its
explicit DSH default effort. Opaque provider/model IDs are preserved.

A catalog response proves configuration availability, not successful inference
or subscription entitlement. DSH may advertise built-in models whose provider
credentials are not configured. A real chat verifies access to that route.

## Execution

- DSH runs in the private OpenResearch session worktree and receives the
  standard playbook and `.agents/skills`.
- The native ACP session ID is persisted and resumed on subsequent turns.
- Model and effort are selected before the prompt is sent. Unsupported values
  fail before inference rather than silently falling back.
- ACP text, reasoning and tool events become standard OpenResearch wire parts.
- DSH permission requests use OpenResearch's existing approval cards. Catalog
  probes deny permissions. The default is Ask for approval.
- This adapter does not advertise Plan mode, steering, image attachments or
  one-shot title generation. Native DSH text/file tools remain available.
- Protocol errors and incomplete turns remain visible; the adapter does not
  replace a failed resumed session with a fresh session behind the user's back.

The adapter is registered in `src/local/harness/mod.rs`. Settings and the model
picker discover it through that registry, alongside the upstream harnesses.

## Local verification (2026-10-15)

- Built the Windows application and embedded dashboard from this checkout.
- Browser: Settings reports DSH 0.2.1-alpha.2 and five ACP models. The three
  imported custom routes are 4.1, 0902 and gml5.3; two built-in DeepSeek routes
  are also advertised by DSH. Only the 4.1 route was inference-tested.
- Browser: selected 4.1 / High, observed a native file-read tool and the exact
  requested reply. A second turn recalled the previous turn's verification
  code through the persisted ACP session.
- UI build, typecheck, style/localization checks and all 299 UI tests passed.
- Rust build, formatting and Clippy with warnings denied passed. The full Rust
  run passed 1,032 tests with three ignored and one LaTeX test failing because
  `sh` was absent from PATH. That unchanged test passed when re-run with the
  installed Git Bash directory on PATH. Both DSH tests passed independently.
- The upstream dev-slot tests require POSIX lifecycle locks and sqlite3 CLI;
  those two cases cannot pass in this Windows environment. This instance uses
  an isolated D: runtime and data directory instead.
