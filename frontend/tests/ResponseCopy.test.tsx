// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { MessageList } from '../src/components/MessageList';

vi.mock('@tanstack/react-virtual', () => ({ useVirtualizer: () => ({ getTotalSize: () => 120, getVirtualItems: () => [{ index: 0, start: 0 }], measureElement: () => {}, scrollToIndex: () => {} }) }));
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

function renderResponse() {
  vi.stubGlobal('requestAnimationFrame', () => 1);
  render(<MessageList mode="quick" messages={[{ id: 'a1', role: 'assistant', content: '**원문**\n응답', created_at: '' }]} hasMore={false} onLoadOlder={vi.fn()} scrollSignal={0} conversationId="c1" imageCapabilities={undefined} imageDrafts={{}} onEnsureImageDraft={vi.fn()} onUpdateImageDraft={vi.fn()} onPersistImageDraft={vi.fn()} onSubmitImageJob={vi.fn()} imageJobs={[]} onCancelImageJob={vi.fn()} onWikiDraft={vi.fn()} dismissedWikiProposals={new Set()} onDismissWikiProposal={vi.fn()} />);
}

it('copies the original response including markdown', async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
  renderResponse();
  fireEvent.click(screen.getByRole('button', { name: '응답 복사' }));
  await waitFor(() => expect(writeText).toHaveBeenCalledWith('**원문**\n응답'));
  expect(await screen.findByText('응답을 복사했습니다.')).toBeTruthy();
});

it('offers the response for manual selection when clipboard access fails', async () => {
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: vi.fn().mockRejectedValue(new Error('denied')) } });
  renderResponse();
  fireEvent.click(screen.getByRole('button', { name: '응답 복사' }));
  expect((await screen.findByLabelText('복사할 응답') as HTMLTextAreaElement).value).toBe('**원문**\n응답');
});

it('keeps the Wiki evidence disclosure with response actions and shows its details when expanded', () => {
  vi.stubGlobal('requestAnimationFrame', () => 1);
  const evidence = { version: 1, retrieved: [], provided: [{ id: 4, title: '자료', excerpt: '참고한 발췌' }], memory_provided: [], cited: [4], invalid_citations: [], evidence_status: 'citation_only' as const };
  render(<MessageList mode="quick" messages={[{ id: 'a1', role: 'assistant', content: '응답', created_at: '', wiki_evidence: evidence }]} hasMore={false} onLoadOlder={vi.fn()} scrollSignal={0} conversationId="c1" imageCapabilities={undefined} imageDrafts={{}} onEnsureImageDraft={vi.fn()} onUpdateImageDraft={vi.fn()} onPersistImageDraft={vi.fn()} onSubmitImageJob={vi.fn()} imageJobs={[]} onCancelImageJob={vi.fn()} onWikiDraft={vi.fn()} dismissedWikiProposals={new Set()} onDismissWikiProposal={vi.fn()} />);
  const summary = screen.getByText('Wiki 참고 정보');
  const details = summary.closest('details')!;
  expect(details.open).toBe(false);
  expect(details.closest('.message-tools')).toBe(screen.getByRole('button', { name: '응답 복사' }).closest('.message-tools'));
  fireEvent.click(summary);
  expect(details.open).toBe(true);
  expect(screen.getByText('참고한 발췌')).toBeTruthy();
});
