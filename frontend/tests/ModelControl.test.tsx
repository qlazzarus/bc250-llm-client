// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ModelControl } from '../src/components/ModelControl';

const opId = 'a'.repeat(32);
const models = (model = 'alpha') => ({
  models: [{ name: 'alpha', current: model === 'alpha' }, { name: 'beta', current: model === 'beta' }],
  configured_model: model, loaded_model: model, ready: true, switch_enabled: true, active_operation_id: null,
});
const response = (data: unknown, status = 200) => new Response(JSON.stringify(data), {
  status, headers: { 'Content-Type': 'application/json' },
});

beforeEach(() => { sessionStorage.clear(); });
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

async function authorizeAndLoad() {
  fireEvent.click(screen.getByRole('button', { name: '상태 조회' }));
  await screen.findByText(/로드된 모델:/);
}

describe('model control UI', () => {
  it('restores a terminal operation after reload and reads models after the operation', async () => {
    sessionStorage.setItem('model-switch-operation', opId);
    const paths: string[] = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input); paths.push(url);
      if (url.endsWith('/current')) return response({ operation: null });
      if (url.endsWith(opId)) return response({ id: opId, model: 'beta', state: 'succeeded' });
      if (url.endsWith('/llm/models')) return response(models('beta'));
      throw new Error('unexpected request');
    }));
    render(<ModelControl />);
    await authorizeAndLoad();
    expect(screen.getByText(/작업 beta: succeeded/)).toBeTruthy();
    expect(screen.getByText(/로드된 모델: beta/)).toBeTruthy();
    expect(paths.findIndex(path => path.endsWith(opId))).toBeLessThan(paths.findIndex(path => path.endsWith('/llm/models')));
  });

  it('keeps a lost response identifiable across reload and resubmits only the same key', async () => {
    const posted: { key: string; model: string }[] = [];
    let attempts = 0;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (init?.method === 'POST') {
        const headers = init.headers as Record<string, string>;
        posted.push({ key: headers['Idempotency-Key'], model: JSON.parse(String(init.body)).model });
        if (++attempts === 1) throw new TypeError('network disconnected');
        return response({ id: opId, model: 'beta', state: 'queued' }, 202);
      }
      if (url.endsWith('/current')) return response({ operation: null });
      if (url.endsWith('/llm/models')) return response(models());
      throw new Error('unexpected request');
    }));
    const view = render(<ModelControl />);
    await authorizeAndLoad();
    fireEvent.change(screen.getByLabelText('설치된 모델'), { target: { value: 'beta' } });
    fireEvent.click(screen.getByRole('button', { name: '전환' }));
    await screen.findByText(/접수 결과를 확인할 수 없습니다/);
    expect((screen.getByRole('button', { name: '전환' }) as HTMLButtonElement).disabled).toBe(true);
    expect(sessionStorage.getItem('model-switch-request')).toContain(posted[0].key);
    expect(JSON.stringify(sessionStorage)).not.toContain('operator-test');
    view.unmount();
    render(<ModelControl />);
    fireEvent.click(screen.getByRole('button', { name: '같은 요청으로 접수 확인' }));
    await waitFor(() => expect(posted).toHaveLength(2));
    expect(posted[1]).toEqual(posted[0]);
    await screen.findByText(/작업 beta: queued/);
    expect(sessionStorage.getItem('model-switch-uncertain')).toBeNull();
  });

  it.each([403, 409, 422])('treats HTTP %i as rejection without allowing a stale second submission', async status => {
    let posts = 0;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (init?.method === 'POST') { posts++; return response({ error: { message: '선택할 수 없는 모델' } }, status); }
      if (url.endsWith('/current')) return response({ operation: null });
      if (url.endsWith('/llm/models')) return response(models());
      throw new Error('unexpected request');
    }));
    render(<ModelControl />);
    await authorizeAndLoad();
    fireEvent.change(screen.getByLabelText('설치된 모델'), { target: { value: 'beta' } });
    fireEvent.click(screen.getByRole('button', { name: '전환' }));
    await screen.findByText(/선택할 수 없는 모델/);
    expect(screen.queryByText(/접수 결과를 확인할 수 없습니다/)).toBeNull();
    expect(sessionStorage.getItem('model-switch-request')).toBeNull();
    expect((screen.getByRole('button', { name: '전환' }) as HTMLButtonElement).disabled).toBe(true);
    expect(posts).toBe(1);
  });

  it('keeps the switch button disabled when current status cannot be verified', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response({ error: { message: 'unavailable' } }, 503)));
    render(<ModelControl />);
    fireEvent.click(screen.getByRole('button', { name: '상태 조회' }));
    await screen.findByText(/일부 상태를 확인하지 못했습니다/);
    expect(screen.queryByRole('button', { name: '전환' })).toBeNull();
  });

  it('prefers the current recovery operation to a saved terminal operation and blocks switches', async () => {
    sessionStorage.setItem('model-switch-operation', 'b'.repeat(32));
    const lookedUp: string[] = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input); lookedUp.push(url);
      if (url.endsWith('/current')) return response({ operation: { id: opId, model: 'beta', state: 'recovery_required' } });
      if (url.endsWith('/llm/models')) return response(models());
      throw new Error('unexpected request');
    }));
    render(<ModelControl />);
    await authorizeAndLoad();
    fireEvent.change(screen.getByLabelText('설치된 모델'), { target: { value: 'beta' } });
    expect(screen.getByText(/관리자 복구가 필요합니다/)).toBeTruthy();
    expect((screen.getByRole('button', { name: '전환' }) as HTMLButtonElement).disabled).toBe(true);
    expect(lookedUp.some(path => path.endsWith('b'.repeat(32)))).toBe(false);
    expect(sessionStorage.getItem('model-switch-operation')).toBe(opId);
  });

  it('preserves the original key when a submission times out', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (init?.method === 'POST') return new Promise<Response>((_resolve, reject) => {
        init.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
      });
      if (url.endsWith('/current')) return response({ operation: null });
      if (url.endsWith('/llm/models')) return response(models());
      throw new Error('unexpected request');
    }));
    render(<ModelControl />);
    await authorizeAndLoad();
    fireEvent.change(screen.getByLabelText('설치된 모델'), { target: { value: 'beta' } });
    vi.useFakeTimers();
    fireEvent.click(screen.getByRole('button', { name: '전환' }));
    const original = sessionStorage.getItem('model-switch-request');
    await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
    expect(screen.getByText(/접수 결과를 확인할 수 없습니다/)).toBeTruthy();
    expect(sessionStorage.getItem('model-switch-request')).toBe(original);
    expect((screen.getByRole('button', { name: '전환' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('does not let a late response from an unmounted screen erase a newer request', async () => {
    let posts = 0;
    let finishOld: ((value: Response) => void) | undefined;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (init?.method === 'POST') {
        posts++;
        if (posts === 1) return new Promise<Response>(resolve => { finishOld = resolve; });
        if (posts === 2) return response({ id: 'b'.repeat(32), model: 'beta', state: 'succeeded' }, 202);
        return new Promise<Response>(() => {});
      }
      if (url.endsWith('/current')) return response({ operation: null });
      if (url.endsWith('b'.repeat(32))) return response({ id: 'b'.repeat(32), model: 'beta', state: 'succeeded' });
      if (url.endsWith('/llm/models')) return response(models('beta'));
      throw new Error('unexpected request');
    }));
    const first = render(<ModelControl />);
    await authorizeAndLoad();
    fireEvent.change(screen.getByLabelText('설치된 모델'), { target: { value: 'beta' } });
    fireEvent.click(screen.getByRole('button', { name: '전환' }));
    await waitFor(() => expect(posts).toBe(1));
    first.unmount();

    render(<ModelControl />);
    fireEvent.click(screen.getByRole('button', { name: '같은 요청으로 접수 확인' }));
    await screen.findByText(/작업 beta: succeeded/);
    await screen.findByText(/로드된 모델: beta/);
    fireEvent.change(screen.getByLabelText('설치된 모델'), { target: { value: 'alpha' } });
    await waitFor(() => expect((screen.getByRole('button', { name: '전환' }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole('button', { name: '전환' }));
    await waitFor(() => expect(posts).toBe(3));
    const current = sessionStorage.getItem('model-switch-request');
    await act(async () => { finishOld?.(response({ id: opId, model: 'beta', state: 'succeeded' }, 202)); });
    expect(sessionStorage.getItem('model-switch-request')).toBe(current);
    expect(sessionStorage.getItem('model-switch-uncertain')).toBe('1');
    expect(sessionStorage.getItem('model-switch-operation')).toBe('b'.repeat(32));
  });

  it('polls slow responses without starvation and refreshes the model after completion', async () => {
    let submitted = false;
    let posts = 0;
    let polls = 0;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (init?.method === 'POST') {
        posts++; submitted = true;
        return response({ id: opId, model: 'beta', state: 'queued' }, 202);
      }
      if (url.endsWith('/current')) {
        if (!submitted) return response({ operation: null });
        polls++;
        return response({ operation: polls === 1 ? { id: opId, model: 'beta', state: 'queued' } : null });
      }
      if (url.endsWith(opId)) return response({ id: opId, model: 'beta', state: 'succeeded' });
      if (url.endsWith('/llm/models')) {
        if (submitted) await new Promise(resolve => setTimeout(resolve, 2300));
        return response(models(polls >= 2 ? 'beta' : 'alpha'));
      }
      throw new Error('unexpected request');
    }));
    render(<ModelControl />);
    await authorizeAndLoad();
    fireEvent.change(screen.getByLabelText('설치된 모델'), { target: { value: 'beta' } });
    const button = screen.getByRole('button', { name: '전환' });
    fireEvent.click(button); fireEvent.click(button);
    await screen.findByText(/작업 beta: succeeded/, {}, { timeout: 12000 });
    expect(screen.getByText(/로드된 모델: beta/)).toBeTruthy();
    expect(posts).toBe(1);
  }, 15000);
});


it('notifies the parent only after a successful operation and model snapshot', async () => {
  const notify = vi.fn();
  sessionStorage.setItem('model-switch-operation', opId);
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith('/current')) return response({ operation: null });
    if (url.endsWith(opId)) return response({ id: opId, model: 'beta', state: 'succeeded' });
    if (url.endsWith('/llm/models')) return response(models('beta'));
    throw new Error(url);
  }));
  render(<ModelControl onSwitchSucceeded={notify} />);
  await authorizeAndLoad();
  await waitFor(() => expect(notify).toHaveBeenCalledTimes(1));
  fireEvent.click(screen.getByRole('button', { name: '상태 조회' }));
  await screen.findByText(/로드된 모델: beta/);
  expect(notify).toHaveBeenCalledTimes(1);
});
