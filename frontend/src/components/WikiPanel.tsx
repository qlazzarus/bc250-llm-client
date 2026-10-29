import { useEffect, useState } from 'react';
import { request } from '../api';

type WikiDoc = { id: number; title: string; category: string; content: string; updated_at: string };
const empty = { title: '', category: 'general', content: '' };
export function WikiPanel() {
  const [docs, setDocs] = useState<WikiDoc[]>([]);
  const [edit, setEdit] = useState<WikiDoc | null>(null);
  const [form, setForm] = useState(empty);
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const [instruction, setInstruction] = useState('');
  const load = async () => { try { setDocs(await request<WikiDoc[]>('/wiki/documents')); } catch (e) { setMessage(e instanceof Error ? e.message : 'Wiki를 불러오지 못했습니다.'); } };
  useEffect(() => { void load(); }, []);
  const open = async (id: number) => {
    try { const doc = await request<WikiDoc>('/wiki/documents/' + id); setEdit(doc); setForm({ title: doc.title, category: doc.category, content: doc.content }); setMessage(''); }
    catch (e) { setMessage(e instanceof Error ? e.message : '문서를 불러오지 못했습니다.'); }
  };
  const save = async () => {
    setBusy(true); setMessage('');
    try {
      await request('/wiki/documents' + (edit ? '/' + edit.id : ''), { method: edit ? 'PUT' : 'POST', body: JSON.stringify({ ...form, expected_updated_at: edit?.updated_at || null }) });
      setEdit(null); setForm(empty); setMessage('저장했습니다.'); await load();
    } catch (e) { setMessage(e instanceof Error ? e.message : '저장하지 못했습니다.'); }
    finally { setBusy(false); }
  };
  const draft = async () => {
    setBusy(true); setMessage('');
    try {
      const result = await request<{ content: string }>('/wiki/draft', { method: 'POST', body: JSON.stringify({ instruction, content: form.content }) });
      setForm(current => ({ ...current, content: result.content }));
      setMessage('초안을 확인하고 수정한 뒤 저장하세요. 아직 Wiki에 저장되지 않았습니다.');
    } catch (e) { setMessage(e instanceof Error ? e.message : '초안을 생성하지 못했습니다.'); }
    finally { setBusy(false); }
  };
  const remove = async () => {
    if (!edit || !confirm('이 Wiki 문서를 삭제할까요?')) return;
    setBusy(true);
    try { await request('/wiki/documents/' + edit.id, { method: 'DELETE' }); setEdit(null); setForm(empty); setMessage('삭제했습니다.'); await load(); }
    catch (e) { setMessage(e instanceof Error ? e.message : '삭제하지 못했습니다.'); }
    finally { setBusy(false); }
  };
  return <section className="system-panel"><h2>가족 Wiki</h2><p>문서를 직접 작성하거나 LLM 초안을 확인한 뒤 저장하세요. 편집은 본문 12,000자까지 지원합니다.</p>
    <button onClick={() => { setEdit(null); setForm(empty); setMessage(''); }}>새 문서</button>{' '}<button onClick={() => void load()}>목록 새로고침</button>
    <ul>{docs.map(doc => <li key={doc.id}><button className="plain" onClick={() => void open(doc.id)}>{doc.title}</button> · {doc.category}</li>)}</ul>
    <h3>{edit ? '문서 수정' : '새 문서'}</h3>
    <label>제목 <input value={form.title} maxLength={200} onChange={e => setForm({ ...form, title: e.target.value })} /></label><br />
    <label>분류 <input value={form.category} maxLength={80} onChange={e => setForm({ ...form, category: e.target.value })} /></label><br />
    <label>LLM 초안 요청 <textarea style={{ display: 'block', width: '100%' }} maxLength={4000} value={instruction} onChange={e => setInstruction(e.target.value)} /></label>
    <button disabled={busy || !instruction.trim() || form.content.length > 12000} onClick={() => void draft()}>본문 초안 작성</button>
    <p>현재 본문을 참고해 초안을 작성합니다. 12,000자 이하에서 사용할 수 있습니다.</p>
    <label>내용 <textarea style={{ display: 'block', width: '100%', minHeight: 240 }} value={form.content} maxLength={12000} onChange={e => setForm({ ...form, content: e.target.value })} /></label>
    <button disabled={busy || !form.title.trim() || !form.content.trim()} onClick={() => void save()}>저장</button>{edit && <button disabled={busy} onClick={() => void remove()}>삭제</button>}
    {message && <p role="status">{message}</p>}
  </section>;
}
