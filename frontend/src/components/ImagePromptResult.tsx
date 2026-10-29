import { useEffect, useRef, useState } from 'react';
import { newRequestId, promptModeLabel, type ImageCapabilities, type ImageJob, type ImageConfig, type ImagePresetName, type PromptMode } from '../api';
import { ImageJobStatus } from './ImageJobStatus';
import type { ImageDraftEditor } from '../main';

type ImageIntent = { intent: 'image_generation' | 'other'; subject: string | null; environment: string | null; style: string | null; lighting: string | null; composition: string | null; required_elements: string[]; forbidden_elements: string[]; exact_text: string[]; preferred_model: string | null; aspect_ratio: string | null };
export type ImagePromptConfig = ImageConfig & { recommendation_reason?: string; prompt_mode?: PromptMode; model_selection?: 'manual' | 'recommend'; structured_intent?: ImageIntent; negative_prompt_omitted?: boolean; preset_name?: ImagePresetName };

export function parseImagePromptConfig(content: string): ImagePromptConfig | undefined {
  try {
    const value = JSON.parse(content);
    if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined;
    if (typeof value.model_id !== 'string' || typeof value.prompt !== 'string' || !value.prompt.trim()) return undefined;
    if (!['width', 'height', 'steps'].every(key => Number.isInteger(value[key]) && value[key] > 0) || !Number.isFinite(value.cfg_scale)) return undefined;
    return value as ImagePromptConfig;
  } catch { return undefined; }
}

async function writeClipboard(text: string): Promise<boolean> {
  try { if (!navigator.clipboard?.writeText) return false; await navigator.clipboard.writeText(text); return true; }
  catch { return false; }
}

function availabilityMessage(capabilities: ImageCapabilities | undefined): string {
  if (!capabilities) return '이미지 생성 서비스 상태를 확인할 수 없습니다.';
  if (capabilities.available && capabilities.simulated) return 'CPU 테스트 엔진입니다. 결과는 실제 이미지 생성 모델의 출력이 아닙니다.';
  if (capabilities.available) return '';
  const reasons: Record<string, string> = {
    disabled: '이미지 생성 연동이 서버에서 비활성화되어 있습니다.', simulated_engine: 'Studio가 모의 엔진으로 동작 중입니다.',
    recovery_required: 'Studio 복구가 필요합니다.', model_unavailable: '이미지 모델을 실행할 수 없습니다.',
    model_profile_mismatch: 'Studio 모델 프로필이 현재 프롬프트 계약과 다릅니다.',
  };
  return reasons[capabilities.reason || ''] || '이미지 생성 서비스를 지금 사용할 수 없습니다.';
}

type Props = {
  conversationId: string; messageId: string; config: ImagePromptConfig; capabilities: ImageCapabilities | undefined; draft: ImageDraftEditor | undefined; jobs: ImageJob[];
  onEnsureDraft: (conversationId: string, messageId: string, config: Pick<ImageDraftEditor, 'prompt' | 'width' | 'height' | 'preset_name'>) => Promise<void>;
  onUpdateDraft: (messageId: string, update: Partial<Pick<ImageDraftEditor, 'prompt' | 'width' | 'height' | 'generation_config' | 'preset_name'>>, fallback: ImageDraftEditor) => void;
  onPersistDraft: (conversationId: string, messageId: string, draft: ImageDraftEditor) => Promise<unknown>;
  onSubmitImageJob: (conversationId: string, messageId: string, draft: ImageDraftEditor, requestId: string) => Promise<ImageJob>;
  onCancelImageJob: (job: ImageJob) => Promise<void>;
};

