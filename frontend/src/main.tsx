import { FormEvent, useCallback, useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { newRequestId, request, streamRequest, type Conversation, type Generation, type ImageCapabilities, type ImageDraft, type ImageJob, type ImagePresetName, type Message, type MessagePage, type Mode, type ModeSettings, type PromptMode, type ResponseLength } from './api';
import { MessageList } from './components/MessageList';
import './image-prompt.css';
import './style.css';
import './scroll.css';

type Detail = Conversation & { messages: Message[]; nextBefore: string | null; hasMore: boolean };
type RetryInfo = { conversationId: string; content: string; requestId: string; message: string; promptMode?: PromptMode; imageModelId?: string };
type InFlightGeneration = { conversationId: string; requestId: string; controller: AbortController };
export type ImageDraftEditor = Pick<ImageDraft, 'prompt' | 'width' | 'height' | 'generation_config' | 'revision' | 'preset_name'> & { persisted: boolean; dirty: boolean };

const MODE_META: Record<Mode, { label: string; description: string; fields: Array<[string, string, string]> }> = {
  quick: { label: '일반 대화', description: '최근 대화와 장기 기억 요약을 참고합니다.', fields: [] },
  roleplay: { label: '롤플레잉', description: '설정과 최근 완료 대화를 이어 갑니다.', fields: [['character', '캐릭터', '이름·역할·성격'], ['world', '세계관', '배경과 규칙'], ['voice', '말투', '예: 차분한 존댓말'], ['scenario', '현재 상황', '시작 장면'], ['rules', '응답 규칙', '지켜야 할 조건']] },
  image_prompt: { label: '이미지 프롬프트', description: '한 번의 요청을 영어 프롬프트와 설정 JSON으로 변환합니다. 이미지는 직접 생성하지 않습니다.', fields: [] },
};
const LENGTH_LABEL: Record<ResponseLength, string> = { short: '짧게', medium: '보통', long: '길게' };

function ModeFields({ mode, value, onChange }: { mode: Mode; value: ModeSettings; onChange: (settings: ModeSettings) => void }) {
  return <div className="mode-fields">{MODE_META[mode].fields.map(([key, label, placeholder]) => <label key={key}>{label}
    <textarea value={value[key] || ''} placeholder={placeholder} onChange={event => onChange({ ...value, [key]: event.target.value })} />
  </label>)}</div>;
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
  return <div className="modal-backdrop" role="presentation"><form className="modal" onSubmit={submit}>
    <header><h2>새 대화</h2><button type="button" className="plain" onClick={onClose}>닫기</button></header>
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
  </form></div>;
}

function ConversationSettings({ conversation, onClose, onSave }: { conversation: Detail; onClose: () => void; onSave: (data: { title?: string; response_length?: ResponseLength; mode_settings?: ModeSettings; summary?: string }) => Promise<void> }) {
  const [title, setTitle] = useState(conversation.title);
  const [responseLength, setResponseLength] = useState<ResponseLength>(conversation.response_length);
  const [settings, setSettings] = useState<ModeSettings>(conversation.mode_settings);
  const [summary, setSummary] = useState(conversation.summary);
  const [error, setError] = useState('');
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    try {
      await onSave(conversation.mode === 'image_prompt'
        ? { ...(title !== conversation.title ? { title } : {}) }
        : { ...(title !== conversation.title ? { title } : {}), response_length: responseLength, mode_settings: settings, ...(summary !== conversation.summary ? { summary } : {}) });
      onClose();
    }
    catch (caught) { setError(caught instanceof Error ? caught.message : '설정을 저장하지 못했습니다.'); }
  };
  return <div className="modal-backdrop" role="presentation"><form className="modal" onSubmit={submit}>
    <header><h2>대화 설정</h2><button type="button" className="plain" onClick={onClose}>닫기</button></header>
    <p className="muted">모드: {MODE_META[conversation.mode].label} · 생성 후에는 모드를 변경할 수 없습니다.</p>
    <label>제목<input value={title} maxLength={120} onChange={event => setTitle(event.target.value)} required /></label>
    {conversation.mode !== 'image_prompt' && <label>응답 길이<select value={responseLength} onChange={event => setResponseLength(event.target.value as ResponseLength)}>
      {(Object.keys(LENGTH_LABEL) as ResponseLength[]).map(option => <option key={option} value={option}>{LENGTH_LABEL[option]}</option>)}
    </select></label>}
    {conversation.mode === 'image_prompt' && <p className="muted">매 요청은 독립적으로 작성됩니다. 원하는 장면을 전체 문장으로 입력하세요. 기존 기록은 보이지만 생성에 사용되지 않습니다.</p>}
    {conversation.mode !== 'image_prompt' && <ModeFields mode={conversation.mode} value={settings} onChange={setSettings} />}
    {conversation.mode !== 'image_prompt' && <label>장기 기억 요약
      <textarea value={summary} maxLength={12000} onChange={event => setSummary(event.target.value)} placeholder="자동 요약된 대화의 핵심 내용입니다." />
      <small className="muted">{conversation.summary_status === 'pending' || conversation.summary_status === 'running' ? '장기 기억을 정리하고 있습니다. 저장하면 현재 자동 요약 작업을 취소합니다.' : '필요하면 직접 수정할 수 있습니다.'}</small>
    </label>}
    {error && <p className="error">{error}</p>}<button type="submit">저장</button>
  </form></div>;
}

