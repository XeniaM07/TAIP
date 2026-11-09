import test from 'node:test';
import assert from 'node:assert/strict';
import { createApiClient, parseRetryAfter } from '../src/api.js';
import * as workflow from '../src/workflow-state.js';

test('client preserves configured API URL and sends a mutation exactly once', async () => {
  const calls = [];
  const request = createApiClient({ baseUrl: 'https://example.test/api/', fetchImpl: async (...args) => {
    calls.push(args);
    return new Response(JSON.stringify({ session_id: 's', models: {} }));
  } });
  const result = await request('/sessions/s/models/provider%2Fmodel/solve?session_view=true', { method: 'POST' });
  assert.equal(result.session_id, 's');
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], 'https://example.test/api/sessions/s/models/provider%2Fmodel/solve?session_view=true');
  assert.equal(calls[0][1].method, 'POST');
});

test('provider HTTP errors retain safe detail, code and Retry-After without retrying', async () => {
  let calls = 0;
  const request = createApiClient({ fetchImpl: async () => {
    calls += 1;
    return new Response(JSON.stringify({ detail: { code: 'rate_limited', message: 'Provider limit reached.' } }), { status: 429, headers: { 'Retry-After': '12' } });
  } });
  await assert.rejects(request('/runtime'), (error) => {
    assert.equal(error.message, 'Provider limit reached.');
    assert.equal(error.code, 'rate_limited');
    assert.equal(error.retryAfterSeconds, 12);
    return true;
  });
  assert.equal(calls, 1);
});

test('validation errors are readable and non-JSON failures use a status message', async () => {
  const validation = createApiClient({ fetchImpl: async () => new Response(JSON.stringify({ detail: [{ msg: 'Prompt is required.' }] }), { status: 422 }) });
  await assert.rejects(validation('/sessions'), /Prompt is required/);
  const invalid = createApiClient({ fetchImpl: async () => new Response('<html>bad gateway</html>', { status: 502 }) });
  await assert.rejects(invalid('/runtime'), /Request failed \(502\)/);
});

test('deadline aborts a hanging request and explains that the session may have changed', async () => {
  let calls = 0;
  const request = createApiClient({ timeoutMs: 5, fetchImpl: (_url, { signal }) => {
    calls += 1;
    return new Promise((_resolve, reject) => signal.addEventListener('abort', () => reject(signal.reason), { once: true }));
  } });
  await assert.rejects(request('/sessions/s/extract', { method: 'POST' }), (error) => {
    assert.equal(error.code, 'request_timeout');
    assert.match(error.message, /Check the session/);
    return true;
  });
  assert.equal(calls, 1);
});

test('caller cancellation remains an AbortError and does not become a timeout', async () => {
  const controller = new AbortController();
  const request = createApiClient({ fetchImpl: (_url, { signal }) => new Promise((_resolve, reject) => signal.addEventListener('abort', () => reject(signal.reason), { once: true })) });
  const pending = request('/sessions/old/compare', { signal: controller.signal });
  controller.abort();
  await assert.rejects(pending, { name: 'AbortError' });
});

test('Retry-After accepts seconds and dates and rejects invalid or negative values', () => {
  assert.equal(parseRetryAfter('4'), 4);
  assert.equal(parseRetryAfter('Mon, 09 Nov 2026 10:00:05 GMT', Date.parse('2026-11-09T10:00:00Z')), 5);
  assert.equal(parseRetryAfter('-2'), null);
  assert.equal(parseRetryAfter('invalid'), null);
});

for (const failure of ['500', '502', 'connection', 'deadline']) {
  test(`${failure} after a mutation reconciles once with GET and never repeats the POST`, async () => {
    const calls = [];
    const refreshed = { session_id: 's', models: { a: { status: 'FAILED', solution: { snapshot_id: 'snap' } } } };
    const request = createApiClient({ timeoutMs: 5, fetchImpl: async (url, options) => {
      calls.push([url, options.method || 'GET']);
      if (options.method === 'POST') {
        if (failure === 'connection') throw new TypeError('connection lost');
        if (failure === 'deadline') return new Promise((_resolve, reject) => options.signal.addEventListener('abort', () => reject(options.signal.reason), { once: true }));
        return new Response('Server failure', { status: Number(failure) });
      }
      return new Response(JSON.stringify(refreshed));
    } });
    const outcome = await workflow.runSessionOperation(() => request('/sessions/s/models/a/solve?session_view=true', { method: 'POST' }), {
      request, sessionId: 's', affectedModels: ['a'],
    });
    assert.equal(outcome.session.models.a.status, 'FAILED');
    assert.ok(outcome.error);
    assert.equal(outcome.reconciliationError, null);
    assert.deepEqual(calls, [['/api/sessions/s/models/a/solve?session_view=true', 'POST'], ['/api/sessions/s', 'GET']]);
  });
}

