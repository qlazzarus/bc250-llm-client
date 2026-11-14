import type { Message, Mode } from './api';

/** D06 is a read-only classification; D07 owns presentation and lifecycle. */
export type WikiProposal = 'none' | 'ordinary' | 'contextual';
type PriorMessage = { message: Message; sameConversation: boolean; immediatelyPrecedes: boolean; completed: boolean };
type ProposalInput = { mode: Mode; message: Message; previous?: PriorMessage };

// Deliberately narrow phrases: asking for information is not permission to save.
const KOREAN_REQUEST = /(?:추가|상세|관련|다른)\s*(?:정보|자료|내용)(?:\s*\([^()\n]{0,120}\))?(?:을|를|이|가)?\s*(?:좀\s*)?(?:알려\s*주(?:세요|시면|실\s*수\s*있나요)|제공해\s*주(?:세요|시면|실\s*수\s*있나요)|주시면)/i;
const KOREAN_CONTEXT_REQUEST = /(?:어떤|어느)\s*(?:분야|기기|제품|모델)(?:\s*\([^()\n]{0,120}\))?[^\n.!?？]{0,80}?\s*알려\s*주(?:세요|시면|실\s*수\s*있나요)/i;
const ENGLISH_REQUEST = /\b(?:please\s+(?:provide|share|send)|(?:could|can|would)\s+you\s+(?:please\s+)?(?:provide|share|send))\s+(?:some\s+|the\s+)?(?:additional|more|further)\s+(?:information|details|context|material)\b/i;

export function classifyWikiProposal({ mode, message, previous }: ProposalInput): WikiProposal {
  if (message.role !== 'user' || message.id.startsWith('local-')) return 'none';
  const content = message.content.trim();
  const length = Array.from(content).length;
  const multilineMaterial = length >= 200 && content.split(/\r?\n/).filter(line => line.trim()).length >= 2;
  if (length < 1000 && !multilineMaterial) return 'none';
  if (mode !== 'quick' || !previous || !previous.sameConversation || !previous.immediatelyPrecedes || !previous.completed
      || previous.message.role !== 'assistant' || previous.message.id.startsWith('stream-')) return 'ordinary';
  // Avoid interpreting quoted source material, code or a question as an affirmative reply.
  if (/^(?:```|~~~|>|[“"'‘])/.test(content) || /[?？]\s*$/.test(content)) return 'ordinary';
  const prior = previous.message.content;
  // A quoted example or code could contain a request without making one.
  if (/[`"“”‘’]/.test(prior) || /^\s*>/m.test(prior)) return 'ordinary';
  return KOREAN_REQUEST.test(prior) || KOREAN_CONTEXT_REQUEST.test(prior) || ENGLISH_REQUEST.test(prior) ? 'contextual' : 'ordinary';
}
