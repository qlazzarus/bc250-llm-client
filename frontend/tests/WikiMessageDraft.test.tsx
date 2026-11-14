// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { MessageList } from '../src/components/MessageList';
import { WikiDraftDialog } from '../src/main';
import type { Message } from '../src/api';
import { useState } from 'react';

vi.mock('@tanstack/react-virtual', () => ({ useVirtualizer: ({ count }: { count: number }) => ({ getTotalSize: () => count * 120, getVirtualItems: () => Array.from({ length: count }, (_, index) => ({ index, start: index * 120 })), measureElement: () => {}, scrollToIndex: () => {} }) }));
beforeEach(() => { HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); }; HTMLDialogElement.prototype.close = vi.fn(); vi.stubGlobal('requestAnimationFrame', () => 1); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it('offers a candidate at 1000 characters and a manual entry only for persisted user messages', () => {
  const open = vi.fn();
  const messages: Message[] = [
    { id: 'short', role: 'user', content: 'a'.repeat(999), created_at: '' },
    { id: 'long', role: 'user', content: 'b'.repeat(1000), created_at: '' },
    { id: 'answer', role: 'assistant', content: 'c'.repeat(12000), created_at: '' },
    { id: 'local-pending', role: 'user', content: 'pending', created_at: '' },
  ];
  function List() {
    const [dismissed, setDismissed] = useState(new Set<string>());
    return <MessageList mode="quick" messages={messages} hasMore={false} onLoadOlder={async () => {}} scrollSignal={0} conversationId="c" imageCapabilities={undefined} imageDrafts={{}} onEnsureImageDraft={async () => {}} onUpdateImageDraft={() => {}} onPersistImageDraft={async () => {}} onSubmitImageJob={async () => { throw Error(); }} imageJobs={[]} onCancelImageJob={async () => {}} onWikiDraft={open} dismissedWikiProposals={dismissed} onDismissWikiProposal={(conversationId, id) => setDismissed(current => new Set(current).add(JSON.stringify([conversationId, id])))} />;
  }
  render(<List />);
  expect(screen.getAllByRole('button', { name: '공유 Wiki에 저장' })).toHaveLength(2);
  fireEvent.click(screen.getByRole('button', { name: '초안 검토' })); expect(open).toHaveBeenCalledWith(messages[1]);
  fireEvent.click(screen.getByRole('button', { name: '문서 후보 닫기' }));
  expect(screen.queryByRole('button', { name: '초안 검토' })).toBeNull();
  expect(screen.getAllByRole('button', { name: '공유 Wiki에 저장' })).toHaveLength(2);
});

it('keeps the full source, allows editing, and confirms discarding changes without writes', () => {
  const close = vi.fn(); const fetch = vi.fn(); vi.stubGlobal('fetch', fetch);
  const original = '제목\n' + '가'.repeat(11997);
  render(<WikiDraftDialog source={{ conversationId: 'c', messageId: 'm', original }} onClose={close} />);
  expect((screen.getByLabelText('제목') as HTMLInputElement).value).toBe('제목');
  expect((screen.getByLabelText('본문') as HTMLTextAreaElement).value).toBe(original);
  expect(screen.getByText(/가족에게 공유/)).toBeTruthy(); expect(screen.getByText(/자동으로 검증하지 않습니다/)).toBeTruthy();
  fireEvent.change(screen.getByLabelText('출처 (선택)'), { target: { value: '직접 입력' } });
  fireEvent.change(screen.getByLabelText('본문'), { target: { value: '수정' } });
  const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValueOnce(true);
  fireEvent.click(screen.getByRole('button', { name: '취소' })); expect(close).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '닫기' }));
  expect(confirm).toHaveBeenCalledTimes(2); expect(close).toHaveBeenCalledOnce(); expect(fetch).not.toHaveBeenCalled();
});