function App() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [active, setActive] = useState<Detail>();
  const [models, setModels] = useState<string[]>([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const [retryInfo, setRetryInfo] = useState<RetryInfo>();
  const [promptMode, setPromptMode] = useState<PromptMode>('ai_optimized');
  const [imageModelId, setImageModelId] = useState('');
  const [gate, setGate] = useState<unknown>();
  const [systemView, setSystemView] = useState(false);
  const [newDialog, setNewDialog] = useState(false);
  const [settingsDialog, setSettingsDialog] = useState(false);
  const [imageCapabilities, setImageCapabilities] = useState<ImageCapabilities>();
  const [imageDrafts, setImageDrafts] = useState<Record<string, ImageDraftEditor>>({});
  const [imageJobs, setImageJobs] = useState<Record<string, ImageJob[]>>({});
  const imageDraftsRef = useRef<Record<string, ImageDraftEditor>>({});
  const imageDraftLoadingRef = useRef(new Set<string>());
  const [scrollSignal, setScrollSignal] = useState(0);
  const activeIdRef = useRef<string | undefined>(undefined);
  const inFlightRef = useRef<InFlightGeneration | undefined>(undefined);

  const refresh = async () => {
    const [conversationResponse, modelResponse, capabilities] = await Promise.all([
      request<{ data: Conversation[] }>('/conversations'), request<{ data: { id: string }[] }>('/models').catch(() => ({ data: [] })),
      request<ImageCapabilities>('/image-generation/capabilities').catch(() => undefined),
    ]);
    setConversations(conversationResponse.data);
    setModels(modelResponse.data.map(model => model.id));
    setImageCapabilities(capabilities);
  };

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
      activeIdRef.current = id;
      setSystemView(false);
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
      if ((caught as DOMException).name !== 'AbortError') setRetryInfo({ conversationId, content, requestId, message, promptMode: selectedMode, imageModelId: selectedModel });
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
      await streamRequest('/conversations/' + conversationId + '/messages', { content, request_id: requestId, stream: true }, (name, data) => {
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
          if (activeIdRef.current === conversationId) {
            setNotice((data.generation as Generation | undefined)?.finish_reason === 'length' ? '답변 길이 제한에 도달했습니다.' : '');
          }
        }
        if (name === 'error') throw new Error(String(data.message || '생성에 실패했습니다.'));
      }, controller.signal);
      if (activeIdRef.current === conversationId) await loadConversation(conversationId);
      await refresh();
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : '생성에 실패했습니다.';
      if ((caught as DOMException).name !== 'AbortError') setRetryInfo({ conversationId, content, requestId, message });
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
    if (!active || !input.trim() || busy || (active.mode === 'image_prompt' && promptMode === 'original' ? !imageModelId : !models.length)) return;
    const content = input.trim();
    setInput('');
    await runGeneration(active, content);
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
    if (!retryInfo) return;
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
    try { setGate(await request<unknown>('/admin/gate-status')); setSystemView(true); }
    catch (error) { setNotice(error instanceof Error ? error.message : 'LLM gate 상태를 가져오지 못했습니다.'); }
  };
  const deleteConversation = async (conversation: Detail) => {
    if (!confirm('이 대화를 삭제할까요?')) return;
    await request('/conversations/' + conversation.id, { method: 'DELETE' });
    if (activeIdRef.current === conversation.id) { activeIdRef.current = undefined; setActive(undefined); }
    await refresh();
  };


  return <main className="layout">
    <aside><h1>BC-250</h1><button onClick={() => setNewDialog(true)}>＋ 새 대화</button>
      <nav aria-label="대화 목록">{conversations.map(conversation => <button className={active?.id === conversation.id && !systemView ? 'selected' : ''} key={conversation.id} onClick={() => loadConversation(conversation.id, true)}>{conversation.title}<small>{MODE_META[conversation.mode].label}</small></button>)}</nav>
      <button onClick={showSystem}>시스템 상태</button><small className="model-name">활성 모델: {models[0]?.split('/').pop() || '조회 중…'}</small>
    </aside>
    <section className="chat">
      {systemView ? <section className="system-panel"><h2>BC-250 시스템 상태</h2><p>이미지 작업이 진행 중이면 image_priority 정책에 따라 LLM이 대기할 수 있습니다.</p><h3>사용 가능 모델</h3><ul>{models.map(model => <li key={model}>{model}</li>)}</ul><h3>LLM Gate</h3><pre>{JSON.stringify(gate, null, 2)}</pre><button onClick={showSystem}>새로고침</button></section>
      : active ? <><header><div><h2>{active.title}</h2><p className="muted">{MODE_META[active.mode].label} · {active.mode === 'image_prompt' ? '영어 JSON 설정 · 매 요청 독립 작성' : `${LENGTH_LABEL[active.response_length]} · ${active.context_policy === 'none' ? '이전 대화 미포함' : '최근 대화 포함'}`}</p></div><button onClick={() => setSettingsDialog(true)} disabled={busy}>설정</button><button className="danger" disabled={busy} onClick={() => void deleteConversation(active)}>삭제</button></header>
        {active.mode !== 'image_prompt' && <div className="mode-banner" role="status">최근 완료 대화{active.summary ? '와 장기 기억 요약' : ''}을 참고합니다. 대화가 길어지면 오래된 내용은 요약하거나 제외할 수 있습니다. <button className="plain" onClick={() => setSettingsDialog(true)} disabled={busy}>기억 확인</button></div>}
        {active.mode === 'image_prompt' && <div className="mode-banner">매 요청은 독립적으로 작성됩니다. 결과 카드에서 모델·프리셋·프롬프트·세부 옵션을 확인·수정한 뒤, 원할 때만 이미지 생성 요청을 누르세요.</div>}
        {active.mode !== 'image_prompt' && active.summary_status !== 'idle' && <div className={active.summary_status === 'failed' ? 'notice error' : 'notice'}>{active.summary_status === 'failed' ? (active.summary_error || '장기 기억 요약에 실패했습니다. 기존 요약은 유지됩니다.') : '장기 기억을 정리하고 있습니다. 완료 전에는 기존 요약과 최근 대화를 참고합니다.'}</div>}
        <MessageList mode={active.mode} messages={active.messages} hasMore={active.hasMore} onLoadOlder={loadOlderMessages} scrollSignal={scrollSignal} conversationId={active.id}
          imageCapabilities={imageCapabilities} imageDrafts={imageDrafts} onEnsureImageDraft={ensureImageDraft}
          onUpdateImageDraft={updateImageDraft} onPersistImageDraft={persistImageDraft} onSubmitImageJob={submitImageJob}
          imageJobs={imageJobs[active.id] || []} onCancelImageJob={cancelImageJob} />
        <div className="composer-panel">
          {active.mode === 'image_prompt' && <div className="image-compose-settings" aria-label="이미지 프롬프트 작성 설정">
            <div className="image-compose-heading"><span className="image-compose-icon" aria-hidden="true">✦</span><div><strong>프롬프트 설정</strong><small>원하는 방식과 모델을 선택하세요</small></div></div>
            <div className="image-compose-fields">
              <label><span>프롬프트 방식</span><select value={promptMode} disabled={busy} onChange={event => setPromptMode(event.target.value as PromptMode)}><option value="ai_optimized">AI 최적화</option><option value="direct_translation">직접 번역</option><option value="original">원본 그대로</option></select></label>
              <label><span>이미지 모델</span><select value={imageModelId} disabled={busy || !imageCapabilities} onChange={event => setImageModelId(event.target.value)}><option value="">추천받기</option>{imageCapabilities?.models.map(model => <option key={model.model_id} value={model.model_id} disabled={Boolean(imageCapabilities.available && !model.execution_enabled)}>{model.display_name}{imageCapabilities.available && !model.execution_enabled ? ' · 실행 불가' : ''}</option>)}</select></label>
            </div>
            {promptMode === 'original' && <p className="image-compose-hint">모델을 직접 선택하세요. 입력 문구의 앞뒤 공백만 제거합니다.</p>}
          </div>}
          <div className="composer"><textarea value={input} disabled={busy} onChange={event => setInput(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); void send(); } }} placeholder={active.mode === 'image_prompt' ? '원하는 이미지 장면을 전체 문장으로 입력하세요 (Enter 전송, Shift+Enter 줄바꿈)' : '메시지를 입력하세요 (Enter 전송, Shift+Enter 줄바꿈)'} /><button disabled={busy || !input.trim() || (active.mode === 'image_prompt' && promptMode === 'original' ? !imageModelId : !models.length)} onClick={() => void send()}>{busy ? (active.mode === 'image_prompt' ? '작성 중' : '생성 중') : (active.mode === 'image_prompt' ? '프롬프트 작성' : '전송')}</button>{busy && inFlightRef.current?.conversationId === active.id && <button className="plain" onClick={() => void cancel()}>취소</button>}</div>
        </div>
        {notice && <div className="notice">{notice}{retryInfo && <button className="plain" onClick={() => void retry()}>재시도</button>}</div>}
      </> : <div className="empty"><h2>새 대화를 시작하세요</h2><button onClick={() => setNewDialog(true)}>대화 만들기</button></div>}
    </section>
    {newDialog && <NewConversationDialog onClose={() => setNewDialog(false)} onCreate={createConversation} />}
    {settingsDialog && active && <ConversationSettings conversation={active} onClose={() => setSettingsDialog(false)} onSave={saveConversation} />}
  </main>;
}

createRoot(document.getElementById('root')!).render(<App />);
