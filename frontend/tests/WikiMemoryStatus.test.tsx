// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { WikiMemoryStatus } from '../src/components/WikiMemoryStatus';
import type { WikiMemoryJob } from '../src/api';

const base = (overrides: Partial<WikiMemoryJob> = {}): WikiMemoryJob => ({
  generation_id: 'g1', status: 'completed', action: 'append', document_id: 2, title: '기기 정보',
  source_text: '내 휴대폰은 예시 기기입니다.', source_message_id: 'm1', previous_content: '이전 본문',
  proposed_content: '새 본문', error_message: null, explicit: 0, memory_kind: 'fact', reason_code: 'stored',
  needs_attention: 0, policy_version: 1, created_at: '2026-11-12T00:00:00Z', updated_at: '2026-11-12T00:00:00Z', ...overrides,
});

const showModalDescriptor = Object.getOwnPropertyDescriptor(HTMLDialogElement.prototype, 'showModal');
const closeDescriptor = Object.getOwnPropertyDescriptor(HTMLDialogElement.prototype, 'close');

function installDialogMocks() {
  const showModal = vi.fn(function (this: HTMLDialogElement) {
    this.setAttribute('open', '');
    this.querySelector<HTMLElement>('button')?.focus();
  });
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', { configurable: true, writable: true, value: showModal });
  Object.defineProperty(HTMLDialogElement.prototype, 'close', { configurable: true, writable: true, value: function (this: HTMLDialogElement) { this.removeAttribute('open'); } });
  return showModal;
}

afterEach(() => {
  cleanup();
  if (showModalDescriptor) Object.defineProperty(HTMLDialogElement.prototype, 'showModal', showModalDescriptor);
  else Reflect.deleteProperty(HTMLDialogElement.prototype, 'showModal');
  if (closeDescriptor) Object.defineProperty(HTMLDialogElement.prototype, 'close', closeDescriptor);
  else Reflect.deleteProperty(HTMLDialogElement.prototype, 'close');
  vi.restoreAllMocks();
});

it('keeps completed source text out of the default panel and exposes it only in the detail dialog', () => {
  const showModal = installDialogMocks();
  render(<WikiMemoryStatus jobs={[base()]} />);
  expect(screen.queryByText('내 휴대폰은 예시 기기입니다.')).toBeNull();
  const trigger = screen.getByRole('button', { name: /Wiki 자동 기억 기록/ });
  trigger.focus(); fireEvent.click(trigger);
  expect(showModal).toHaveBeenCalled();
  expect(document.activeElement).toBe(screen.getByRole('button', { name: '닫기' }));
  expect(screen.getByText('내 휴대폰은 예시 기기입니다.')).toBeTruthy();
  fireEvent(screen.getByRole('dialog'), new Event('cancel', { bubbles: false, cancelable: true }));
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(document.activeElement).toBe(trigger);
});

it('prioritizes an earlier unknown result over a newer skipped result', () => {
  render(<WikiMemoryStatus jobs={[
    base({ generation_id: 'new', status: 'skipped', source_text: null, reason_code: 'question' }),
    base({ generation_id: 'old', status: 'unknown', error_message: 'Wiki 문서를 확인해 주세요.', needs_attention: 1, source_text: null, reason_code: 'write_unknown' }),
  ]} />);
  expect(screen.getByRole('status').textContent).toContain('저장 결과 확인 필요');
  expect(screen.getByRole('status').textContent).toContain('Wiki 문서를 확인해 주세요.');
});

it('explains an explicitly requested long document without reporting a failure', () => {
  installDialogMocks();
  render(<WikiMemoryStatus jobs={[base({ status: 'skipped', explicit: 1, needs_attention: 1,
    reason_code: 'document_input', error_message: '긴 자료는 자동 기억으로 저장할 수 없습니다.' })]} />);
  expect(screen.getByRole('status').textContent).toContain('저장하지 않음');
  expect(screen.getByRole('status').textContent).toContain('긴 자료는 자동 기억으로 저장할 수 없습니다.');
  fireEvent.click(screen.getByRole('button', { name: /Wiki 자동 기억 기록/ }));
  expect(screen.getByText('긴 자료는 자동 기억 대상이 아님')).toBeTruthy();
});

it('keeps a long history behind the detail dialog', () => {
  installDialogMocks();
  const jobs = Array.from({ length: 30 }, (_, index) => base({ generation_id: `g${index}`, title: `기록 ${index}`, source_text: `원문 ${index}` }));
  render(<WikiMemoryStatus jobs={jobs} />);
  expect(screen.queryByText('원문 0')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: 'Wiki 자동 기억 기록 (30)' }));
  expect(document.querySelectorAll('.wiki-memory-record')).toHaveLength(30);
});

it('marks old external Wiki records in the detail dialog', () => {
  installDialogMocks();
  render(<WikiMemoryStatus jobs={[base({ store_id: 'legacy_external' })]} />);
  fireEvent.click(screen.getByRole('button', { name: 'Wiki 자동 기억 기록 (1)' }));
  expect(screen.getByText(/기존 Wiki 기록/)).toBeTruthy();
});

it('shows the existing and proposed values and submits an explicit decision', async () => {
  installDialogMocks();
  const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ status: 'completed' }), { status: 200 }));
  const onChanged = vi.fn();
  render(<WikiMemoryStatus conversationId="conversation" onChanged={onChanged} jobs={[base({ status: 'awaiting_confirmation',
    store_id: 'integrated', previous_content: 'Fedora', proposed_content: 'Ubuntu', confirmation_token: 'a'.repeat(64), needs_attention: 1 })]} />);
  fireEvent.click(screen.getByRole('button', { name: /Wiki 자동 기억 기록/ }));
  expect(screen.getByText('기존 값: Fedora')).toBeTruthy();
  expect(screen.getByText('새 값: Ubuntu')).toBeTruthy();
  expect(fetchMock).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '변경' }));
  await vi.waitFor(() => expect(onChanged).toHaveBeenCalled());
  expect(fetchMock).toHaveBeenCalledOnce();
  const [url, options] = fetchMock.mock.calls[0];
  expect(url).toBe('/llm/api/v1/conversations/conversation/wiki-memory/g1/confirmation');
  expect(JSON.parse(String(options?.body))).toEqual({ decision: 'approve', token: 'a'.repeat(64) });
});

it('loads sensitive details only when requested', async () => {
  installDialogMocks();
  const full = base();
  const summary = { ...full };
  delete summary.source_text; delete summary.source_message_id; delete summary.previous_content; delete summary.proposed_content;
  const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ data: full }), { status: 200 }));
  render(<WikiMemoryStatus jobs={[summary]} conversationId="c" />);
  fireEvent.click(screen.getByRole('button', { name: /Wiki 자동 기억 기록/ }));
  expect(fetchMock).not.toHaveBeenCalled();
  expect(screen.queryByText('내 휴대폰은 예시 기기입니다.')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '상세 불러오기' }));
  await screen.findByText('내 휴대폰은 예시 기기입니다.');
  expect(fetchMock).toHaveBeenCalledOnce();
});
