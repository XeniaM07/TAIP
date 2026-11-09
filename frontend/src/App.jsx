import { useEffect, useMemo, useState } from 'react';

const API = import.meta.env.VITE_API_URL || '/api';
const MODEL_IDS = ['model-a', 'model-b', 'model-c'];

async function request(path, options = {}) {
  const response = await fetch(`${API}${path}`, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...options.headers },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `Request failed (${response.status})`);
  return body;
}

export default function App() {
  const [prompt, setPrompt] = useState('Create a simple two-class mathematics timetable for Monday.');
  const [session, setSession] = useState(null);
  const [selectedModel, setSelectedModel] = useState(MODEL_IDS[0]);
  const [draftText, setDraftText] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const workspace = session?.models?.[selectedModel];

  async function action(label, callback) {
    setBusy(true); setError(''); setNotice('');
    try { const result = await callback(); if (result) setSession(result); setNotice(label); }
    catch (err) { setError(err.message); }
    finally { setBusy(false); }
  }

  async function createSession() {
    const created = await request('/sessions', { method: 'POST', body: JSON.stringify({ prompt }) });
    setSession(created);
    setNotice('Session created. Extract a draft to continue.');
  }

  async function refresh() {
    if (session) setSession(await request(`/sessions/${session.session_id}`));
  }

  function selectModel(modelId) {
    setSelectedModel(modelId);
    const candidate = session?.models?.[modelId]?.draft?.data;
    setDraftText(candidate ? JSON.stringify(candidate, null, 2) : '');
  }

  async function extract() {
    const updated = await request(`/sessions/${session.session_id}/extract`, { method: 'POST', body: JSON.stringify({ model_ids: [selectedModel] }) });
    setSession(updated);
    setDraftText(JSON.stringify(updated.models[selectedModel].draft.data, null, 2));
    return updated;
  }

  async function extractAll() {
    const updated = await request(`/sessions/${session.session_id}/extract`, { method: 'POST', body: JSON.stringify({ model_ids: MODEL_IDS }) });
    setDraftText(JSON.stringify(updated.models[selectedModel].draft.data, null, 2));
    return updated;
  }

  async function saveDraft() {
    let data;
    try { data = JSON.parse(draftText); } catch { throw new Error('Draft JSON is invalid.'); }
    await request(`/sessions/${session.session_id}/drafts/${selectedModel}`, { method: 'PATCH', body: JSON.stringify({ data }) });
    const updated = await request(`/sessions/${session.session_id}`);
    setDraftText(JSON.stringify(updated.models[selectedModel].draft.data, null, 2));
    return updated;
  }

  async function modelAction(actionName) {
    await request(`/sessions/${session.session_id}/models/${selectedModel}/${actionName}`, { method: 'POST' });
    return request(`/sessions/${session.session_id}`);
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
    <header className="topbar"><a className="brand" href="#"><span className="brand-mark">T</span><span>TAIP <small>timetable assistant</small></span></a><span className="local-pill"><i /> Local mock workflow</span></header>
    <section className="hero"><div><p className="eyebrow">AI assisted · independently verified</p><h1>Build a timetable<br /><em>you can trust.</em></h1><p className="lede">Describe the schedule, review what each model extracted, and verify the finished timetable.</p></div><div className="hero-art" aria-hidden="true"><div className="orbit orbit-one"/><div className="orbit orbit-two"/><div className="orb-center">T</div><span className="orb orb-a">A</span><span className="orb orb-b">B</span><span className="orb orb-c">C</span></div></section>
    <section className="workspace">
      <aside className="panel prompt-panel"><div className="section-heading"><span className="step">01</span><div><h2>Your problem</h2><p>Start a private in-memory session.</p></div></div><label htmlFor="prompt">Describe the timetable</label><textarea id="prompt" className="prompt-input" value={prompt} onChange={(e) => setPrompt(e.target.value)} placeholder="Describe the schedule you need…"/><button className="button primary wide" disabled={busy || !prompt.trim()} onClick={() => action('Session ready', createSession)}>{session ? 'Start a new session' : 'Create session'} <span>↗</span></button>
          {session && <div className="session-meta"><span>Session</span><code>{session.session_id.slice(0, 8)}…</code><span className="status-chip">{session.status}</span></div>}
          <div className="privacy-note"><span>◉</span><p>Session is isolated and held in memory. It expires after 2 hours of inactivity.</p></div>
        </aside>
        <section className="panel main-panel"><div className="section-heading"><span className="step">02</span><div><h2>Review &amp; solve</h2><p>Each model keeps its own draft and results.</p></div></div>
          <div className="model-row"><label htmlFor="model">Model workspace</label><select id="model" value={selectedModel} onChange={(e) => selectModel(e.target.value)}>{MODEL_IDS.map((id, i) => <option key={id} value={id}>Model {String.fromCharCode(65 + i)} · Mock provider</option>)}</select><span className={`status-chip ${workspace?.status?.toLowerCase() || ''}`}>{workspace?.status || 'PENDING'}</span></div>
          <div className="toolbar"><button className="button secondary" disabled={!session || busy} onClick={() => action('Selected draft extracted', extract)}>Extract selected</button><button className="button secondary" disabled={!session || busy} onClick={() => action('Drafts extracted for all models', extractAll)}>Extract all</button><button className="button secondary" disabled={!workspace?.draft || busy} onClick={() => action('Draft saved', saveDraft)}>Save edits</button><button className="button primary" disabled={!workspace?.draft || busy} onClick={() => action('Snapshot confirmed', () => modelAction('confirm'))}>Confirm</button><button className="button solve" disabled={workspace?.status !== 'CONFIRMED' || busy} onClick={() => action('Solution generated', () => modelAction('solve'))}>Solve <span>→</span></button></div>
          <label htmlFor="draft" className="editor-label">Constraint &amp; timetable editor <span>JSON · edit entities and constraints</span></label><textarea id="draft" className="code-editor" value={draftText} onChange={(e) => setDraftText(e.target.value)} spellCheck="false" placeholder="Extract a draft to edit timetable data." disabled={!workspace?.draft}/>
          <div className="solution-heading"><div><h3>Generated timetable</h3><p>Assignments from the selected model</p></div>{workspace?.solution && <button className="button text-button" disabled={busy} onClick={() => action('Verification complete', () => modelAction('verify'))}>Verify solution ↗</button>}</div>
          <div className="table-wrap"><table><thead><tr><th>Class</th><th>Subject</th><th>Day</th><th>Period</th><th>Room</th></tr></thead><tbody>{assignments.map((assignment, i) => { const req = lookup.requirements[assignment.requirement_id]; const slot = lookup.slots[assignment.slot_id]; return <tr key={`${assignment.requirement_id}-${assignment.occurrence}`}><td>{lookup.classes[req?.class_id] || req?.class_id || '—'}</td><td>{lookup.subjects[req?.subject_id] || req?.subject_id || '—'}</td><td>{slot?.day || '—'}</td><td>{slot?.period ?? '—'}</td><td>{lookup.rooms[assignment.room_id] || assignment.room_id}</td></tr>; })}{assignments.length === 0 && <tr><td colSpan="5" className="empty-cell">Your timetable will appear here after solving.</td></tr>}</tbody></table></div>
          {workspace?.verification && <div className={`verification ${workspace.verification.status}`}><strong>Verification: {workspace.verification.status}</strong><span>{workspace.verification.violations.length ? `${workspace.verification.violations.length} violation(s)` : 'No constraint violations found.'}</span>{workspace.verification.violations.map((v, i) => <small key={i}>{v.message}</small>)}</div>}
        </section>
      </section>
      {session && <Comparison sessionId={session.session_id} refresh={refresh} version={JSON.stringify(session.models)} />}
      {(error || notice) && <div className={`toast ${error ? 'error' : ''}`}>{error || notice}<button onClick={() => { setError(''); setNotice(''); }}>×</button></div>}
      <footer><span>TAIP · Lab 06</span><span>Mock providers · replaceable service interfaces</span></footer>
    </main>;
}

function Comparison({ sessionId, refresh, version }) {
  const [rows, setRows] = useState([]);
  const [error, setError] = useState('');
  useEffect(() => { load(); }, [sessionId, version]);
  async function load() {
    try { const result = await request(`/sessions/${sessionId}/compare`); setRows(result.models); setError(''); }
    catch (err) { setError(err.message); }
  }
  return <section className="panel compare-panel"><div className="solution-heading"><div><span className="step">03</span><div><h2>Model comparison</h2><p>Compare extraction, solving and independent verification.</p></div></div><button className="button secondary" onClick={async () => { await refresh(); await load(); }}>Refresh comparison</button></div>{error && <p className="error-text">{error}</p>}<div className="table-wrap"><table><thead><tr><th>Model</th><th>Draft</th><th>Snapshot</th><th>Solution</th><th>Verification</th><th>Extract ms</th><th>Solve ms</th></tr></thead><tbody>{rows.map((row) => <tr key={row.model_id}><td>{row.model_id}</td><td>{row.proposal_id ? 'Ready' : '—'}</td><td>{row.snapshot_id ? 'Confirmed' : '—'}</td><td>{row.solution_id ? 'Generated' : '—'}</td><td>{row.verification_status || '—'}</td><td>{row.extraction_duration_ms ?? '—'}</td><td>{row.solving_duration_ms ?? '—'}</td></tr>)}</tbody></table></div></section>;
}
