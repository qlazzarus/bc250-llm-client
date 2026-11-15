import type { WikiSection } from './components/WikiManager';

export const BASE = '/llm';
export const chatUrl = (id: string) => `${BASE}/chat/${encodeURIComponent(id)}`;
export const wikiUrl = (segments: string[], section: WikiSection = 'read') =>
  `${BASE}/wiki/${segments.map(encodeURIComponent).join('/')}${section === 'read' ? '' : `?view=${section}`}`;
export const wikiIdUrl = (id: number, section: WikiSection = 'read') =>
  `${BASE}/#/wiki/${id}${section === 'read' ? '' : '/' + section}`;