test('successful mutations consume their returned session without a following GET', async () => {
  const calls = [];
  const request = createApiClient({ fetchImpl: async (url, options) => {
    calls.push([url, options.method || 'GET']);
    return new Response(JSON.stringify({ session_id: 's', models: {} }));
  } });
  const outcome = await workflow.runSessionOperation(() => request('/sessions/s/models/a/solve?session_view=true', { method: 'POST' }), { request, sessionId: 's', affectedModels: ['a'] });
  assert.equal(outcome.session.session_id, 's');
  assert.equal(outcome.error, null);
  assert.deepEqual(calls, [['/api/sessions/s/models/a/solve?session_view=true', 'POST']]);
});

test('a busy reconciliation preserves the original failure and does not poll', async () => {
  const calls = [];
  const request = createApiClient({ fetchImpl: async (url, options) => {
    calls.push([url, options.method || 'GET']);
    return new Response(JSON.stringify({ detail: options.method === 'POST' ? 'Server failure' : 'Session operation is still active.' }), { status: options.method === 'POST' ? 500 : 409 });
  } });
  const outcome = await workflow.runSessionOperation(() => request('/sessions/s/extract', { method: 'POST' }), { request, sessionId: 's', affectedModels: ['a'] });
  assert.equal(outcome.session, null);
  assert.equal(outcome.error.status, 500);
  assert.equal(outcome.reconciliationError.status, 409);
  assert.equal(calls.length, 2);
});

test('local validation and read-only failures do not start reconciliation requests', async () => {
  let reads = 0;
  const request = async () => { reads += 1; };
  const local = await workflow.runSessionOperation(() => { throw new Error('Invalid draft JSON.'); }, { request, sessionId: 's', affectedModels: ['a'] });
  const readOnly = await workflow.runSessionOperation(() => { throw Object.assign(new Error('Server failure'), { status: 500 }); }, { request, sessionId: 's', affectedModels: [] });
  assert.equal(local.session, null);
  assert.equal(readOnly.session, null);
  assert.equal(reads, 0);
});

test('a recovery response from an old session is ignored', async () => {
  let current = true;
  let finish;
  const pending = workflow.runSessionOperation(() => { throw Object.assign(new Error('Server failure'), { status: 500 }); }, {
    sessionId: 'old', affectedModels: ['a'], isCurrent: () => current,
    request: () => new Promise((resolve) => { finish = resolve; }),
  });
  current = false;
  finish({ session_id: 'old', models: {} });
  const outcome = await pending;
  assert.equal(outcome.session, null);
  assert.equal(outcome.stale, true);
});

test('partial draft recovery preserves both intended room edits for the next explicit Save', async () => {
  const original = { rooms: [{ room_id: 'one', capacity: 30 }, { room_id: 'two', capacity: 30 }] };
  const intended = { rooms: [{ room_id: 'one', capacity: 35 }, { room_id: 'two', capacity: 40 }] };
  const partial = { rooms: [{ room_id: 'one', capacity: 35 }, { room_id: 'two', capacity: 30 }] };
  const sessionView = (data, status = 'DRAFT') => ({ session_id: 's', models: { a: { status, draft: { data } } } });
  const key = workflow.editorKey('s', 'a');
  let editors = workflow.reconcileEditors({}, sessionView(original));
  const intendedText = JSON.stringify(intended, null, 2);
  editors[key] = { ...editors[key], text: intendedText };
  const calls = [];
  const savedPayloads = [];
  const request = createApiClient({ fetchImpl: async (_url, options) => {
    calls.push(options.method || 'GET');
    if (options.method === 'PATCH') {
      savedPayloads.push(JSON.parse(options.body).data);
      if (savedPayloads.length === 1) return new Response('Benchmark write failed after the first room update.', { status: 500 });
      return new Response(JSON.stringify(sessionView(JSON.parse(options.body).data)));
    }
    return new Response(JSON.stringify(sessionView(partial, 'FAILED')));
  } });
  const save = () => workflow.runSessionOperation(() => request('/sessions/s/drafts/a?session_view=true', {
    method: 'PATCH', body: JSON.stringify({ data: JSON.parse(editors[key].text) }),
  }), { request, sessionId: 's', affectedModels: ['a'] });

  const failedSave = await save();
  assert.equal(failedSave.error.status, 500);
  editors = workflow.reconcileEditors(editors, failedSave.session, []);
  assert.equal(editors[key].text, intendedText);
  assert.deepEqual(JSON.parse(editors[key].source), partial);
  assert.deepEqual(calls, ['PATCH', 'GET']);

  const explicitRetry = await save();
  assert.equal(explicitRetry.error, null);
  assert.deepEqual(savedPayloads, [intended, intended]);
  assert.deepEqual(calls, ['PATCH', 'GET', 'PATCH']);
  editors = workflow.reconcileEditors(editors, explicitRetry.session, ['a']);
  assert.equal(editors[key].text, editors[key].source);
  assert.deepEqual(JSON.parse(editors[key].text), intended);
});
