// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

const json = (data: unknown) => new Response(JSON.stringify(data), { status: 200, headers: { 'Content-Type': 'application/json' } });
const conversation = { id: 'c1', title: '대화', mode: 'quick', mode_settings: {}, context_policy: 'recent', response_length: 'medium', summary: '', summary_status: 'idle' };
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

it('requests summaries without overlap and stops while the tab is hidden', async () => {
  let calls = 0;
  let resolvePending!: (response: Response) => void;
  let hidden = false;
  vi.spyOn(document, 'hidden', 'get').mockImplementation(() => hidden);
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.includes('/wiki-memory?view=summary')) {
      calls++;
      if (calls === 1) return Promise.resolve(json({ data: [] }));
      return new Promise<Response>((resolve, reject) => {
        resolvePending = resolve;
        init?.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
      });
    }
    if (url.endsWith('/conversations')) return Promise.resolve(json({ data: [conversation] }));
    if (url.endsWith('/conversations/c1')) return Promise.resolve(json(conversation));
    if (url.includes('/messages?')) return Promise.resolve(json({ data: [], next_before: null, has_more: false }));
    if (url.endsWith('/models')) return Promise.resolve(json({ data: [{ id: 'model' }] }));
    if (url.endsWith('/image-jobs')) return Promise.resolve(json({ data: [] }));
    if (url.endsWith('/image-generation/capabilities')) return Promise.resolve(json({ models: [], available: false }));
    return Promise.reject(new Error(url));
  }));
  const { App } = await import('../src/main');
  render(<App />);
  fireEvent.click(await within(await screen.findByRole('navigation', { name: '대화 목록' })).findByRole('button'));
  await waitFor(() => expect(calls).toBe(1));
  vi.useFakeTimers();
  // The existing first timer was created before fake timers; a visibility cycle schedules a fresh timer.
  hidden = true; document.dispatchEvent(new Event('visibilitychange'));
  hidden = false; document.dispatchEvent(new Event('visibilitychange'));
  expect(calls).toBe(2);
  await vi.advanceTimersByTimeAsync(10000);
  expect(calls).toBe(2);
  hidden = true; document.dispatchEvent(new Event('visibilitychange'));
  await vi.advanceTimersByTimeAsync(20000);
  expect(calls).toBe(2);
  resolvePending(json({ data: [] }));
  hidden = false; document.dispatchEvent(new Event('visibilitychange'));
  await vi.advanceTimersByTimeAsync(0);
  expect(calls).toBe(3);
});

it('does not show a delayed summary from a previously selected conversation', async () => {
  const other = { ...conversation, id: 'c2', title: '다른 대화' };
  let resolveFirst!: (response: Response) => void;
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes('/c1/wiki-memory?')) return new Promise<Response>(resolve => { resolveFirst = resolve; });
    if (url.includes('/c2/wiki-memory?')) return Promise.resolve(json({ data: [] }));
    if (url.endsWith('/conversations')) return Promise.resolve(json({ data: [conversation, other] }));
    if (url.endsWith('/conversations/c1')) return Promise.resolve(json(conversation));
    if (url.endsWith('/conversations/c2')) return Promise.resolve(json(other));
    if (url.includes('/messages?')) return Promise.resolve(json({ data: [], next_before: null, has_more: false }));
    if (url.endsWith('/models')) return Promise.resolve(json({ data: [{ id: 'model' }] }));
    if (url.endsWith('/image-jobs')) return Promise.resolve(json({ data: [] }));
    if (url.endsWith('/image-generation/capabilities')) return Promise.resolve(json({ models: [], available: false }));
    return Promise.reject(new Error(url));
  }));
  const { App } = await import('../src/main');
  render(<App />);
  const nav = await screen.findByRole('navigation', { name: '대화 목록' });
  fireEvent.click((await within(nav).findAllByRole('button'))[0]);
  await waitFor(() => expect(resolveFirst).toBeTypeOf('function'));
  fireEvent.click(within(nav).getByRole('button', { name: /다른 대화/ }));
  await screen.findByRole('heading', { name: '다른 대화' });
  resolveFirst(json({ data: [{ generation_id: 'old', status: 'unknown', needs_attention: 1, error_message: '다른 대화 오류' }] }));
  await waitFor(() => expect(screen.queryByText(/다른 대화 오류/)).toBeNull());
});

it('keeps polling after a network failure and discovers a later memory job', async () => {
  let calls = 0; let hidden = false;
  vi.spyOn(document, 'hidden', 'get').mockImplementation(() => hidden);
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes('/wiki-memory?view=summary')) {
      calls++;
      if (calls === 1) return Promise.resolve(json({ data: [] }));
      if (calls === 2) return Promise.reject(new Error('temporary failure'));
      return Promise.resolve(json({ data: [{ generation_id: 'g', status: 'awaiting_confirmation', needs_attention: 1, created_at: 't', updated_at: 't' }] }));
    }
    if (url.endsWith('/conversations')) return Promise.resolve(json({ data: [conversation] }));
    if (url.endsWith('/conversations/c1')) return Promise.resolve(json(conversation));
    if (url.includes('/messages?')) return Promise.resolve(json({ data: [], next_before: null, has_more: false }));
    if (url.endsWith('/models')) return Promise.resolve(json({ data: [{ id: 'model' }] }));
    if (url.endsWith('/image-jobs')) return Promise.resolve(json({ data: [] }));
    if (url.endsWith('/image-generation/capabilities')) return Promise.resolve(json({ models: [], available: false }));
    return Promise.reject(new Error(url));
  }));
  const { App } = await import('../src/main');
  render(<App />);
  fireEvent.click(await within(await screen.findByRole('navigation', { name: '대화 목록' })).findByRole('button'));
  await waitFor(() => expect(calls).toBe(1));
  vi.useFakeTimers();
  hidden = true; document.dispatchEvent(new Event('visibilitychange'));
  hidden = false; document.dispatchEvent(new Event('visibilitychange'));
  expect(calls).toBe(2);
  await vi.advanceTimersByTimeAsync(5001);
  expect(calls).toBe(3);
  await vi.advanceTimersByTimeAsync(0);
  expect(screen.getByText(/변경 확인 필요/)).toBeTruthy();
});
