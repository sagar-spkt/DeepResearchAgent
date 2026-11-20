'use client';
import {useEffect, useRef, useState} from 'react';
import {AssistantRuntimeProvider, useExternalStoreRuntime, ThreadPrimitive, MessagePrimitive} from '@assistant-ui/react';
import ReactMarkdown from 'react-markdown';
import {api, list, terminal, validateUpload, safeConsoleUrl} from '../lib/api.mjs';
import {streamRun} from '../lib/sse.mjs';
import {createSelectionGate, commitIfCurrent, documentUsage} from '../lib/selection.mjs';
import {canViewAdminNavigation} from '../lib/access.mjs';

function ChatMessage() {return <MessagePrimitive.Root className="message"><MessagePrimitive.Content /></MessagePrimitive.Root>;}
function Chat({messages, busy, onSend}) {
  const runtime = useExternalStoreRuntime({messages, isRunning: busy, convertMessage: m => ({id: m.id, role: m.role === 'user' ? 'user' : 'assistant', content: [{type: 'text', text: m.content || ''}]}), onNew: async m => onSend(m.content.filter(c => c.type === 'text').map(c => c.text).join('\n'))});
  return <AssistantRuntimeProvider runtime={runtime}><ThreadPrimitive.Root><ThreadPrimitive.Viewport className="transcript"><ThreadPrimitive.Messages components={{UserMessage: ChatMessage, AssistantMessage: ChatMessage}} /></ThreadPrimitive.Viewport></ThreadPrimitive.Root></AssistantRuntimeProvider>;
}
const json = body => JSON.stringify(body);
export default function ResearchDesk() {
  const [me, setMe] = useState(null), [authReady, setAuthReady] = useState(false), [error, setError] = useState('');
  const [tab, setTab] = useState('research'), [conversations, setConversations] = useState([]), [conversation, setConversation] = useState(null);
  const [documents, setDocuments] = useState([]), [memories, setMemories] = useState([]), [users, setUsers] = useState([]);
  const [traces, setTraces] = useState([]), [traceError, setTraceError] = useState(''), [traceLoading, setTraceLoading] = useState(false), [consoleUrl, setConsoleUrl] = useState(null);
  const [prompt, setPrompt] = useState(''), [memory, setMemory] = useState(''), [report, setReport] = useState(''), [run, setRun] = useState(null);
  const [progress, setProgress] = useState([]), [busy, setBusy] = useState(false), [loading, setLoading] = useState(false);
  const stream = useRef(null), selection = useRef(null), lastEvent = useRef(null), sending = useRef(false), gate = useRef(null), detailGate = useRef(null);
  if (!gate.current) gate.current = createSelectionGate();
  if (!detailGate.current) detailGate.current = createSelectionGate();
  const fail = e => setError(e.message || 'Something went wrong. Please try again.');
  async function refresh() {
    const [cs, ds, ms] = await Promise.all([api('/conversations'), api('/documents'), api('/memories')]);
    setConversations(list(cs)); setDocuments(list(ds)); setMemories(list(ms));
  }
  useEffect(() => {
    if (new URLSearchParams(window.location.search).get('signin') === 'expired') {
      setError('Your sign-in took too long to complete. Please log in again.');
      window.history.replaceState(null, '', '/');
    }
    api('/me').then(async value => {setMe(value); await refresh();}).catch(e => {if (e.status !== 401) fail(e);}).finally(() => setAuthReady(true));
    return () => stream.current?.abort();
  }, []);
  useEffect(() => {
    if (!documents.some(d => ['pending', 'processing', 'queued', 'indexing'].includes(d.status))) return;
    const timer = setInterval(() => api('/documents').then(value => setDocuments(list(value))).catch(fail), 2500);
    return () => clearInterval(timer);
  }, [documents]);
  async function operate(task) {
    setError(''); setLoading(true);
    try {return await task();} catch(e) {fail(e);} finally {setLoading(false);}
  }
  function beginSelection(id) {
    stream.current?.abort(); selection.current = id;
    const ticket = gate.current.begin(id); detailGate.current.begin(id);
    setBusy(false); setRun(null); setReport(''); setProgress([]); setConversation(null);
    return ticket;
  }
  async function refreshConversation(ticket) {
    if (!ticket.id || !gate.current.isCurrent(ticket)) return null;
    const request = detailGate.current.begin(ticket.id);
    return commitIfCurrent(detailGate.current, request, () => api(`/conversations/${ticket.id}`), value => {if (gate.current.isCurrent(ticket)) setConversation(value);});
  }
  async function selectConversation(id) {
    const ticket = beginSelection(id);
    const detail = await refreshConversation(ticket);
    if (!detail || !gate.current.isCurrent(ticket)) return null;
    const previous = detail.active_run || detail.latest_run;
    if (previous) {setRun(previous); setReport(previous.result || ''); if (!terminal(previous.status)) followRun(previous, id, ticket);}
    return detail;
  }
  async function createConversation() {
    setTab('research');
    const ticket = beginSelection(null);
    const created = await api('/conversations', {method: 'POST', body: json({title: 'New research'})});
    await refresh();
    if (!gate.current.isCurrent(ticket)) return null;
    return selectConversation(created.id);
  }
  async function followRun(value, conversationId, ticket = gate.current.snapshot()) {
    if (!gate.current.isCurrent(ticket) || ticket.id !== conversationId) return;
    stream.current?.abort();
    const controller = new AbortController(); stream.current = controller; lastEvent.current = null;
    // Replay starts at zero so documents_used events and prior token text are restored.
    setReport(''); setProgress([]); setBusy(true); setRun({...value, used_document_ids: value.used_document_ids || []});
    let completed = false;
    const update = event => {
      if (!gate.current.isCurrent(ticket) || controller.signal.aborted) return;
      if (event.id) lastEvent.current = event.id;
      const data = event.data || {};
      if (event.event === 'documents_used') setRun(current => ({...current, used_document_ids: documentUsage(current?.used_document_ids, data.document_ids)}));
      else if (event.event === 'token') setReport(text => text + (data.token || data.content || data.text || ''));
      else if (event.event === 'progress' || event.event === 'started' || event.event === 'queued') {setProgress(items => [...items.slice(-29), data.message || event.event]); if (event.event !== 'progress') setRun(current => ({...current, status: event.event === 'started' ? 'running' : event.event}));}
      else if (['completed', 'failed', 'cancelled'].includes(event.event)) {
        completed = true; setBusy(false); setRun(current => ({...current, status: event.event, used_document_ids: documentUsage(current?.used_document_ids, data.used_document_ids)}));
        if (data.result || data.report) setReport(data.result || data.report);
        if (event.event === 'failed') setError(data.error || data.message || 'Research failed. You can retry.');
      }
    };
    try {
      for (let attempt = 0; attempt < 4 && !completed && !controller.signal.aborted; attempt++) {
        try {await streamRun(value.id, update, {signal: controller.signal, after: lastEvent.current});}
        catch(e) {if (controller.signal.aborted) return; if (attempt === 3) throw e;}
        if (!completed) {
          const current = await api(`/runs/${value.id}`);
          if (!gate.current.isCurrent(ticket) || controller.signal.aborted) return;
          setRun(existing => ({...existing, used_document_ids: documentUsage(existing?.used_document_ids, current.used_document_ids)}));
          if (terminal(current.status)) {update({event: current.status, data: current}); break;}
          await new Promise(resolve => setTimeout(resolve, 1200));
        }
      }
      if (!completed && !controller.signal.aborted && gate.current.isCurrent(ticket)) setError('Progress connection paused. Use Reconnect to continue watching; your research can keep running.');
      if (completed && gate.current.isCurrent(ticket) && !controller.signal.aborted) {
        await refreshConversation(ticket);
        if (gate.current.isCurrent(ticket) && !controller.signal.aborted) await refresh();
      }
    } catch(e) {if (!controller.signal.aborted && gate.current.isCurrent(ticket)) fail(e);}
  }
  async function sendPrompt(text = prompt) {
    if (!text.trim() || busy || sending.current || !me?.approved) return;
    sending.current = true; setError('');
    try {
      let selected = conversation;
      if (!selected) selected = await createConversation();
      if (!selected || gate.current.snapshot().id !== selected.id) return;
      const ticket = gate.current.snapshot();
      const value = await api(`/conversations/${selected.id}/runs`, {method: 'POST', body: json({prompt: text})});
      if (!gate.current.isCurrent(ticket)) return;
      setPrompt(''); setReport(''); setProgress([]);
      setConversation(current => ({...current, title: value.conversation_title || current?.title, messages: [...(current?.messages || []), {id: crypto.randomUUID(), role: 'user', content: text}]}));
      if (value.conversation_title) setConversations(items => items.map(c => c.id === selected.id ? {...c, title: value.conversation_title} : c));
      followRun(value, selected.id, ticket);
    } catch(e) {fail(e);} finally {sending.current = false;}
  }
  async function upload(event) {
    const file = event.target.files?.[0]; const validation = validateUpload(file);
    if (validation) {setError(validation); return;}
    const ticket = gate.current.snapshot();
    await operate(async () => {
      const body = new FormData(); body.set('file', file);
      if (tab === 'research' && ticket.id) body.set('conversation_id', ticket.id);
      await api('/documents', {method: 'POST', body}); await refresh();
      await refreshConversation(ticket);
    });
    event.target.value = '';
  }
  async function loadTraces() {
    setTraceLoading(true); setTraceError(''); setConsoleUrl(null);
    try {const response = await api('/admin/traces'); setTraces(list(response)); setConsoleUrl(safeConsoleUrl(response.console_url, window.location.href));}
    catch(e) {setTraceError(e.message || 'Trace metadata is unavailable.');}
    finally {setTraceLoading(false);}
  }
  async function loadAdmin() {setTab('admin'); loadTraces(); await operate(async () => setUsers(list(await api('/admin/users'))));}
  if (!authReady) return <main className="welcome"><p>Opening your research desk…</p></main>;
  if (!me) return <main className="welcome"><div className="eyebrow">RESEARCH DESK</div><h1>A question is<br/>a starting point.</h1><p>Explore the web and your documents. Keep the sources, conversation, and useful knowledge together.</p><div className="actions auth-actions"><a className="primary" href="/auth/login">Log in</a><a className="secondary" href="/auth/signup">Sign up</a></div>{error && <p role="alert" className="error">{error}</p>}</main>;
  return <div className="shell">
    <aside><div className="brand">Research<span>Desk</span></div><p className="muted">A little further into the question.</p><button className="primary" disabled={loading} onClick={() => operate(createConversation)}>＋ New conversation</button><nav aria-label="Workspace"><button aria-current={tab === 'research' ? 'page' : undefined} onClick={() => setTab('research')}>Research</button><button aria-current={tab === 'documents' ? 'page' : undefined} onClick={() => setTab('documents')}>Documents <small>{documents.length}</small></button><button aria-current={tab === 'memories' ? 'page' : undefined} onClick={() => setTab('memories')}>Saved memory <small>{memories.length}</small></button>{canViewAdminNavigation(me) && <button onClick={loadAdmin} aria-current={tab === 'admin' ? 'page' : undefined}>Administration</button>}</nav><h2 className="label">CONVERSATIONS</h2><div className="history">{conversations.map(c => <button key={c.id} className={conversation?.id === c.id ? 'selected' : ''} onClick={() => operate(async () => {const detail = await selectConversation(c.id); if (detail) setTab('research');})}>{c.title || 'Untitled research'}</button>)}</div><footer><strong>{me.email}</strong><small>{me.role} · {me.approved ? 'Approved' : 'Awaiting approval'}</small><a href="/auth/logout">Sign out</a></footer></aside>
    <main><header><div><span className="eyebrow">YOUR PRIVATE WORKSPACE</span><h1>{tab === 'research' ? conversation?.title || 'What would you like to explore?' : tab === 'documents' ? 'Your source library' : tab === 'memories' ? 'Knowledge you choose to keep' : 'Manage access'}</h1></div>{tab === 'research' && conversation && <button className="quiet" disabled={busy || loading} onClick={() => operate(async () => {if (!confirm('Delete this conversation?')) return; const ticket = gate.current.snapshot(); await api(`/conversations/${ticket.id}`, {method: 'DELETE'}); if (gate.current.isCurrent(ticket)) beginSelection(null); await refresh();})}>Delete conversation</button>}</header>
      {error && <div role="alert" className="error">{error}<button aria-label="Dismiss error" onClick={() => setError('')}>×</button></div>}
      {!me.approved && <p role="status" className="notice">Your account is awaiting administrator approval. You can prepare your workspace; research becomes available after approval. <button onClick={() => operate(async () => setMe(await api('/me')))}>Refresh access</button></p>}
      {tab === 'research' && <>
        {!conversation?.messages?.length && !report && <section className="empty"><div className="orb">✳</div><h2>Follow your curiosity.</h2><p>Ask a focused question or compare a few perspectives.<br/>Add documents whenever they become relevant.</p><div className="suggestions">{['Compare approaches to long-term agent memory', 'Research how cities adapt to extreme heat'].map(text => <button key={text} onClick={() => setPrompt(text)}>{text} ↗</button>)}</div></section>}
        <Chat messages={(conversation?.messages || []).filter((m,i,all) => !(report && i === all.length - 1 && m.role === 'assistant' && m.content === report))} busy={busy} onSend={sendPrompt} />
        {run && <section className="progress" aria-live="polite"><div className="row"><strong>Research · {run.status}</strong><div>{busy && <button onClick={() => operate(async () => {const ticket = gate.current.snapshot(); await api(`/runs/${run.id}/cancel`, {method: 'POST'}); if (gate.current.isCurrent(ticket)) setProgress(items => [...items, 'Cancellation requested']);})}>Cancel research</button>}{!terminal(run.status) && <button onClick={() => followRun(run, conversation.id)}>Reconnect</button>}</div></div><p className="muted">{run.used_document_ids?.length ? `Documents used: ${run.used_document_ids.map(id => documents.find(d => d.id === id)?.name || id).join(', ')}` : 'No uploaded documents recorded as used in this run yet.'}</p>{progress.length > 0 && <ol>{progress.map((p,i) => <li key={i}>{p}</li>)}</ol>}</section>}
        {report && <article className="report"><span className="eyebrow">RESEARCH REPORT</span><ReactMarkdown components={{img: ({alt}) => <span>[Image: {alt || 'omitted'}]</span>, a: ({href,children}) => <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>}}>{report}</ReactMarkdown></article>}
        {conversation?.documents?.length > 0 && <div className="attached">Attached: {conversation.documents.map(d => {const current = documents.find(item => item.id === d.id) || d; return `${d.name} (${current.status}${run ? run.used_document_ids?.includes(d.id) ? '; used in this run' : '; not yet used in this run' : ''})`;}).join(', ')}</div>}
        <form className="composer" onSubmit={e => {e.preventDefault(); sendPrompt();}}><label className="sr-only" htmlFor="prompt">Research question</label><textarea id="prompt" rows={3} placeholder="Ask a question, add context, or follow up…" value={prompt} onChange={e => setPrompt(e.target.value)} /><div className="row"><label className="upload">＋ Add document<input type="file" accept=".pdf,.docx,.txt,.md,.markdown" onChange={upload} disabled={loading} /></label><button className="primary" disabled={busy || loading || !me.approved || !prompt.trim()} type="submit">{busy ? 'Researching…' : 'Start research ↗'}</button></div><small>Sources are cited. Documents remain private. New uploads are used once indexing completes.</small></form>
      </>}
      {tab === 'documents' && <section><div className="section-intro"><p>Private files stay available here. Attach a source to a conversation to use it there.</p><label className="primary upload">Upload document<input type="file" accept=".pdf,.docx,.txt,.md,.markdown" onChange={upload} disabled={loading} /></label></div><p className="muted">PDF, DOCX, TXT, Markdown · up to 20 MB · scanned PDFs need text extraction before upload.</p>{!documents.length && <p className="empty-small">Your library is ready for its first source.</p>}<div className="cards">{documents.map(d => <article className="card" key={d.id}><span className="file-icon">↳</span><h2>{d.name}</h2><p>{d.status}{d.error ? ` · ${d.error}` : ''}</p><div className="actions"><button disabled={!conversation || loading || d.status !== 'ready'} onClick={() => operate(async () => {const ticket = gate.current.snapshot(); if (!ticket.id) return; await api(`/conversations/${ticket.id}/documents/${d.id}`, {method: 'POST'}); await refreshConversation(ticket);})}>Attach to current chat</button><button onClick={() => operate(async () => {if (confirm(`Delete ${d.name}?`)) {const ticket = gate.current.snapshot(); await api(`/documents/${d.id}`, {method: 'DELETE'}); await refresh(); await refreshConversation(ticket);}})}>Delete</button></div></article>)}</div></section>}
      {tab === 'memories' && <section><p>Only facts you explicitly save carry into other conversations. Edit or remove them at any time.</p><form className="memory-form" onSubmit={e => {e.preventDefault(); operate(async () => {await api('/memories', {method: 'POST', body: json({content: memory})}); setMemory(''); await refresh();});}}><label htmlFor="memory">Save a preference or useful fact</label><textarea id="memory" value={memory} onChange={e => setMemory(e.target.value)} placeholder="For example: Prefer primary sources and include publication dates."/><button className="primary" disabled={!memory.trim() || loading}>Save memory</button></form><div className="cards">{memories.map(m => <MemoryCard key={m.id} value={m} disabled={loading} save={content => operate(async () => {await api(`/memories/${m.id}`, {method: 'PATCH', body: json({content})}); await refresh(); return true;})} remove={() => operate(async () => {await api(`/memories/${m.id}`, {method: 'DELETE'}); await refresh();})}/>)}</div></section>}
      {tab === 'admin' && <section><p>Approve research access and manage roles. The server enforces permissions and protects the last owner.</p><div className="table-wrap"><table><thead><tr><th>User</th><th>Research access</th><th>Role</th></tr></thead><tbody>{users.map(user => <tr key={user.id}><td>{user.email}</td><td><button disabled={loading} onClick={() => operate(async () => {await api(`/admin/users/${user.id}`, {method: 'PATCH', body: json({approved: !user.approved})}); setUsers(list(await api('/admin/users'))); setMe(await api('/me'));})}>{user.approved ? 'Revoke approval' : 'Approve'}</button></td><td><select aria-label={`Role for ${user.email}`} value={user.role} disabled={loading || me.role !== 'owner'} onChange={e => {const role = e.target.value; operate(async () => {await api(`/admin/users/${user.id}`, {method: 'PATCH', body: json({role})}); setUsers(list(await api('/admin/users'))); setMe(await api('/me'));});}}><option value="user">User</option><option value="admin">Administrator</option><option value="owner">Owner</option></select></td></tr>)}</tbody></table></div><section aria-label="Trace metadata"><div className="section-intro"><h2>Research traces</h2>{consoleUrl && <a href={consoleUrl} target="_blank" rel="noopener noreferrer">Open private Langfuse console ↗</a>}<button disabled={traceLoading} onClick={loadTraces}>{traceLoading ? 'Loading traces…' : 'Refresh traces'}</button></div>{traceError && <p role="status" className="notice">{traceError}</p>}{!traceError && !traces.length && <p className="muted">No trace metadata available.</p>}<div className="cards">{traces.map(trace => <article className="card" key={trace.id}><h2>{trace.name || 'Research'}</h2><p>{trace.timestamp || 'Time unavailable'}{trace.latency != null ? ` · ${trace.latency}s` : ''}</p><small>{trace.id}</small>{trace.metadata && <pre>{JSON.stringify(trace.metadata, null, 2)}</pre>}</article>)}</div></section><p className="muted">This view shows trace metadata only. Access to the private native Langfuse console requires a separately verified deployment. Its owner manages console organization membership through the native owner UI.</p></section>}
    </main>
  </div>;
}
function MemoryCard({value, save, remove, disabled}) {
  const [editing, setEditing] = useState(false), [content, setContent] = useState(value.content);
  return <article className="card">{editing ? <textarea aria-label="Memory content" value={content} onChange={e => setContent(e.target.value)} /> : <p>{value.content}</p>}<div className="actions">{editing ? <><button disabled={disabled || !content.trim()} onClick={async () => {if (await save(content)) setEditing(false);}}>Save</button><button onClick={() => {setContent(value.content); setEditing(false);}}>Cancel</button></> : <button onClick={() => setEditing(true)}>Edit</button>}<button disabled={disabled} onClick={remove}>Delete</button></div></article>;
}
