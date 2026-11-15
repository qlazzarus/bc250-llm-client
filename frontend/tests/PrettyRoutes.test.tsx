// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
const paths = ['general', 'iot', 'AMD BC-250 AI 서버'];
const doc = { id: 2, title: paths[2], category: 'general/iot', path_segments: paths, content: '본문', revision: 1, updated_at: '' };
const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });
beforeEach(() => {
  vi.stubGlobal('requestAnimationFrame', () => 1);
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith('/conversations')) return json({ data: [] });
    if (url.endsWith('/models')) return json({ data: [] });
    if (url.endsWith('/image-generation/capabilities')) return json({ models: [], available: false });
    if (url.endsWith('/wiki/capabilities')) return json({ store_id: 'integrated' });
    if (url.endsWith('/wiki/documents')) return json([doc]);
    if (url.endsWith('/wiki/resolve-path')) return json({ id: 2, path_segments: paths });
    if (url.includes('/wiki/documents/2?')) return json(doc);
    if (url.includes('/wiki/documents/2/aliases')) return json([]);
    throw new Error(url);
  }));
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); history.replaceState(null, '', '/llm/'); });
const pretty = '/llm/wiki/general/iot/AMD%20BC-250%20AI%20%EC%84%9C%EB%B2%84';
it('opens a nested Wiki permalink directly and shows its category hierarchy', async () => {
  history.replaceState(null, '', pretty);
  const { App } = await import('../src/main'); render(<App />);
  expect(await screen.findByRole('heading', { name: doc.title })).toBeTruthy();
  const parent = screen.getByRole('button', { name: /general/, expanded: true });
  const category = parent.closest('section')!;
  expect(within(category).getByRole('button', { name: /iot/, expanded: true })).toBeTruthy();
  expect(window.location.pathname).toBe(pretty);
});
it('upgrades the old document hash after resolving the document', async () => {
  history.replaceState(null, '', '/llm/#/wiki/2/edit');
  const { App } = await import('../src/main'); render(<App />);
  expect(await screen.findByLabelText('본문')).toBeTruthy();
  await waitFor(() => expect(window.location.pathname + window.location.search).toBe(pretty + '?view=edit'));
});
it('copies a document URL using the actual path rather than the legacy hash', async () => {
  history.replaceState(null, '', pretty);
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
  const { App } = await import('../src/main'); render(<App />);
  fireEvent.click(await screen.findByRole('button', { name: '문서 링크' }));
  await waitFor(() => expect(writeText).toHaveBeenCalledWith(new URL(pretty, location.origin).href));
});

it('keeps an unsaved Wiki draft and its URL when leaving to chat is cancelled', async () => {
  history.replaceState(null, '', pretty + '?view=edit');
  vi.spyOn(window, 'confirm').mockReturnValue(false);
  const { App } = await import('../src/main'); render(<App />);
  fireEvent.change(await screen.findByLabelText('본문'), { target: { value: '수정 중' } });
  history.replaceState(null, '', '/llm/chat/c1');
  fireEvent(window, new PopStateEvent('popstate'));
  await waitFor(() => expect(window.location.pathname + window.location.search).toBe(pretty + '?view=edit'));
  expect((screen.getByLabelText('본문') as HTMLTextAreaElement).value).toBe('수정 중');
});
