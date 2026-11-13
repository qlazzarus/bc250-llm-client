import { useEffect, useRef, useState } from 'react';
import { request } from '../api';
import type { WikiMemoryJob } from '../api';

const STATUS_LABEL: Record<string, string> = {
  pending: '대기 중', running: '판단 중', writing: '저장 중', completed: '저장 완료',
  skipped: '저장하지 않음', failed: '처리 실패', unknown: '저장 결과 확인 필요',
  awaiting_confirmation: '변경 확인 필요',
};
const KIND_LABEL: Record<NonNullable<WikiMemoryJob['memory_kind']>, string> = {
  fact: '사실', preference: '선호', plan: '계획', question: '질문', uncertain: '불확실',
};
const REASON_LABEL: Record<string, string> = {
  durable: '지속적으로 유용한 정보', stored: '저장 완료', duplicate: '기존 기록과 중복',
  question: '질문 또는 요청', greeting: '인사 또는 단순 요청', insufficient_context: '문맥이 부족함',
  not_durable: '일시적이거나 저장하기 부적절함', write_unknown: 'Wiki 저장 결과를 확인할 수 없음',
  processing_failed: '처리 중 오류', processing_interrupted: '서비스 재시작으로 중단됨',
  conflict: '기존 값과 다름', conflict_kept: '기존 값을 유지함',
};
const ACTIVE = new Set(['pending', 'running', 'writing']);

function labelFor(job: WikiMemoryJob) { return STATUS_LABEL[job.status] || job.status; }

function MemoryRecord({ job, conversationId, onChanged }: { job: WikiMemoryJob; conversationId?: string; onChanged?: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [detail, setDetail] = useState<WikiMemoryJob>();
  const current = detail?.updated_at === job.updated_at ? detail : job;
  const loadDetail = async () => {
    if (!conversationId || busy) return;
    setBusy(true); setError('');
    try {
      const result = await request<{ data: WikiMemoryJob }>(`/conversations/${encodeURIComponent(conversationId)}/wiki-memory/${encodeURIComponent(job.generation_id)}`);
      setDetail(result.data);
    } catch (caught) { setError(caught instanceof Error ? caught.message : '상세 기록을 가져오지 못했습니다.'); }
    finally { setBusy(false); }
  };
  const confirm = async (decision: 'approve' | 'keep') => {
    if (!conversationId || !current.confirmation_token || busy) return;
    setBusy(true); setError('');
    try {
      await request(`/conversations/${encodeURIComponent(conversationId)}/wiki-memory/${encodeURIComponent(job.generation_id)}/confirmation`, {
        method: 'POST', body: JSON.stringify({ decision, token: current.confirmation_token }),
      });
      onChanged?.();
    } catch (caught) { setError(caught instanceof Error ? caught.message : '처리하지 못했습니다.'); }
    finally { setBusy(false); }
  };
  return <article className={'wiki-memory-record wiki-memory-' + job.status}>
    <h4>{labelFor(job)}{job.explicit ? ' · 명시 요청' : ''}</h4>
    <dl>
      {job.memory_kind && <><dt>분류</dt><dd>{KIND_LABEL[job.memory_kind]}</dd></>}
      {job.reason_code && <><dt>판정</dt><dd>{REASON_LABEL[job.reason_code] || job.reason_code}</dd></>}
      {(job.title || job.document_id != null) && <><dt>대상</dt><dd>{job.title || '제목 없음'}{job.document_id != null ? ` (문서 ${job.document_id})` : ''}{job.store_id === 'legacy_external' ? ' · 기존 Wiki 기록' : ''}</dd></>}
      {job.error_message && <><dt>안내</dt><dd>{job.error_message}</dd></>}
    </dl>
    {current.source_text === undefined && conversationId && <button type="button" disabled={busy} onClick={() => void loadDetail()}>상세 불러오기</button>}
    {current.status === 'awaiting_confirmation' && current.confirmation_token && conversationId && <section aria-label="기억 변경 확인">
      <p>기존 값: {current.previous_content}</p><p>새 값: {current.proposed_content}</p>
      <p>이 공유 기억을 변경할까요?</p>
      <button type="button" disabled={busy} onClick={() => void confirm('approve')}>변경</button>
      <button type="button" disabled={busy} onClick={() => void confirm('keep')}>기존 값 유지</button>
    </section>}
    {error && <p role="alert">{error}</p>}
    {current.source_text && <details><summary>선택한 사용자 발언</summary><pre>{current.source_text}</pre></details>}
    {current.previous_content && <details><summary>변경 전 본문</summary><pre>{current.previous_content}</pre></details>}
    {current.proposed_content && <details><summary>저장 시도한 본문</summary><pre>{current.proposed_content}</pre></details>}
  </article>;
}

function MemoryHistoryDialog({ jobs, onClose, conversationId, onChanged }: { jobs: WikiMemoryJob[]; onClose: () => void; conversationId?: string; onChanged?: () => void }) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = dialogRef.current;
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    if (dialog && !dialog.open && typeof dialog.showModal === 'function') dialog.showModal();
    return () => { if (dialog?.open && typeof dialog.close === 'function') dialog.close(); previous?.focus(); };
  }, []);
  return <dialog ref={dialogRef} className="modal-dialog wiki-memory-dialog" aria-labelledby="wiki-memory-dialog-title"
    onCancel={event => { event.preventDefault(); onClose(); }}>
    <section className="modal wiki-memory-history">
      <header><h2 id="wiki-memory-dialog-title">Wiki 자동 기억 기록</h2><button type="button" className="plain" onClick={onClose}>닫기</button></header>
      <p className="muted">원문과 Wiki 본문은 이 상세 화면에서만 확인할 수 있습니다.</p>
      <div className="wiki-memory-records">{jobs.map(job => <MemoryRecord key={job.generation_id} job={job} conversationId={conversationId} onChanged={onChanged} />)}</div>
    </section>
  </dialog>;
}

export function WikiMemoryStatus({ jobs, conversationId, onChanged }: { jobs: WikiMemoryJob[]; conversationId?: string; onChanged?: () => void }) {
  const [open, setOpen] = useState(false);
  if (!jobs.length) return null;
  const attention = jobs.find(job => job.needs_attention === 1 || job.status === 'unknown' || job.status === 'failed');
  const active = jobs.find(job => ACTIVE.has(job.status));
  const highlighted = attention || active;
  return <section className="wiki-memory-status" aria-label="Wiki 자동 기억 상태">
    {highlighted && <p role="status" aria-live="polite">Wiki 자동 기억: {labelFor(highlighted)}{highlighted.error_message ? ` · ${highlighted.error_message}` : ''}</p>}
    <button type="button" className="plain" onClick={() => setOpen(true)}>Wiki 자동 기억 기록 ({jobs.length})</button>
    {open && <MemoryHistoryDialog jobs={jobs} onClose={() => setOpen(false)} conversationId={conversationId} onChanged={onChanged} />}
  </section>;
}
