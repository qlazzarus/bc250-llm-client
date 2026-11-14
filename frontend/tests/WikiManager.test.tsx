// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { ApiError, request } from '../src/api';
import { WikiManager } from '../src/components/WikiManager';

vi.mock('../src/api', async importOriginal => ({ ...await importOriginal<typeof import('../src/api')>(), request: vi.fn() }));
const mock = vi.mocked(request);
const original = { id: 1, title: '서버', category: 'general', content: 'Fedora', revision: 1, updated_at: 't' };
const latest = { ...original, content: 'Ubuntu', revision: 2 };

beforeEach(() => {
  mock.mockReset();
  mock.mockImplementation(async path => {
    if (path === '/wiki/capabilities') return { store_id: 'integrated' } as never;
    if (path === '/wiki/documents') return [original] as never;
    if (path === '/wiki/documents/1?store_id=integrated') return original as never;
    throw new Error(`unexpected ${path}`);
  });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it('shows shared space, searches, and creates a document', async () => {
  mock.mockImplementation(async (path, options) => {
    if (path === '/wiki/capabilities') return { store_id: 'integrated' } as never;
    if (path === '/wiki/search?q=server') return [original] as never;
    if (path === '/wiki/documents' && options?.method === 'POST') return { id: 2, revision: 1 } as never;
    if (path === '/wiki/documents') return [original] as never;
    throw new Error(`unexpected ${path}`);
  });
  render(<WikiManager onClose={() => {}} />);
  expect(await screen.findByText(/이 앱에 접근할 수 있는 가족/)).toBeTruthy();
  fireEvent.change(await screen.findByLabelText('문서 검색'), { target: { value: 'server' } });
  fireEvent.click(screen.getByRole('button', { name: '검색' }));
  await waitFor(() => expect(mock).toHaveBeenCalledWith('/wiki/search?q=server'));
  fireEvent.change(screen.getByLabelText('제목'), { target: { value: '새 문서' } });
  fireEvent.change(screen.getByLabelText('본문'), { target: { value: '<script>안전</script>' } });
  fireEvent.click(screen.getByRole('button', { name: '저장' }));
  expect(await screen.findByText('저장했습니다.')).toBeTruthy();
  expect(document.querySelector('script')).toBeNull();
});

it('retains the draft after conflict and requires an explicit new save', async () => {
  let reads = 0;
  mock.mockImplementation(async (path, options) => {
    if (path === '/wiki/capabilities') return { store_id: 'integrated' } as never;
    if (path === '/wiki/documents') return [original] as never;
    if (path === '/wiki/documents/1?store_id=integrated' && options?.method === 'PUT') {
      const body = JSON.parse(String(options.body));
      if (body.expected_revision === 1) throw new ApiError(409, '충돌');
      expect(body.expected_revision).toBe(2); expect(body.content).toBe('내 초안');
      return { id: 1, revision: 3 } as never;
    }
    if (path === '/wiki/documents/1?store_id=integrated') return (++reads === 1 ? original : latest) as never;
    throw new Error(`unexpected ${path}`);
  });
  render(<WikiManager onClose={() => {}} />);
  fireEvent.click(await screen.findByRole('button', { name: '서버' }));
  await waitFor(() => expect((screen.getByLabelText('본문') as HTMLTextAreaElement).value).toBe('Fedora'));
  fireEvent.change(screen.getByLabelText('본문'), { target: { value: '내 초안' } });
  fireEvent.click(screen.getByRole('button', { name: '저장' }));
  const conflict = await screen.findByRole('region', { name: '문서 충돌 비교' });
  expect(conflict.textContent).toContain('Ubuntu'); expect(conflict.textContent).toContain('내 초안');
  expect((screen.getByLabelText('본문') as HTMLTextAreaElement).value).toBe('내 초안');
  fireEvent.click(within(conflict).getByRole('button', { name: '최신본 기준으로 계속 편집' }));
  expect(mock.mock.calls.filter(([, options]) => options?.method === 'PUT')).toHaveLength(1);
  fireEvent.click(screen.getByRole('button', { name: '저장' }));
  expect(await screen.findByText('저장했습니다.')).toBeTruthy();
  expect(mock.mock.calls.filter(([, options]) => options?.method === 'PUT')).toHaveLength(2);
});

it('shows saved revision diff and asks before deleting', async () => {
  vi.spyOn(window, 'confirm').mockReturnValue(true);
  mock.mockImplementation(async (path, options) => {
    if (path === '/wiki/capabilities') return { store_id: 'integrated' } as never;
    if (path === '/wiki/documents') return [original] as never;
    if (path === '/wiki/documents/1?store_id=integrated' && !options?.method) return original as never;
    if (path.includes('/revisions?')) return [{ revision: 2, created_at: 't' }, { revision: 1, created_at: 't' }] as never;
    if (path.includes('/diff?')) return { diff: { title: '', category: '', content: '-old\n+new' } } as never;
    if (options?.method === 'DELETE') return { status: 'deleted' } as never;
    throw new Error(`unexpected ${path}`);
  });
  render(<WikiManager onClose={() => {}} />);
  fireEvent.click(await screen.findByRole('button', { name: '서버' }));
  fireEvent.click(await screen.findByRole('button', { name: '이력 불러오기' }));
  fireEvent.click(await screen.findByRole('button', { name: '차이 보기' }));
  expect(await screen.findByText('-old +new')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: '문서 삭제' }));
  expect(await screen.findByText('삭제했습니다.')).toBeTruthy();
  expect(window.confirm).toHaveBeenCalled();
});

