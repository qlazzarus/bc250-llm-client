import { FormEvent, useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { createRoot } from 'react-dom/client';
import { createTheme, ThemeProvider } from '@mui/material/styles';
import Drawer from '@mui/material/Drawer';
import { BookOpen, Menu, Plus, Activity, Settings, Trash2, Link2, MessageSquare, X } from 'lucide-react';
import { ApiError, newRequestId, request, streamRequest, type Conversation, type Generation, type ImageCapabilities, type ImageDraft, type ImageJob, type ImagePresetName, type Message, type MessagePage, type Mode, type ModeSettings, type PromptMode, type ResponseLength, type WikiMemoryJob } from './api';
import { MessageList } from './components/MessageList';
import { SystemStatus } from './components/SystemStatus';
import { WikiMemoryStatus } from './components/WikiMemoryStatus';
import { WikiManager } from './components/WikiManager';
import './image-prompt.css';
import './style.css';
import './scroll.css';
import './workspace.css';

type Detail = Conversation & { messages: Message[]; nextBefore: string | null; hasMore: boolean };
type RetryInfo = { conversationId: string; content: string; requestId: string; message: string; promptMode?: PromptMode; imageModelId?: string; epochMismatch?: boolean };
type InFlightGeneration = { conversationId: string; requestId: string; controller: AbortController };
export type ImageDraftEditor = Pick<ImageDraft, 'prompt' | 'width' | 'height' | 'generation_config' | 'revision' | 'preset_name'> & { persisted: boolean; dirty: boolean };

const MODE_META: Record<Mode, { label: string; description: string; fields: Array<[string, string, string]> }> = {
  quick: { label: '일반 대화', description: '최근 대화와 장기 기억 요약을 참고합니다.', fields: [] },
  roleplay: { label: '롤플레잉', description: '설정과 최근 완료 대화를 이어 갑니다.', fields: [['character', '캐릭터', '이름·역할·성격'], ['world', '세계관', '배경과 규칙'], ['voice', '말투', '예: 차분한 존댓말'], ['scenario', '현재 상황', '시작 장면'], ['rules', '응답 규칙', '지켜야 할 조건']] },
  image_prompt: { label: '이미지 프롬프트', description: '한 번의 요청을 영어 프롬프트와 설정 JSON으로 변환합니다. 이미지는 직접 생성하지 않습니다.', fields: [] },
};
const appTheme = createTheme({ palette: { mode: 'dark', primary: { main: '#83bff4' }, background: { default: '#101721', paper: '#192532' } }, typography: { fontFamily: 'Inter, -apple-system, BlinkMacSystemFont, sans-serif' }, shape: { borderRadius: 10 }, components: { MuiButton: { defaultProps: { disableElevation: true }, styleOverrides: { root: { textTransform: 'none', fontWeight: 600 } } } } });
const LENGTH_LABEL: Record<ResponseLength, string> = { short: '짧게', medium: '보통', long: '길게' };

function ModeFields({ mode, value, onChange }: { mode: Mode; value: ModeSettings; onChange: (settings: ModeSettings) => void }) {
  return <div className="mode-fields">{MODE_META[mode].fields.map(([key, label, placeholder]) => <label key={key}>{label}
    <textarea value={value[key] || ''} placeholder={placeholder} onChange={event => onChange({ ...value, [key]: event.target.value })} />
  </label>)}</div>;
}

function Modal({ children, titleId, onClose }: { children: ReactNode; titleId: string; onClose: () => void }) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = dialogRef.current;
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    dialog?.showModal();
    return () => { dialog?.close(); previous?.focus(); };
  }, []);
  return <dialog ref={dialogRef} className="modal-dialog" aria-labelledby={titleId} onCancel={event => { event.preventDefault(); onClose(); }}>{children}</dialog>;
}

function NewConversationDialog({ onClose, onCreate }: { onClose: () => void; onCreate: (data: { title: string; mode: Mode; response_length: ResponseLength; mode_settings: ModeSettings }) => Promise<void> }) {
  const [title, setTitle] = useState('새 대화');
  const [mode, setMode] = useState<Mode>('quick');
  const [responseLength, setResponseLength] = useState<ResponseLength>('medium');
  const [settings, setSettings] = useState<ModeSettings>({});
  const [error, setError] = useState('');
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    try { await onCreate({ title, mode, response_length: responseLength, mode_settings: settings }); }
    catch (caught) { setError(caught instanceof Error ? caught.message : '대화를 만들지 못했습니다.'); }
  };
  return <Modal titleId="new-dialog-title" onClose={onClose}><form className="modal" onSubmit={submit}>
    <header><h2 id="new-dialog-title">새 대화</h2><button type="button" className="plain" onClick={onClose}>닫기</button></header>
    <label>제목<input value={title} maxLength={120} onChange={event => setTitle(event.target.value)} required /><small className="muted">기본 제목은 첫 답변 후 자동으로 정해집니다.</small></label>
    <fieldset><legend>용도</legend>{(Object.keys(MODE_META) as Mode[]).map(option =>
      <label className="mode-option" key={option}><input type="radio" checked={mode === option} onChange={() => { setMode(option); setSettings({}); }} />
        <span><b>{MODE_META[option].label}</b><small>{MODE_META[option].description}</small></span>
      </label>)}</fieldset>
    {mode !== 'image_prompt' && <label>응답 길이<select value={responseLength} onChange={event => setResponseLength(event.target.value as ResponseLength)}>
      {(Object.keys(LENGTH_LABEL) as ResponseLength[]).map(option => <option key={option} value={option}>{LENGTH_LABEL[option]}</option>)}
    </select></label>}
    {mode === 'image_prompt' && <p className="muted">원하는 장면과 모델을 입력하면 영어 프롬프트를 작성합니다. 생성 설정은 결과 카드에서 확인하고 변경할 수 있습니다.</p>}
    {mode !== 'image_prompt' && <ModeFields mode={mode} value={settings} onChange={setSettings} />}
    {error && <p className="error">{error}</p>}<button type="submit">대화 만들기</button>
  </form></Modal>;
}

