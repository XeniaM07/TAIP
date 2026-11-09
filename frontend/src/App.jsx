import { useEffect, useMemo, useRef, useState } from 'react';
import { createApiClient } from './api.js';
import { comparisonRows, createLatestRequest, editorKey, modelCatalog, reconcileEditors, reconcileFailureTimes, runExclusive, runSessionOperation, runtimePrompt, solveRetryState } from './workflow-state.js';

const request = createApiClient({ baseUrl: import.meta.env.VITE_API_URL || '/api' });
const segment = encodeURIComponent;
const displayNumber = (value) => Number.isFinite(value) ? value : '—';

export default function App() {
  const [runtime, setRuntime] = useState(null);
  const [runtimeReceivedAt, setRuntimeReceivedAt] = useState(0);
  const [runtimeWarning, setRuntimeWarning] = useState('');
  const [prompt, setPrompt] = useState('');
  const promptEdited = useRef(false);
  const [session, setSession] = useState(null);
  const activeSessionId = useRef(null);
  const [failureTimes, setFailureTimes] = useState({});
  const [selectedModel, setSelectedModel] = useState('');
  const [editors, setEditors] = useState({});
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [now, setNow] = useState(Date.now());
  const catalog = modelCatalog(runtime, session);
  const selected = catalog.find((model) => model.model_id === selectedModel);
  const workspace = session?.models?.[selectedModel];
  const currentEditorKey = editorKey(session?.session_id, selectedModel);
  const editor = editors[currentEditorKey];
  const draftText = editor?.text || '';
  const dirty = Boolean(editor && editor.text !== editor.source);
  const quotaRemaining = Math.max(0, Math.ceil((runtimeReceivedAt + (selected?.quota?.cooldown_seconds || 0) * 1000 - now) / 1000));
  const retry = solveRetryState(workspace, { receivedAt: failureTimes[currentEditorKey]?.receivedAt, now, cooldownSeconds: selected?.quota?.cooldown_seconds, cooldownReceivedAt: runtimeReceivedAt });
  const lastAttempt = workspace?.attempts?.at(-1);
  const failedSolve = workspace?.status === 'FAILED' && workspace.snapshot;

  function acceptRuntime(result) {
    setRuntime(result);
    setRuntimeReceivedAt(Date.now());
    setRuntimeWarning('');
    setSelectedModel((current) => current || result.models?.[0]?.model_id || '');
  }

  useEffect(() => {
    const controller = new AbortController();
    request('/runtime', { signal: controller.signal }).then((result) => {
      acceptRuntime(result);
      if (!promptEdited.current) setPrompt(runtimePrompt(result));
    }).catch((err) => { if (err.name !== 'AbortError') setError(err.message); });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const failureWaiting = Object.entries(session?.models || {}).some(([id, model]) => (failureTimes[editorKey(session.session_id, id)]?.receivedAt || 0) + (model.failure?.retry_after_seconds || 0) * 1000 > now);
    const quotaWaiting = (runtime?.models || []).some((model) => runtimeReceivedAt + (model.quota?.cooldown_seconds || 0) * 1000 > now);
    if (!failureWaiting && !quotaWaiting) return;
    const timer = setTimeout(() => setNow(Date.now()), 1000);
    return () => clearTimeout(timer);
  }, [session, failureTimes, runtime, runtimeReceivedAt, now]);

  async function refreshRuntime() {
    try { acceptRuntime(await request('/runtime')); }
    catch { setRuntimeWarning('Usage information could not be refreshed.'); }
  }

  function acceptSession(result, resetModels = []) {
    const receivedAt = Date.now();
    activeSessionId.current = result.session_id;
    setSession(result);
    setFailureTimes((current) => reconcileFailureTimes(current, result, receivedAt));
    setNow(receivedAt);
    setEditors((current) => reconcileEditors(current, result, resetModels));
    setSelectedModel((current) => result.models[current] ? current : Object.keys(result.models)[0] || '');
  }

  async function action(label, callback, affectedModels = [], resetModels = []) {
    return runExclusive(busyRef, async () => {
      setBusy(true); setError(''); setNotice('');
      try {
        const currentSessionId = activeSessionId.current;
        const outcome = await runSessionOperation(callback, {
          request, sessionId: affectedModels.length > 0 ? currentSessionId : null, affectedModels,
          isCurrent: () => activeSessionId.current === currentSessionId,
        });
        if (outcome.stale) return;
        if (outcome.session) {
          acceptSession(outcome.session, outcome.error ? [] : resetModels);
          if (!outcome.error) {
            const failed = affectedModels.filter((id) => outcome.session.models[id]?.status === 'FAILED');
            setNotice(failed.length ? 'Operation finished with model failures. Review their details below.' : label);
          }
          await refreshRuntime();
        }
        if (outcome.error) {
          const err = outcome.error;
          const recoveryHint = outcome.session ? ' Session state refreshed. Review the workspace before continuing.'
            : outcome.reconciliationError?.status === 409 || err.status === 409 ? ' Work may still be running. Use Refresh session after it finishes.'
              : affectedModels.length > 0 ? ' Use Refresh session to check the latest state before trying again.' : '';
          setError(`${err.message}${err.retryAfterSeconds != null ? ` Try again after ${err.retryAfterSeconds} seconds.` : ''}${recoveryHint}`);
        }
      } catch (err) {
        setError(`${err.message}${err.retryAfterSeconds != null ? ` Try again after ${err.retryAfterSeconds} seconds.` : ''}`);
      } finally { setBusy(false); }
    });
  }

  async function reloadCatalog() {
    return runExclusive(busyRef, async () => {
      setBusy(true); setError('');
      try {
        const result = await request('/runtime');
        acceptRuntime(result);
        if (!promptEdited.current) setPrompt(runtimePrompt(result));
      } catch (err) { setError(err.message); }
      finally { setBusy(false); }
    });
  }

  function createSession() {
    return request('/sessions', { method: 'POST', body: JSON.stringify({ prompt }) });
  }

  function extract(modelIds) {
    return request(`/sessions/${segment(session.session_id)}/extract`, { method: 'POST', body: JSON.stringify({ model_ids: modelIds }) });
  }

  function saveDraft() {
    let data;
    try { data = JSON.parse(draftText); } catch { throw new Error('Draft JSON is invalid.'); }
    return request(`/sessions/${segment(session.session_id)}/drafts/${segment(selectedModel)}?session_view=true`, { method: 'PATCH', body: JSON.stringify({ data }) });
  }

  function modelAction(actionName) {
    return request(`/sessions/${segment(session.session_id)}/models/${segment(selectedModel)}/${actionName}?session_view=true`, { method: 'POST' });
  }

  const data = workspace?.draft?.data;
  const assignments = workspace?.solution?.assignments || [];
  const lookup = useMemo(() => ({
    requirements: Object.fromEntries((data?.requirements || []).map((row) => [row.requirement_id, row])),
    classes: Object.fromEntries((data?.classes || []).map((row) => [row.class_id, row.name])),
    subjects: Object.fromEntries((data?.subjects || []).map((row) => [row.subject_id, row.name])),
    slots: Object.fromEntries((data?.time_slots || []).map((row) => [row.slot_id, row])),
    rooms: Object.fromEntries((data?.rooms || []).map((row) => [row.room_id, row.name])),
  }), [data]);

  return <main className="shell">
    <header className="topbar"><a className="brand" href="#"><span className="brand-mark">T</span><span>TAIP <small>timetable assistant</small></span></a><span className="local-pill"><i /> {runtime ? `${runtime.mode === 'mock' ? 'Local mock' : runtime.mode === 'live' ? 'Live AI' : 'Custom provider'} workflow` : 'Loading model catalog…'}</span></header>
    <section className="hero"><div><p className="eyebrow">AI assisted · independently verified</p><h1>Build a timetable<br /><em>you can trust.</em></h1><p className="lede">Describe the schedule, review what each model extracted, and verify the finished timetable.</p></div><div className="hero-art" aria-hidden="true"><div className="orbit orbit-one"/><div className="orbit orbit-two"/><div className="orb-center">T</div><span className="orb orb-a">A</span><span className="orb orb-b">B</span><span className="orb orb-c">C</span></div></section>
    <section className="workspace">
      <aside className="panel prompt-panel"><div className="section-heading"><span className="step">01</span><div><h2>Your problem</h2><p>Start a private in-memory session.</p></div></div><label htmlFor="prompt">Describe the timetable</label><textarea id="prompt" className="prompt-input" value={prompt} onChange={(e) => { promptEdited.current = true; setPrompt(e.target.value); }} disabled={busy} maxLength={20000} placeholder="Describe the schedule you need…"/><button className="button primary wide" disabled={busy || !runtime || catalog.length === 0 || !prompt.trim()} onClick={() => action('Session created. Extract a draft to continue.', createSession)}>{session ? 'Start a new session' : 'Create session'} <span>↗</span></button>
        {!runtime && <button className="button text-button" disabled={busy} onClick={reloadCatalog}>Reload model catalog</button>}
        {session && <div className="session-meta"><span>Session</span><code>{session.session_id.slice(0, 8)}…</code><span className="status-chip">{session.status}</span></div>}
        <div className="privacy-note"><span>◉</span><p>Session is isolated and held in memory. It expires after 2 hours of inactivity.</p></div>
      </aside>
      <section className="panel main-panel"><div className="section-heading"><span className="step">02</span><div><h2>Review &amp; solve</h2><p>Each model keeps its own draft and results.</p></div></div>
        <div className="model-row"><label htmlFor="model">Model workspace</label><select id="model" value={selectedModel} disabled={busy || catalog.length === 0} onChange={(e) => setSelectedModel(e.target.value)}>{catalog.length === 0 && <option value="">Loading models…</option>}{catalog.map((model) => <option key={model.model_id} value={model.model_id}>{model.label}</option>)}</select><span className={`status-chip ${workspace?.status?.toLowerCase() || ''}`}>{workspace?.status || 'PENDING'}</span></div>
        <ModelUsage model={selected} lastAttempt={lastAttempt} runtimeWarning={runtimeWarning} cooldown={quotaRemaining} />
        <div className="toolbar">
          <button className="button secondary" disabled={!session || !selectedModel || busy || quotaRemaining > 0} onClick={() => action('Selected draft extracted', () => extract([selectedModel]), [selectedModel], [selectedModel])}>Extract selected</button>
          <button className="button secondary" disabled={!session || busy || catalog.length === 0} onClick={() => { const ids = catalog.map((model) => model.model_id); action('Drafts extracted for all models', () => extract(ids), ids, ids); }}>Extract all</button>
          <button className="button secondary" disabled={!workspace?.draft || busy} onClick={() => action('Draft saved', saveDraft, [selectedModel], [selectedModel])}>Save edits</button>
          <button className="button primary" disabled={!workspace?.draft || busy || dirty} onClick={() => action('Snapshot confirmed', () => modelAction('confirm'), [selectedModel])}>Confirm</button>
          {failedSolve ? <button className="button solve" disabled={busy || (!retry.reusesResult && dirty) || !retry.eligible} onClick={() => action(retry.reusesResult ? 'Existing solution resumed' : 'Solution generated', () => modelAction('solve'), [selectedModel])}>{retry.reusesResult ? 'Resume solve' : 'Retry solve'}{retry.remainingSeconds > 0 ? ` (${retry.remainingSeconds}s)` : ''} <span>→</span></button>
            : <button className="button solve" disabled={workspace?.status !== 'CONFIRMED' || busy || dirty || quotaRemaining > 0} onClick={() => action('Solution generated', () => modelAction('solve'), [selectedModel])}>Solve <span>→</span></button>}
        </div>
        {workspace?.failure && <div className="verification FAILED" role="alert"><strong>{workspace.failure.message}</strong><small>Code: {workspace.failure.code}. {retry.reusesResult ? 'Resume solve uses the existing result and makes no new AI request.' : workspace.failure.retryable ? 'You may explicitly retry when the cooldown has elapsed.' : 'Review the problem or provider configuration before trying again.'}</small></div>}
        {dirty && <p className="editor-note">You have unsaved edits. Save them before confirming or starting a new solve.</p>}
        <label htmlFor="draft" className="editor-label">Constraint &amp; timetable editor <span>JSON · edit entities and constraints</span></label><textarea id="draft" className="code-editor" value={draftText} onChange={(e) => setEditors((current) => ({ ...current, [currentEditorKey]: { ...current[currentEditorKey], text: e.target.value } }))} spellCheck="false" placeholder="Extract a draft to edit timetable data." disabled={!workspace?.draft || busy}/>
        {workspace?.proposal?.missing_information?.length > 0 && <div className="verification FAILED"><strong>Extraction needs explicit data</strong>{workspace.proposal.missing_information.map((item, i) => <small key={`${item.code}-${i}`}>{item.message}</small>)}</div>}
        {workspace?.proposal?.unsupported_requirements?.length > 0 && <div className="verification FAILED"><strong>Unsupported requirements</strong>{workspace.proposal.unsupported_requirements.map((item, i) => <small key={i}>{item}</small>)}</div>}
        <div className="solution-heading"><div><h3>Generated timetable</h3><p>Assignments from the selected model</p></div>{workspace?.solution && <button className="button text-button" disabled={busy || retry.reusesResult} onClick={() => action('Verification complete', () => modelAction('verify'), [selectedModel])}>Verify solution ↗</button>}</div>
        <div className="table-wrap"><table><thead><tr><th>Class</th><th>Subject</th><th>Day</th><th>Period</th><th>Room</th></tr></thead><tbody>{assignments.map((assignment) => { const req = lookup.requirements[assignment.requirement_id]; const slot = lookup.slots[assignment.slot_id]; return <tr key={`${assignment.requirement_id}-${assignment.occurrence}`}><td>{lookup.classes[req?.class_id] || req?.class_id || '—'}</td><td>{lookup.subjects[req?.subject_id] || req?.subject_id || '—'}</td><td>{slot?.day || '—'}</td><td>{slot?.period ?? '—'}</td><td>{lookup.rooms[assignment.room_id] || assignment.room_id}</td></tr>; })}{assignments.length === 0 && <tr><td colSpan="5" className="empty-cell">Your timetable will appear here after solving.</td></tr>}</tbody></table></div>
        {workspace?.verification && <div className={`verification ${workspace.verification.status}`}><strong>Verification: {workspace.verification.status}</strong><span>{workspace.verification.violations.length ? `${workspace.verification.violations.length} violation(s)` : 'No constraint violations found.'}</span>{workspace.verification.violations.map((violation, i) => <small key={i}>{violation.message}</small>)}</div>}
        {session && <button className="button text-button session-refresh" disabled={busy} onClick={() => action('Session refreshed', () => request(`/sessions/${segment(session.session_id)}`))}>Refresh session ↻</button>}
      </section>
    </section>
    {session && <Comparison session={session} catalog={catalog} busy={busy} />}
    {(error || notice) && <div className={`toast ${error ? 'error' : ''}`} role={error ? 'alert' : 'status'}>{error || notice}<button aria-label="Dismiss notification" onClick={() => { setError(''); setNotice(''); }}>×</button></div>}
    <footer><span>TAIP · Lab 06</span><span>{runtime?.mode === 'mock' ? 'Mock providers' : 'Configured AI providers'} · independent timetable verification</span></footer>
  </main>;
}

