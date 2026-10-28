import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { promptModeLabel, request, requestBlob, type ImageJob } from '../api';

const STATUS_LABEL: Record<ImageJob['status'], string> = {
  submission_pending: '제출 처리 중', submission_unknown: '제출 확인 필요', queued: '대기열',
  waiting_for_gpu: 'GPU 대기', running: '생성 중', completed: '완료', failed: '실패', cancelled: '취소됨', interrupted: '중단됨',
};
const ACTIVE = new Set<ImageJob['status']>(['submission_pending', 'submission_unknown', 'queued', 'waiting_for_gpu', 'running']);

function ImageLightbox({ url, assetId, onClose }: { url: string; assetId: string; onClose: () => void }) {
  const dialogRef = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    dialog.showModal();
    return () => { if (dialog.open) dialog.close(); };
  }, []);

  return createPortal(<dialog ref={dialogRef} className="image-lightbox" aria-label="생성 이미지 확대 보기" onClose={onClose}
    onClick={event => { if (event.target === event.currentTarget) onClose(); }}>
    <div className="image-lightbox-content">
      <div className="image-lightbox-toolbar"><span>생성 이미지</span><div>
        <a href={url} download={'bc250-' + assetId + '.png'}>다운로드</a>
        <button type="button" className="image-lightbox-close" aria-label="확대 이미지 닫기" onClick={onClose}>×</button>
      </div></div>
      <img src={url} alt="확대된 생성 이미지" />
    </div>
  </dialog>, document.body);
}

function JobAssets({ job }: { job: ImageJob }) {
  const [urls, setUrls] = useState<Record<string, string>>({});
  const [error, setError] = useState('');
  const [expandedAssetId, setExpandedAssetId] = useState<string>();
  const assetKey = job.asset_ids.join('|');

  useEffect(() => {
    if (!job.asset_ids.length) { setUrls({}); return; }
    let live = true;
    const created: string[] = [];
    setError('');
    void Promise.all(job.asset_ids.map(async assetId => {
      const blob = await requestBlob('/image-jobs/' + job.id + '/assets/' + encodeURIComponent(assetId) + '/file');
      return [assetId, URL.createObjectURL(blob)] as const;
    })).then(items => {
      if (!live) { items.forEach(([, url]) => URL.revokeObjectURL(url)); return; }
      items.forEach(([, url]) => created.push(url));
      setUrls(Object.fromEntries(items));
    }).catch(caught => {
      if (live) setError(caught instanceof Error ? caught.message : '결과 이미지를 불러오지 못했습니다.');
    });
    return () => { live = false; created.forEach(url => URL.revokeObjectURL(url)); };
  }, [assetKey, job.id]);

  if (!job.asset_ids.length) return null;
  return <div className="image-job-assets">{Object.entries(urls).map(([assetId, url]) => <figure key={assetId}>
    <button type="button" className="image-thumbnail" aria-label="생성 이미지 확대 보기" onClick={() => setExpandedAssetId(assetId)}>
      <img src={url} alt="생성 결과" /><span>확대 보기</span>
    </button>
    <a href={url} download={'bc250-' + assetId + '.png'}>다운로드</a>
  </figure>)}{error && <p className="image-prompt-error">{error}</p>}
    {expandedAssetId && urls[expandedAssetId] && <ImageLightbox url={urls[expandedAssetId]} assetId={expandedAssetId} onClose={() => setExpandedAssetId(undefined)} />}
  </div>;
}

function JobEvaluation({ job }: { job: ImageJob }) {
  const [score, setScore] = useState(job.evaluation?.score?.toString() || '');
  const [savedScore, setSavedScore] = useState(job.evaluation?.score ?? null);
  const [note, setNote] = useState(job.evaluation?.note || '');
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState('');

  const save = async () => {
    setSaving(true); setStatus('');
    try {
      const response = await request<{ data: ImageJob }>('/image-jobs/' + job.id + '/evaluation', {
        method: 'PUT', body: JSON.stringify({ score: score ? Number(score) : null, note }),
      });
      setScore(response.data.evaluation.score?.toString() || '');
      setSavedScore(response.data.evaluation.score);
      setNote(response.data.evaluation.note);
      setStatus('평가를 저장했습니다.');
    } catch (error) { setStatus(error instanceof Error ? error.message : '평가를 저장하지 못했습니다.'); }
    finally { setSaving(false); }
  };
  return <details><summary>사용자 평가{savedScore ? ` · ${savedScore}/5` : ''}</summary>
    <label>점수 <select value={score} disabled={saving} onChange={event => setScore(event.target.value)}>
      <option value="">평가 없음</option>{[1, 2, 3, 4, 5].map(value => <option value={value} key={value}>{value} / 5</option>)}
    </select></label>
    <label>메모<textarea value={note} maxLength={1000} disabled={saving} onChange={event => setNote(event.target.value)} /></label>
    <button type="button" className="plain" disabled={saving} onClick={() => void save()}>{saving ? '저장 중…' : '평가 저장'}</button>
    {status && <p role="status">{status}</p>}
  </details>;
}

