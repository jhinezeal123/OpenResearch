// ACP omits command dispatch. Use DSH's public profile and command APIs so
// /compact runs the installed plugin and preserves its native session history.
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import path from 'node:path';

export async function compactSession(ctx, sessionId, model, signal) {
  const agentOptions = {};
  if (model) {
    const route = JSON.parse(model);
    if (!Array.isArray(route) || route.length !== 2 || route.some(v => typeof v !== 'string' || !v)) {
      throw new Error('Invalid DSH provider/model route');
    }
    [agentOptions.provider, agentOptions.model] = route;
  }
  const handle = await ctx.agents.resume({ resumeSessionId: sessionId, agentOptions, signal });
  try {
    const execution = await ctx.commands.execute(handle.agent, '/compact', [], signal);
    if (!execution) throw new Error('Installed DSH profile does not register /compact');
    if (execution.result.kind !== 'success') throw new Error(execution.result.text);
    return { ok: true, sessionId, text: execution.result.text };
  } finally {
    try { await ctx.sessions.flush(handle.agent.session); }
    finally { await handle.dispose(); }
  }
}

async function main() {
  const [entry, overlay, sessionId, model] = process.argv.slice(2);
  const require = createRequire(path.resolve(entry));
  const load = name => import(pathToFileURL(require.resolve(name)).href);
  const { runProfile } = await load('@deepseek-ai/dsh/profile-boot');
  const { createLaunchEnvironmentSnapshot } = await load('@deepseek-ai/dsh-launch-environment');
  const runtime = await runProfile({
    environment: createLaunchEnvironmentSnapshot([{ source: 'process', values: process.env }]),
    profile: 'acp', patchFiles: [overlay], args: [],
  });
  let code = 0;
  try {
    const result = await compactSession(runtime.ctx, sessionId, model, new AbortController().signal);
    process.stdout.write(JSON.stringify(result) + '\n');
  } catch (error) {
    code = 1;
    process.stdout.write(JSON.stringify({ ok: false, error: error.message }) + '\n');
  } finally {
    await runtime.shutdown.shutdown(code);
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  main().catch(error => {
    process.stderr.write(String(error.stack ?? error) + '\n');
    process.exitCode = 1;
  });
}