function ModelUsage({ model, lastAttempt, runtimeWarning, cooldown }) {
  const invocation = lastAttempt?.invocation;
  const usage = invocation?.usage;
  return <div className="usage-note">
    {model?.quota && <p>Usage: {displayNumber(model.quota.requests_in_minute)} / {displayNumber(model.limits?.requests_per_minute)} requests per minute · {displayNumber(model.quota.requests_in_day)} / {displayNumber(model.limits?.requests_per_day)} per day · {displayNumber(model.quota.tokens_in_minute)} / {displayNumber(model.limits?.tokens_per_minute)} tokens per minute · {displayNumber(model.quota.in_flight)} in flight</p>}
    {cooldown > 0 && <p className="error-text">Provider cooldown: {cooldown}s remaining.</p>}
    {(model?.warnings || []).map((warning, index) => <p className="error-text" key={index}>{warning}</p>)}
    {runtimeWarning && <p className="error-text">{runtimeWarning}</p>}
    {lastAttempt && <p>Last {lastAttempt.operation}: {invocation?.returned_model_id || invocation?.requested_model_id || lastAttempt.model_id || model?.model_id} · {displayNumber(lastAttempt.duration_ms)} ms{usage && <> · input {displayNumber(usage.input_tokens)} / output {displayNumber(usage.output_tokens)} / total {displayNumber(usage.total_tokens)} tokens{Number.isFinite(usage.cost_usd) ? ` · $${usage.cost_usd}` : ''}</>}</p>}
  </div>;
}

