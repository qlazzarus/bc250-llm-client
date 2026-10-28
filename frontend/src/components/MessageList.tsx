import { useVirtualizer } from '@tanstack/react-virtual';
import { memo, useEffect, useRef, useState } from 'react';
import { type ImageCapabilities, type ImageJob, type Message, type Mode } from '../api';
import { ImagePromptResult, parseImagePromptConfig } from './ImagePromptResult';
import { MarkdownContent } from './MarkdownContent';
import type { ImageDraftEditor } from '../main';

const FOLLOW_THRESHOLD_PX = 80;

type ImageCardProps = {
  capabilities: ImageCapabilities | undefined; drafts: Record<string, ImageDraftEditor>;
  onEnsureDraft: (conversationId: string, messageId: string, config: Pick<ImageDraftEditor, 'prompt' | 'width' | 'height' | 'preset_name'>) => Promise<void>;
  onUpdateDraft: (messageId: string, update: Partial<Pick<ImageDraftEditor, 'prompt' | 'width' | 'height' | 'generation_config' | 'preset_name'>>, fallback: ImageDraftEditor) => void;
  onPersistDraft: (conversationId: string, messageId: string, draft: ImageDraftEditor) => Promise<unknown>;
  onSubmitImageJob: (conversationId: string, messageId: string, draft: ImageDraftEditor, requestId: string) => Promise<ImageJob>;
  jobs: ImageJob[]; onCancelImageJob: (job: ImageJob) => Promise<void>;
};

const ChatMessage = memo(function ChatMessage({ message, mode, conversationId, imageCards }: { message: Message; mode: Mode; conversationId: string; imageCards: ImageCardProps }) {
  const imagePrompt = mode === 'image_prompt' && message.role === 'assistant' ? parseImagePromptConfig(message.content) : undefined;
  return <article className={message.role}><b>{message.role === 'user' ? '나' : 'BC-250'}</b>
    {message.role === 'assistant'
      ? imagePrompt
        ? <ImagePromptResult conversationId={conversationId} messageId={message.id} config={imagePrompt} capabilities={imageCards.capabilities}
            draft={imageCards.drafts[message.id]} onEnsureDraft={imageCards.onEnsureDraft} onUpdateDraft={imageCards.onUpdateDraft}
            onPersistDraft={imageCards.onPersistDraft} onSubmitImageJob={imageCards.onSubmitImageJob}
            jobs={imageCards.jobs.filter(job => job.source_message_id === message.id)} onCancelImageJob={imageCards.onCancelImageJob} />
        : <div className="markdown-content">{message.content ? <MarkdownContent content={message.content} /> : (message.id.startsWith('stream-') ? '응답 생성 또는 대기열 처리 중…' : '')}</div>
      : <p>{message.content}</p>}
  </article>;
});

export function MessageList({ mode, messages, hasMore, onLoadOlder, scrollSignal, conversationId, imageCapabilities, imageDrafts, onEnsureImageDraft, onUpdateImageDraft, onPersistImageDraft, onSubmitImageJob, imageJobs, onCancelImageJob }: {
  mode: Mode; messages: Message[]; hasMore: boolean; onLoadOlder: () => Promise<void>; scrollSignal: number;
  conversationId: string; imageCapabilities: ImageCapabilities | undefined; imageDrafts: Record<string, ImageDraftEditor>;
  onEnsureImageDraft: ImageCardProps['onEnsureDraft']; onUpdateImageDraft: ImageCardProps['onUpdateDraft'];
  onPersistImageDraft: ImageCardProps['onPersistDraft']; onSubmitImageJob: ImageCardProps['onSubmitImageJob'];
  imageJobs: ImageJob[]; onCancelImageJob: ImageCardProps['onCancelImageJob'];
}) {
  const parentRef = useRef<HTMLDivElement>(null);
  const followingRef = useRef(true);
  const loadingOlderRef = useRef(false);
  const [following, setFollowing] = useState(true);
  const last = messages[messages.length - 1];
  const virtualizer = useVirtualizer({
    count: messages.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 120,
    overscan: 8,
  });

  const scrollToLatest = () => {
    followingRef.current = true;
    setFollowing(true);
    requestAnimationFrame(() => virtualizer.scrollToIndex(Math.max(0, messages.length - 1), { align: 'end' }));
  };

  useEffect(() => { scrollToLatest(); }, [scrollSignal]);
  useEffect(() => {
    if (followingRef.current) requestAnimationFrame(() => virtualizer.scrollToIndex(Math.max(0, messages.length - 1), { align: 'end' }));
  }, [messages.length, last?.content]);

  const loadOlder = async () => {
    const element = parentRef.current;
    if (!element || loadingOlderRef.current || !hasMore) return;
    loadingOlderRef.current = true;
    const beforeHeight = element.scrollHeight;
    const beforeTop = element.scrollTop;
    try {
      await onLoadOlder();
      requestAnimationFrame(() => requestAnimationFrame(() => {
        const target = parentRef.current;
        if (target) target.scrollTop = beforeTop + target.scrollHeight - beforeHeight;
      }));
    } finally {
      loadingOlderRef.current = false;
    }
  };

  return <div className="messages" ref={parentRef} onScroll={() => {
    const element = parentRef.current;
    if (!element) return;
    const atBottom = element.scrollHeight - element.scrollTop - element.clientHeight <= FOLLOW_THRESHOLD_PX;
    followingRef.current = atBottom;
    setFollowing(atBottom);
    if (element.scrollTop < FOLLOW_THRESHOLD_PX) void loadOlder();
  }}>
    <div className="virtual-spacer" style={{ height: virtualizer.getTotalSize(), position: 'relative' }}>
      {virtualizer.getVirtualItems().map(item => {
        const message = messages[item.index];
        return <div key={message.id} ref={virtualizer.measureElement} data-index={item.index} className="virtual-message" style={{ transform: 'translateY(' + item.start + 'px)' }}>
          <ChatMessage message={message} mode={mode} conversationId={conversationId}
            imageCards={{ capabilities: imageCapabilities, drafts: imageDrafts, onEnsureDraft: onEnsureImageDraft, onUpdateDraft: onUpdateImageDraft, onPersistDraft: onPersistImageDraft, onSubmitImageJob, jobs: imageJobs, onCancelImageJob }} />
        </div>;
      })}
    </div>
    {!following && <button className="jump-latest" onClick={scrollToLatest}>최신 메시지로</button>}
  </div>;
}
