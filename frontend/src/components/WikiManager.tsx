import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { ApiError, request } from '../api';
import Button from '@mui/material/Button';
import { ArrowLeft, BookOpen, FileText, Link2, Pencil, Plus, Search, X, ChevronDown, Folder } from 'lucide-react';
import { MarkdownContent } from './MarkdownContent';
import { copyText } from '../clipboard';
import './wiki-manager.css';

export type WikiSection = 'read' | 'edit' | 'aliases' | 'history';
type WikiSummary = { id: number; title: string; category: string; revision: number };
type WikiDocument = WikiSummary & { content: string; updated_at: string; source?: string };
type Draft = Pick<WikiDocument, 'title' | 'content' | 'category'>;
type Revision = { revision: number; created_at: string };
type Diff = { diff: Record<'title' | 'content' | 'category', string> };
const emptyDraft = (): Draft => ({ title: '', content: '', category: 'general' });
const docPath = (id: number) => `/wiki/documents/${id}?store_id=integrated`;
const message = (error: unknown) => error instanceof Error ? error.message : '요청을 처리하지 못했습니다.';
const revisionDate = (value: string) => {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('ko-KR', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
};
function DiffText({ text }: { text: string }) {
  if (!text) return <p className="wiki-diff-empty">변경 없음</p>;
  const lines = text.split('\n');
  if (lines[lines.length - 1] === '') lines.pop();
  return <pre className="wiki-diff-lines">{lines.map((line, index) => {
    const kind = line.startsWith('+++') || line.startsWith('---') || line.startsWith('@@') || line.startsWith('\\') ? 'meta' : line.startsWith('+') ? 'added' : line.startsWith('-') ? 'removed' : 'context';
    return <span className={'wiki-diff-' + kind} key={index}>{line || '\u00a0'}</span>;
  })}</pre>;
}

// The changed middle is highlighted, preserving every character without an expensive diff.
function ChangedText({ before, after }: { before: string; after: string }) {
  let start = 0;
  while (start < before.length && start < after.length && before[start] === after[start]) start++;
  let end = 0;
  while (end < before.length - start && end < after.length - start && before[before.length - end - 1] === after[after.length - end - 1]) end++;
  return <div className="wiki-conflict-columns"><div><strong>최신 저장본</strong><pre>{before.slice(0, start)}<del>{before.slice(start, end ? -end : undefined)}</del>{end ? before.slice(-end) : ''}</pre></div>
    <div><strong>내 초안</strong><pre>{after.slice(0, start)}<ins>{after.slice(start, end ? -end : undefined)}</ins>{end ? after.slice(-end) : ''}</pre></div></div>;
}

export function WikiManager({ documentId, section = 'read', onNavigate }: { documentId?: number; section?: WikiSection; onNavigate?: (id?: number, section?: WikiSection, replace?: boolean) => void }) {
  const [mode, setMode] = useState<'loading' | 'integrated' | 'legacy_external' | 'error'>('loading');
  const [docs, setDocs] = useState<WikiSummary[]>([]);
  const [doc, setDoc] = useState<WikiDocument>();
  const [draft, setDraft] = useState<Draft>(emptyDraft);
  const [query, setQuery] = useState('');
  const [preview, setPreview] = useState(() => !window.matchMedia?.('(max-width: 700px)').matches);
  const [editing, setEditing] = useState(false);
  const [activeSection, setActiveSection] = useState<WikiSection>('read');
  const [managementOpen, setManagementOpen] = useState(false);
  const loadAttempt = useRef('');
  const [creating, setCreating] = useState(false);
  const [searched, setSearched] = useState(false);
  const [collapsedCategories, setCollapsedCategories] = useState<Set<string>>(() => new Set());
  const [listOpen, setListOpen] = useState(true);
  const editorRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLElement>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [linkCopy, setLinkCopy] = useState<{ id: number; status: 'copied' | 'failed' }>();
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [latest, setLatest] = useState<WikiDocument>();
  const [revisions, setRevisions] = useState<Revision[]>([]);
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');
  const [diff, setDiff] = useState<Diff>();
  const [compared, setCompared] = useState<{ from: string; to: string }>();
  const [aliases, setAliases] = useState<string>();
  const [savedAliases, setSavedAliases] = useState<string>();
  const dirty = (doc ? (draft.title !== doc.title || draft.content !== doc.content || draft.category !== doc.category)
    : Boolean(draft.title || draft.content || draft.category !== 'general')) || (aliases !== undefined && aliases !== savedAliases);

  const list = useCallback(async () => {
    setDocs(await request<WikiSummary[]>('/wiki/documents'));
  }, []);
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const capability = await request<{ store_id: 'integrated' | 'legacy_external' }>('/wiki/capabilities');
        if (cancelled) return;
        setMode(capability.store_id);
        if (capability.store_id === 'integrated') {
          const documents = await request<WikiSummary[]>('/wiki/documents');
          if (!cancelled) setDocs(documents);
        }
      } catch (caught) { if (!cancelled) { setMode('error'); setError(message(caught)); } }
    };
    void load();
    return () => { cancelled = true; };
  }, []);

  const exclusive = async (task: () => Promise<void>) => {
    if (busyRef.current) return;
    busyRef.current = true; setBusy(true); setError(''); setNotice('');
    try { await task(); } catch (caught) { setError(message(caught)); }
    finally { busyRef.current = false; setBusy(false); }
  };
  const allowDiscard = () => !dirty || window.confirm('저장하지 않은 변경사항을 버릴까요?');
  const resetHistory = () => { setRevisions([]); setFrom(''); setTo(''); setDiff(undefined); setCompared(undefined); setLatest(undefined); setAliases(undefined); setSavedAliases(undefined); };
  const revealCategory = (category: string) => setCollapsedCategories(current => {
    const key = category?.trim() || '미분류'; if (!current.has(key)) return current;
    const next = new Set(current); next.delete(key); return next;
  });
  const open = (id: number) => {
    if (id === doc?.id) {
      if (activeSection !== 'read') goSection('read');
      else { setListOpen(false); editorRef.current?.focus(); }
      return;
    }
    if (!allowDiscard()) return;
    void exclusive(async () => {
      const selected = await request<WikiDocument>(docPath(id));
      setDoc(selected); setDraft({ title: selected.title, content: selected.content, category: selected.category }); revealCategory(selected.category); resetHistory(); setActiveSection('read'); setManagementOpen(false); setEditing(false); setCreating(false); setListOpen(false); onNavigate?.(selected.id);
      if (window.matchMedia?.('(max-width: 700px)').matches) editorRef.current?.focus();
    });
  };
  useEffect(() => {
    if (mode !== 'integrated') return;
    if (documentId === doc?.id) {
      if (!doc || section === activeSection) return;
      if (!allowDiscard()) { onNavigate?.(doc.id, activeSection, true); return; }
      setDraft({ title: doc.title, category: doc.category, content: doc.content }); setAliases(savedAliases); setLatest(undefined);
      setEditing(section === 'edit'); setCreating(false); setActiveSection(section); setManagementOpen(false); setListOpen(false);
      return;
    }
    if (!allowDiscard()) { onNavigate?.(doc?.id, activeSection, true); return; }
    if (documentId === undefined) { setDoc(undefined); setDraft(emptyDraft()); resetHistory(); setEditing(false); setActiveSection('read'); setManagementOpen(false); setCreating(false); return; }
    void exclusive(async () => {
      const selected = await request<WikiDocument>(docPath(documentId));
      setDoc(selected); setDraft({ title: selected.title, content: selected.content, category: selected.category }); revealCategory(selected.category); resetHistory(); setEditing(section === 'edit'); setActiveSection(section); setManagementOpen(false); setCreating(false); setListOpen(false);
    });
  }, [documentId, section, mode]);
  const save = (event: FormEvent) => {
    event.preventDefault();
    if (aliases !== undefined && aliases !== savedAliases) { setError('별칭을 먼저 저장하거나 문서를 다시 열어 주세요.'); return; }
    void exclusive(async () => {
      try {
        const result = await request<{ id: number; revision: number }>(doc ? docPath(doc.id) : '/wiki/documents', {
          method: doc ? 'PUT' : 'POST', body: JSON.stringify({ ...draft, ...(doc ? { expected_revision: doc.revision } : {}) }),
        });
        // Save completed: use our submitted content even if a subsequent refresh fails.
        setDoc({ ...draft, id: result.id, revision: result.revision, updated_at: '', source: doc?.source });
        resetHistory(); revealCategory(draft.category); setEditing(false); setActiveSection('read'); setManagementOpen(false); setCreating(false); onNavigate?.(result.id); setNotice('저장했습니다.');
        try { await list(); } catch { setNotice('저장했지만 문서 목록을 새로고침하지 못했습니다.'); }
      } catch (caught) {
        if (caught instanceof ApiError && caught.status === 409 && doc) {
          try { setLatest(await request<WikiDocument>(docPath(doc.id))); setError('다른 변경이 먼저 저장되었습니다. 최신본과 초안을 비교해 주세요.'); }
          catch { setError('충돌이 발생했고 최신 문서를 가져오지 못했습니다. 초안은 유지됩니다.'); }
          return;
        }
        throw caught;
      }
    });
  };
  const remove = () => {
    if (!doc || !window.confirm(dirty ? '저장하지 않은 변경사항을 버리고 이 문서와 저장된 revision 이력을 삭제할까요?' : '이 문서와 저장된 revision 이력을 삭제할까요?')) return;
    void exclusive(async () => {
      await request(`${docPath(doc.id)}&expected_revision=${doc.revision}`, { method: 'DELETE' });
      setDoc(undefined); setDraft(emptyDraft()); resetHistory(); setEditing(false); setActiveSection('read'); setManagementOpen(false); setCreating(false); setListOpen(true); onNavigate?.(); setNotice('삭제했습니다.');
      try { await list(); } catch { setNotice('삭제했지만 문서 목록을 새로고침하지 못했습니다.'); }
    });
  };
  const search = (event: FormEvent) => {
    event.preventDefault();
    void exclusive(async () => { if (!query.trim()) { await list(); setCollapsedCategories(new Set()); setSearched(false); return; }
      setDocs(await request<WikiSummary[]>(`/wiki/search?q=${encodeURIComponent(query.trim())}`)); setCollapsedCategories(new Set()); setSearched(true); });
  };
  const loadRevisions = () => {
    if (!doc) return;
    void exclusive(async () => {
      const result = await request<Revision[]>(`/wiki/documents/${doc.id}/revisions?store_id=integrated`);
      setRevisions(result); setFrom(String(result[1]?.revision ?? '')); setTo(String(result[0]?.revision ?? '')); setDiff(undefined); setCompared(undefined);
    });
  };
  const loadDiff = () => {
    if (!doc || !from || !to || from === to) return;
    void exclusive(async () => {
      const result = await request<Diff>(`/wiki/documents/${doc.id}/diff?store_id=integrated&from_revision=${encodeURIComponent(from)}&to_revision=${encodeURIComponent(to)}`);
      setDiff(result); setCompared({ from, to });
    });
  };
  const loadAliases = () => {
    if (!doc) return;
    void exclusive(async () => { const text = (await request<string[]>(`/wiki/documents/${doc.id}/aliases?store_id=integrated`)).join('\n'); setAliases(text); setSavedAliases(text); });
  };
  const saveAliases = () => {
    if (!doc || aliases === undefined) return;
    void exclusive(async () => {
      try {
        const result = await request<{ revision: number; aliases: string[] }>(`/wiki/documents/${doc.id}/aliases?store_id=integrated`, {
          method: 'PUT', body: JSON.stringify({ aliases: aliases.split('\n').map(value => value.trim()).filter(Boolean), expected_revision: doc.revision }),
        });
        setDoc({ ...doc, revision: result.revision }); setAliases(result.aliases.join('\n')); setSavedAliases(result.aliases.join('\n')); setRevisions([]); setDiff(undefined); setCompared(undefined); setNotice('별칭을 저장했습니다.');
      } catch (caught) {
        if (caught instanceof ApiError && caught.status === 409) {
          setError('문서가 변경되어 별칭을 저장하지 못했습니다. 별칭 입력을 복사한 뒤 최신 문서를 열어 주세요.'); return;
        }
        throw caught;
      }
    });
  };

  useEffect(() => {
    if (!doc || (activeSection !== 'aliases' && activeSection !== 'history')) { loadAttempt.current = ''; return; }
    if (busy) return;
    const key = `${doc.id}:${activeSection}`;
    if (loadAttempt.current === key) return;
    loadAttempt.current = key;
    if (activeSection === 'aliases' && aliases === undefined) loadAliases();
    if (activeSection === 'history' && revisions.length === 0) loadRevisions();
  }, [doc?.id, activeSection, editing, busy]);
  const copyDocumentLink = async () => {
    if (!doc) return;
    const id = doc.id;
    try { await copyText(new URL('#/wiki/' + id, window.location.href).href); setLinkCopy({ id, status: 'copied' }); }
    catch { setLinkCopy({ id, status: 'failed' }); }
  };
  const goSection = (next: WikiSection) => {
    if (!doc || busyRef.current || next === activeSection) return;
    if (!allowDiscard()) return;
    setDraft({ title: doc.title, category: doc.category, content: doc.content }); setAliases(savedAliases); setLatest(undefined);
    setEditing(next === 'edit'); setCreating(false); setActiveSection(next); setManagementOpen(false); setListOpen(false);
    onNavigate?.(doc.id, next);
  };

  const categories = new Map<string, WikiSummary[]>();
  for (const item of docs) {
    const category = item.category?.trim() || '미분류';
    categories.set(category, [...(categories.get(category) || []), item]);
  }
  const toggleCategory = (category: string) => setCollapsedCategories(current => {
    const next = new Set(current); if (next.has(category)) next.delete(category); else next.add(category); return next;
  });

  const startNew = () => {
    if (!allowDiscard()) return;
    setDoc(undefined); setDraft(emptyDraft()); resetHistory(); setActiveSection('edit'); setManagementOpen(false); setEditing(true); setCreating(true);
    onNavigate?.(); setError(''); setNotice(''); setListOpen(false);
    if (window.matchMedia?.('(max-width: 700px)').matches) editorRef.current?.focus();
  };
  const cancelEdit = () => {
    if (!allowDiscard()) return;
    setDraft(doc ? { title: doc.title, category: doc.category, content: doc.content } : emptyDraft());
    setAliases(savedAliases); setLatest(undefined); setError(''); setEditing(false); setActiveSection('read'); setManagementOpen(false); setCreating(false);
    if (doc) onNavigate?.(doc.id); else setListOpen(true);
  };

  return <section className="wiki-manager" aria-labelledby="wiki-manager-title">
    <header className="wiki-topbar"><h2 id="wiki-manager-title">공유 Wiki</h2></header>
    {mode === 'loading' && <p role="status">Wiki를 확인하고 있습니다.</p>}
    {mode === 'legacy_external' && <p role="status">통합 Wiki가 아직 활성화되지 않았습니다. 기존 <a href="/wiki/">가족 Wiki</a>를 이용하세요.</p>}
    {error && <p className="error wiki-feedback" role="alert">{error}</p>}{notice && <p className="wiki-feedback" role="status">{notice}</p>}
    {mode === 'integrated' && <div className={'wiki-manager-layout' + (listOpen ? ' wiki-show-list' : ' wiki-show-document')}>
      <aside ref={listRef} aria-label="Wiki 문서 목록" className="wiki-list">
        <div id="wiki-list-content" className="wiki-list-content">{editing && <Button className="wiki-return-draft" type="button" variant="outlined" onClick={() => { setListOpen(false); editorRef.current?.focus(); }}>작성 중인 문서로 돌아가기</Button>}<form className="wiki-search" onSubmit={search}><label><span className="wiki-visually-hidden">문서 검색</span><Search size={17} aria-hidden="true" /><input placeholder="문서 검색" value={query} onChange={event => setQuery(event.target.value)} maxLength={4000} /></label><Button type="submit" variant="outlined" disabled={busy}>검색</Button></form>
        {searched && <div className="wiki-search-status"><span>검색 결과 {docs.length}개</span><button type="button" className="wiki-clear-search" disabled={busy} onClick={() => { setQuery(''); void exclusive(async () => { await list(); setCollapsedCategories(new Set()); setSearched(false); }); }}>전체 보기</button></div>}
        <Button className="wiki-new-button" type="button" variant="contained" startIcon={<Plus size={17} />} disabled={busy} onClick={startNew}>새 문서</Button>
        <div className="wiki-list-heading"><h3>문서 <span>{docs.length}</span></h3></div>
        <div className="wiki-category-list">{Array.from(categories, ([category, items]) => <section className="wiki-category" key={category}>
          <button type="button" className="wiki-category-toggle" aria-expanded={!collapsedCategories.has(category)} onClick={() => toggleCategory(category)}><Folder size={16} aria-hidden="true" /><span>{category}</span><small>{items.length}</small><ChevronDown className="wiki-category-chevron" size={16} aria-hidden="true" /></button>
          {!collapsedCategories.has(category) && <ul>{items.map(item => <li key={item.id}><a className={doc?.id === item.id ? 'wiki-selected' : undefined} aria-current={doc?.id === item.id ? 'page' : undefined} role="button" aria-label={item.title} href={'#/wiki/' + item.id} onClick={event => { if (!event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey && event.button === 0) { event.preventDefault(); if (!busy) open(item.id); } }}><FileText size={16} aria-hidden="true" /><span><strong>{item.title}</strong><small>버전 {item.revision}</small></span></a></li>)}</ul>}
        </section>)}</div>{docs.length === 0 && <div className="wiki-empty-list"><BookOpen size={22} /><p>{searched ? '검색 결과가 없습니다.' : '아직 문서가 없습니다.'}</p><small>{searched ? '다른 검색어로 찾아보세요.' : '첫 번째 문서를 만들어 보세요.'}</small></div>}</div>
      </aside>
      <div className="wiki-manager-editor" ref={editorRef} tabIndex={-1}>
        <Button className="wiki-mobile-return" type="button" startIcon={<ArrowLeft size={16} />} onClick={() => { setListOpen(true); listRef.current?.scrollIntoView?.({ block: 'start' }); }}>문서 목록</Button>
        {!editing && !doc && <div className="wiki-welcome"><div className="wiki-welcome-icon"><BookOpen size={26} /></div><h3>함께 쌓아가는 이야기</h3><p>문서를 선택해 내용을 살펴보거나<br />새 문서를 만들어 가족과 공유하세요.</p><Button variant="contained" startIcon={<Plus size={17} />} onClick={startNew}>새 문서 만들기</Button></div>}
        {!editing && doc && activeSection === 'read' && <article className="wiki-reader"><div className="wiki-reader-top"><div className="wiki-breadcrumb"><BookOpen size={15} /> 공유 Wiki <span>/</span> {doc.category || 'general'}</div><div className="wiki-reader-actions"><Button type="button" className="wiki-copy-link" startIcon={<Link2 size={15} />} onClick={() => void copyDocumentLink()}>{linkCopy?.id === doc.id && linkCopy.status === 'copied' ? '복사됨' : '문서 링크'}</Button><Button variant="contained" startIcon={<Pencil size={15} />} disabled={busy} onClick={() => goSection('edit')}>편집</Button><Button type="button" variant="outlined" disabled={busy} onClick={() => goSection('aliases')}>별칭</Button><Button type="button" variant="outlined" disabled={busy} onClick={() => goSection('history')}>버전 이력</Button></div></div>
          {linkCopy?.id === doc.id && <div className="wiki-link-feedback" role="status">{linkCopy.status === 'copied' ? '문서 링크를 복사했습니다.' : <label>자동 복사에 실패했습니다. 주소를 선택해 복사하세요.<input aria-label="복사할 문서 링크" readOnly value={new URL('#/wiki/' + doc.id, window.location.href).href} onFocus={event => event.currentTarget.select()} /></label>}</div>}
          <h3>{doc.title}</h3><div className="wiki-doc-meta"><span>버전 {doc.revision}</span>{doc.updated_at && <span>수정 {doc.updated_at}</span>}</div>{doc.source && <p className="wiki-source">사용자 제공 출처: {doc.source}</p>}
          <div className="wiki-reader-body markdown-content"><MarkdownContent content={doc.content} onWikiNavigate={open} /></div>
        </article>}
        {editing && <form className="wiki-editor-form" onSubmit={save}>
        <div className="wiki-editor-heading"><div><small>{creating ? '새 문서' : '문서 편집'}</small><h3>{creating ? '새로운 문서 작성' : doc?.title}</h3>{doc && <p className="wiki-editor-version">버전 {doc.revision}</p>}</div><span className={'wiki-edit-state' + (dirty ? ' is-dirty' : '')}>{dirty ? '저장되지 않은 변경' : creating ? '새 초안' : '저장됨'}</span></div>
        <div className="wiki-fields"><label><span>제목</span><input value={draft.title} maxLength={200} required onChange={event => setDraft({ ...draft, title: event.target.value })} placeholder="문서 제목을 입력하세요" /></label>
        <label><span>분류</span><input value={draft.category} maxLength={80} onChange={event => setDraft({ ...draft, category: event.target.value })} /></label></div>
        <div className="wiki-content-heading"><div><strong>본문</strong><small>마크다운으로 작성할 수 있습니다.</small></div><Button size="small" type="button" aria-pressed={preview} onClick={() => setPreview(value => !value)}>{preview ? '미리보기 숨기기' : '미리보기 보기'}</Button></div><div className={preview ? 'wiki-content-layout' : 'wiki-content-layout no-preview'}><label><span className="wiki-field-caption">마크다운 원문</span><textarea aria-label="본문" value={draft.content} maxLength={100000} required onChange={event => setDraft({ ...draft, content: event.target.value })} placeholder="내용을 입력하세요" /></label>{preview && <section className="wiki-preview" aria-label="마크다운 미리보기"><span className="wiki-field-caption">미리보기</span><div className="markdown-content">{draft.content ? <MarkdownContent content={draft.content} onWikiNavigate={open} /> : <p className="muted">본문을 작성하면 여기에 표시됩니다.</p>}</div></section>}</div>
        <div className="wiki-actions"><Button type="submit" variant="contained" disabled={busy || !!latest}>저장</Button><Button type="button" variant="outlined" startIcon={<X size={16} />} disabled={busy} onClick={cancelEdit}>취소</Button></div>
      </form>}
      {doc && editing && <details className="wiki-history wiki-editor-management" open={managementOpen} onToggle={event => setManagementOpen(event.currentTarget.open)}><summary><span className="wiki-management-title">문서 관리 <small>문서 삭제</small></span><ChevronDown size={18} aria-hidden="true" /></summary><div className="wiki-management"><section className="wiki-management-section wiki-danger-zone"><h3>문서 삭제</h3><p>문서와 보관된 버전 이력이 삭제됩니다.</p><Button type="button" color="error" variant="outlined" disabled={busy} onClick={remove}>문서 삭제</Button></section></div></details>}
      {doc && activeSection === 'aliases' && <section className="wiki-subpage" aria-labelledby="wiki-alias-page-title"><div className="wiki-subpage-heading"><div><small>{doc.title} · 버전 {doc.revision}</small><h3 id="wiki-alias-page-title">문서 별칭</h3><p>검색에 사용할 이름을 한 줄에 하나씩 등록합니다.</p></div></div>
        {aliases === undefined ? <Button type="button" variant="outlined" disabled={busy} onClick={loadAliases}>{busy ? '불러오는 중' : '별칭 다시 불러오기'}</Button> : <><label>별칭 목록<textarea value={aliases} maxLength={4000} onChange={event => setAliases(event.target.value)} /></label><p className="wiki-subpage-hint">별칭은 문서 본문과 별도로 저장됩니다.</p><div className="wiki-actions"><Button type="button" variant="contained" disabled={busy || !!latest} onClick={saveAliases}>별칭 저장</Button><Button type="button" variant="outlined" disabled={busy} onClick={() => goSection('read')}>문서로 돌아가기</Button></div></>}
        {aliases === undefined && <Button type="button" disabled={busy} onClick={() => goSection('read')}>문서로 돌아가기</Button>}
      </section>}
      {doc && activeSection === 'history' && <section className="wiki-subpage" aria-labelledby="wiki-history-page-title"><div className="wiki-subpage-heading"><div><small>{doc.title} · 버전 {doc.revision}</small><h3 id="wiki-history-page-title">버전 이력</h3><p>최근 100개 버전이 보관됩니다. 문서를 삭제하면 이력도 삭제됩니다.</p></div></div>
        {revisions.length === 0 && <Button type="button" variant="outlined" disabled={busy} onClick={loadRevisions}>{busy ? '불러오는 중' : '이력 다시 불러오기'}</Button>}
        {revisions.length > 0 && <>
          <div className="wiki-history-toolbar"><div><strong>변경 이력</strong><span>비교할 이전 버전과 비교 버전을 선택하세요.</span></div><Button type="button" variant="outlined" disabled={busy} onClick={loadRevisions}>새로고침</Button></div>
          <div className="wiki-revision-list" role="group" aria-label="비교할 버전 선택">
            <div className="wiki-revision-header" aria-hidden="true"><span>버전</span><span>이전</span><span>비교</span></div>
            {revisions.map((revision, index) => <div className="wiki-revision-row" key={revision.revision}>
              <div className="wiki-revision-info"><strong>r{revision.revision}</strong>{index === 0 && <span className="wiki-revision-latest">최신</span>}<time>{revisionDate(revision.created_at)}</time></div>
              <label><span className="wiki-visually-hidden">이전 버전 r{revision.revision}</span><input type="radio" name="wiki-history-from" checked={from === String(revision.revision)} disabled={busy} onChange={() => { setFrom(String(revision.revision)); setDiff(undefined); setCompared(undefined); }} /></label>
              <label><span className="wiki-visually-hidden">비교 버전 r{revision.revision}</span><input type="radio" name="wiki-history-to" checked={to === String(revision.revision)} disabled={busy} onChange={() => { setTo(String(revision.revision)); setDiff(undefined); setCompared(undefined); }} /></label>
            </div>)}
          </div>
          <div className="wiki-history-compare"><div><strong>{from ? `r${from}` : '이전 버전 선택'} → {to ? `r${to}` : '비교 버전 선택'}</strong>{revisions.length < 2 ? <small>비교하려면 두 개 이상의 버전이 필요합니다.</small> : from === to ? <small>서로 다른 두 버전을 선택하세요.</small> : Number(from) > Number(to) ? <small>이전 버전이 더 최신입니다. 선택한 순서대로 비교합니다.</small> : <small>왼쪽은 이전, 오른쪽은 비교 버전입니다.</small>}</div><Button type="button" variant="contained" disabled={busy || !from || !to || from === to} onClick={loadDiff}>차이 보기</Button></div>
          {diff && compared && <section className="wiki-diff-result" aria-label="버전 차이"><div className="wiki-diff-title"><h4>버전 차이</h4><span>r{compared.from} → r{compared.to}</span></div><p className="wiki-diff-legend"><span>− 삭제</span><span>＋ 추가</span></p>{(['title', 'category', 'content'] as const).map(field => <div className="wiki-diff-field" key={field}><h4>{{ title: '제목', category: '분류', content: '본문' }[field]}</h4><DiffText text={diff.diff[field]} /></div>)}</section>}
        </>}
        <Button type="button" variant="outlined" disabled={busy} onClick={() => goSection('read')}>문서로 돌아가기</Button>
      </section>}
      {latest && <section className="wiki-conflict" aria-label="문서 충돌 비교"><h3>최신 저장본과 내 초안</h3><p>내 초안은 저장되지 않았습니다. 차이를 확인하고 최신 revision을 기준으로 다시 저장할 수 있습니다.</p>
        {(['title', 'category', 'content'] as const).map(field => <div key={field}><h4>{{ title: '제목', category: '분류', content: '본문' }[field]}</h4><ChangedText before={latest[field]} after={draft[field]} /></div>)}
        <button type="button" disabled={busy} onClick={() => { setDoc(latest); setLatest(undefined); setNotice('최신 revision을 기준으로 초안을 편집합니다. 저장을 눌러야 반영됩니다.'); }}>최신본 기준으로 계속 편집</button></section>}
      </div>
    </div>}
  </section>;
}
