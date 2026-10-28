const API = '/llm/api/v1';

export type Mode = 'quick' | 'roleplay' | 'image_prompt';
export type PromptMode = 'ai_optimized' | 'direct_translation' | 'original';
export type ImagePresetName = 'fast' | 'balanced' | 'quality' | 'custom';
export const promptModeLabel = (mode: PromptMode) => ({ ai_optimized: 'AI 최적화', direct_translation: '직접 번역', original: '원본 그대로' })[mode];
export type ResponseLength = 'short' | 'medium' | 'long';
export type ModeSettings = Record<string, string>;
export type Message = { id: string; role: 'user' | 'assistant'; content: string; created_at: string };
export type MessagePage = { data: Message[]; next_before: string | null; has_more: boolean };
export type Conversation = {
  id: string; title: string; mode: Mode; context_policy: 'none' | 'recent'; mode_settings: ModeSettings;
  response_length: ResponseLength; summary: string; summary_up_to_message_id: string | null;
  summary_status: 'idle' | 'pending' | 'running' | 'failed'; summary_error: string | null;
  created_at: string; updated_at: string;
};
export type Generation = {
  id: string; status: 'pending' | 'completed' | 'failed' | 'cancelled'; finish_reason: string | null;
  error: { code: string; message: string } | null; model_id: string | null; requested_max_tokens: number | null;
  estimated_input_tokens: number | null; usage: Record<string, number> | null; queue_duration_ms: number | null; generation_duration_ms: number | null;
};
export type GenerationOption = { mode: string; default?: number | string | null; allowed_values?: (number | string)[]; minimum?: number; maximum?: number; note?: string; requires?: Record<string, (number | string)[]> };
export type ImageModel = {
  model_id: string; display_name: string; supported_resolutions: number[][]; execution_enabled: boolean;
  simulated?: boolean;
  presets: Record<string, { width: number; height: number; steps: number; cfg_scale: number; validation_status?: string }>;
  generation_options: Record<string, GenerationOption>; server_settings?: { lora?: { enabled: boolean; strength?: number; trigger?: string }; flow_shift?: number }; lora_toggle_supported?: boolean;
};
export type ImageConfig = { model_id: string; prompt: string; width: number; height: number; steps: number; cfg_scale: number; seed?: number; negative_prompt?: string; sampling_method?: string; scheduler?: string; lora_enabled?: boolean };
export type ImageCapabilities = {
  enabled: boolean; available: boolean; simulated?: boolean; reason: string | null; models: ImageModel[]; model: ImageModel;
};
export type ImageDraft = {
  source_message_id: string; conversation_id: string; prompt: string; width: number; height: number; generation_config?: ImageConfig;
  preset_name?: ImagePresetName | null;
  revision: number; created_at: string; updated_at: string;
};
export type ImageJob = {
  id: string; request_id: string; conversation_id: string; source_message_id: string; draft_revision: number;
  prompt: string; width: number; height: number; steps: number; cfg_scale: number; upstream_job_id: string | null;
  prompt_mode: PromptMode | null;
  preset_name: ImagePresetName | null;
  generation_config?: ImageConfig;
  studio_metadata: { seed?: number; started_at?: string; completed_at?: string; duration_ms?: number; quantization?: string; lora?: { enabled: boolean; strength?: number } };
  evaluation: { score: number | null; note: string; updated_at: string | null };
  status: 'submission_pending' | 'submission_unknown' | 'queued' | 'waiting_for_gpu' | 'running' | 'completed' | 'failed' | 'cancelled' | 'interrupted';
  error: { code: string; message: string } | null; asset_ids: string[]; cancel_requested: boolean;
  created_at: string; updated_at: string;
};


export function newRequestId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  const bytes = new Uint8Array(16);
  if (typeof crypto !== 'undefined' && typeof crypto.getRandomValues === 'function') crypto.getRandomValues(bytes);
  else for (let index = 0; index < bytes.length; index += 1) bytes[index] = Math.floor(Math.random() * 256);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map(value => value.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

export const request = async <T>(path: string, init: RequestInit = {}): Promise<T> => {
  const response = await fetch(`${API}${path}`, { ...init, headers: { 'Content-Type': 'application/json', 'X-BC250-Client': '1', ...init.headers } });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(body?.error?.message || '요청에 실패했습니다.');
  }
  return response.status === 204 ? undefined as T : response.json();
};

export const requestBlob = async (path: string, init: RequestInit = {}): Promise<Blob> => {
  const response = await fetch(`${API}${path}`, { ...init, headers: { 'X-BC250-Client': '1', ...init.headers } });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(body?.error?.message || '요청에 실패했습니다.');
  }
  return response.blob();
};

export const streamRequest = async (
  path: string,
  body: unknown,
  onEvent: (name: string, data: Record<string, unknown>) => void,
  signal?: AbortSignal,
): Promise<void> => {
  const response = await fetch(`${API}${path}`, {
    method: 'POST', signal, headers: { 'Content-Type': 'application/json', 'X-BC250-Client': '1' }, body: JSON.stringify(body),
  });
  if (!response.ok || !response.body) {
    const error = await response.json().catch(() => null);
    throw new Error(error?.error?.message || '요청에 실패했습니다.');
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = ''; let name = 'message'; let data = '';
  const dispatch = () => {
    if (data) onEvent(name, JSON.parse(data) as Record<string, unknown>);
    name = 'message'; data = '';
  };
  while (true) {
    const part = await reader.read();
    if (part.done) break;
    buffer += decoder.decode(part.value, { stream: true });
    let lineEnd: number;
    while ((lineEnd = buffer.indexOf('\n')) >= 0) {
      const line = buffer.slice(0, lineEnd).replace(/\r$/, '');
      buffer = buffer.slice(lineEnd + 1);
      if (!line) dispatch();
      else if (line.startsWith('event:')) name = line.slice(6).trim();
      else if (line.startsWith('data:')) data += line.slice(5).trim();
    }
  }
  dispatch();
};