function Comparison({ session, catalog, busy }) {
  const [loaded, setLoaded] = useState(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  const loads = useRef(null);
  const refreshPending = useRef(false);
  if (!loads.current) loads.current = createLatestRequest();
  useEffect(() => {
    const ticket = loads.current.begin();
    refreshPending.current = true;
    setLoading(true); setError('');
    request(`/sessions/${segment(session.session_id)}/compare`, { signal: ticket.signal }).then((result) => {
      if (ticket.isCurrent()) setLoaded({ session, result });
    }).catch((err) => { if (ticket.isCurrent() && err.name !== 'AbortError') setError(err.message); })
      .finally(() => { if (ticket.isCurrent()) { refreshPending.current = false; setLoading(false); } });
    return () => loads.current.cancel();
  }, [session, revision]);
  const result = loaded?.session === session ? loaded.result : null;
  const report = result?.report;
  const rows = comparisonRows(catalog, session, result);
  const confirmedCount = Object.values(session.models || {}).filter((model) => model.snapshot).length;
  const comparisonNotes = (report?.notes || []).filter((note) => confirmedCount >= 2 || !note.startsWith('Runs use different confirmed snapshots.'));
  if (confirmedCount < 2) comparisonNotes.unshift('Confirm at least two model drafts before comparing their snapshots.');
  else if (!comparisonNotes.length) comparisonNotes.push(report?.snapshots_comparable ? 'Runs use the same confirmed snapshot.' : 'Runs use different confirmed snapshots; compare their verification metrics separately.');
  function refresh() {
    if (busy || refreshPending.current) return;
    refreshPending.current = true;
    setRevision((value) => value + 1);
  }
  return <section className="panel compare-panel"><div className="solution-heading"><div><span className="step">03</span><div><h2>Model comparison</h2><p>Extraction evidence, corrections, validity, and timings by model.</p></div></div><button className="button secondary" disabled={busy || loading} onClick={refresh}>{loading ? 'Loading comparison…' : 'Refresh comparison'}</button></div>{error && <p className="error-text" role="alert">{error}</p>}{report && <div className="privacy-note"><span>i</span><p>{comparisonNotes.join(' ')}</p></div>}<div className="table-wrap"><table><thead><tr><th>Model</th><th>Corrections</th><th>Hard violations</th><th>Soft penalty</th><th>Extraction F1</th><th>Verify</th><th>Total ms</th></tr></thead><tbody>{rows.map((row) => <tr key={row.model_id}><td>{row.label} <span className={`status-chip ${row.status.toLowerCase()}`}>{row.status}</span>{row.failure && <small className="model-failure">{row.failure.message}</small>}</td><td>{row.edit_count ?? '—'}</td><td>{row.hard_violation_count ?? '—'}</td><td>{row.soft_penalty ?? '—'}</td><td>{Number.isFinite(row.extraction_constraints?.f1) ? row.extraction_constraints.f1.toFixed(2) : 'Unavailable'}</td><td>{row.verification_status || '—'}</td><td>{row.total_duration_ms ?? '—'}</td></tr>)}{rows.length === 0 && <tr><td colSpan="7" className="empty-cell">Extract model drafts to begin comparison.</td></tr>}</tbody></table></div></section>;
}