function ConversationSettings({ conversation, wikiEnabled, wikiAutoUpdate, onSaveWiki, onClose, onSave }: { conversation: Detail; wikiEnabled: boolean; wikiAutoUpdate: boolean; onSaveWiki: (search: boolean, auto: boolean) => void; onClose: () => void; onSave: (data: { title?: string; response_length?: ResponseLength; mode_settings?: ModeSettings; summary?: string }) => Promise<void> }) {
  const [title, setTitle] = useState(conversation.title);
  const [responseLength, setResponseLength] = useState<ResponseLength>(conversation.response_length);
  const [settings, setSettings] = useState<ModeSettings>(conversation.mode_settings);
  const [summary, setSummary] = useState(conversation.summary);
  const [wikiSearch, setWikiSearch] = useState(wikiEnabled);
  const [wikiAuto, setWikiAuto] = useState(wikiAutoUpdate);
  const [error, setError] = useState('');
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    try {
      await onSave(conversation.mode === 'image_prompt'
        ? { ...(title !== conversation.title ? { title } : {}) }
        : { ...(title !== conversation.title ? { title } : {}), response_length: responseLength, mode_settings: settings, ...(summary !== conversation.summary ? { summary } : {}) });
      if (conversation.mode === 'quick') onSaveWiki(wikiSearch, wikiAuto);
      onClose();
    }
    catch (caught) { setError(caught instanceof Error ? caught.message : '설정을 저장하지 못했습니다.'); }
  };
  return <Modal titleId="settings-dialog-title" onClose={onClose}><form className="modal" onSubmit={submit}>
    <header><h2 id="settings-dialog-title">대화 설정</h2><button type="button" className="plain" onClick={onClose}>닫기</button></header>
    <p className="muted">모드: {MODE_META[conversation.mode].label} · 생성 후에는 모드를 변경할 수 없습니다.</p>
    <label>제목<input value={title} maxLength={120} onChange={event => setTitle(event.target.value)} required /></label>
    {conversation.mode !== 'image_prompt' && <label>응답 길이<select value={responseLength} onChange={event => setResponseLength(event.target.value as ResponseLength)}>
      {(Object.keys(LENGTH_LABEL) as ResponseLength[]).map(option => <option key={option} value={option}>{LENGTH_LABEL[option]}</option>)}
    </select></label>}
    {conversation.mode === 'image_prompt' && <p className="muted">매 요청은 독립적으로 작성됩니다. 원하는 장면을 전체 문장으로 입력하세요. 기존 기록은 보이지만 생성에 사용되지 않습니다.</p>}
    {conversation.mode !== 'image_prompt' && <ModeFields mode={conversation.mode} value={settings} onChange={setSettings} />}
    {conversation.mode === 'quick' && <fieldset className="wiki-settings"><legend>공유 Wiki</legend>
      <label className="wiki-setting-option"><input type="checkbox" checked={wikiSearch} onChange={event => setWikiSearch(event.target.checked)} /><span><b>Wiki 검색 참고</b><small>답변할 때 공유 Wiki에서 관련 문서를 찾습니다.</small></span></label>
      <label className="wiki-setting-option"><input type="checkbox" checked={wikiAuto} onChange={event => setWikiAuto(event.target.checked)} /><span><b>Wiki 자동 기억</b><small>켜면 사용자 발언에서 선택된 원문을 공유 Wiki에 저장할 수 있습니다. 명시적인 기억 요청은 꺼져 있어도 처리합니다.</small></span></label>
      <small className="muted">이 옵션은 현재 브라우저의 모든 일반 대화에 적용됩니다.</small>
    </fieldset>}
    {conversation.mode !== 'image_prompt' && <label>장기 기억 요약
      <textarea value={summary} maxLength={12000} onChange={event => setSummary(event.target.value)} placeholder="자동 요약된 대화의 핵심 내용입니다." />
      <small className="muted">{conversation.summary_status === 'pending' || conversation.summary_status === 'running' ? '장기 기억을 정리하고 있습니다. 저장하면 현재 자동 요약 작업을 취소합니다.' : '필요하면 직접 수정할 수 있습니다.'}</small>
    </label>}
    {error && <p className="error">{error}</p>}<button type="submit">저장</button>
  </form></Modal>;
}

type WikiMessageDraft = { conversationId: string; messageId: string; original: string };

type WikiCandidate = { id: number; title: string; revision: number; category: string };
type WikiCandidateDocument = WikiCandidate & { content: string; source?: string };
type WikiSavePayload = { request_id: string; conversation_id: string; message_id: string; title: string; content: string; source: string; action: 'create' | 'update'; document_id: number | null; expected_revision: number | null; acknowledged_duplicate_ids: number[] };