it('shows the legacy Wiki link without issuing integrated writes', async () => {
  mock.mockResolvedValueOnce({ store_id: 'legacy_external' } as never);
  render(<WikiManager onClose={() => {}} />);
  expect((await screen.findByRole('link', { name: '가족 Wiki' })).getAttribute('href')).toBe('/wiki/');
  expect(screen.queryByRole('button', { name: '저장' })).toBeNull();
});

it('does not submit duplicate creates while a save is pending', async () => {
  let resolveSave!: (value: { id: number; revision: number }) => void;
  const pending = new Promise<{ id: number; revision: number }>(resolve => { resolveSave = resolve; });
  mock.mockImplementation(async (path, options) => {
    if (path === '/wiki/capabilities') return { store_id: 'integrated' } as never;
    if (path === '/wiki/documents' && options?.method === 'POST') return await pending as never;
    if (path === '/wiki/documents') return [] as never;
    throw new Error(`unexpected ${path}`);
  });
  render(<WikiManager onClose={() => {}} />);
  fireEvent.change(await screen.findByLabelText('제목'), { target: { value: '제목' } });
  fireEvent.change(screen.getByLabelText('본문'), { target: { value: '본문' } });
  const save = screen.getByRole('button', { name: '저장' });
  fireEvent.click(save); fireEvent.click(save);
  expect(mock.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(1);
  resolveSave({ id: 1, revision: 1 });
  expect(await screen.findByText('저장했습니다.')).toBeTruthy();
});

it('loads and explicitly saves aliases using the current revision', async () => {
  mock.mockImplementation(async (path, options) => {
    if (path === '/wiki/capabilities') return { store_id: 'integrated' } as never;
    if (path === '/wiki/documents') return [original] as never;
    if (path === '/wiki/documents/1?store_id=integrated') return original as never;
    if (path === '/wiki/documents/1/aliases?store_id=integrated' && options?.method === 'PUT') {
      expect(JSON.parse(String(options.body))).toEqual({ expected_revision: 1, aliases: ['bc250'] });
      return { revision: 2, aliases: ['bc250'] } as never;
    }
    if (path === '/wiki/documents/1/aliases?store_id=integrated') return [] as never;
    throw new Error(`unexpected ${path}`);
  });
  render(<WikiManager onClose={() => {}} />);
  fireEvent.click(await screen.findByRole('button', { name: '서버' }));
  fireEvent.click(await screen.findByRole('button', { name: '별칭 불러오기' }));
  fireEvent.change(await screen.findByLabelText('별칭 목록'), { target: { value: 'bc250' } });
  fireEvent.click(screen.getByRole('button', { name: '별칭 저장' }));
  expect(await screen.findByText('별칭을 저장했습니다.')).toBeTruthy();
  expect(screen.getByText('문서 편집 · revision 2')).toBeTruthy();
});

it('previews markdown safely while keeping the editable source intact', async () => {
  render(<WikiManager onClose={() => {}} />);
  const editor = await screen.findByLabelText('본문');
  const source = '# 제목\n\n**굵게**\n\n<script>alert(1)</script>';
  fireEvent.change(editor, { target: { value: source } });
  const preview = screen.getByRole('region', { name: '마크다운 미리보기' });
  expect(within(preview).getByRole('heading', { name: '제목' })).toBeTruthy();
  expect(within(preview).getByText('굵게').tagName).toBe('STRONG');
  expect(preview.querySelector('script')).toBeNull();
  expect((editor as HTMLTextAreaElement).value).toBe(source);
  fireEvent.click(screen.getByRole('button', { name: '미리보기 숨기기' }));
  expect(screen.queryByRole('region', { name: '마크다운 미리보기' })).toBeNull();
});

it('opens linked Wiki documents in place and protects unsaved edits', async () => {
  const target = { ...original, id: 2, title: '두 번째 문서', content: '두 번째 내용' };
  const first = { ...original, content: '[두 번째로 이동](#/wiki/2)' };
  mock.mockImplementation(async path => {
    if (path === '/wiki/capabilities') return { store_id: 'integrated' } as never;
    if (path === '/wiki/documents') return [first, target] as never;
    if (path === '/wiki/documents/1?store_id=integrated') return first as never;
    if (path === '/wiki/documents/2?store_id=integrated') return target as never;
    throw new Error(`unexpected ${path}`);
  });
  const navigate = vi.fn();
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
  render(<WikiManager onClose={() => {}} onNavigate={navigate} />);
  fireEvent.click(await screen.findByRole('button', { name: '서버' }));
  await screen.findByRole('link', { name: '두 번째로 이동' });
  fireEvent.change(screen.getByLabelText('제목'), { target: { value: '변경 중' } });
  fireEvent.click(screen.getByRole('link', { name: '두 번째로 이동' }));
  expect(confirm).toHaveBeenCalled();
  expect(mock).not.toHaveBeenCalledWith('/wiki/documents/2?store_id=integrated');
  confirm.mockReturnValue(true);
  fireEvent.click(screen.getByRole('link', { name: '두 번째로 이동' }));
  await waitFor(() => expect((screen.getByLabelText('제목') as HTMLInputElement).value).toBe('두 번째 문서'));
  expect(navigate).toHaveBeenLastCalledWith(2);
});
