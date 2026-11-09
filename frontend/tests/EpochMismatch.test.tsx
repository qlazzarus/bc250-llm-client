// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });
const conversation = { id: 'c1', title: '대화', mode: 'quick', mode_settings: {}, context_policy: 'none', response_length: 'medium', summary: '', summary_status: 'idle' };

afterEach(() => { cleanup(); vi.unstubAllGlobals(); document.body.innerHTML = ''; });

it.each(['http', 'sse'])('preserves the draft and reuses its request after a %s model mismatch', async kind => {
  let models = 0;
  const posts: { request_id: string; content: string }[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (init?.method === 'POST' && url.endsWith('/messages')) {
      posts.push(JSON.parse(String(init.body)));
      if (posts.length === 1) {
        if (kind === 'http') return json({ error: { code: 'model_epoch_mismatch', message: '모델 전환 중' } }, 409);
        return new Response('event: error\ndata: {"code":"model_epoch_mismatch","message":"모델 전환 중"}\n\n');
      }
      return new Response('event: done\ndata: {}\n\n');
    }
    if (url.endsWith('/models')) {
      models++;
      if (models === 2) return json({ error: { message: '전환 중' } }, 503);
      return json({ data: [{ id: models === 1 ? 'alpha' : 'beta' }] });
    }
    if (url.endsWith('/conversations')) return json({ data: [conversation] });
    if (url.endsWith('/conversations/c1')) return json(conversation);
    if (url.includes('/messages?')) return json({ data: [], next_before: null, has_more: false });
    if (url.endsWith('/wiki-memory')) return json({ data: [] });
    if (url.endsWith('/image-generation/capabilities')) return json({ models: [], available: false });
    throw new Error(url);
  }));
  const { App } = await import('../src/main');
  render(<App />);
  fireEvent.click(await within(await screen.findByRole('navigation', { name: '대화 목록' })).findByRole('button'));
  const input = await screen.findByLabelText('메시지 입력') as HTMLTextAreaElement;
  fireEvent.change(input, { target: { value: '내 초안' } });
  fireEvent.click(screen.getByRole('button', { name: '전송' }));
  await waitFor(() => expect(input.value).toBe('내 초안'));
  fireEvent.click(await screen.findByRole('button', { name: '모델 다시 조회' }));
  await waitFor(() => expect(screen.getByText(/활성 모델: beta/)).toBeTruthy());
  await waitFor(() => expect((screen.getByRole('button', { name: '전송' }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole('button', { name: '전송' }));
  await waitFor(() => expect(posts).toHaveLength(2));
  expect(posts[1].request_id).toBe(posts[0].request_id);
});
