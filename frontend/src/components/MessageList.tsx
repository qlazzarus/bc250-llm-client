import { useVirtualizer } from '@tanstack/react-virtual';
import { memo, useEffect, useRef, useState } from 'react';
import { type ImageCapabilities, type ImageJob, type Message, type Mode } from '../api';
import { ImagePromptResult, parseImagePromptConfig } from './ImagePromptResult';
import { MarkdownContent } from './MarkdownContent';
import type { ImageDraftEditor } from '../main';
import { classifyWikiProposal, type WikiProposal } from '../wikiProposal';

const FOLLOW_THRESHOLD_PX = 80;

type ImageCardProps = {
  capabilities: ImageCapabilities | undefined; drafts: Record<string, ImageDraftEditor>;
  onEnsureDraft: (conversationId: string, messageId: string, config: Pick<ImageDraftEditor, 'prompt' | 'width' | 'height' | 'preset_name'>) => Promise<void>;
  onUpdateDraft: (messageId: string, update: Partial<Pick<ImageDraftEditor, 'prompt' | 'width' | 'height' | 'generation_config' | 'preset_name'>>, fallback: ImageDraftEditor) => void;
  onPersistDraft: (conversationId: string, messageId: string, draft: ImageDraftEditor) => Promise<unknown>;
  onSubmitImageJob: (conversationId: string, messageId: string, draft: ImageDraftEditor, requestId: string) => Promise<ImageJob>;
  jobs: ImageJob[]; onCancelImageJob: (job: ImageJob) => Promise<void>;
};

const ChatMessage = memo(function ChatMessage({ message, mode, conversationId, imageCards, onWikiDraft, candidateDismissed, onDismissCandidate, proposal }: { message: Message; mode: Mode; conversationId: string; imageCards: ImageCardProps; onWikiDraft: (message: Message) => void; candidateDismissed: boolean; onDismissCandidate: (id: string) => void; proposal: WikiProposal }) {
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
    {message.role === 'user' && !message.id.startsWith('local-') && <div className="wiki-message-actions">
      {!candidateDismissed && proposal === 'contextual' && <div className="wiki-context-proposal" role="region" aria-label="공유 Wiki 저장 제안"><p>이 내용을 다음에도 참고할 수 있도록 가족 공유 Wiki에 저장할까요?</p><small>선택은 이 브라우저 화면에서만 유지되며 새로고침하면 다시 표시될 수 있습니다.</small><div><button type="button" onClick={() => onWikiDraft(message)}>저장 내용 확인</button> <button type="button" className="plain" onClick={() => onDismissCandidate(message.id)}>이번 대화에서만 사용</button></div></div>}
      {!candidateDismissed && proposal === 'ordinary' && <div>이 메시지를 공유 Wiki 문서로 검토할 수 있습니다. <button type="button" className="plain" onClick={() => onWikiDraft(message)}>초안 검토</button> <button type="button" className="plain" aria-label="문서 후보 닫기" onClick={() => onDismissCandidate(message.id)}>닫기</button></div>}
      <button type="button" className="plain" onClick={() => onWikiDraft(message)}>공유 Wiki에 저장</button>
    </div>}
    {message.role === 'assistant' && message.wiki_evidence && <details className="wiki-evidence"><summary>Wiki 참고 정보</summary>
      <p>검색 {message.wiki_evidence.retrieved.length}건 · 모델에 전달 {message.wiki_evidence.provided.length}건 · 공유 기억 {message.wiki_evidence.memory_provided.length}건</p>
      {message.wiki_evidence.retrieved.filter(item => !message.wiki_evidence!.provided.some(source => source.id === item.id)).map(item => <p key={item.id}>검색됐으나 모델에 전달되지 않은 문서: {item.title} (문서 {item.id})</p>)}
      <p>전달된 문서 ID 인용: {message.wiki_evidence.cited.length ? message.wiki_evidence.cited.join(', ') : '없음'}</p>
      {(message.wiki_evidence.memory_cited?.length ?? 0) > 0 && <p>전달된 공유 기억 ID 인용: {message.wiki_evidence.memory_cited?.join(', ')}</p>}
      <p>문서 ID 인용만 확인했습니다. 실제 사용 여부와 내용 일치는 검증하지 않았습니다.</p>
      {message.wiki_evidence.provided.map(item => <div key={item.id}><b>{item.title} (문서 {item.id})</b><p>{item.excerpt}</p></div>)}
      {message.wiki_evidence.invalid_citations.length > 0 && <p>전달하지 않은 문서 ID 인용: {message.wiki_evidence.invalid_citations.join(', ')}</p>}
      {message.wiki_evidence.unrecognized_citations === undefined && <p>이전 기록에는 인용 형식 검사 결과가 없습니다.</p>}
      {(message.wiki_evidence.unrecognized_citations?.length ?? 0) > 0 && <p>확인할 수 없는 인용 형식 (문서 ID로 인정되지 않음): {message.wiki_evidence.unrecognized_citations?.map((value, index) => <span key={index}><code>{value}</code>{index + 1 < message.wiki_evidence!.unrecognized_citations!.length ? ', ' : ''}</span>)}</p>}
    </details>}
  </article>;
});

export function MessageList({ mode, messages, hasMore, onLoadOlder, scrollSignal, conversationId, imageCapabilities, imageDrafts, onEnsureImageDraft, onUpdateImageDraft, onPersistImageDraft, onSubmitImageJob, imageJobs, onCancelImageJob, onWikiDraft, dismissedWikiProposals, onDismissWikiProposal }: {
  mode: Mode; messages: Message[]; hasMore: boolean; onLoadOlder: () => Promise<void>; scrollSignal: number;
  conversationId: string; imageCapabilities: ImageCapabilities | undefined; imageDrafts: Record<string, ImageDraftEditor>;
  onEnsureImageDraft: ImageCardProps['onEnsureDraft']; onUpdateImageDraft: ImageCardProps['onUpdateDraft'];
  onPersistImageDraft: ImageCardProps['onPersistDraft']; onSubmitImageJob: ImageCardProps['onSubmitImageJob'];
  imageJobs: ImageJob[]; onCancelImageJob: ImageCardProps['onCancelImageJob']; onWikiDraft: (message: Message) => void;
  dismissedWikiProposals: Set<string>; onDismissWikiProposal: (conversationId: string, messageId: string) => void;
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

  return <div className="message-viewport">
    <div className="messages" ref={parentRef} onScroll={() => {
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
          const prior = messages[item.index - 1];
          const proposal = classifyWikiProposal({ mode, message, previous: prior ? { message: prior, sameConversation: true, immediatelyPrecedes: true, completed: prior.generation_completed === true } : undefined });
          return <div key={message.id} ref={virtualizer.measureElement} data-index={item.index} className="virtual-message" style={{ transform: 'translateY(' + item.start + 'px)' }}>
            <ChatMessage message={message} mode={mode} conversationId={conversationId}
              onWikiDraft={onWikiDraft} proposal={proposal} candidateDismissed={dismissedWikiProposals.has(JSON.stringify([conversationId, message.id]))} onDismissCandidate={id => onDismissWikiProposal(conversationId, id)}
              imageCards={{ capabilities: imageCapabilities, drafts: imageDrafts, onEnsureDraft: onEnsureImageDraft, onUpdateDraft: onUpdateImageDraft, onPersistDraft: onPersistImageDraft, onSubmitImageJob, jobs: imageJobs, onCancelImageJob }} />
          </div>;
        })}
      </div>
    </div>
    {!following && <button className="jump-latest" onClick={scrollToLatest}>최신 메시지로</button>}
  </div>;
}
