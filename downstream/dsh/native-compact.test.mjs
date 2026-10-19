import assert from 'node:assert/strict';
import test from 'node:test';
import { compactSession } from './native-compact.mjs';

function fixture(result = { kind: 'success', text: 'Compacted 12 history items (~4000 tokens).' }) {
  const calls = [];
  const agent = { session: { id: 'native-id' } };
  const ctx = {
    agents: { async resume(options) {
      calls.push(['resume', options]);
      return { agent, async dispose() { calls.push(['dispose']); } };
    } },
    commands: { async execute(...args) { calls.push(['execute', ...args]); return result && { result }; } },
    sessions: { async flush(session) { calls.push(['flush', session]); } },
  };
  return { ctx, calls, agent };
}

test('dispatches the installed native /compact plugin against the same session', async () => {
  const { ctx, calls, agent } = fixture();
  const signal = new AbortController().signal;
  const result = await compactSession(ctx, 'native-id', '["provider","model"]', signal);
  assert.equal(result.sessionId, 'native-id');
  assert.equal(result.ok, true);
  assert.deepEqual(calls[0], ['resume', { resumeSessionId: 'native-id', agentOptions: { provider: 'provider', model: 'model' }, signal }]);
  assert.deepEqual(calls[1], ['execute', agent, '/compact', [], signal]);
  assert.deepEqual(calls.slice(2), [['flush', agent.session], ['dispose']]);
});

test('native plugin errors stay errors and still flush and dispose', async () => {
  const { ctx, calls } = fixture({ kind: 'error', text: 'Compaction unavailable: busy' });
  await assert.rejects(compactSession(ctx, 'native-id', '', new AbortController().signal), /busy/);
  assert.deepEqual(calls.slice(-2).map(c => c[0]), ['flush', 'dispose']);
});

test('missing plugin fails explicitly instead of starting a model turn', async () => {
  const { ctx, calls } = fixture(null);
  await assert.rejects(compactSession(ctx, 'native-id', '', new AbortController().signal), /does not register/);
  assert.deepEqual(calls.map(c => c[0]), ['resume', 'execute', 'flush', 'dispose']);
});

test('flush failures do not leak the owned native agent', async () => {
  const { ctx, calls } = fixture();
  ctx.sessions.flush = async () => { throw new Error('disk failure'); };
  await assert.rejects(compactSession(ctx, 'native-id', '', new AbortController().signal), /disk failure/);
  assert.equal(calls.at(-1)[0], 'dispose');
});

test('invalid routes are rejected before the session is opened', async () => {
  const { ctx, calls } = fixture();
  await assert.rejects(compactSession(ctx, 'native-id', '["model"]', new AbortController().signal), /Invalid DSH/);
  assert.equal(calls.length, 0);
});
