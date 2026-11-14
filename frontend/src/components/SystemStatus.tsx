import { Activity, Database, RefreshCw } from 'lucide-react';
import Button from '@mui/material/Button';
import { ModelControl } from './ModelControl';
import './system-status.css';

type Props = { gate: unknown; gateError: string; loading: boolean; models: string[]; modelsUnavailable: boolean; onRefresh: () => void; onSwitchSucceeded: () => void };

export function SystemStatus({ gate, gateError, loading, models, modelsUnavailable, onRefresh, onSwitchSucceeded }: Props) {
  const gateLabel = loading ? '조회 중' : gateError ? '조회 실패' : gate === undefined ? '확인 전' : '응답 확인';
  return <section className="system-panel" aria-labelledby="system-title">
    <div className="system-page-heading"><div><span className="system-eyebrow">WORKSPACE / SYSTEM</span><h2 id="system-title">시스템 상태</h2><p>모델과 LLM Gate의 상태를 확인하고 관리합니다.</p></div>
      <Button variant="outlined" startIcon={<RefreshCw size={16} />} disabled={loading} onClick={onRefresh}>새로고침</Button></div>
    <div className="system-overview" aria-label="상태 요약">
      <section className="system-card system-overview-card"><div className="system-card-icon"><Activity size={18} /></div><div><span>LLM Gate</span><strong>{gateLabel}</strong><small>{gateError || '서버의 Gate 상태 응답'}</small></div></section>
      <section className="system-card system-overview-card"><div className="system-card-icon"><Database size={18} /></div><div><span>사용 가능 모델</span><strong>{modelsUnavailable ? '조회 실패' : `${models.length}개`}</strong><small>현재 서버에서 제공하는 모델</small></div></section>
    </div>
    <div className="system-grid">
      <section className="system-card system-models"><div className="system-section-heading"><div><h3>사용 가능 모델</h3><p>대화 요청에 사용할 수 있는 모델 목록입니다.</p></div></div>
        {models.length ? <ul>{models.map(model => <li key={model}><span className="system-model-dot" aria-hidden="true" /><span>{model}</span></li>)}</ul> : <p className="system-empty">{modelsUnavailable ? '모델 목록을 가져오지 못했습니다.' : '표시할 모델이 없습니다.'}</p>}
      </section>
      <div className="system-card system-switch"><ModelControl onSwitchSucceeded={onSwitchSucceeded} /></div>
    </div>
    <section className="system-card system-gate"><div className="system-section-heading"><div><h3>LLM Gate</h3><p>이미지 작업 중에는 image_priority 정책에 따라 LLM이 대기할 수 있습니다.</p></div><span className="system-status-pill">{gateLabel}</span></div>
      {gateError && <p className="system-error" role="alert">{gateError}</p>}
      {gate !== undefined && <details><summary>상태 상세 보기</summary><pre>{JSON.stringify(gate, null, 2)}</pre></details>}
    </section>
  </section>;
}
