import { useCallback, useEffect, useRef, useState } from 'react';
import { newRequestId, request } from '../api';

type Operation = { id: string; model: string; state: string; error_code?: string | null };
type Models = { models: { name: string; current: boolean }[]; configured_model: string | null; loaded_model: string | null; ready: boolean; switch_enabled: boolean };
const terminal = new Set(['succeeded', 'failed', 'failed_rolled_back', 'recovery_required']);
const operationStorage = 'model-switch-operation';
const requestStorage = 'model-switch-request';
export function ModelControl() {
  const [key, setKey] = useState(''); const [models, setModels] = useState<Models | null>(null);
  const [operation, setOperation] = useState<Operation | null>(null); const [selection, setSelection] = useState('');
  const [error, setError] = useState(''); const [pending, setPending] = useState(false); const [uncertain, setUncertain] = useState(() => sessionStorage.getItem('model-switch-uncertain') === '1');
  const [unknownRequest, setUnknownRequest] = useState(() => sessionStorage.getItem(requestStorage));
  const [statusVerified, setStatusVerified] = useState(false);
  const generation = useRef(0);
  const headers = useCallback(() => ({ 'X-BC250-Operator-Key': key }), [key]);
  const refresh = useCallback(async () => {
    if (!key || pending) return;
    const version = ++generation.current;
    setStatusVerified(false);
    const list = request<Models>('/llm/models', { headers: headers() })
      .then(value => { if (generation.current === version) setModels(value); return true; }).catch(() => { if (generation.current === version) setModels(null); return false; });
    const current = (async () => {
      const active = await request<{ operation: Operation | null }>('/llm/switches/current', { headers: headers() });
      const id = active.operation?.id || operation?.id || sessionStorage.getItem(operationStorage);
      const value = active.operation || (id ? await request<Operation>(`/llm/switches/${id}`, { headers: headers() }) : null);
      if (generation.current === version) {
        if (value) sessionStorage.setItem(operationStorage, value.id);
        setOperation(value);
      }
      return true;
    })().catch(() => false);
    const [listOk, currentOk] = await Promise.all([list, current]);
    if (generation.current === version) {
      setStatusVerified(listOk && currentOk);
      setError(listOk && currentOk ? '' : '일부 상태를 확인하지 못했습니다. 다시 조회해 주세요.');
    }
  }, [key, headers, operation, pending]);
  useEffect(() => {
    if (!key || !operation || terminal.has(operation.state)) return;
    const timer = window.setInterval(() => { void refresh(); }, 2000); return () => window.clearInterval(timer);
  }, [key, operation, refresh]);
  const submit = async () => {
    ++generation.current;
    setStatusVerified(false);
    const requestId = newRequestId();
    const requestInfo = JSON.stringify({ id: requestId, model: selection });
    sessionStorage.setItem(requestStorage, requestInfo); setUnknownRequest(requestInfo);
    setPending(true); setError(''); sessionStorage.setItem('model-switch-uncertain', '1');
    try {
      const result = await request<Operation>('/llm/switches', { method: 'POST', headers: { ...headers(), 'Idempotency-Key': requestId }, body: JSON.stringify({ model: selection }) });
      sessionStorage.setItem(operationStorage, result.id); setOperation(result); setStatusVerified(true);
      sessionStorage.removeItem('model-switch-uncertain'); sessionStorage.removeItem(requestStorage); setUnknownRequest(null); setUncertain(false);
    } catch (caught) { setUncertain(true); setError((caught instanceof Error ? caught.message : '전환 결과를 확인하지 못했습니다.') + ' 상태를 다시 조회하기 전까지 재제출하지 않습니다.'); }
    finally { setPending(false); }
  };
  return <section><h3>LLM 모델 전환</h3><p>이 변경은 서버 전체에 적용됩니다. 진행 중인 대화와 이미지 작업에 영향을 줄 수 있습니다.</p>
    <label>운영자 인증 <input type="password" autoComplete="off" disabled={pending} value={key} onChange={event => { ++generation.current; setStatusVerified(false); setKey(event.target.value); setModels(null); setOperation(null); }} /></label>
    <button onClick={() => void refresh()} disabled={!key || pending}>상태 조회</button>{error && <p role="alert">{error}</p>}{uncertain && <p role="alert">접수 결과를 확인할 수 없습니다. 관리자가 작업 기록을 확인해야 합니다.{unknownRequest && <> 요청 정보: <code>{unknownRequest}</code></>}</p>}
    {models && <><p>설정된 모델: {models.configured_model || '확인 불가'} · 로드된 모델: {models.loaded_model || '확인 불가'} · 준비: {models.ready ? '완료' : '확인 불가'}</p>
      <label>설치된 모델 <select value={selection} onChange={event => setSelection(event.target.value)}><option value="">선택</option>{models.models.map(model => <option key={model.name} value={model.name}>{model.name}</option>)}</select></label>
      <button onClick={() => void submit()} disabled={!selection || pending || uncertain || !statusVerified || !models.switch_enabled || Boolean(operation && !terminal.has(operation.state)) || operation?.state === 'recovery_required'}>전환</button>
      {!models.switch_enabled && <p>모델 전환 기능을 사용할 수 없습니다.</p>}</>}
    {operation && <p>작업 {operation.model}: {operation.state}{operation.error_code ? ` (${operation.error_code})` : ''}{operation.state === 'recovery_required' ? ' · 관리자 복구가 필요합니다.' : ''}</p>}
  </section>;
}
