import test from 'node:test';
import assert from 'node:assert/strict';
import { ReadingInstructionGuard } from '../ws/reading-instructions.js';
import { S2sRealtimeClient } from '../s2s-realtime-client.js';

const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
function fixture() {
  const events = [];
  const wire = { sendMessage: () => events.push('conversation.item.create'), requestResponse: () => events.push('response.create') };
  const client = { options: {}, _session: { updateAgent: async agent => {
    await Promise.resolve(); // SDK performs asynchronous agent/config preparation.
    events.push(`session.update:${agent.instructions}`);
  } }, _transport: wire, _buildAgent() { return { instructions: this.options.instructions }; },
    _sendManualVoice() {}, dispatchEvent() {} };
  client.updateSession = S2sRealtimeClient.prototype.updateSession;
  client.sendUserText = S2sRealtimeClient.prototype.sendUserText;
  const state = { mode: 'READING', documentId: 'A', documentRevision: 1, client };
  const guard = new ReadingInstructionGuard(() => state);
  return { events, client, state, guard };
}
globalThis.CustomEvent ||= class { constructor(type, options) { this.type = type; this.detail = options.detail; } };

test('READING_UPDATE_BEFORE_RESPONSE with 50 ms prompt build', async () => {
  const { guard, client, events } = fixture();
  await guard.deliver(guard.capture(), async () => {
    await new Promise(r => setTimeout(r, 50));
    events.push('prompt-ready');
    return { ok: true, mode: 'READING', text: 'A' };
  }, text => client.updateSession({ instructions: text }));
  client.sendUserText('question');
  assert.deepEqual(events, ['prompt-ready', 'session.update:A', 'conversation.item.create', 'response.create']);
});
test('old document prompt completing after new document is dropped', async () => {
  const { guard, client, state, events } = fixture();
  const old = deferred();
  const a = guard.deliver(guard.capture(), () => old.promise, text => client.updateSession({ instructions: text }));
  state.documentId = 'B'; state.documentRevision += 1;
  await guard.deliver(guard.capture(), async () => ({ ok: true, mode: 'READING', text: 'B' }), text => client.updateSession({ instructions: text }));
  old.resolve({ ok: true, mode: 'READING', text: 'A' });
  await assert.rejects(a, /STALE_INSTRUCTION_UPDATE/);
  assert.deepEqual(events, ['session.update:B']);
  assert.equal(client.options.instructions, 'B');
});
for (const change of ['mode', 'documentRevision', 'client', 'liveSession', 'revision']) {
  test(`in-flight prompt rejected after ${change} change`, async () => {
    const { guard, client, state, events } = fixture(); const pending = deferred();
    const task = guard.deliver(guard.capture(), () => pending.promise, text => client.updateSession({ instructions: text }));
    if (change === 'revision') guard.invalidate();
    else state[change] = change === 'mode' ? 'ROLEPLAY' : change === 'client' ? {} : 2;
    pending.resolve({ ok: true, mode: 'READING', text: 'A' });
    await assert.rejects(task, /STALE_INSTRUCTION_UPDATE/);
    assert.deepEqual(events, []);
  });
}
test('Reading build failure cannot send a response', async () => {
  const { guard, client, events } = fixture();
  await assert.rejects(async () => {
    await guard.deliver(guard.capture(), async () => ({ ok: false }), text => client.updateSession({ instructions: text }));
    client.sendUserText('question');
  }, /PROMPT_BUILD_FAILED/);
  assert.deepEqual(events, []);
});
test('live updates serialize even when first SDK update is slow', async () => {
  const { client, events } = fixture(); const first = deferred();
  client._session.updateAgent = async agent => { if (agent.instructions === 'A') await first.promise; events.push(agent.instructions); };
  const a = client.updateSession({ instructions: 'A' }); const b = client.updateSession({ instructions: 'B' });
  first.resolve(); await Promise.all([a, b]); assert.deepEqual(events, ['A', 'B']);
});
test('ROLEPLAY text submission does not await the Reading barrier', () => {
  const { client, events } = fixture(); client._instructionUpdate = new Promise(() => {});
  assert.equal(client.sendUserText('hello'), true);
  assert.deepEqual(events, ['conversation.item.create', 'response.create']);
});
