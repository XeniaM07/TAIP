export function runtimePrompt(runtime) {
  return runtime?.mode === 'mock' && typeof runtime.demo_prompt === 'string' ? runtime.demo_prompt : '';
}

export function modelCatalog(runtime, session) {
  const models = new Map((runtime?.models || []).map((model) => [model.model_id, { ...model, label: model.label || model.model_id }]));
  for (const modelId of Object.keys(session?.models || {})) {
    if (!models.has(modelId)) models.set(modelId, { model_id: modelId, label: modelId });
  }
  return [...models.values()];
}

export const editorKey = (sessionId, modelId) => JSON.stringify([sessionId, modelId]);

export function reconcileEditors(editors, session, resetModelIds = []) {
  const next = { ...editors };
  for (const [modelId, workspace] of Object.entries(session.models || {})) {
    const key = editorKey(session.session_id, modelId);
    const source = workspace?.draft?.data ? JSON.stringify(workspace.draft.data, null, 2) : '';
    const previous = editors[key];
    const preserveEdits = previous && previous.text !== previous.source && !resetModelIds.includes(modelId);
    next[key] = { source, text: preserveEdits ? previous.text : source };
  }
  return next;
}

export function reconcileFailureTimes(previous, session, receivedAt) {
  const next = { ...previous };
  for (const [modelId, workspace] of Object.entries(session.models || {})) {
    const key = editorKey(session.session_id, modelId);
    if (!workspace.failure) { delete next[key]; continue; }
    const marker = JSON.stringify([workspace.attempts?.at(-1)?.attempt_id, workspace.failure]);
    if (next[key]?.marker !== marker) next[key] = { marker, receivedAt };
  }
  return next;
}

export function comparisonRows(catalog, session, comparison) {
  const report = new Map((comparison?.report?.rows || []).map((row) => [row.model_id, row]));
  const summary = new Map((comparison?.models || []).map((row) => [row.model_id, row]));
  return catalog.map((model) => ({
    ...summary.get(model.model_id),
    ...report.get(model.model_id),
    model_id: model.model_id,
    label: model.label,
    status: session?.models?.[model.model_id]?.status || 'PENDING',
    failure: session?.models?.[model.model_id]?.failure,
  }));
}

export function solveRetryState(workspace, { receivedAt = 0, now = Date.now(), cooldownSeconds = 0, cooldownReceivedAt = 0 } = {}) {
  const reusesResult = workspace?.status === 'FAILED' && Boolean(workspace.snapshot?.snapshot_id)
    && workspace.solution?.snapshot_id === workspace.snapshot.snapshot_id;
  if (reusesResult) return { eligible: true, remainingSeconds: 0, reusesResult: true };
  const attempt = workspace?.attempts?.at(-1);
  const retrySeconds = Number.isFinite(workspace?.failure?.retry_after_seconds) ? Math.max(0, workspace.failure.retry_after_seconds) : 0;
  const quotaSeconds = Number.isFinite(cooldownSeconds) ? Math.max(0, cooldownSeconds) : 0;
  const remainingSeconds = Math.max(0, Math.ceil((Math.max(receivedAt + retrySeconds * 1000, cooldownReceivedAt + quotaSeconds * 1000) - now) / 1000));
  const matchingAttempt = attempt?.operation !== 'solve' || attempt.snapshot_id === workspace?.snapshot?.snapshot_id;
  const eligible = workspace?.status === 'FAILED' && Boolean(workspace.snapshot?.snapshot_id) && !workspace.solution
    && workspace.failure?.retryable === true && matchingAttempt && remainingSeconds === 0;
  return { eligible, remainingSeconds, reusesResult: false };
}

export async function runSessionOperation(callback, { request, sessionId = null, affectedModels = [], isCurrent = () => true } = {}) {
  const outcome = { session: null, error: null, reconciliationError: null, stale: false };
  function validateSession(result) {
    if (!result?.session_id || !result.models || (sessionId && result.session_id !== sessionId)) {
      throw Object.assign(new Error('The server returned an incomplete session. Refresh the session before continuing.'), { code: 'invalid_response' });
    }
    return result;
  }
  try {
    const result = await callback();
    if (!isCurrent()) return { ...outcome, stale: true };
    return { ...outcome, session: validateSession(result) };
  } catch (error) {
    outcome.error = error;
    if (!isCurrent()) return { ...outcome, stale: true };
    const uncertain = (error.status >= 500 && error.status <= 599) || error.status === 409
      || ['request_timeout', 'connection_error', 'invalid_response'].includes(error.code);
    if (!sessionId || affectedModels.length === 0 || !uncertain) return outcome;
    try {
      // Reconcile state once; never repeat a mutation whose result may already exist.
      const result = await request(`/sessions/${encodeURIComponent(sessionId)}`);
      if (!isCurrent()) return { ...outcome, stale: true };
      return { ...outcome, session: validateSession(result) };
    } catch (reconciliationError) {
      return { ...outcome, reconciliationError, stale: !isCurrent() };
    }
  }
}

export async function runExclusive(lock, callback) {
  if (lock.current) return;
  lock.current = true;
  try { return await callback(); }
  finally { lock.current = false; }
}

export function createLatestRequest() {
  let active = null;
  return {
    begin() {
      active?.abort();
      const controller = new AbortController();
      active = controller;
      return { signal: controller.signal, isCurrent: () => active === controller && !controller.signal.aborted };
    },
    cancel() { active?.abort(); active = null; },
  };
}
