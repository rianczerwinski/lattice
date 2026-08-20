import DOMPurify from 'dompurify';
import { marked } from 'marked';

export function renderMarkdown(value: string): string {
  return DOMPurify.sanitize(marked.parse(value) as string);
}
