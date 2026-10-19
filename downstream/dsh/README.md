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
changing providers in the source profile, and after upgrading OpenResearch so
the generated profile picks up adapter plugins such as `orx-full-access.mjs`.
No runtime is downloaded by detection or by a chat turn.

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
- ACP `usage_update` supplies the current token count and model context capacity
  to the shared context meter. These values come from DSH, not a guessed catalog.
- The shared **Compact** control invokes DSH's installed `/compact` command via
  its public profile/command APIs. ACP itself exposes no command dispatch, so a
  short-lived Node child boots the same ACP profile with only its ACP transport
  disabled, resumes the same native session, executes the native command, flushes
  persistence and disposes. It does not send `/compact` as a model prompt or
  replace the session with a separately summarized transcript. Automatic native
  compaction remains owned by DSH.
- This downstream adapter runs with the native DSH `danger-full-access` preset:
  no file sandbox and no approval prompts. The profile plugin applies the preset
  to new and resumed agents, including native children. Catalog probes still
  never submit model prompts and deny any unexpected permission requests.
- This adapter does not advertise Plan mode, steering, image attachments or
  one-shot title generation. Native DSH text/file tools remain available.
- Protocol errors and incomplete turns remain visible; the adapter does not
  replace a failed resumed session with a fresh session behind the user's back.

The adapter is registered in `src/local/harness/mod.rs`. Settings and the model
picker discover it through that registry, alongside the upstream harnesses.

## Windows terminal troubleshooting

The adapter's full-access profile does not invoke the Windows file sandbox,
so `orx` can reach its shared store, Git metadata and project artifacts outside
the worktree. The following diagnosis applies only to a custom restricted DSH
profile with the full-access plugin removed.

`pwsh` is a native DSH tool, not a client-terminal ACP capability. A
`SetNamedSecurityInfoW ... grantWrite` failure happens before the shell starts:
DSH cannot provision the Windows sandbox for that worktree. Modify permission
alone is insufficient; the sandbox also needs effective `WRITE_DAC` and
`WRITE_OWNER`. Keep sandbox confinement enabled and use the installed DSH
`dsh-sandbox-windows-acl/assets/diagnose-windows-sandbox-acl` skill's bundled
repair command after user approval. Bound `AllowRoot` to the OpenResearch
worktrees directory and keep its recovery files outside the worktrees. Repeat
the original command confined afterwards. Do not silently switch to full access.

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
