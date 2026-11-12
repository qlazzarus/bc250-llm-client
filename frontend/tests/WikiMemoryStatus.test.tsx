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

it('keeps a long history behind the detail dialog', () => {
  installDialogMocks();
  const jobs = Array.from({ length: 30 }, (_, index) => base({ generation_id: `g${index}`, title: `기록 ${index}`, source_text: `원문 ${index}` }));
  render(<WikiMemoryStatus jobs={jobs} />);
  expect(screen.queryByText('원문 0')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: 'Wiki 자동 기억 기록 (30)' }));
  expect(document.querySelectorAll('.wiki-memory-record')).toHaveLength(30);
});