function ComparisonExport({ job }: { job: ImageJob }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const download = async () => {
    setBusy(true); setError('');
    try {
      const record = await request<unknown>('/image-jobs/' + encodeURIComponent(job.id) + '/comparison-record');
      const url = URL.createObjectURL(new Blob([JSON.stringify(record, null, 2)], { type: 'application/json' }));
      const link = document.createElement('a');
      link.href = url; link.download = 'bc250-comparison-' + job.id.replace(/[^a-zA-Z0-9_-]/g, '_') + '.json';
      document.body.appendChild(link); link.click(); link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (caught) { setError(caught instanceof Error ? caught.message : '비교 기록을 가져오지 못했습니다.'); }
    finally { setBusy(false); }
  };
  return <div><button type="button" className="plain" disabled={busy} onClick={() => void download()}>{busy ? '기록 준비 중…' : '비교 기록 저장 (JSON)'}</button>
    {error && <p className="image-prompt-error" role="alert">{error}</p>}</div>;
}

export function ImageJobStatus({ jobs, onCancel }: { jobs: ImageJob[]; onCancel: (job: ImageJob) => Promise<void> }) {
  const [cancelling, setCancelling] = useState<string>();
  const [cancelError, setCancelError] = useState('');
  if (!jobs.length) return null;
  return <section className="image-job-status" aria-label="이미지 생성 작업 상태">
    <h4>이미지 작업</h4>{jobs.map(job => <article key={job.id} className={'image-job image-job-' + job.status}>
      <div><b>{STATUS_LABEL[job.status]}</b>{job.cancel_requested && job.status !== 'cancelled' && <small>취소 요청을 전달했습니다. Studio의 최종 상태를 기다리는 중입니다.</small>}
        {job.generation_config?.model_id === 'fake' && <small>CPU 테스트 결과</small>}
        {job.prompt_mode && <small>프롬프트 방식: {promptModeLabel(job.prompt_mode)}</small>}
        {job.status === 'submission_unknown' && <small>Studio 접수 여부를 확인해야 합니다. 자동으로 다시 요청하지 않습니다.</small>}
        {job.error && <small>{job.error.message}</small>}</div>
      {ACTIVE.has(job.status) && job.upstream_job_id && !job.cancel_requested && <button type="button" className="plain" disabled={cancelling === job.id} onClick={() => {
        setCancelling(job.id); setCancelError(''); void onCancel(job).catch(error => setCancelError(error instanceof Error ? error.message : '취소 요청에 실패했습니다.')).finally(() => setCancelling(undefined));
      }}>{cancelling === job.id ? '취소 요청 중…' : '취소 요청'}</button>}
      <details><summary>생성 기록</summary>
        <p>모델: {job.generation_config?.model_id || '기록 없음'}{job.studio_metadata?.quantization ? ` · ${job.studio_metadata.quantization}` : ''}</p>
        <p>프리셋: {job.preset_name === 'custom' ? '사용자 설정' : job.preset_name || '기록 없음'} · {job.width} × {job.height} · {job.steps} steps · CFG {job.cfg_scale}</p>
        <p>실제 Seed: {job.studio_metadata?.seed ?? '확인 전'} · 실행 시간: {job.studio_metadata?.duration_ms != null ? `${(job.studio_metadata.duration_ms / 1000).toFixed(1)}초` : '확인 전'}</p>
        {job.studio_metadata?.lora && <p>LoRA: {job.studio_metadata.lora.enabled ? `ON${job.studio_metadata.lora.strength != null ? ` · 강도 ${job.studio_metadata.lora.strength}` : ''}` : 'OFF'}</p>}
        <p>프롬프트: {job.prompt}</p>
        <ComparisonExport job={job} />
      </details>
      <JobAssets job={job} />
      {job.status === 'completed' && job.asset_ids.length > 0 && <JobEvaluation job={job} />}
    </article>)}{cancelError && <p className="image-prompt-error">{cancelError}</p>}</section>;
}