export function ImagePromptResult({ conversationId, messageId, config, capabilities, draft, jobs, onEnsureDraft, onUpdateDraft, onPersistDraft, onSubmitImageJob, onCancelImageJob }: Props) {
  const [copied, setCopied] = useState<'prompt' | 'json' | undefined>();
  const [manualCopy, setManualCopy] = useState<{ label: string; text: string }>();
  const [saveState, setSaveState] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle');
  const [saveError, setSaveError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [submittedJob, setSubmittedJob] = useState<ImageJob>();
  const requestIdRef = useRef<string | undefined>(undefined);
  const { recommendation_reason: recommendationReason, prompt_mode: promptMode, model_selection: modelSelection, structured_intent: intent, negative_prompt_omitted: negativeOmitted, preset_name: initialPreset, ...generationConfig } = config;
  const baseDraft: ImageDraftEditor = { prompt: config.prompt, width: config.width, height: config.height, generation_config: generationConfig, preset_name: initialPreset ?? null, revision: 0, persisted: false, dirty: false };
  const current = draft || baseDraft;
  const selected = current.generation_config?.model_id ? current.generation_config : generationConfig;
  const model = capabilities?.models.find(item => item.model_id === selected.model_id);
  const resolutions = model?.supported_resolutions || [[512, 512], [768, 768]];
  const supportedResolution = resolutions.some(([width, height]) => width === current.width && height === current.height);
  const output = { ...selected, prompt: current.prompt, width: current.width, height: current.height };
  const changeOptions = (update: Partial<ImageConfig>) => onUpdateDraft(messageId, { generation_config: { ...output, ...update }, ...((update.steps !== undefined && update.steps !== output.steps) || (update.cfg_scale !== undefined && update.cfg_scale !== output.cfg_scale) ? { preset_name: 'custom' as const } : {}) }, current);
  const negativeAllowed = model?.generation_options.negative_prompt.mode !== 'unsupported';
  const requirements = model?.generation_options.negative_prompt.requires || {};
  const invalidNegative = Boolean(output.negative_prompt && Object.entries(requirements).some(([key, values]) => !values.includes(output[key as keyof ImageConfig] as number | string)));
  const unsupportedLora = output.lora_enabled !== undefined && !model?.lora_toggle_supported;
  const invalidLoraTag = output.lora_enabled === false && /<lora:/i.test(current.prompt);
  const canSubmit = Boolean(capabilities?.available && model?.execution_enabled && supportedResolution && current.prompt.trim() && !invalidNegative && !unsupportedLora && !invalidLoraTag);
  const json = JSON.stringify(output, null, 2);

  useEffect(() => { void onEnsureDraft(conversationId, messageId, { prompt: config.prompt, width: config.width, height: config.height, preset_name: initialPreset ?? null }); }, [conversationId, messageId, config, initialPreset, onEnsureDraft]);
  useEffect(() => { requestIdRef.current = undefined; setSubmittedJob(undefined); }, [current.prompt, current.width, current.height, current.generation_config, current.preset_name]);
  useEffect(() => {
    if (!draft?.dirty) return;
    const timer = window.setTimeout(() => {
      setSaveState('saving'); setSaveError('');
      void onPersistDraft(conversationId, messageId, draft).then(() => setSaveState('saved')).catch(error => {
        setSaveState('error'); setSaveError(error instanceof Error ? error.message : '초안을 저장하지 못했습니다.');
      });
    }, 650);
    return () => window.clearTimeout(timer);
  }, [conversationId, draft, messageId, onPersistDraft]);

  const copy = async (kind: 'prompt' | 'json', text: string) => {
    if (await writeClipboard(text)) {
      setCopied(kind); setManualCopy(undefined);
      window.setTimeout(() => setCopied(currentKind => currentKind === kind ? undefined : currentKind), 1600);
    } else { setCopied(undefined); setManualCopy({ label: kind === 'prompt' ? '영어 프롬프트' : '설정 JSON', text }); }
  };
  const saveNow = async () => {
    setSaveState('saving'); setSaveError('');
    try { await onPersistDraft(conversationId, messageId, current); setSaveState('saved'); }
    catch (error) { setSaveState('error'); setSaveError(error instanceof Error ? error.message : '초안을 저장하지 못했습니다.'); }
  };
  const submit = async () => {
    if (!canSubmit || submitting) return;
    setSubmitting(true); setSaveError('');
    const requestId = requestIdRef.current || newRequestId();
    requestIdRef.current = requestId;
    try { const job = await onSubmitImageJob(conversationId, messageId, current, requestId); setSubmittedJob(job); setSaveState('saved'); requestIdRef.current = undefined; }
    catch (error) { setSaveState('error'); setSaveError(error instanceof Error ? error.message : '이미지 생성 요청을 보내지 못했습니다.'); }
    finally { setSubmitting(false); }
  };

  return <section className="image-prompt-result" aria-label="이미지 생성 프롬프트 설정">
    {promptMode && <p className="muted">프롬프트 방식: {promptModeLabel(promptMode)}</p>}
    {recommendationReason && <p className="muted">{modelSelection === 'manual' ? '선택한 모델' : '추천'}: {config.model_id} · {recommendationReason} (참고용, 품질 비교 미검증)</p>}
    {negativeOmitted && <p className="image-prompt-availability">처음 선택한 모델의 프리셋에서는 생성된 Negative Prompt를 사용할 수 없어 제외했습니다. 금지 요소를 영어 프롬프트에서 확인해 주세요.</p>}
    {intent && <details><summary>요청 분석 확인</summary><p className="muted">AI가 추출한 내용입니다. 원문과 영어 프롬프트를 함께 확인해 주세요.</p>
      <dl>{([['분류', intent.intent === 'image_generation' ? '이미지 생성' : '기타'], ['피사체', intent.subject], ['배경', intent.environment], ['스타일', intent.style], ['조명', intent.lighting], ['구도', intent.composition], ['필수 요소', intent.required_elements.join(', ')], ['금지 요소', intent.forbidden_elements.join(', ')], ['표시 문구', intent.exact_text.join(', ')], ['지정 모델', intent.preferred_model], ['화면 비율', intent.aspect_ratio]] as const).filter(([, value]) => value).map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
    </details>}
    <label className="image-prompt-editor">{promptMode === 'original' ? '원본 프롬프트' : '영어 프롬프트'}
      <textarea value={current.prompt} maxLength={8000} onChange={event => onUpdateDraft(messageId, { prompt: event.target.value }, current)} />
    </label>
    <details className="image-options-disclosure"><summary>모델 및 세부 설정 · {model?.display_name || selected.model_id} · {current.preset_name || '기록 없음'}</summary><div className="image-prompt-options">
      <label>대상 모델<select value={selected.model_id} onChange={event => {
        const target = capabilities?.models.find(item => item.model_id === event.target.value);
        if (!target) return;
        const preset = target.presets.balanced;
        onUpdateDraft(messageId, { width: preset.width, height: preset.height, preset_name: 'balanced', generation_config: {
          model_id: target.model_id, prompt: current.prompt, width: preset.width, height: preset.height, steps: preset.steps, cfg_scale: preset.cfg_scale,
          ...(target.lora_toggle_supported && target.server_settings?.lora ? { lora_enabled: target.server_settings.lora.enabled } : {}),
        } }, current);
      }}>{!model && <option value={selected.model_id}>{selected.model_id}</option>}{capabilities?.models.map(item => <option key={item.model_id} value={item.model_id} disabled={Boolean(item.simulated && config.model_id !== 'fake')}>{item.display_name}{item.execution_enabled ? '' : ' · 실행 불가'}</option>)}</select></label>
      {selected.model_id !== config.model_id && <p className="muted">모델을 바꿔도 기존 프롬프트는 다시 작성되지 않습니다. 새 모델에 맞추려면 입력 단계에서 모델을 선택하고 다시 작성하세요.</p>}
      <label>프리셋<select value={current.preset_name ?? ''} onChange={event => {
        const preset = model?.presets[event.target.value];
        if (preset) onUpdateDraft(messageId, { width: preset.width, height: preset.height, preset_name: event.target.value as ImagePresetName, generation_config: { ...output, width: preset.width, height: preset.height, steps: preset.steps, cfg_scale: preset.cfg_scale } }, current);
      }}><option value="" disabled>기록 없음</option><option value="custom" disabled>사용자 설정</option>{Object.entries(model?.presets || {}).map(([key, preset]) => <option value={key} key={key}>{key} · {preset.steps} steps{preset.validation_status ? ` · ${preset.validation_status}` : ''}</option>)}</select></label>
      <label>해상도<select value={supportedResolution ? `${current.width}x${current.height}` : ''} onChange={event => {
        const [width, height] = event.target.value.split('x').map(Number); onUpdateDraft(messageId, { width, height, preset_name: 'custom' }, current);
      }}><option value="" disabled>선택</option>{resolutions.map(([width, height]) => <option key={`${width}x${height}`} value={`${width}x${height}`}>{width} × {height}</option>)}</select></label>
      <label>Steps<input type="number" min={1} max={100} value={output.steps} onChange={event => changeOptions({ steps: Number(event.target.value) })} /></label>
      <label>CFG{model?.generation_options.cfg_scale.allowed_values?.length
        ? <select value={output.cfg_scale} onChange={event => changeOptions({ cfg_scale: Number(event.target.value) })}>{model.generation_options.cfg_scale.allowed_values.map(value => <option key={value} value={value}>{value}</option>)}</select>
        : <input type="number" min={model?.generation_options.cfg_scale.minimum ?? 0} max={model?.generation_options.cfg_scale.maximum ?? 30} step={0.1} value={output.cfg_scale} onChange={event => changeOptions({ cfg_scale: Number(event.target.value) })} />}</label>
      <label>Seed (-1: 무작위)<input type="number" min={-1} max={2147483631} value={output.seed ?? -1} onChange={event => changeOptions({ seed: Number(event.target.value) })} /></label>
      {(['sampling_method', 'scheduler'] as const).map(key => <label key={key}>{key}<select value={output[key] ?? ''} onChange={event => changeOptions({ [key]: event.target.value || undefined })}><option value="">모델 기본값{model?.generation_options[key].default ? ` (${model.generation_options[key].default})` : ''}</option>{model?.generation_options[key].allowed_values?.map(value => <option key={value} value={value}>{value}</option>)}</select></label>)}
      {negativeAllowed && <label>Negative prompt<textarea value={output.negative_prompt ?? ''} maxLength={4000} onChange={event => changeOptions({ negative_prompt: event.target.value })} /></label>}
      {model?.lora_toggle_supported && <label>MOSS LoRA <select value={String(output.lora_enabled ?? model.server_settings?.lora?.enabled ?? false)} onChange={event => changeOptions({ lora_enabled: event.target.value === 'true' })}><option value="false">OFF · 기본 MOSS</option><option value="true">ON · Studio 설정 사용</option></select></label>}
      {unsupportedLora && <p className="image-prompt-error">Studio의 작업별 LoRA 지원을 확인할 수 없습니다. <button type="button" className="plain" onClick={() => changeOptions({ lora_enabled: undefined })}>작업별 LoRA 선택 지우기</button></p>}
      {invalidLoraTag && <p className="image-prompt-error">LoRA OFF 상태에서는 프롬프트의 LoRA 태그를 제거해 주세요.</p>}
      {invalidNegative && <p className="image-prompt-error">Negative prompt 사용에 필요한 옵션: {JSON.stringify(requirements)}</p>}
      {model?.generation_options.cfg_scale.note && <p className="muted">{model.generation_options.cfg_scale.note}</p>}
      {model?.server_settings && <p className="muted">Studio 기본 설정: {JSON.stringify(model.server_settings)}</p>}
      {model && !model.execution_enabled && <p className="image-prompt-error">이 모델은 현재 Studio에서 실행할 수 없습니다. 모델 설치 상태와 옵션 계약을 확인하세요.</p>}

    </div></details>
    {invalidNegative && <p className="image-prompt-error">Negative prompt 사용에 필요한 옵션을 확인해 주세요.</p>}
    {unsupportedLora && <p className="image-prompt-error">작업별 LoRA 설정을 확인해 주세요.</p>}
    {invalidLoraTag && <p className="image-prompt-error">LoRA OFF 상태에서는 프롬프트의 LoRA 태그를 제거해 주세요.</p>}
    {model && !model.execution_enabled && <p className="image-prompt-error">이 모델은 현재 실행할 수 없습니다. 설정을 확인해 주세요.</p>}
    {!supportedResolution && <p className="image-prompt-legacy-resolution">선택한 모델이 지원하는 해상도 조합을 다시 선택해 주세요.</p>}
    {availabilityMessage(capabilities) && <p className="image-prompt-availability">{availabilityMessage(capabilities)}</p>}
    <div className="image-prompt-actions">
      <button type="button" className="plain" disabled={saveState === 'saving'} onClick={() => void saveNow()}>{saveState === 'saving' ? '초안 저장 중…' : saveState === 'saved' ? '초안 저장됨' : '초안 저장'}</button>
      <button type="button" className="plain" disabled={JSON.stringify(output) === JSON.stringify(generationConfig) && (current.preset_name ?? null) === (initialPreset ?? null)}
        onClick={() => onUpdateDraft(messageId, { prompt: config.prompt, width: config.width, height: config.height, generation_config: generationConfig, preset_name: initialPreset ?? null }, current)}>원문으로 되돌리기</button>
      <button type="button" onClick={() => void copy('prompt', current.prompt)}>{copied === 'prompt' ? '프롬프트 복사됨' : '프롬프트 복사'}</button>
      <button type="button" className="plain" onClick={() => void copy('json', json)}>{copied === 'json' ? '설정 JSON 복사됨' : '설정 JSON 복사'}</button>
      <button type="button" className="image-generate" disabled={!canSubmit || submitting} onClick={() => void submit()}>{submitting ? '요청 중…' : requestIdRef.current ? '생성 요청 재시도' : '이미지 생성 요청'}</button>
    </div>
    {saveState === 'error' && <p className="image-prompt-error">{saveError}</p>}
    {submittedJob && <p className="image-prompt-submitted">이미지 생성 요청이 접수되었습니다. 현재 상태: {submittedJob.status}</p>}
    <ImageJobStatus jobs={jobs} onCancel={onCancelImageJob} />
    {manualCopy && <label className="image-prompt-manual-copy">자동 복사를 사용할 수 없습니다. 아래 {manualCopy.label}을 선택해 복사하세요.
      <textarea readOnly value={manualCopy.text} onFocus={event => event.currentTarget.select()} aria-label={manualCopy.label + ' 수동 복사'} />
    </label>}
  </section>;
}