it('reviews duplicates before saving and retries an unknown result with identical input', async () => {
  const calls: RequestInit[] = [];
  const fetchMock = vi.fn(async (path: string, init: RequestInit) => {
    if (path.includes('/candidates?')) return { ok: true, status: 200, json: async () => ({ matches: [{ id: 7, title: '제목', category: 'general', revision: 1 }], similar: [] }) };
    if (path.includes('/message-drafts/save')) {
      calls.push(init);
      if (calls.length === 1) throw new Error('lost response');
      return { ok: true, status: 200, json: async () => ({ id: 8, revision: 1, status: 'created' }) };
    }
    throw new Error('unexpected request');
  });
  vi.stubGlobal('fetch', fetchMock);
  render(<WikiDraftDialog source={{ conversationId: 'c', messageId: 'm', original: '제목\n본문' }} onClose={() => {}} />);
  expect(fetchMock).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '중복 후보 확인' }));
  expect(await screen.findByText(/제목 · revision 1/)).toBeTruthy(); expect(calls).toHaveLength(0);
  fireEvent.click(screen.getByRole('button', { name: '새 문서 생성 확인 및 저장' }));
  expect(await screen.findByText(/저장 결과 확인 필요/)).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: '같은 요청으로 결과 확인' }));
  expect(await screen.findByText(/저장 완료 · 문서 8 · revision 1/)).toBeTruthy();
  expect(calls).toHaveLength(2); expect(calls[0].body).toBe(calls[1].body);
  expect(JSON.parse(String(calls[0].body)).acknowledged_duplicate_ids).toEqual([7]);
});

it('requires selecting a compared document and retains the draft on revision conflict', async () => {
  const calls: RequestInit[] = [];
  vi.stubGlobal('fetch', vi.fn(async (path: string, init: RequestInit) => {
    if (path.includes('/candidates?')) return { ok: true, status: 200, json: async () => ({ matches: [{ id: 1, title: '제목', category: 'general', revision: 1 }], similar: [] }) };
    if (path.includes('/documents/1?')) return { ok: true, status: 200, json: async () => ({ id: 1, title: '제목', category: 'general', revision: 1, content: '기존 내용' }) };
    calls.push(init);
    return { ok: false, status: 409, json: async () => ({ error: { code: 'wiki_conflict', message: '충돌' } }) };
  }));
  render(<WikiDraftDialog source={{ conversationId: 'c', messageId: 'm', original: '제목\n새 내용' }} onClose={() => {}} />);
  fireEvent.click(screen.getByRole('button', { name: '중복 후보 확인' }));
  fireEvent.click(await screen.findByRole('button', { name: '비교' }));
  expect(await screen.findByText('기존 내용')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: '이 문서를 초안 내용으로 편집' }));
  fireEvent.click(screen.getByRole('button', { name: '기존 문서 변경 확인 및 저장' }));
  expect(await screen.findByText(/문서가 변경되었습니다/)).toBeTruthy();
  expect((screen.getByLabelText('본문') as HTMLTextAreaElement).value).toBe('제목\n새 내용');
  expect(JSON.parse(String(calls[0].body))).toMatchObject({ action: 'update', document_id: 1, expected_revision: 1 });
});

it('prevents duplicate submission while saving and supports Escape cancellation', async () => {
  let resolveSave: ((response: unknown) => void) | undefined;
  const fetchMock = vi.fn((path: string) => {
    if (path.includes('/candidates?')) return Promise.resolve({ ok: true, status: 200, json: async () => ({ matches: [], similar: [] }) });
    return new Promise(resolve => { resolveSave = resolve; });
  });
  vi.stubGlobal('fetch', fetchMock);
  const close = vi.fn();
  const view = render(<WikiDraftDialog source={{ conversationId: 'c', messageId: 'm', original: '본문' }} onClose={close} />);
  fireEvent.click(screen.getByRole('button', { name: '중복 후보 확인' }));
  const save = await screen.findByRole('button', { name: '새 문서 생성 확인 및 저장' });
  fireEvent.click(save); fireEvent.click(save);
  expect(fetchMock.mock.calls.filter(([path]) => path.includes('/save'))).toHaveLength(1);
  resolveSave?.({ ok: true, status: 200, json: async () => ({ id: 1, revision: 1, status: 'created' }) });
  await screen.findByText(/저장 완료/);
  fireEvent(view.container.querySelector('dialog')!, new Event('cancel', { bubbles: false, cancelable: true }));
  expect(close).toHaveBeenCalledOnce();
});

