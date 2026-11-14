// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
const json = (data: unknown) => new Response(JSON.stringify(data), { status: 200, headers: { 'Content-Type': 'application/json' } });
const doc = { id: 2, title: '문서', category: 'general', content: '본문', revision: 1, updated_at: 't' };
beforeEach(() => {
  vi.stubGlobal('requestAnimationFrame', () => 1);
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith('/conversations')) return json({ data: [] });
    if (url.endsWith('/models')) return json({ data: [] });
    if (url.endsWith('/image-generation/capabilities')) return json({ models: [], available: false });
    if (url.endsWith('/wiki/capabilities')) return json({ store_id: 'integrated' });
    if (url.endsWith('/wiki/documents')) return json([doc]);
    if (url.includes('/wiki/documents/2/aliases')) return json(['별칭값']);
    if (url.includes('/wiki/documents/2/revisions')) return json([{ revision: 1, created_at: 't' }]);
    if (url.includes('/wiki/documents/2?')) return json(doc);
    throw new Error(url);
  }));
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); history.replaceState(null, '', '/'); });

it.each(['edit', 'aliases', 'history'] as const)('opens a direct %s permalink', async section => {
  history.replaceState(null, '', `/#/wiki/2/${section}`);
  const { App } = await import('../src/main');
  render(<App />);
  expect(await screen.findByLabelText('본문')).toBeTruthy();
  if (section === 'aliases') expect((await screen.findByLabelText('별칭 목록') as HTMLTextAreaElement).value).toBe('별칭값');
  if (section === 'history') expect(await screen.findByLabelText('이전 버전')).toBeTruthy();
  expect(window.location.hash).toBe(`#/wiki/2/${section}`);
});

it('keeps the editing URL and draft when navigation to the document is declined', async () => {
  history.replaceState(null, '', '/#/wiki/2/edit');
  vi.spyOn(window, 'confirm').mockReturnValue(false);
  const { App } = await import('../src/main');
  render(<App />);
  fireEvent.change(await screen.findByLabelText('본문'), { target: { value: '수정 초안' } });
  history.replaceState(null, '', '/#/wiki/2');
  fireEvent(window, new HashChangeEvent('hashchange'));
  await waitFor(() => expect(window.location.hash).toBe('#/wiki/2/edit'));
  expect((screen.getByLabelText('본문') as HTMLTextAreaElement).value).toBe('수정 초안');
});

it.each([['별칭', 'aliases'], ['버전 이력', 'history']] as const)('opens %s from the document action row', async (label, section) => {
  history.replaceState(null, '', '/#/wiki/2');
  const { App } = await import('../src/main');
  render(<App />);
  fireEvent.click(await screen.findByRole('button', { name: label }));
  await waitFor(() => expect(window.location.hash).toBe(`#/wiki/2/${section}`));
  if (section === 'aliases') expect(await screen.findByLabelText('별칭 목록')).toBeTruthy();
  else expect(await screen.findByLabelText('이전 버전')).toBeTruthy();
  expect(screen.queryByRole('link', { name: '편집 링크' })).toBeNull();
});

it('copies the document permalink without navigating', async () => {
  history.replaceState(null, '', '/#/wiki/2');
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
  const { App } = await import('../src/main');
  render(<App />);
  fireEvent.click(await screen.findByRole('button', { name: '문서 링크' }));
  await waitFor(() => expect(writeText).toHaveBeenCalledWith(new URL('/#/wiki/2', window.location.href).href));
  expect(await screen.findByText('문서 링크를 복사했습니다.')).toBeTruthy();
  expect(window.location.hash).toBe('#/wiki/2');
});

it('shows a selectable document link when clipboard access fails', async () => {
  history.replaceState(null, '', '/#/wiki/2');
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: vi.fn().mockRejectedValue(new Error('denied')) } });
  const { App } = await import('../src/main');
  render(<App />);
  fireEvent.click(await screen.findByRole('button', { name: '문서 링크' }));
  expect((await screen.findByLabelText('복사할 문서 링크') as HTMLInputElement).value).toBe(new URL('/#/wiki/2', window.location.href).href);
});
