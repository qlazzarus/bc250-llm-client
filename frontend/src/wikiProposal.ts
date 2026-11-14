import type { Message, Mode } from './api';

/** D06 is a read-only classification; D07 owns presentation and lifecycle. */
export type WikiProposal = 'none' | 'ordinary' | 'contextual';
type PriorMessage = { message: Message; sameConversation: boolean; immediatelyPrecedes: boolean; completed: boolean };
type ProposalInput = { mode: Mode; message: Message; previous?: PriorMessage };

// Deliberately narrow phrases: asking for information is not permission to save.
const KOREAN_REQUEST = /(?:추가|상세|관련)\s*(?:정보|자료|내용)(?:을|를|이|가)?\s*(?:좀\s*)?(?:알려\s*주(?:세요|시면|실\s*수\s*있나요)|제공해\s*주(?:세요|시면|실\s*수\s*있나요)|주시면)/i;
const ENGLISH_REQUEST = /\b(?:please\s+(?:provide|share|send)|(?:could|can|would)\s+you\s+(?:please\s+)?(?:provide|share|send))\s+(?:some\s+|the\s+)?(?:additional|more|further)\s+(?:information|details|context|material)\b/i;

export function classifyWikiProposal({ mode, message, previous }: ProposalInput): WikiProposal {
  if (message.role !== 'user' || message.id.startsWith('local-')) return 'none';
  const content = message.content.trim();
  if (Array.from(content).length < 1000) return 'none';
  if (mode !== 'quick' || !previous || !previous.sameConversation || !previous.immediatelyPrecedes || !previous.completed
      || previous.message.role !== 'assistant' || previous.message.id.startsWith('stream-')) return 'ordinary';
  // Avoid interpreting quoted source material, code or a question as an affirmative reply.
  if (/^(?:```|~~~|>|[“"'‘])/.test(content) || /[?？]\s*$/.test(content)) return 'ordinary';
  const prior = previous.message.content;
  // A quoted example or code could contain a request without making one.
  if (/[`"“”‘’]/.test(prior) || /^\s*>/m.test(prior)) return 'ordinary';
  return KOREAN_REQUEST.test(prior) || ENGLISH_REQUEST.test(prior) ? 'contextual' : 'ordinary';
}
