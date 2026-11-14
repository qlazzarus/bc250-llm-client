// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

const json = (data: unknown) => new Response(JSON.stringify(data), { status: 200, headers: { 'Content-Type': 'application/json' } });
const conversation = { id: 'created', title: '새 대화', mode: 'quick', mode_settings: {}, context_policy: 'recent', response_length: 'medium', summary: '', summary_status: 'idle' };
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); history.replaceState(null, '', '/'); });

it('opens and closes the dialog, then creates and opens a conversation', async () => {
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute('open'); };
  vi.stubGlobal('requestAnimationFrame', () => 1);
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith('/conversations') && init?.method === 'POST') return json(conversation);
    if (url.endsWith('/conversations')) return json({ data: [conversation] });
    if (url.endsWith('/conversations/created')) return json(conversation);
    if (url.includes('/messages?')) return json({ data: [], next_before: null, has_more: false });
    if (url.endsWith('/models')) return json({ data: [] });
    if (url.includes('/wiki-memory?')) return json({ data: [] });
    if (url.endsWith('/image-generation/capabilities')) return json({ models: [], available: false });
    throw new Error(url);
  });
  vi.stubGlobal('fetch', fetchMock);
  const { App } = await import('../src/main');
  render(<App />);
  fireEvent.click(screen.getByRole('button', { name: '새 대화' }));
  let dialog = screen.getByRole('dialog', { name: '새 대화' });
  fireEvent.click(within(dialog).getByRole('button', { name: '닫기' }));
  expect(screen.queryByRole('dialog', { name: '새 대화' })).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '새 대화' }));
  dialog = screen.getByRole('dialog', { name: '새 대화' });
  fireEvent.click(within(dialog).getByRole('button', { name: '대화 만들기' }));
  await waitFor(() => expect(fetchMock.mock.calls.some(([url, init]) => String(url).endsWith('/conversations') && init?.method === 'POST')).toBe(true));
  expect(await screen.findByLabelText('메시지 입력')).toBeTruthy();
});