export function WikiDraftDialog({ source, onClose, onSaved }: { source: WikiMessageDraft; onClose: () => void; onSaved?: () => void }) {
  const initialTitle = Array.from(source.original.split(/\r?\n/).map(line => line.trim()).find(Boolean)?.replace(/\s+/g, ' ') || '대화 자료').slice(0, 200).join('');
  const [title, setTitle] = useState(initialTitle);
  const [content, setContent] = useState(source.original);
  const [sourceText, setSourceText] = useState('');
  const [candidates, setCandidates] = useState<{ matches: WikiCandidate[]; similar: WikiCandidate[] }>();
  const [comparison, setComparison] = useState<WikiCandidateDocument>();
  const [target, setTarget] = useState<WikiCandidateDocument>();
  const [pending, setPending] = useState<WikiSavePayload>();
  const [result, setResult] = useState<{ id: number; revision: number; status: string }>();
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const dirty = title !== initialTitle || content !== source.original || Boolean(sourceText);
  useEffect(() => {
    if (result) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [result]);
  const close = () => { if (busyRef.current) return; if (pending ? window.confirm('저장 결과를 아직 확인하지 못했습니다. 닫으면 이 요청으로 재확인할 수 없습니다. 닫을까요?') : result || !dirty || window.confirm('편집한 초안을 버릴까요?')) onClose(); };
  const review = async () => {
    if (busyRef.current || !title.trim() || !content.trim()) { if (!title.trim() || !content.trim()) setError('제목과 본문을 입력해 주세요.'); return; }
    busyRef.current = true; setBusy(true); setError('');
    try { setCandidates(await request(`/wiki/message-drafts/candidates?title=${encodeURIComponent(title)}`)); setComparison(undefined); setTarget(undefined); }
    catch (caught) { setError(caught instanceof Error ? caught.message : '중복 후보를 불러오지 못했습니다.'); }
    finally { busyRef.current = false; setBusy(false); }
  };
  const compare = async (item: WikiCandidate) => {
    if (busyRef.current) return;
    busyRef.current = true; setBusy(true); setError('');
    try { setComparison(await request<WikiCandidateDocument>(`/wiki/documents/${item.id}?store_id=integrated`)); }
    catch (caught) { setError(caught instanceof Error ? caught.message : '문서를 불러오지 못했습니다.'); }
    finally { busyRef.current = false; setBusy(false); }
  };
  const send = async (payload: WikiSavePayload) => {
    if (busyRef.current) return;
    busyRef.current = true; setBusy(true); setError(''); setPending(payload);
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 15000);
    try {
      const saved = await request<{ id: number; revision: number; status: string }>('/wiki/message-drafts/save', { method: 'POST', body: JSON.stringify(payload), signal: controller.signal });
      setResult(saved); setPending(undefined); onSaved?.();
    } catch (caught) {
      if (caught instanceof ApiError && caught.status < 500) {
        setPending(undefined);
        if (caught.code === 'wiki_conflict') { setTarget(undefined); setComparison(undefined); setCandidates(undefined); setError('문서가 변경되었습니다. 초안을 유지했습니다. 중복 후보와 최신 문서를 다시 확인해 주세요.'); }
        else if (caught.code === 'wiki_duplicate_title') { setCandidates(undefined); setError('같은 제목의 문서가 생겼습니다. 중복 후보를 다시 확인해 주세요.'); }
        else setError(caught.message);
      } else setError('저장 결과 확인 필요: 응답을 받지 못했습니다. 같은 요청으로 결과를 확인해 주세요.');
    } finally { window.clearTimeout(timeout); busyRef.current = false; setBusy(false); }
  };
  const save = () => {
    if (!candidates || busyRef.current) return;
    void send({ request_id: newRequestId(), conversation_id: source.conversationId, message_id: source.messageId, title, content, source: sourceText,
      action: target ? 'update' : 'create', document_id: target?.id ?? null, expected_revision: target?.revision ?? null, acknowledged_duplicate_ids: candidates.matches.map(item => item.id) });
  };
  const invalidate = () => { setCandidates(undefined); setTarget(undefined); setComparison(undefined); setError(''); };
  return <Modal titleId="wiki-draft-title" onClose={close}><div className="modal wiki-draft">
    <header><h2 id="wiki-draft-title">공유 Wiki 문서 초안</h2><button type="button" className="plain" disabled={busy} onClick={close}>닫기</button></header>
    <p>저장하면 이 앱에 접근할 수 있는 가족에게 공유됩니다. 개인정보와 민감한 내용을 확인해 주세요.</p>
    <p>선택한 사용자 메시지가 원문입니다. 출처와 내용의 사실성은 자동으로 검증하지 않습니다.</p>
    <p>초안은 이 화면의 메모리에만 유지됩니다. 새로고침하면 사라지며, 닫거나 대화를 바꾸기 전에 편집 내용 버리기를 확인합니다.</p>
    <label>제목<input value={title} maxLength={200} disabled={busy || !!pending || !!result} onChange={event => { setTitle(event.target.value); invalidate(); }} /></label>
    <label>본문<textarea value={content} maxLength={100000} disabled={busy || !!pending || !!result} onChange={event => { setContent(event.target.value); invalidate(); }} /></label>
    <label>출처 (선택)<input value={sourceText} maxLength={4000} disabled={busy || !!pending || !!result} onChange={event => { setSourceText(event.target.value); invalidate(); }} /></label>
    {error && <p className="error" role="alert">{error}</p>}
    {result ? <p role="status">저장 완료 · 문서 {result.id} · revision {result.revision}</p> : pending ? <button type="button" disabled={busy} onClick={() => void send(pending)}>같은 요청으로 결과 확인</button> : <>
      <button type="button" disabled={busy} onClick={() => void review()}>중복 후보 확인</button>
      {candidates && <section aria-label="중복 후보"><h3>제목 일치 문서</h3>
        {candidates.matches.length === 0 && <p>일치하는 제목이 없습니다.</p>}
        {candidates.matches.map(item => <p key={item.id}>{item.title} · revision {item.revision} <button type="button" disabled={busy} onClick={() => void compare(item)}>비교</button></p>)}
        <h3>유사 검색 참고</h3><p>검색 결과가 없어도 중복이 없다는 뜻은 아닙니다.</p>
        {candidates.similar.map(item => <p key={item.id}>{item.title} <button type="button" disabled={busy} onClick={() => void compare(item)}>비교</button></p>)}
        {comparison && <div className="wiki-draft-comparison"><h3>기존 문서: {comparison.title} · revision {comparison.revision}</h3><pre>{comparison.content}</pre><p>사용자 제공 출처: {comparison.source || '없음'}</p><button type="button" disabled={busy} onClick={() => setTarget(comparison)}>이 문서를 초안 내용으로 편집</button></div>}
        <p>{target ? `기존 문서 ${target.id}의 revision ${target.revision}을 기준으로 저장합니다.` : '새 문서로 생성합니다. 기존 문서는 변경하지 않습니다.'}</p>
        {target && <button type="button" disabled={busy} onClick={() => setTarget(undefined)}>새 문서 생성 선택</button>}
        <button type="button" disabled={busy} onClick={save}>{target ? '기존 문서 변경 확인 및 저장' : '새 문서 생성 확인 및 저장'}</button>
      </section>}
    </>}
    <button type="button" className="plain" disabled={busy} onClick={close}>{result ? '닫기' : '취소'}</button>
  </div></Modal>;
}

