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
