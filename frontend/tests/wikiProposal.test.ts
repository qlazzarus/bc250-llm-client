import { describe, expect, it } from 'vitest';
import { classifyWikiProposal } from '../src/wikiProposal';
import type { Message } from '../src/api';

const message = (id: string, role: Message['role'], content: string): Message => ({ id, role, content, created_at: '' });
const text = 'BC-250 참고 자료입니다. ' + '자료 내용입니다. '.repeat(120);
const user = message('u', 'user', text);
const prior = (content: string) => ({ message: message('a', 'assistant', content), sameConversation: true, immediatelyPrecedes: true, completed: true });
const classify = (previous = prior('추가 정보를 주시면 더 정확하게 답변해 드릴 수 있습니다!')) => classifyWikiProposal({ mode: 'quick', message: user, previous });

describe('contextual Wiki proposal is conservative and read-only', () => {
  it.each([
    '정보를 찾을 수 없습니다. 혹시 추가 정보가 있으신가요? 추가 정보를 주시면 더 정확하게 답변해 드릴 수 있습니다!',
    '관련 자료를 알려주세요.',
    'Please share more information about the server.',
    'Could you provide additional details?',
  ])('recognizes a direct request: %s', phrase => expect(classify(prior(phrase))).toBe('contextual'));
  it.each([
    '어떤 서버인가요?', '추가 정보는 제공하지 마세요.', '설명을 알려주세요.',
    '“추가 정보를 알려주세요”라는 문구를 해석하세요.', '```\n추가 정보를 알려주세요\n```',
    '> 추가 정보를 알려주세요', 'More information is available.',
  ])('keeps ambiguous requests ordinary: %s', phrase => expect(classify(prior(phrase))).toBe('ordinary'));
  it('handles character boundaries and mode independently of memory settings', () => {
    expect(classifyWikiProposal({ mode: 'quick', message: { ...user, content: '가'.repeat(999) }, previous: prior('추가 정보를 알려주세요') })).toBe('none');
    expect(classifyWikiProposal({ mode: 'quick', message: { ...user, content: '😀'.repeat(1000) }, previous: prior('추가 정보를 알려주세요') })).toBe('contextual');
    expect(classifyWikiProposal({ mode: 'quick', message: { ...user, content: '가'.repeat(12000) }, previous: prior('추가 정보를 알려주세요') })).toBe('contextual');
    expect(classifyWikiProposal({ mode: 'roleplay', message: user, previous: prior('추가 정보를 알려주세요') })).toBe('ordinary');
  });
  it('requires a persisted user message and a known adjacent completed assistant response', () => {
    expect(classifyWikiProposal({ mode: 'quick', message: { ...user, id: 'local-1' }, previous: prior('추가 정보를 알려주세요') })).toBe('none');
    expect(classifyWikiProposal({ mode: 'quick', message: { ...user, role: 'assistant' }, previous: prior('추가 정보를 알려주세요') })).toBe('none');
    expect(classifyWikiProposal({ mode: 'quick', message: user })).toBe('ordinary');
    for (const update of [{ completed: false }, { sameConversation: false }, { immediatelyPrecedes: false }, { message: message('stream-1', 'assistant', '추가 정보를 알려주세요') }, { message: message('u2', 'user', '추가 정보를 알려주세요') }]) {
      expect(classify({ ...prior('추가 정보를 알려주세요'), ...update })).toBe('ordinary');
    }
  });
  it.each(['```\n', '> 인용\n', '“인용 ', '질문입니다. '])('does not elevate code, quotations or questions: %s', prefix => {
    const content = prefix === '질문입니다. ' ? prefix + text + '?' : prefix + text;
    expect(classifyWikiProposal({ mode: 'quick', message: { ...user, content }, previous: prior('추가 정보를 알려주세요') })).toBe('ordinary');
  });
});
