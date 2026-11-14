import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, newRequestId, request } from '../api';

type Operation = { id: string; model: string; state: string; error_code?: string | null };
type Models = { models: { name: string; current: boolean }[]; configured_model: string | null; loaded_model: string | null; ready: boolean; switch_enabled: boolean };
type PendingRequest = { id: string; model: string };
const terminal = new Set(['succeeded', 'failed', 'failed_rolled_back', 'recovery_required']);
const operationStorage = 'model-switch-operation';
const requestStorage = 'model-switch-request';
const uncertainStorage = 'model-switch-uncertain';

function readPending(): PendingRequest | null {
  try {
    const item = JSON.parse(sessionStorage.getItem(requestStorage) || 'null');
    return item && typeof item.id === 'string' && /^[A-Za-z0-9_.:-]{1,128}$/.test(item.id)
      && typeof item.model === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(item.model) ? item : null;
  } catch { return null; }
}

export function ModelControl({ onSwitchSucceeded }: { onSwitchSucceeded?: () => void } = {}) {
  const [models, setModels] = useState<Models | null>(null);
  const [operation, setOperation] = useState<Operation | null>(null);
  const [selection, setSelection] = useState('');
  const [error, setError] = useState('');
  const [pending, setPending] = useState(false);
  const [uncertain, setUncertain] = useState(() => sessionStorage.getItem(uncertainStorage) === '1');
  const [unknownRequest, setUnknownRequest] = useState<PendingRequest | null>(readPending);
  const [statusVerified, setStatusVerified] = useState(false);
  const generation = useRef(0);
  const refreshing = useRef(false);
  const submitting = useRef(false);
  const terminalRefreshed = useRef('');
  const mounted = useRef(false);
  const refreshController = useRef<AbortController | null>(null);
  const submitController = useRef<AbortController | null>(null);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      ++generation.current;
      refreshController.current?.abort();
      submitController.current?.abort();
    };
  }, []);

  const refresh = useCallback(async () => {
    if (pending || refreshing.current) return;
    refreshing.current = true;
    const version = ++generation.current;
    const controller = new AbortController();
    refreshController.current = controller;
    const timeout = window.setTimeout(() => controller.abort(), 10000);
    setStatusVerified(false);
    try {
      const options = { signal: controller.signal };
      const active = await request<{ operation: Operation | null }>('/llm/switches/current', options);
      const id = active.operation?.id || operation?.id || sessionStorage.getItem(operationStorage);
      const value = active.operation || (id ? await request<Operation>(`/llm/switches/${id}`, options) : null);
      // Read models after operation state so a terminal operation is followed by a fresh snapshot.
      const list = await request<Models>('/llm/models', options);
      if (generation.current !== version) return;
      if (value) sessionStorage.setItem(operationStorage, value.id);
      setOperation(value); setModels(list); setStatusVerified(true); setError('');
      if (value && terminal.has(value.state)) {
        if (value.state === 'succeeded' && terminalRefreshed.current !== value.id) onSwitchSucceeded?.();
        terminalRefreshed.current = value.id;
      }
    } catch {
      if (generation.current === version) setError('일부 상태를 확인하지 못했습니다. 다시 조회해 주세요.');
    } finally {
      window.clearTimeout(timeout); refreshing.current = false;
      if (refreshController.current === controller) refreshController.current = null;
    }
  }, [operation, pending, onSwitchSucceeded]);

  useEffect(() => {
    if (!operation || terminal.has(operation.state)) return;
    const timer = window.setInterval(() => { void refresh(); }, 2000);
    return () => window.clearInterval(timer);
  }, [operation, refresh]);

  useEffect(() => {
    if (pending || !operation || !terminal.has(operation.state) || terminalRefreshed.current === operation.id) return;
    void refresh();
    const timer = window.setInterval(() => { void refresh(); }, 2000);
    return () => window.clearInterval(timer);
  }, [pending, operation, refresh]);

  const submit = async (saved?: PendingRequest) => {
    if (submitting.current) return;
    submitting.current = true;
    ++generation.current;
    setStatusVerified(false);
    const requestInfo = saved || { id: newRequestId(), model: selection };
    sessionStorage.setItem(requestStorage, JSON.stringify(requestInfo));
    sessionStorage.setItem(uncertainStorage, '1');
    setUnknownRequest(requestInfo); setUncertain(true); setPending(true); setError('');
    const controller = new AbortController();
    submitController.current = controller;
    const timeout = window.setTimeout(() => controller.abort(), 15000);
    try {
      const result = await request<Operation>('/llm/switches', { method: 'POST', headers: { 'Idempotency-Key': requestInfo.id }, body: JSON.stringify({ model: requestInfo.model }), signal: controller.signal });
      const current = readPending();
      if (!mounted.current || current?.id !== requestInfo.id || current.model !== requestInfo.model) return;
      sessionStorage.setItem(operationStorage, result.id); setOperation(result);
      sessionStorage.removeItem(uncertainStorage); sessionStorage.removeItem(requestStorage);
      setUnknownRequest(null); setUncertain(false);
    } catch (caught) {
      const current = readPending();
      if (!mounted.current || current?.id !== requestInfo.id || current.model !== requestInfo.model) return;
      // A retry after a lost response remains uncertain even if a later attempt is rejected.
      const rejected = !saved && caught instanceof ApiError && [400, 401, 403, 404, 409, 422].includes(caught.status);
      if (rejected) {
        sessionStorage.removeItem(uncertainStorage); sessionStorage.removeItem(requestStorage);
        setUnknownRequest(null); setUncertain(false);
      }
      setError((caught instanceof Error ? caught.message : '전환 결과를 확인하지 못했습니다.') +
        (rejected ? ' 상태를 다시 조회해 주세요.' : ' 같은 요청만 확인할 수 있습니다.'));
    } finally {
      window.clearTimeout(timeout); submitting.current = false;
      if (submitController.current === controller) submitController.current = null;
      if (mounted.current) setPending(false);
    }
  };

  return <section className="model-control"><h3>LLM 모델 전환</h3><p>이 변경은 서버 전체에 적용됩니다. 진행 중인 대화와 이미지 작업에 영향을 줄 수 있습니다.</p>
    <button onClick={() => void refresh()} disabled={pending}>상태 조회</button>
    {error && <p role="alert">{error}</p>}
    {uncertain && <div role="alert"><p>접수 결과를 확인할 수 없습니다. 관리자가 작업 기록을 확인해야 합니다.</p>
      {unknownRequest && <><p>모델: {unknownRequest.model} · 요청 키: <code>{unknownRequest.id}</code></p>
        <button onClick={() => void submit(unknownRequest)} disabled={pending}>같은 요청으로 접수 확인</button>
        <p>같은 키로 확인하며, 접수되지 않았다면 전환이 시작될 수 있습니다.</p></>}
    </div>}
    {models && <><p>설정된 모델: {models.configured_model || '확인 불가'} · 로드된 모델: {models.loaded_model || '확인 불가'} · 준비: {models.ready ? '완료' : '확인 불가'}</p>
      <label>설치된 모델 <select value={selection} onChange={event => setSelection(event.target.value)}><option value="">선택</option>{models.models.map(model => <option key={model.name} value={model.name}>{model.name}</option>)}</select></label>
      <button onClick={() => void submit()} disabled={!selection || pending || uncertain || !statusVerified || !models.switch_enabled || Boolean(operation && !terminal.has(operation.state)) || operation?.state === 'recovery_required'}>전환</button>
      {!models.switch_enabled && <p>모델 전환 기능을 사용할 수 없습니다.</p>}</>}
    {operation && <p>작업 {operation.model}: {operation.state}{operation.error_code ? ` (${operation.error_code})` : ''}{operation.state === 'recovery_required' ? ' · 관리자 복구가 필요합니다.' : ''}</p>}
  </section>;
}
