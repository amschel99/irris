import DOMPurify from "dompurify";
import { marked } from "marked";

marked.setOptions({ gfm: true, breaks: true });

/** Markdown -> safe HTML. Citation markers like [2] become clickable chips (data-cite="2"). */
export function renderMarkdown(text: string, citable: Set<number> = new Set()): string {
  const html = marked.parse(text || "", { async: false }) as string;
  const withCites = html.replace(/(<[^>]+>)|\[(\d{1,3})\]/g, (m, tag, n) => {
    if (tag) return tag;
    const k = Number(n);
    if (citable.size && !citable.has(k)) return m;
    return `<span class="cite" role="button" tabindex="0" data-cite="${k}" aria-label="Source ${k}">${k}</span>`;
  });
  return DOMPurify.sanitize(withCites, { ADD_ATTR: ["data-cite", "tabindex", "role"] });
}
