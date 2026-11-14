// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

vi.mock('@tanstack/react-virtual', () => ({ useVirtualizer: ({ count }: { count: number }) => ({ getTotalSize: () => count * 120, getVirtualItems: () => Array.from({ length: count }, (_, index) => ({ index, start: index * 120 })), measureElement: () => {}, scrollToIndex: () => {} }) }));
const json = (data: unknown) => new Response(JSON.stringify(data), { status: 200, headers: { 'Content-Type': 'application/json' } });
const conversation = { id: 'c1', title: '대화', mode: 'quick', mode_settings: {}, context_policy: 'recent', response_length: 'medium', summary: '', summary_status: 'idle' };
const prior = [
  { id: 'u0', role: 'user', content: 'BC250이 뭔가요?', created_at: '1' },
  { id: 'a0', role: 'assistant', content: '추가 정보를 주시면 더 정확하게 답변해 드릴 수 있습니다!', created_at: '2', generation_completed: true },
];
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it('uses the completed SSE message identity before a delayed reload and keeps dismissal through view changes', async () => {
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
  HTMLDialogElement.prototype.close = vi.fn();
  vi.stubGlobal('requestAnimationFrame', () => 1); vi.stubGlobal('cancelAnimationFrame', () => {});
  let pages = 0;
  const writes: string[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (init?.method === 'POST' && url.endsWith('/messages')) {
      const body = JSON.parse(String(init.body));
      const event = { generation: { id: body.request_id, user_message_id: 'u1', status: 'completed', finish_reason: 'stop' }, assistant_message: { id: 'a1', role: 'assistant', content: '확인했습니다.', created_at: '4' } };
      return new Response(`event: done\ndata: ${JSON.stringify(event)}\n\n`);
    }
    if (init?.method && init.method !== 'GET') writes.push(url);
    if (url.endsWith('/models')) return json({ data: [{ id: 'model' }] });
    if (url.endsWith('/conversations')) return json({ data: [conversation] });
    if (url.endsWith('/conversations/c1')) return json(conversation);
    if (url.includes('/messages?')) {
      pages++;
      if (pages > 1) return new Promise<Response>(() => {});
      return json({ data: prior, next_before: null, has_more: false });
    }
    if (url.includes('/wiki-memory?')) return json({ data: [] });
    if (url.endsWith('/image-generation/capabilities')) return json({ models: [], available: false });
    if (url.endsWith('/image-jobs')) return json({ data: [] });
    if (url.endsWith('/wiki/capabilities')) return json({ store_id: 'legacy_external' });
    throw new Error(url);
  }));
  const { App } = await import('../src/main');
  render(<App />);
  fireEvent.click(await within(await screen.findByRole('navigation', { name: '대화 목록' })).findByRole('button'));
  const input = await screen.findByLabelText('메시지 입력');
  fireEvent.change(input, { target: { value: '자료입니다. '.repeat(160) } });
  fireEvent.click(screen.getByRole('button', { name: '전송' }));
  expect(await screen.findByRole('region', { name: '공유 Wiki 저장 제안' })).toBeTruthy();
  expect(screen.queryByRole('button', { name: '초안 검토' })).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '저장 내용 확인' }));
  expect(await screen.findByRole('dialog', { name: '공유 Wiki 문서 초안' })).toBeTruthy();
  expect(writes).toEqual([]);
  fireEvent.click(within(screen.getByRole('dialog', { name: '공유 Wiki 문서 초안' })).getByRole('button', { name: '취소' }));
  fireEvent.click(screen.getByRole('button', { name: '이번 대화에서만 사용' }));
  expect(screen.queryByRole('region', { name: '공유 Wiki 저장 제안' })).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '공유 Wiki' }));
  fireEvent.click(await screen.findByRole('button', { name: '대화로 돌아가기' }));
  await waitFor(() => expect(screen.queryByRole('region', { name: '공유 Wiki 저장 제안' })).toBeNull());
  expect(screen.getAllByRole('button', { name: '공유 Wiki에 저장' })).toHaveLength(2);
  expect(writes).toEqual([]);
});

it('reviews and explicitly saves the prompted source, then hides the proposal', async () => {
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
  HTMLDialogElement.prototype.close = vi.fn();
  vi.stubGlobal('requestAnimationFrame', () => 1); vi.stubGlobal('cancelAnimationFrame', () => {});
  const original = 'BC250 자료\n' + '자료입니다. '.repeat(160);
  const current = [...prior, { id: 'u1', role: 'user', content: original, created_at: '3' }];
  const saved: Record<string, unknown>[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.includes('/message-drafts/save')) { saved.push(JSON.parse(String(init?.body))); return json({ id: 2, revision: 1, status: 'created' }); }
    if (url.includes('/message-drafts/candidates')) return json({ matches: [{ id: 1, title: 'BC250 자료', revision: 1, category: 'general' }], similar: [] });
    if (url.endsWith('/models')) return json({ data: [{ id: 'model' }] });
    if (url.endsWith('/conversations')) return json({ data: [conversation] });
    if (url.endsWith('/conversations/c1')) return json(conversation);
    if (url.includes('/messages?')) return json({ data: current, next_before: null, has_more: false });
    if (url.includes('/wiki-memory?')) return json({ data: [] });
    if (url.endsWith('/image-generation/capabilities')) return json({ models: [], available: false });
    if (url.endsWith('/image-jobs')) return json({ data: [] });
    throw new Error(url);
  }));
  const { App } = await import('../src/main');
  render(<App />);
  fireEvent.click(await within(await screen.findByRole('navigation', { name: '대화 목록' })).findByRole('button'));
  fireEvent.click(await screen.findByRole('button', { name: '저장 내용 확인' }));
  const dialog = await screen.findByRole('dialog', { name: '공유 Wiki 문서 초안' });
  expect((within(dialog).getByLabelText('본문') as HTMLTextAreaElement).value).toBe(original);
  fireEvent.change(within(dialog).getByLabelText('본문'), { target: { value: original + '\n편집 확인값: fixture-314' } });
  fireEvent.change(within(dialog).getByLabelText('출처 (선택)'), { target: { value: '사용자 제공' } });
  fireEvent.click(within(dialog).getByRole('button', { name: '중복 후보 확인' }));
  await within(dialog).findByText(/BC250 자료 · revision 1/);
  expect(saved).toHaveLength(0);
  fireEvent.click(within(dialog).getByRole('button', { name: '새 문서 생성 확인 및 저장' }));
  await within(dialog).findByText(/저장 완료 · 문서 2 · revision 1/);
  expect(saved).toHaveLength(1);
  expect(saved[0]).toMatchObject({ conversation_id: 'c1', message_id: 'u1', source: '사용자 제공', action: 'create', acknowledged_duplicate_ids: [1] });
  expect(String(saved[0].content)).toContain('fixture-314');
  fireEvent.click(within(dialog).getAllByRole('button', { name: '닫기' })[0]);
  expect(screen.queryByRole('region', { name: '공유 Wiki 저장 제안' })).toBeNull();
});