it('only emphasizes a long message after a known completed adjacent assistant request', () => {
  const open = vi.fn();
  const source: Message = { id: 'u', role: 'user', content: '자료 '.repeat(400), created_at: '' };
  const assistant: Message = { id: 'a', role: 'assistant', content: '추가 정보를 주시면 더 정확하게 답변해 드릴 수 있습니다!', created_at: '', generation_completed: true };
  const base = { mode: 'quick' as const, hasMore: false, onLoadOlder: async () => {}, scrollSignal: 0, conversationId: 'c', imageCapabilities: undefined,
    imageDrafts: {}, onEnsureImageDraft: async () => {}, onUpdateImageDraft: () => {}, onPersistImageDraft: async () => {},
    onSubmitImageJob: async () => { throw Error(); }, imageJobs: [], onCancelImageJob: async () => {}, onWikiDraft: open,
    dismissedWikiProposals: new Set<string>(), onDismissWikiProposal: vi.fn() };
  const view = render(<MessageList {...base} messages={[assistant, source]} />);
  expect(screen.getByRole('region', { name: '공유 Wiki 저장 제안' })).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: '저장 내용 확인' }));
  expect(open).toHaveBeenCalledWith(source);
  view.rerender(<MessageList {...base} messages={[{ ...assistant, generation_completed: false }, source]} />);
  expect(screen.queryByRole('region', { name: '공유 Wiki 저장 제안' })).toBeNull();
  expect(screen.getByRole('button', { name: '초안 검토' })).toBeTruthy();
  view.rerender(<MessageList {...base} messages={[{ ...assistant, generation_completed: undefined }, source]} />);
  expect(screen.queryByRole('region', { name: '공유 Wiki 저장 제안' })).toBeNull();
  view.rerender(<MessageList {...base} messages={[assistant, { ...source, id: 'local-pending' }]} />);
  expect(screen.queryByRole('region', { name: '공유 Wiki 저장 제안' })).toBeNull();
});

it('distinguishes searched, provided, valid ID and unrecognized citations, including older snapshots', () => {
  const base = { mode: 'quick' as const, hasMore: false, onLoadOlder: async () => {}, scrollSignal: 0, conversationId: 'c', imageCapabilities: undefined,
    imageDrafts: {}, onEnsureImageDraft: async () => {}, onUpdateImageDraft: () => {}, onPersistImageDraft: async () => {},
    onSubmitImageJob: async () => { throw Error(); }, imageJobs: [], onCancelImageJob: async () => {}, onWikiDraft: () => {},
    dismissedWikiProposals: new Set<string>(), onDismissWikiProposal: () => {} };
  const evidence = { version: 1, retrieved: [{ id: 1, title: '전달 문서', revision: 1, store_id: 'integrated' }, { id: 2, title: '예산 제외 문서', revision: 1, store_id: 'integrated' }],
    provided: [{ id: 1, title: '전달 문서', excerpt: '참고 발췌' }], memory_provided: [], cited: [1], invalid_citations: [99], evidence_status: 'citation_only' as const,
    unrecognized_citations: ['[wiki:전달 문서]', '[wiki:]'] };
  const answer: Message = { id: 'a', role: 'assistant', content: '답 [wiki:전달 문서]', created_at: '', wiki_evidence: evidence };
  const view = render(<MessageList {...base} messages={[answer]} />);
  expect(screen.getByText(/검색 2건 · 모델에 전달 1건/)).toBeTruthy();
  expect(screen.getByText(/검색됐으나 모델에 전달되지 않은 문서: 예산 제외 문서/)).toBeTruthy();
  expect(screen.getByText(/전달된 문서 ID 인용: 1/)).toBeTruthy();
  expect(screen.getByText(/전달하지 않은 문서 ID 인용: 99/)).toBeTruthy();
  expect(screen.getByText(/확인할 수 없는 인용 형식/)).toBeTruthy();
  expect(screen.getByText('[wiki:전달 문서]')).toBeTruthy();
  expect(screen.getByText(/내용 일치는 검증하지 않았습니다/)).toBeTruthy();
  const oldEvidence = { ...evidence, unrecognized_citations: undefined };
  view.rerender(<MessageList {...base} messages={[{ ...answer, wiki_evidence: oldEvidence }]} />);
  expect(screen.queryByText(/확인할 수 없는 인용 형식/)).toBeNull();
  expect(screen.getByText(/이전 기록에는 인용 형식 검사 결과가 없습니다/)).toBeTruthy();
});
