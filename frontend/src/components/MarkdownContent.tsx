import { memo, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import rehypeSanitize from 'rehype-sanitize';
import remarkGfm from 'remark-gfm';

function safeUrl(url: string) {
  if (url.startsWith('#') || url.startsWith('/')) return url;
  try {
    const parsed = new URL(url);
    return ['http:', 'https:', 'mailto:'].includes(parsed.protocol) ? url : '';
  } catch {
    return '';
  }
}

async function copyText(text: string) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.style.position = 'fixed';
  textarea.style.opacity = '0';
  document.body.append(textarea);
  textarea.select();
  const copied = document.execCommand('copy');
  textarea.remove();
  if (!copied) throw new Error('복사에 실패했습니다.');
}

function CodeBlock({ className, children }: { className?: string; children?: React.ReactNode }) {
  const [copied, setCopied] = useState(false);
  const text = String(children || '').replace(/\n$/, '');
  const isBlock = Boolean(className?.startsWith('language-')) || text.includes('\n');
  if (!isBlock) return <code className={className}>{children}</code>;
  const copy = async () => {
    try {
      await copyText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      setCopied(false);
    }
  };
  return <div className="markdown-code"><button type="button" onClick={() => void copy()}>{copied ? '복사됨' : '코드 복사'}</button><pre><code className={className}>{text}</code></pre></div>;
}

export const MarkdownContent = memo(function MarkdownContent({ content }: { content: string }) {
  return <ReactMarkdown
    remarkPlugins={[remarkGfm]}
    rehypePlugins={[rehypeSanitize]}
    urlTransform={safeUrl}
    components={{
      a: ({ href, children }) => href ? <a href={href} target="_blank" rel="noreferrer">{children}</a> : <>{children}</>,
      img: ({ alt }) => <span className="markdown-image-blocked">[이미지 미리보기 비활성화{alt ? ': ' + alt : ''}]</span>,
      pre: ({ children }) => <>{children}</>,
      code: ({ className, children }) => <CodeBlock className={className}>{children}</CodeBlock>,
    }}
  >{content}</ReactMarkdown>;
});
