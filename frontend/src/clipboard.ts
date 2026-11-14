export async function copyText(text: string) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.style.position = 'fixed';
  textarea.style.opacity = '0';
  document.body.append(textarea);
  try {
    textarea.select();
    if (!document.execCommand('copy')) throw new Error('복사에 실패했습니다.');
  } finally {
    textarea.remove();
  }
}
