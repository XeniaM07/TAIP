import test from 'node:test';
import assert from 'node:assert/strict';
import { runtimePrompt, modelCatalog, reconcileEditors, reconcileFailureTimes, editorKey, comparisonRows, solveRetryState, runExclusive, createLatestRequest } from '../src/workflow-state.js';

const session = (sessionId, models) => ({ session_id: sessionId, models });
const draft = (name) => ({ draft: { data: { name } }, status: 'DRAFT' });

test('live mode starts empty and runtime catalog supplies IDs and labels', () => {
  assert.equal(runtimePrompt({ mode: 'live', demo_prompt: 'example' }), '');
  assert.equal(runtimePrompt({ mode: 'mock', demo_prompt: 'example' }), 'example');
  assert.deepEqual(modelCatalog({ models: [{ model_id: 'live/model', label: 'Live model' }] }), [{ model_id: 'live/model', label: 'Live model' }]);
});

test('editor text survives model changes and is isolated from a new session', () => {
  const first = session('first', { a: draft('A'), b: draft('B') });
  let editors = reconcileEditors({}, first);
  const key = editorKey('first', 'a');
  editors = { ...editors, [key]: { ...editors[key], text: 'unsaved A edits' } };
  editors = reconcileEditors(editors, first);
  assert.equal(editors[key].text, 'unsaved A edits');
  const second = session('second', { a: draft('Other A') });
  editors = reconcileEditors(editors, second);
  assert.match(editors[editorKey('second', 'a')].text, /Other A/);
  assert.equal(editors[key].text, 'unsaved A edits');
});

test('partial extraction safely handles missing drafts and resets only affected editors', () => {
  const original = session('s', { a: draft('A'), b: draft('B') });
  let editors = reconcileEditors({}, original);
  const keyB = editorKey('s', 'b');
  editors[keyB] = { ...editors[keyB], text: 'keep B edits' };
  const updated = session('s', { a: { status: 'FAILED', draft: null }, b: draft('B') });
  editors = reconcileEditors(editors, updated, ['a']);
  assert.equal(editors[editorKey('s', 'a')].text, '');
  assert.equal(editors[keyB].text, 'keep B edits');
});

test('clean editor refresh and an explicit successful reset accept changed server drafts', () => {
  const key = editorKey('s', 'a');
  let editors = reconcileEditors({}, session('s', { a: draft('original') }));
  editors = reconcileEditors(editors, session('s', { a: draft('server update') }));
  assert.match(editors[key].text, /server update/);
  assert.equal(editors[key].text, editors[key].source);
  editors[key] = { ...editors[key], text: 'unsaved text' };
  editors = reconcileEditors(editors, session('s', { a: draft('successful result') }), ['a']);
  assert.match(editors[key].text, /successful result/);
  assert.equal(editors[key].text, editors[key].source);
});

test('comparison includes failed catalog models even when the report omits them', () => {
  const models = [{ model_id: 'a', label: 'Alpha' }, { model_id: 'b', label: 'Beta' }];
  const result = comparisonRows(models, session('s', { a: draft('A'), b: { status: 'FAILED', failure: { message: 'Limit reached.' } } }), { report: { rows: [{ model_id: 'a', total_duration_ms: 12 }] } });
  assert.equal(result.length, 2);
  assert.equal(result[1].label, 'Beta');
  assert.equal(result[1].status, 'FAILED');
  assert.equal(result[1].failure.message, 'Limit reached.');
  assert.equal(result[1].total_duration_ms, undefined);
});

test('failed solve retry requires a matching snapshot, retryable failure and elapsed cooldown', () => {
  const workspace = { status: 'FAILED', snapshot: { snapshot_id: 'snap' }, failure: { retryable: true, retry_after_seconds: 10 }, attempts: [{ operation: 'solve', snapshot_id: 'snap' }] };
  assert.deepEqual(solveRetryState(workspace, { receivedAt: 1000, now: 6000 }), { eligible: false, remainingSeconds: 5, reusesResult: false });
  assert.deepEqual(solveRetryState(workspace, { receivedAt: 1000, now: 11000 }), { eligible: true, remainingSeconds: 0, reusesResult: false });
  assert.equal(solveRetryState({ ...workspace, snapshot: null }, { now: 12000 }).eligible, false);
  assert.equal(solveRetryState({ ...workspace, failure: { retryable: false } }, { now: 12000 }).eligible, false);
  assert.equal(solveRetryState({ ...workspace, solution: { solution_id: 'existing' } }, { now: 12000 }).eligible, false);
  assert.equal(solveRetryState({ ...workspace, attempts: [{ operation: 'solve', snapshot_id: 'older' }] }, { now: 12000 }).eligible, false);
});

test('a cached solve resumes its local commit immediately without provider retry permission', () => {
  const workspace = {
    status: 'FAILED', snapshot: { snapshot_id: 'snap' }, solution: { snapshot_id: 'snap', solution_id: 'cached' },
    failure: { code: 'unexpected_error', retryable: false, retry_after_seconds: 60 },
    attempts: [{ operation: 'solve', snapshot_id: 'snap', failure: null }],
  };
  assert.deepEqual(solveRetryState(workspace, { receivedAt: 1000, now: 2000, cooldownSeconds: 120, cooldownReceivedAt: 1000 }), {
    eligible: true, remainingSeconds: 0, reusesResult: true,
  });
  assert.equal(solveRetryState({ ...workspace, solution: { snapshot_id: 'older' } }).eligible, false);
  assert.equal(solveRetryState({ ...workspace, snapshot: null }).eligible, false);
  assert.equal(solveRetryState({ ...workspace, status: 'COMPLETED' }).reusesResult, false);
});

test('a solve rejected before admission can be retried even without a new AI attempt', () => {
  const workspace = { status: 'FAILED', snapshot: { snapshot_id: 'snap' }, failure: { retryable: true }, attempts: [{ operation: 'extract', attempt_id: 'extract' }] };
  assert.equal(solveRetryState(workspace).eligible, true);
});

test('unrelated updates do not restart a failed model cooldown', () => {
  const failed = { status: 'FAILED', failure: { code: 'rate_limit', retry_after_seconds: 10 }, attempts: [{ attempt_id: 'one' }] };
  let clocks = reconcileFailureTimes({}, session('s', { a: failed }), 1000);
  clocks = reconcileFailureTimes(clocks, session('s', { a: failed, b: draft('new') }), 6000);
  assert.equal(clocks[editorKey('s', 'a')].receivedAt, 1000);
  clocks = reconcileFailureTimes(clocks, session('s', { a: { ...failed, attempts: [{ attempt_id: 'two' }] } }), 9000);
  assert.equal(clocks[editorKey('s', 'a')].receivedAt, 9000);
});

test('same-tick double clicks cannot start two mutations and the gate unlocks after failure', async () => {
  const lock = { current: false };
  let starts = 0;
  let finish;
  const first = runExclusive(lock, () => { starts += 1; return new Promise((resolve) => { finish = resolve; }); });
  await runExclusive(lock, () => { starts += 1; });
  assert.equal(starts, 1);
  finish();
  await first;
  await assert.rejects(runExclusive(lock, () => { throw new Error('failure'); }), /failure/);
  assert.equal(lock.current, false);
});

test('new comparisons abort earlier loads and stale responses cannot update the view', () => {
  const requests = createLatestRequest();
  const old = requests.begin();
  const latest = requests.begin();
  assert.equal(old.signal.aborted, true);
  assert.equal(old.isCurrent(), false);
  assert.equal(latest.isCurrent(), true);
  requests.cancel();
  assert.equal(latest.isCurrent(), false);
});
