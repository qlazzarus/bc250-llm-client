import { useEffect, useRef, useState } from 'react';
import type { WikiMemoryJob } from '../api';

const STATUS_LABEL: Record<string, string> = {
  pending: '대기 중', running: '판단 중', writing: '저장 중', completed: '저장 완료',
  skipped: '저장하지 않음', failed: '처리 실패', unknown: '저장 결과 확인 필요',
};
const KIND_LABEL: Record<NonNullable<WikiMemoryJob['memory_kind']>, string> = {
  fact: '사실', preference: '선호', plan: '계획', question: '질문', uncertain: '불확실',
};
const REASON_LABEL: Record<string, string> = {
  durable: '지속적으로 유용한 정보', stored: '저장 완료', duplicate: '기존 기록과 중복',
  question: '질문 또는 요청', greeting: '인사 또는 단순 요청', insufficient_context: '문맥이 부족함',
  not_durable: '일시적이거나 저장하기 부적절함', write_unknown: 'Wiki 저장 결과를 확인할 수 없음',
  processing_failed: '처리 중 오류', processing_interrupted: '서비스 재시작으로 중단됨',
};
const ACTIVE = new Set(['pending', 'running', 'writing']);

function labelFor(job: WikiMemoryJob) { return STATUS_LABEL[job.status] || job.status; }

function MemoryRecord({ job }: { job: WikiMemoryJob }) {
  return <article className={'wiki-memory-record wiki-memory-' + job.status}>
    <h4>{labelFor(job)}{job.explicit ? ' · 명시 요청' : ''}</h4>
    <dl>
      {job.memory_kind && <><dt>분류</dt><dd>{KIND_LABEL[job.memory_kind]}</dd></>}
      {job.reason_code && <><dt>판정</dt><dd>{REASON_LABEL[job.reason_code] || job.reason_code}</dd></>}
      {(job.title || job.document_id != null) && <><dt>대상</dt><dd>{job.title || '제목 없음'}{job.document_id != null ? ` (문서 ${job.document_id})` : ''}</dd></>}
      {job.error_message && <><dt>안내</dt><dd>{job.error_message}</dd></>}
    </dl>
    {job.source_text && <details><summary>선택한 사용자 발언</summary><pre>{job.source_text}</pre></details>}
    {job.previous_content && <details><summary>변경 전 본문</summary><pre>{job.previous_content}</pre></details>}
    {job.proposed_content && <details><summary>저장 시도한 본문</summary><pre>{job.proposed_content}</pre></details>}
  </article>;
}

function MemoryHistoryDialog({ jobs, onClose }: { jobs: WikiMemoryJob[]; onClose: () => void }) {
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
      <div className="wiki-memory-records">{jobs.map(job => <MemoryRecord key={job.generation_id} job={job} />)}</div>
    </section>
  </dialog>;
}

export function WikiMemoryStatus({ jobs }: { jobs: WikiMemoryJob[] }) {
  const [open, setOpen] = useState(false);
  if (!jobs.length) return null;
  const attention = jobs.find(job => job.needs_attention === 1 || job.status === 'unknown' || job.status === 'failed');
  const active = jobs.find(job => ACTIVE.has(job.status));
  const highlighted = attention || active;
  return <section className="wiki-memory-status" aria-label="Wiki 자동 기억 상태">
    {highlighted && <p role="status" aria-live="polite">Wiki 자동 기억: {labelFor(highlighted)}{highlighted.error_message ? ` · ${highlighted.error_message}` : ''}</p>}
    <button type="button" className="plain" onClick={() => setOpen(true)}>Wiki 자동 기억 기록 ({jobs.length})</button>
    {open && <MemoryHistoryDialog jobs={jobs} onClose={() => setOpen(false)} />}
  </section>;
}