export function App() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [active, setActive] = useState<Detail>();
  const [models, setModels] = useState<string[]>([]);
  const [modelsUnavailable, setModelsUnavailable] = useState(false);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const [retryInfo, setRetryInfo] = useState<RetryInfo>();
  const [promptMode, setPromptMode] = useState<PromptMode>('ai_optimized');
  const [imageModelId, setImageModelId] = useState('');
  const [gate, setGate] = useState<unknown>();
  const [gateError, setGateError] = useState('');
  const [gateLoading, setGateLoading] = useState(false);
  const [systemView, setSystemView] = useState(false);
  const [wikiView, setWikiView] = useState(false);
  const [wikiVisited, setWikiVisited] = useState(false);
  const [wikiDocumentId, setWikiDocumentId] = useState<number>();
  const [wikiEnabled, setWikiEnabled] = useState(() => { try { return localStorage.getItem('bc250-wiki-search') !== 'false'; } catch { return true; } });
  const [wikiAutoUpdate, setWikiAutoUpdate] = useState(() => { try { return localStorage.getItem('bc250-wiki-auto') !== 'false'; } catch { return true; } });
  const [wikiMemory, setWikiMemory] = useState<WikiMemoryJob[]>([]);
  const [newDialog, setNewDialog] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [settingsDialog, setSettingsDialog] = useState(false);
  const [wikiDraft, setWikiDraft] = useState<WikiMessageDraft>();
  const [dismissedWikiProposals, setDismissedWikiProposals] = useState<Set<string>>(() => new Set());
  const dismissWikiProposal = (conversationId: string, messageId: string) => setDismissedWikiProposals(current => new Set(current).add(JSON.stringify([conversationId, messageId])));
  const [imageCapabilities, setImageCapabilities] = useState<ImageCapabilities>();
  const [imageDrafts, setImageDrafts] = useState<Record<string, ImageDraftEditor>>({});
  const [imageJobs, setImageJobs] = useState<Record<string, ImageJob[]>>({});
  const imageDraftsRef = useRef<Record<string, ImageDraftEditor>>({});
  const imageDraftLoadingRef = useRef(new Set<string>());
  const [scrollSignal, setScrollSignal] = useState(0);
  const activeIdRef = useRef<string | undefined>(undefined);
  const inFlightRef = useRef<InFlightGeneration | undefined>(undefined);
  const refreshWikiMemoryRef = useRef<() => void>(() => {});

  useEffect(() => {
    if (!active || active.mode !== 'quick') { setWikiMemory([]); return; }
    let cancelled = false; let inFlight = false; let timer: number | undefined;
    let requestController: AbortController | undefined;
    const conversationId = active.id;
    const load = async () => {
      if (cancelled || inFlight || document.hidden) return;
      inFlight = true;
      const controller = new AbortController(); requestController = controller;
      const timeout = window.setTimeout(() => controller.abort(), 15000);
      try {
        const result = await request<{ data: WikiMemoryJob[] }>('/conversations/' + encodeURIComponent(conversationId) + '/wiki-memory?view=summary', { signal: controller.signal });
        if (!cancelled) setWikiMemory(result.data);
      } catch { /* Keep the last known status until the next successful request. */ }
      finally {
        window.clearTimeout(timeout); requestController = undefined;
        inFlight = false;
        if (!cancelled && !document.hidden) timer = window.setTimeout(() => void load(), 5000);
      }
    };
    const refresh = () => { if (timer !== undefined) window.clearTimeout(timer); void load(); };
    refreshWikiMemoryRef.current = refresh;
    const visibility = () => { if (document.hidden) { if (timer !== undefined) window.clearTimeout(timer); requestController?.abort(); } else refresh(); };
    document.addEventListener('visibilitychange', visibility);
    void load();
    return () => { cancelled = true; if (timer !== undefined) window.clearTimeout(timer); requestController?.abort(); document.removeEventListener('visibilitychange', visibility); refreshWikiMemoryRef.current = () => {}; };
  }, [active?.id, active?.mode]);

  const refresh = async () => {
    const [conversationResponse, modelResponse, capabilities] = await Promise.all([
      request<{ data: Conversation[] }>('/conversations'), request<{ data: { id: string }[] }>('/models').catch(() => ({ data: [] })),
      request<ImageCapabilities>('/image-generation/capabilities').catch(() => undefined),
    ]);
    setConversations(conversationResponse.data);
    setModels(modelResponse.data.map(model => model.id));
    setImageCapabilities(capabilities);
  };

  const refreshModels = useCallback(async () => {
    try {
      const result = await request<{ data: { id: string }[] }>('/models');
      setModels(result.data.map(model => model.id));
      setModelsUnavailable(false);
    } catch { setModels([]); setModelsUnavailable(true); }
  }, []);

  useEffect(() => {
    if (!modelsUnavailable) return;
    const timer = window.setInterval(() => void refreshModels(), 5000);
    return () => window.clearInterval(timer);
  }, [modelsUnavailable, refreshModels]);

  const ensureImageDraft = useCallback(async (conversationId: string, messageId: string, config: { prompt: string; width: number; height: number; preset_name?: ImagePresetName | null }) => {
    if (imageDraftsRef.current[messageId] || imageDraftLoadingRef.current.has(messageId)) return;
    imageDraftLoadingRef.current.add(messageId);
    try {
      const saved = await request<ImageDraft>('/conversations/' + conversationId + '/messages/' + messageId + '/image-draft');
      if (!imageDraftsRef.current[messageId]) {
        const next = { ...imageDraftsRef.current, [messageId]: { ...saved, persisted: true, dirty: false } };
        imageDraftsRef.current = next; setImageDrafts(next);
      }
    } catch {
      if (!imageDraftsRef.current[messageId]) {
        const next = { ...imageDraftsRef.current, [messageId]: { ...config, revision: 0, persisted: false, dirty: false } };
        imageDraftsRef.current = next; setImageDrafts(next);
      }
    } finally { imageDraftLoadingRef.current.delete(messageId); }
  }, []);

  const updateImageDraft = useCallback((messageId: string, update: Partial<Pick<ImageDraftEditor, 'prompt' | 'width' | 'height' | 'generation_config' | 'preset_name'>>, fallback: ImageDraftEditor) => {
    const currentDraft = imageDraftsRef.current[messageId] || fallback;
    const next = { ...imageDraftsRef.current, [messageId]: { ...currentDraft, ...update, dirty: true } };
    imageDraftsRef.current = next; setImageDrafts(next);
  }, []);

  const persistImageDraft = useCallback(async (conversationId: string, messageId: string, draft: ImageDraftEditor): Promise<ImageDraft> => {
    const saved = await request<ImageDraft>('/conversations/' + conversationId + '/messages/' + messageId + '/image-draft', {
      method: 'PUT', body: JSON.stringify({ prompt: draft.prompt, width: draft.width, height: draft.height, generation_config: draft.generation_config, preset_name: draft.preset_name ?? null, revision: draft.revision }),
    });
    const latest = imageDraftsRef.current[messageId];
      const unchanged = latest && latest.prompt === draft.prompt && latest.width === draft.width && latest.height === draft.height && latest.preset_name === draft.preset_name && JSON.stringify(latest.generation_config) === JSON.stringify(draft.generation_config);
      const next = { ...imageDraftsRef.current, [messageId]: unchanged
        ? { ...saved, persisted: true, dirty: false }
        : { ...(latest || draft), revision: saved.revision, persisted: true, dirty: true },
      };
    imageDraftsRef.current = next; setImageDrafts(next);
    return saved;
  }, []);

  const submitImageJob = useCallback(async (conversationId: string, messageId: string, draft: ImageDraftEditor, requestId: string): Promise<ImageJob> => {
    const saved = draft.dirty || !draft.persisted ? await persistImageDraft(conversationId, messageId, draft) : draft;
    const response = await request<{ data: ImageJob }>('/conversations/' + conversationId + '/image-jobs', {
      method: 'POST', body: JSON.stringify({
        source_message_id: messageId, prompt: saved.prompt, width: saved.width, height: saved.height,
        generation_config: saved.generation_config, draft_revision: saved.revision, request_id: requestId,
      }),
    });
    setImageJobs(current => ({ ...current, [conversationId]: [response.data, ...(current[conversationId] || []).filter(job => job.id !== response.data.id)] }));
    return response.data;
  }, [persistImageDraft]);

  const replaceImageJob = useCallback((job: ImageJob) => {
    setImageJobs(current => ({ ...current, [job.conversation_id]: [job, ...(current[job.conversation_id] || []).filter(item => item.id !== job.id)] }));
  }, []);

  const loadImageJobs = useCallback(async (conversationId: string) => {
    const response = await request<{ data: ImageJob[] }>('/conversations/' + conversationId + '/image-jobs');
    setImageJobs(current => ({ ...current, [conversationId]: response.data }));
  }, []);

  const cancelImageJob = useCallback(async (job: ImageJob) => {
    const response = await request<{ data: ImageJob }>('/image-jobs/' + job.id + '/cancel', { method: 'POST' });
    replaceImageJob(response.data);
  }, [replaceImageJob]);
  const loadConversation = async (id: string, select = false) => {
    if (select) {
      const hash = '#/chat/' + encodeURIComponent(id);
      if (window.location.hash !== hash) history.pushState(null, '', hash);
      activeIdRef.current = id;
      setSystemView(false); setWikiView(false);
      if (inFlightRef.current?.conversationId !== id) {
        setNotice('');
        setRetryInfo(current => {
          if (current?.conversationId === id) {
            setNotice(current.message);
            return current;
          }
          return undefined;
        });
      }
    }
    const [detail, page] = await Promise.all([request<Conversation>('/conversations/' + id), request<MessagePage>('/conversations/' + id + '/messages?limit=50')]);
    if (activeIdRef.current === id) setActive({ ...detail, messages: page.data, nextBefore: page.next_before, hasMore: page.has_more });
  };
  const loadOlderMessages = async () => {
    if (!active?.nextBefore) return;
    const conversationId = active.id;
    const page = await request<MessagePage>('/conversations/' + conversationId + '/messages?limit=50&before=' + encodeURIComponent(active.nextBefore));
    setActive(current => current?.id === conversationId ? {
      ...current,
      messages: [...page.data, ...current.messages.filter(message => !page.data.some(older => older.id === message.id))],
      nextBefore: page.next_before,
      hasMore: page.has_more,
    } : current);
  };
  useEffect(() => { refresh().catch(error => setNotice(error.message)); }, []);
  useEffect(() => {
    const route = () => {
      const hash = window.location.hash;
      const wiki = hash.match(/^#\/wiki(?:\/(\d+))?$/);
      const chat = hash.match(/^#\/chat\/([^/]+)$/);
      if (wiki) {
        setSystemView(false); setWikiVisited(true); setWikiView(true);
        setWikiDocumentId(wiki[1] ? Number(wiki[1]) : undefined);
      } else if (chat) {
        try { void loadConversation(decodeURIComponent(chat[1]), true).catch(error => setNotice(error instanceof Error ? error.message : '대화를 불러오지 못했습니다.')); }
        catch { setNotice('잘못된 대화 링크입니다.'); }
      } else if (!hash || hash === '#/') { setWikiView(false); setSystemView(false); }
    };
    route(); window.addEventListener('hashchange', route);
    return () => window.removeEventListener('hashchange', route);
  }, []);
  useEffect(() => {
    if (!active || !['pending', 'running'].includes(active.summary_status)) return;
    const conversationId = active.id;
    const timer = window.setTimeout(() => {
      request<Conversation>('/conversations/' + conversationId)
        .then(detail => setActive(current => current?.id === conversationId ? { ...current, ...detail } : current))
        .catch(() => undefined);
    }, 2000);
    return () => window.clearTimeout(timer);
  }, [active?.id, active?.summary_status]);
  useEffect(() => {
    if (!active?.title_pending) return;
    const conversationId = active.id;
    const timer = window.setInterval(() => {
      request<Conversation>('/conversations/' + conversationId)
        .then(detail => {
          setActive(current => current?.id === conversationId ? { ...current, ...detail } : current);
          setConversations(current => current.map(item => item.id === conversationId ? { ...item, ...detail } : item));
        })
        .catch(() => undefined);
    }, 2000);
    return () => window.clearInterval(timer);
  }, [active?.id, active?.title_pending]);
  useEffect(() => {
    if (active?.mode !== 'image_prompt') return;
    void loadImageJobs(active.id).catch(error => setNotice(error instanceof Error ? error.message : '이미지 작업 목록을 가져오지 못했습니다.'));
  }, [active?.id, active?.mode, loadImageJobs]);
  useEffect(() => {
    if (active?.mode !== 'image_prompt') return;
    const jobs = imageJobs[active.id] || [];
    const pending = jobs.filter(job => ['queued', 'waiting_for_gpu', 'running'].includes(job.status));
    if (!pending.length) return;
    const timer = window.setTimeout(() => {
      void Promise.all(pending.map(job => request<{ data: ImageJob }>('/image-jobs/' + job.id)))
        .then(results => results.forEach(result => replaceImageJob(result.data)))
        .catch(error => setNotice(error instanceof Error ? error.message : '이미지 작업 상태를 확인하지 못했습니다.'));
    }, 2500);
    return () => window.clearTimeout(timer);
  }, [active?.id, active?.mode, imageJobs, replaceImageJob]);

  const createConversation = async (data: { title: string; mode: Mode; response_length: ResponseLength; mode_settings: ModeSettings }) => {
    const created = await request<Conversation>('/conversations', { method: 'POST', body: JSON.stringify(data) });
    setNewDialog(false); await refresh(); await loadConversation(created.id, true);
  };
  const saveConversation = async (data: { title?: string; response_length?: ResponseLength; mode_settings?: ModeSettings; summary?: string }) => {
    if (!active) return;
    await request('/conversations/' + active.id, { method: 'PATCH', body: JSON.stringify(data) });
    await refresh(); await loadConversation(active.id);
  };

  const runImagePrompt = async (conversation: Detail, content: string, requestId: string, reuseExistingUser: boolean, selectedMode: PromptMode, selectedModel: string) => {
    const conversationId = conversation.id;
    const controller = new AbortController();
    activeIdRef.current = conversationId;
    inFlightRef.current = { conversationId, requestId, controller };
    setBusy(true);
    setNotice('이미지 프롬프트를 작성 중입니다. 이미지 작업 우선 처리 중이면 LLM 요청이 대기할 수 있습니다.');
    setRetryInfo(undefined);
    setScrollSignal(signal => signal + 1);
    setActive(current => current?.id === conversationId && !reuseExistingUser
      ? { ...current, messages: [...current.messages, { id: 'local-' + requestId, role: 'user', content, created_at: '' }] }
      : current);
    try {
      await request<{ generation: Generation; assistant_message: Message }>(
        '/conversations/' + conversationId + '/messages',
        { method: 'POST', signal: controller.signal, body: JSON.stringify({ content, request_id: requestId, stream: false, prompt_mode: selectedMode, image_model_id: selectedModel || null }) },
      );
      if (activeIdRef.current === conversationId) {
        setNotice('');
        await loadConversation(conversationId);
      }
      await refresh();
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : '이미지 프롬프트 작성에 실패했습니다.';
      const epochMismatch = caught instanceof ApiError && caught.code === 'model_epoch_mismatch';
      if ((caught as DOMException).name !== 'AbortError') setRetryInfo({ conversationId, content, requestId, message, promptMode: selectedMode, imageModelId: selectedModel, epochMismatch });
      if (epochMismatch) {
        if (activeIdRef.current === conversationId) setInput(current => current || content);
        await refreshModels();
      }
      if (activeIdRef.current === conversationId) {
        setNotice((caught as DOMException).name === 'AbortError' ? '생성 취소를 요청했습니다.' : message);
        await loadConversation(conversationId);
      }
    } finally {
      if (inFlightRef.current?.controller === controller) {
        inFlightRef.current = undefined;
        setBusy(false);
      }
    }
  };

  const runGeneration = async (conversation: Detail, content: string, requestId: string = newRequestId(), reuseExistingUser = false, selectedMode: PromptMode = promptMode, selectedModel: string = imageModelId) => {
    if (conversation.mode === 'image_prompt') return runImagePrompt(conversation, content, requestId, reuseExistingUser, selectedMode, selectedModel);
    const conversationId = conversation.id;
    const streamId = 'stream-' + requestId;
    const controller = new AbortController();
    let generated = '';
    let animationFrame: number | undefined;
    const flushStream = () => {
      animationFrame = undefined;
      setActive(current => current?.id === conversationId ? { ...current, messages: current.messages.map(message => message.id === streamId ? { ...message, content: generated } : message) } : current);
    };
    const scheduleStreamFlush = () => {
      if (animationFrame === undefined) animationFrame = requestAnimationFrame(flushStream);
    };
    activeIdRef.current = conversationId;
    inFlightRef.current = { conversationId, requestId, controller };
    setBusy(true);
    setNotice('이미지 작업 우선 처리 중이면 LLM 요청이 대기할 수 있습니다.');
    setRetryInfo(undefined);
    setScrollSignal(signal => signal + 1);
    setActive(current => current?.id === conversationId ? { ...current, messages: [...current.messages, ...(reuseExistingUser ? [] : [{ id: 'local-' + requestId, role: 'user' as const, content, created_at: '' }]), { id: streamId, role: 'assistant', content: '', created_at: '' }] } : current);
    try {
      await streamRequest('/conversations/' + conversationId + '/messages', { content, request_id: requestId, stream: true, wiki: conversation.mode === 'quick' && wikiEnabled, wiki_evidence: conversation.mode === 'quick' && wikiEnabled, wiki_auto_update: conversation.mode === 'quick' && wikiAutoUpdate }, (name, data) => {
        if (name === 'status') {
          if (activeIdRef.current === conversationId) {
            setNotice(String(data.message || '응답을 기다리는 중입니다.'));
          }
        }
        if (name === 'delta') {
          generated += String(data.content || '');
          scheduleStreamFlush();
        }
        if (name === 'done') {
          const completed = data.generation as Generation | undefined;
          const assistant = data.assistant_message as Message | undefined;
          if (completed?.status === 'completed' && typeof completed.user_message_id === 'string' && assistant?.role === 'assistant' && typeof assistant.id === 'string') {
            if (animationFrame !== undefined) { cancelAnimationFrame(animationFrame); animationFrame = undefined; }
            const userId = completed.user_message_id;
            setActive(current => current?.id === conversationId ? { ...current, messages: current.messages.map(message => message.id === 'local-' + requestId ? { ...message, id: userId } : message.id === streamId ? { ...assistant, generation_completed: true } : message) } : current);
          }
          if (activeIdRef.current === conversationId) {
            setNotice((data.generation as Generation | undefined)?.finish_reason === 'length' ? '답변 길이 제한에 도달했습니다.' : '');
            refreshWikiMemoryRef.current();
          }
        }
        if (name === 'error') throw new ApiError(0, String(data.message || '생성에 실패했습니다.'), typeof data.code === 'string' ? data.code : undefined);
      }, controller.signal);
      if (activeIdRef.current === conversationId) await loadConversation(conversationId);
      await refresh();
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : '생성에 실패했습니다.';
      const epochMismatch = caught instanceof ApiError && caught.code === 'model_epoch_mismatch';
      if ((caught as DOMException).name !== 'AbortError') setRetryInfo({ conversationId, content, requestId, message, epochMismatch });
      if (epochMismatch) {
        if (!generated && activeIdRef.current === conversationId) setInput(current => current || content);
        await refreshModels();
      }
      if (activeIdRef.current === conversationId) {
        setNotice((caught as DOMException).name === 'AbortError' ? '생성 취소를 요청했습니다.' : message);
        await loadConversation(conversationId);
      }
    } finally {
      if (animationFrame !== undefined) {
        cancelAnimationFrame(animationFrame);
        flushStream();
      }
      if (inFlightRef.current?.controller === controller) {
        inFlightRef.current = undefined;
        setBusy(false);
      }
    }
  };
  const send = async () => {
    if (!active || !input.trim() || busy || inFlightRef.current || (active.mode === 'image_prompt' && promptMode === 'original' ? !imageModelId : !models.length)) return;
    const content = input.trim();
    const saved = retryInfo?.epochMismatch && retryInfo.conversationId === active.id && retryInfo.content === content
      && (active.mode !== 'image_prompt' || (retryInfo.promptMode === promptMode && retryInfo.imageModelId === imageModelId)) ? retryInfo : undefined;
    setInput('');
    await runGeneration(active, content, saved?.requestId, Boolean(saved));
  };
  const cancel = async () => {
    const inFlight = inFlightRef.current;
    if (!inFlight) return;
    inFlight.controller.abort();
    try { await request('/conversations/' + inFlight.conversationId + '/generations/' + inFlight.requestId, { method: 'DELETE' }); }
    catch (error) {
      if (activeIdRef.current === inFlight.conversationId) setNotice(error instanceof Error ? error.message : '취소 요청에 실패했습니다.');
    }
  };
  const retry = async () => {
    if (!retryInfo || busy || inFlightRef.current) return;
    if (retryInfo.epochMismatch && activeIdRef.current === retryInfo.conversationId) setInput(current => current.trim() === retryInfo.content ? '' : current);
    let target = active;
    if (activeIdRef.current !== retryInfo.conversationId) {
      activeIdRef.current = retryInfo.conversationId;
      const [detail, page] = await Promise.all([request<Conversation>('/conversations/' + retryInfo.conversationId), request<MessagePage>('/conversations/' + retryInfo.conversationId + '/messages?limit=50')]);
      target = { ...detail, messages: page.data, nextBefore: page.next_before, hasMore: page.has_more };
      setActive(target); setSystemView(false);
    }
    if (target?.id === retryInfo.conversationId) await runGeneration(target, retryInfo.content, retryInfo.requestId, true, retryInfo.promptMode, retryInfo.imageModelId);
  };
  const showSystem = async () => {
    setWikiView(false); setSystemView(true); setGateLoading(true); setGateError('');
    void refreshModels();
    if (window.location.hash !== '#/') history.pushState(null, '', '#/');
    try { setGate(await request<unknown>('/admin/gate-status')); }
    catch (error) { setGate(undefined); setGateError(error instanceof Error ? error.message : 'LLM gate 상태를 가져오지 못했습니다.'); }
    finally { setGateLoading(false); }
  };
  const deleteConversation = async (conversation: Detail) => {
    if (!confirm('이 대화를 삭제할까요?')) return;
    await request('/conversations/' + conversation.id, { method: 'DELETE' });
    if (activeIdRef.current === conversation.id) { activeIdRef.current = undefined; setActive(undefined); history.replaceState(null, '', '#/'); }
    await refresh();
  };


  return <ThemeProvider theme={appTheme}><main className={'layout' + (wikiView ? ' wiki-mode' : '')}>
    <aside className="workspace-sidebar"><h1><span className="brand-mark" aria-hidden="true">✳</span><span className="sidebar-brand-text">BC-250<small>개인 AI 워크스페이스</small></span></h1><button className="mobile-menu-toggle plain" aria-expanded={menuOpen} aria-controls="mobile-workspace-menu" aria-label="메뉴 열기" onClick={() => setMenuOpen(value => !value)}><Menu size={18} aria-hidden="true" /><span className="sidebar-action-label">{menuOpen ? '닫기' : '메뉴'}</span></button><button className="new-chat-button" aria-label="새 대화" onClick={() => { setMenuOpen(false); setNewDialog(true); }}><Plus size={18} aria-hidden="true" /><span className="sidebar-action-label">새 대화</span></button>
      <nav id="conversation-nav" className={menuOpen ? 'mobile-open' : ''} aria-label="대화 목록">{conversations.map(conversation => <a role="button" href={'#/chat/' + encodeURIComponent(conversation.id)} className={active?.id === conversation.id && !systemView && !wikiView ? 'selected' : ''} key={conversation.id} onClick={event => { event.preventDefault(); setMenuOpen(false); void loadConversation(conversation.id, true).catch(error => setNotice(error.message)); }}>{conversation.title}<small>{MODE_META[conversation.mode].label}</small></a>)}</nav>
      <button className={'mobile-nav-action' + (menuOpen ? ' mobile-open' : '')} aria-label="공유 Wiki" onClick={() => { setMenuOpen(false); setSystemView(false); setWikiVisited(true); setWikiView(true); setWikiDocumentId(undefined); history.pushState(null, '', '#/wiki'); }}><BookOpen size={17} aria-hidden="true" /><span className="sidebar-action-label">공유 Wiki</span></button>
      <button className={'mobile-nav-action' + (menuOpen ? ' mobile-open' : '')} aria-label="시스템 상태" onClick={() => { setMenuOpen(false); void showSystem(); }}><Activity size={17} aria-hidden="true" /><span className="sidebar-action-label">시스템 상태</span></button><small className="model-name">활성 모델: {models[0]?.split('/').pop() || '조회 중…'}</small>
    </aside>
    <Drawer className="mobile-workspace-drawer" anchor="left" open={menuOpen} onClose={() => setMenuOpen(false)} slotProps={{ paper: { id: 'mobile-workspace-menu', 'aria-label': '워크스페이스 메뉴' } }}>
      <div className="drawer-heading"><div><span className="brand-mark" aria-hidden="true">✳</span><strong>BC-250</strong></div><button type="button" className="drawer-close" aria-label="메뉴 닫기" onClick={() => setMenuOpen(false)}><X size={20} /></button></div>
      <button type="button" className="drawer-new" onClick={() => { setMenuOpen(false); setNewDialog(true); }}><Plus size={18} /> 새 대화</button>
      <div className="drawer-section-label">워크스페이스</div>
      <div className="drawer-links"><button type="button" className={wikiView ? 'drawer-selected' : ''} onClick={() => { setMenuOpen(false); setSystemView(false); setWikiVisited(true); setWikiView(true); setWikiDocumentId(undefined); history.pushState(null, '', '#/wiki'); }}><BookOpen size={18} /> 공유 Wiki</button>
      <button type="button" className={systemView && !wikiView ? 'drawer-selected' : ''} onClick={() => { setMenuOpen(false); void showSystem(); }}><Activity size={18} /> 시스템 상태</button></div>
      <div className="drawer-section-label drawer-conversations-heading">대화 <span>{conversations.length}</span></div>
      <nav className="drawer-conversations" aria-label="모바일 대화 목록">{conversations.map(conversation => <a role="button" href={'#/chat/' + encodeURIComponent(conversation.id)} className={active?.id === conversation.id && !systemView && !wikiView ? 'drawer-selected' : ''} key={conversation.id} onClick={event => { if (!event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey && event.button === 0) { event.preventDefault(); setMenuOpen(false); void loadConversation(conversation.id, true).catch(error => setNotice(error.message)); } }}><span>{conversation.title}</span><small>{MODE_META[conversation.mode].label}</small></a>)}</nav>
      <div className="drawer-model">활성 모델 <span>{models[0]?.split('/').pop() || '조회 중…'}</span></div>
    </Drawer>
    <section className="chat">
      {wikiVisited && <div className="wiki-manager-host" hidden={!wikiView}><WikiManager documentId={wikiDocumentId} onNavigate={id => { setWikiDocumentId(id); history.pushState(null, '', id ? '#/wiki/' + id : '#/wiki'); }} onClose={() => { setWikiView(false); history.pushState(null, '', active ? '#/chat/' + encodeURIComponent(active.id) : '#/'); }} /></div>}
      {!wikiView && (systemView ? <SystemStatus gate={gate} gateError={gateError} loading={gateLoading} models={models} modelsUnavailable={modelsUnavailable} onRefresh={() => { void showSystem(); }} onSwitchSucceeded={refreshModels} />
      : active ? <><header className="chat-header"><div><h2>{active.title}</h2><p className="muted">{MODE_META[active.mode].label} · {active.mode === 'image_prompt' ? '영어 JSON 설정 · 매 요청 독립 작성' : `${LENGTH_LABEL[active.response_length]} · ${active.context_policy === 'none' ? '이전 대화 미포함' : '최근 대화 포함'}`}</p></div><a className="permalink" href={'#/chat/' + encodeURIComponent(active.id)}><Link2 size={16} aria-hidden="true" /> 대화 링크</a><button className="header-action" onClick={() => setSettingsDialog(true)} disabled={busy}><Settings size={16} aria-hidden="true" /> 설정</button><button className="danger header-action" disabled={busy} onClick={() => void deleteConversation(active)}><Trash2 size={16} aria-hidden="true" /> 삭제</button></header>
        {active.mode !== 'image_prompt' && <div className="mode-banner" role="status"><span className="mode-banner-copy">최근 완료 대화{active.summary ? '와 장기 기억 요약' : ''}을 참고합니다. 대화가 길어지면 오래된 내용은 요약하거나 제외할 수 있습니다. 기억은 대화 설정에서 확인할 수 있습니다.</span><span className="mode-banner-short">최근 대화{active.summary ? '와 장기 기억' : ''} 참고 중</span></div>}
        {active.mode === 'image_prompt' && <div className="mode-banner"><span className="mode-banner-copy">매 요청은 독립적으로 작성됩니다. 결과 카드에서 모델·프리셋·프롬프트·세부 옵션을 확인·수정한 뒤, 원할 때만 이미지 생성 요청을 누르세요.</span><span className="mode-banner-short">매 요청은 독립적으로 작성됩니다.</span></div>}
        {active.mode !== 'image_prompt' && active.summary_status !== 'idle' && <div className={active.summary_status === 'failed' ? 'notice error' : 'notice'}>{active.summary_status === 'failed' ? (active.summary_error || '장기 기억 요약에 실패했습니다. 기존 요약은 유지됩니다.') : '장기 기억을 정리하고 있습니다. 완료 전에는 기존 요약과 최근 대화를 참고합니다.'}</div>}
        <MessageList mode={active.mode} messages={active.messages} hasMore={active.hasMore} onLoadOlder={loadOlderMessages} scrollSignal={scrollSignal} conversationId={active.id}
          imageCapabilities={imageCapabilities} imageDrafts={imageDrafts} onEnsureImageDraft={ensureImageDraft}
          onUpdateImageDraft={updateImageDraft} onPersistImageDraft={persistImageDraft} onSubmitImageJob={submitImageJob}
          imageJobs={imageJobs[active.id] || []} onCancelImageJob={cancelImageJob} dismissedWikiProposals={dismissedWikiProposals} onDismissWikiProposal={dismissWikiProposal} onWikiDraft={message => {
            if (message.role === 'user' && !message.id.startsWith('local-') && active.messages.some(item => item.id === message.id && item.role === 'user' && item.content === message.content))
              setWikiDraft({ conversationId: active.id, messageId: message.id, original: message.content });
          }} />
        <div className="composer-panel">
          {active.mode === 'quick' && <WikiMemoryStatus jobs={wikiMemory} conversationId={active.id} onChanged={() => refreshWikiMemoryRef.current()} />}
          {active.mode === 'image_prompt' && <details className="image-compose-disclosure"><summary>프롬프트 설정 · {promptMode === 'ai_optimized' ? 'AI 최적화' : promptMode === 'direct_translation' ? '직접 번역' : '원본 그대로'} · {imageCapabilities?.models.find(model => model.model_id === imageModelId)?.display_name || '추천받기'}</summary><div className="image-compose-settings" aria-label="이미지 프롬프트 작성 설정">
            <div className="image-compose-heading"><span className="image-compose-icon" aria-hidden="true">✦</span><div><strong>프롬프트 설정</strong><small>원하는 방식과 모델을 선택하세요</small></div></div>
            <div className="image-compose-fields">
              <label><span>프롬프트 방식</span><select value={promptMode} disabled={busy} onChange={event => setPromptMode(event.target.value as PromptMode)}><option value="ai_optimized">AI 최적화</option><option value="direct_translation">직접 번역</option><option value="original">원본 그대로</option></select></label>
              <label><span>이미지 모델</span><select value={imageModelId} disabled={busy || !imageCapabilities} onChange={event => setImageModelId(event.target.value)}><option value="">추천받기</option>{imageCapabilities?.models.map(model => <option key={model.model_id} value={model.model_id} disabled={Boolean(imageCapabilities.available && !model.execution_enabled)}>{model.display_name}{imageCapabilities.available && !model.execution_enabled ? ' · 실행 불가' : ''}</option>)}</select></label>
            </div>
            {promptMode === 'original' && <p className="image-compose-hint">모델을 직접 선택하세요. 입력 문구의 앞뒤 공백만 제거합니다.</p>}
          </div></details>}
          <div className="composer"><textarea aria-label={active.mode === 'image_prompt' ? '이미지 장면 입력' : '메시지 입력'} value={input} disabled={busy} onChange={event => setInput(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void send(); } }} placeholder={active.mode === 'image_prompt' ? '원하는 이미지 장면을 전체 문장으로 입력하세요 (Enter 전송, Shift+Enter 줄바꿈)' : '메시지를 입력하세요 (Enter 전송, Shift+Enter 줄바꿈)'} /><button disabled={busy || !input.trim() || (active.mode === 'image_prompt' && promptMode === 'original' ? !imageModelId : !models.length)} onClick={() => void send()}>{busy ? (active.mode === 'image_prompt' ? '작성 중' : '생성 중') : (active.mode === 'image_prompt' ? '프롬프트 작성' : '전송')}</button>{busy && inFlightRef.current?.conversationId === active.id && <button className="plain" onClick={() => void cancel()}>취소</button>}</div>
        </div>
        {(notice || modelsUnavailable) && <div className="notice">{notice}{modelsUnavailable && <> 모델 목록을 확인하지 못했습니다. 자동으로 다시 확인합니다. <button className="plain" onClick={() => void refreshModels()}>모델 다시 조회</button></>}{retryInfo && <button className="plain" disabled={busy || modelsUnavailable} onClick={() => void retry()}>재시도</button>}</div>}
      </> : <div className="empty"><div className="empty-icon"><MessageSquare size={27} aria-hidden="true" /></div><h2>무엇을 도와드릴까요?</h2><p>새 대화를 만들고 편하게 이야기를 시작하세요.</p><button onClick={() => setNewDialog(true)}><Plus size={17} aria-hidden="true" /> 대화 만들기</button></div>)}
    </section>
    {wikiDraft && <WikiDraftDialog source={wikiDraft} onClose={() => setWikiDraft(undefined)} onSaved={() => dismissWikiProposal(wikiDraft.conversationId, wikiDraft.messageId)} />}
    {newDialog && <NewConversationDialog onClose={() => setNewDialog(false)} onCreate={createConversation} />}
    {settingsDialog && active && <ConversationSettings conversation={active} wikiEnabled={wikiEnabled} wikiAutoUpdate={wikiAutoUpdate} onSaveWiki={(search, auto) => {
      setWikiEnabled(search); setWikiAutoUpdate(auto);
      try { localStorage.setItem('bc250-wiki-search', String(search)); localStorage.setItem('bc250-wiki-auto', String(auto)); } catch { /* Storage may be disabled. */ }
    }} onClose={() => setSettingsDialog(false)} onSave={saveConversation} />}
  </main></ThemeProvider>;
}

const root = document.getElementById('root');
if (root) createRoot(root).render(<App />);
