import assert from 'node:assert/strict';
import test from 'node:test';
import { apply, inject } from './full-access.mjs';

test('full access uses the native permission preset for fresh and resumed agents', () => {
  const calls = [];
  let created;
  assert.deepEqual(inject, ['permissionPresets']);
  apply({
    on(event, handler) { assert.equal(event, 'agent/created'); created = handler; },
    permissionPresets: { set(session, preset) { calls.push({ session, preset }); } },
  });
  for (const source of ['startup', 'resume', 'compact']) {
    const session = { id: source };
    created({ agent: { session }, source });
    assert.deepEqual(calls.at(-1), { session, preset: 'danger-full-access' });
  }
});

test('failure to apply native full access stops agent publication', () => {
  let created;
  apply({
    on(_event, handler) { created = handler; },
    permissionPresets: { set() { throw new Error('preset unavailable'); } },
  });
  assert.throws(() => created({ agent: { session: {} } }), /preset unavailable/);
});
