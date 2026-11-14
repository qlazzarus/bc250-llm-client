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

it('offers an ordinary review entry for 200–999 character multiline material', () => {
  const material = 'BC250 자료\n' + '가상 서버 항목입니다. '.repeat(20);
  expect(classifyWikiProposal({ mode: 'quick', message: { ...user, content: material }, previous: prior('추가 정보를 알려주세요') })).toBe('contextual');
  expect(classifyWikiProposal({ mode: 'quick', message: { ...user, content: material } })).toBe('ordinary');
  expect(classifyWikiProposal({ mode: 'quick', message: { ...user, content: 'a'.repeat(198) + '\nb' } })).toBe('ordinary');
  expect(classifyWikiProposal({ mode: 'quick', message: { ...user, content: 'a'.repeat(197) + '\nb' } })).toBe('none');
  expect(classifyWikiProposal({ mode: 'quick', message: { ...user, content: 'a'.repeat(999) } })).toBe('none');
});


it('recognizes the actual-style follow-up request for a mid-sized multiline message', () => {
  const assistant = 'Wiki에서 **bc-250**에 대한 검색 결과가 없습니다.\n\n혹시 다른 정보(예: 어떤 분야의 bc-250인지, 전체 이름 등)를 알려주시면, 다시 검색하여 더 정확한 답변을 드릴 수 있습니다.';
  const material = 'ChatGPT 답변을 붙여넣었습니다.\nBC250 서버 개요\n' + '서버 구성 정보입니다. '.repeat(25);
  expect(classifyWikiProposal({ mode: 'quick', message: { ...user, content: material }, previous: prior(assistant) })).toBe('contextual');
  expect(classify(prior('혹시 어떤 분야(예: 부품, 차량)인지 알려주시면 다시 확인하겠습니다.'))).toBe('contextual');
});
